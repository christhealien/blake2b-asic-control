"""Fan curve add-on for the web dashboard.

Adds a "curve" mode to the dashboard's auto fan controller (fan_controller.py): a
profile is a list of points (board temperature -> fan %), and the fan follows the
line between them. Built-in curve profiles are added next to the stock step
profiles, and your own curves are saved in miners.json under "profiles", so the
Miner and Settings pages can pick them too.

It also keeps each miner's presets (High / Middle / Low / Lowest power, and your own):
a preset is a clock + voltage + PV and the fan curve to run with it. The tuner builds and
tests them per miner; they are imported here when its run ends. While a tuning run holds a
miner's fans, the dashboard's auto fan leaves that miner alone.

Routes (all behind the login):
  GET  /api/fans/state            curves, defaults, presets, miners with live temps and fan state
  POST /api/fans/profile          save a custom curve  {key?, label, points, ...}
  POST /api/fans/profile_delete   delete a custom curve {key}
  POST /api/presets/apply         {miner_id, key}  put a preset on the miner
  POST /api/presets/save          {miner_id, key?, label, mhz, mv, pv, fan_profile}
  POST /api/presets/delete        {miner_id, key}
  GET  /api/hardware/list         what each miner was detected as (model, firmware, boards, chips, fans)
  POST /api/hardware/probe        {miner_id}  detect it now
  GET  /api/hardware/chips?miner= every chip's accumulated hardware errors (from /dbg/icinfo)
  GET  /api/schedule/state, POST /api/schedule/save|pause|copy   (schedule_addon)
  POST /api/quick/demo            {count}  show that many demo miners on Fleet (0 = none)
  POST /api/quick/restart         {ids}    restart the miner software, in the background; the progress
                                           shows in /api/hardware/list ("restart") until it's hashing again
  POST /api/quick/manual          {miner_id}  take manual control: clear its preset, pause its schedule
  GET  /api/hardware/shares       best shares (since restart, record, the 20 biggest finds)
  POST /api/hardware/shares_reset {miner_id}  clear its best-share record
  POST /api/hardware/power_cal    {miner_id, watts, mhz, pv} or {miner_id, clear}  calibrate the power estimate
  POST /api/hardware/mains        {mains_v}  mains voltage for the amps
  POST /api/fans/temp_source      {miner_id, source: "board"|"chip"}  what the fan curve follows


guard_post() runs before the dashboard's own POST routes: while a miner is being tuned it
refuses fan, temperature-control, clock and restart actions for it, and a clock / voltage set
by hand turns off that miner's preset and pauses its schedule.

Every miner is probed once automatically soon after it is added (and again every 10 minutes
while that fails); the result is kept in miners.json as "hardware". The tuner needs it.
Turning auto fan on or off per miner, and the default profile, use the dashboard's
own routes (/api/miners/<id>/fan_control and /api/fan/defaults).
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import fan_controller
import health_addon
import schedule_addon
import shares_addon
import tuner_addon
import notify_addon
import rejects_addon
import hashrate_addon
import report_addon

# ---------------------------------------------------------------- curve mode

CURVE_DEFAULTS: dict[str, Any] = {
    "mode": "curve",
    "poll_s": 10.0,          # how often the board temperature is read
    "cooldown_s": 45,        # how often the fan setting is re-sent (the miner walks it back)
    "smoothing": 0,          # 0 off, 1 light, 2 strong
    "board": "max",
    "force_tempcontrol_on": True,
    "abort_c": 88.0,
}

BUILTIN_CURVES: dict[str, dict[str, Any]] = {
    "curve-60": {
        **CURVE_DEFAULTS,
        "label": "Curve: cool, holds about 60 °C",
        "smoothing": 1,
        "points": [{"temp": 50, "fan": 45}, {"temp": 57, "fan": 60}, {"temp": 62, "fan": 75},
                   {"temp": 68, "fan": 90}, {"temp": 74, "fan": 100}],
    },
    "curve-balanced": {
        **CURVE_DEFAULTS,
        "label": "Curve: balanced, about 65-70 °C",
        "smoothing": 1,
        "points": [{"temp": 55, "fan": 40}, {"temp": 65, "fan": 60}, {"temp": 72, "fan": 80},
                   {"temp": 80, "fan": 100}],
    },
    "curve-quiet": {
        **CURVE_DEFAULTS,
        "label": "Curve: quiet, lets boards run warmer",
        "smoothing": 2,
        "points": [{"temp": 60, "fan": 30}, {"temp": 72, "fan": 50}, {"temp": 80, "fan": 75},
                   {"temp": 85, "fan": 100}],
    },
}

LIMITS = {"temp": (20, 100), "fan": (20, 100), "abort_c": (70, 95), "cooldown_s": (15, 600),
          "poll_s": (5, 60), "points": (2, 8)}

_orig_target_fan = fan_controller.target_fan


def curve_fan(points: list[dict[str, Any]], temp: float) -> float:
    """Fan % on the line through the points (flat before the first and after the last)."""
    pts = sorted(((float(p["temp"]), float(p["fan"])) for p in points), key=lambda p: p[0])
    if not pts:
        return 70.0
    if temp <= pts[0][0]:
        return pts[0][1]
    for (t0, f0), (t1, f1) in zip(pts, pts[1:]):
        if temp <= t1:
            return f0 + (temp - t0) / max(1e-6, t1 - t0) * (f1 - f0)
    return pts[-1][1]


def target_fan(profile: dict[str, Any], temp: float, history: list[float]) -> int | None:
    temp = temp + float(profile.get("_temp_add") or 0)   # "follow the hottest chip": board sensor + chip offset
    if (profile.get("mode") or "").lower() != "curve":
        return _orig_target_fan(profile, temp, history)
    instant = curve_fan(profile.get("points") or [], temp)
    level = int(profile.get("smoothing") or 0)
    # a nudge (+/- 5 % steps) shifts the curve, but never below the curves' own floor
    shift = float(profile.get("_offset") or 0)
    if level <= 0:
        return int(round(max(FAN_MIN_PCT, min(100, instant + shift))))
    # same blend the stock "smooth" mode uses: recent readings weigh more
    history.insert(0, instant)
    del history[4 if level == 1 else 10:]
    weights = list(range(len(history), 0, -1))
    avg = sum(v * w for v, w in zip(history, weights)) / sum(weights)
    # never let smoothing hold the fan below what the current temperature asks for
    # by more than 10 %: heat is dealt with quickly, slowing down is gentle
    out = max(avg, instant - 10)
    return int(round(max(FAN_MIN_PCT, min(100, out + shift))))


fan_controller.target_fan = target_fan
for _k, _v in BUILTIN_CURVES.items():
    fan_controller.BUILTIN_PROFILES.setdefault(_k, _v)

_orig_tick_one = fan_controller.FanController._tick_one
_orig_tick_all = fan_controller.FanController._tick_all


def _tick_one(self: Any, mid: str, client: Any, profile: dict, offset: int, st: dict) -> None:
    # a model whose settings the app can't write yet (SC BOX, HS BOX) is never driven, even if auto fan was switched
    # on for it before that lock existed: on an HS BOX, cooling the chips below about 54 C raised its hardware errors
    # (crProductGuy's write tests, 2026-10-07), and every settings write sets off a 10-20 minute fan spike
    try:
        row = next((m for m in _srv().load_registry() if _row_id(m) == mid), None)
        ro = tuner_addon.format_problem((row or {}).get("hardware"))
    except Exception:
        ro = None
    if ro:
        st["last_poll"] = time.time()
        st["last_status"] = "the firmware's own fan loop is in charge on this model (set its fan target on the Miner page)"
        st["last_applied_fan"] = None
        return
    if tuner_addon.running(mid):   # a tuning run is on: it holds the fans, or was told to leave them alone
        # (a fan write rewrites the whole power plan, so one landing between the run's own writes could put an
        # old test clock back on the miner, even after the run restored it)
        st["last_poll"] = time.time()
        st["last_status"] = "TUNER is holding the fans" if tuner_addon.fan_owner(mid) else "a tuning run is going: auto fan waits"
        st["last_kick_ts"] = 0.0      # re-apply straight away once the run ends
        st["last_applied_fan"] = None
        return
    if (profile.get("mode") or "").lower() == "curve" and offset:
        profile, offset = {**profile, "_offset": offset}, 0   # applied (and floored) in target_fan
    add = chip_offset(mid) if _temp_source(mid) == "chip" else None
    if add:
        profile = {**profile, "_temp_add": add}
        st["temp_source"] = f"hottest chip (board sensor +{add:.1f} °C)"
    else:
        st.pop("temp_source", None)
    _orig_tick_one(self, mid, client, profile, offset, st)


_src_cache: dict[str, tuple[float, str]] = {}


def _temp_source(mid: str) -> str:
    ts, v = _src_cache.get(mid, (0.0, "board"))
    if time.time() - ts > 10:
        row = next((m for m in _srv().load_registry() if _row_id(m) == mid), None) or {}
        v = str((row.get("fan_control") or {}).get("temp_source") or "board")
        _src_cache[mid] = (time.time(), v)
    return v


def chip_offset(mid: str) -> float | None:
    """How much hotter the hottest chip runs than the board sensor (from the miner's log; 0-15 °C)."""
    hit = health_addon._cache.get(mid)
    if not hit or time.time() - hit[0] > 3600:
        return None
    off = health_addon.chip_temp_offset(hit[1])
    return None if off is None else max(0.0, min(15.0, off))


def set_temp_source(body: dict[str, Any]) -> dict[str, Any]:
    mid, src = str(body.get("miner_id") or ""), str(body.get("source") or "board")
    if src not in ("board", "chip"):
        raise ValueError("source must be board or chip")
    srv = _srv()
    doc = srv.load_registry_doc()
    row = next((m for m in doc.get("miners") or [] if _row_id(m) == mid), None)
    if not row:
        raise ValueError(f"unknown miner {mid}")
    fc = dict(row.get("fan_control") or {})
    fc["temp_source"] = src
    row["fan_control"] = fc
    srv.save_registry_doc(doc)
    _src_cache.pop(mid, None)
    return {"ok": True, "offset": chip_offset(mid)}


_last_import_check = [0.0]


def _tick_all(self: Any) -> None:
    now = time.time()
    if _tz_applied[0] is None:
        apply_saved_tz()          # once, as soon as the registry can be read
    if not _scale_loaded[0]:
        apply_saved_scale()
    if now - _last_import_check[0] >= 5:
        _last_import_check[0] = now
        try:
            import_presets()
        except Exception as e:  # never stop the fan loop over this
            print(f"[presets] import: {e}", flush=True)
        try:
            auto_probe()
        except Exception as e:
            print(f"[hardware] auto probe: {e}", flush=True)
        try:
            schedule_addon.tick()
        except Exception as e:
            print(f"[schedule] tick: {e}", flush=True)
        try:
            shares_addon.tick()
        except Exception as e:
            print(f"[shares] tick: {e}", flush=True)
        try:
            health_tick()
        except Exception as e:
            print(f"[health] tick: {e}", flush=True)
        try:
            notify_addon.tick()
        except Exception as e:
            print(f"[notify] tick: {e}", flush=True)
    _orig_tick_all(self)


fan_controller.FanController._tick_one = _tick_one
fan_controller.FanController._tick_all = _tick_all

# ---------------------------------------------------------------- helpers


def _srv() -> Any:
    """The running dashboard module (server.py runs as __main__)."""
    return sys.modules.get("server") or sys.modules["__main__"]


def _row_id(m: dict[str, Any]) -> str:
    return str(m.get("id") or m.get("ip") or "")


def validate_curve(body: dict[str, Any]) -> dict[str, Any]:
    pts_in = body.get("points")
    if not isinstance(pts_in, list):
        raise ValueError("points must be a list")
    lo, hi = LIMITS["points"]
    if not lo <= len(pts_in) <= hi:
        raise ValueError(f"a curve needs {lo} to {hi} points")
    pts = []
    for p in pts_in:
        t, f = float(p.get("temp")), float(p.get("fan"))
        if not LIMITS["temp"][0] <= t <= LIMITS["temp"][1]:
            raise ValueError(f"temperature {t:g} °C is outside {LIMITS['temp'][0]}-{LIMITS['temp'][1]} °C")
        if not LIMITS["fan"][0] <= f <= LIMITS["fan"][1]:
            raise ValueError(f"fan {f:g} % is outside {LIMITS['fan'][0]}-{LIMITS['fan'][1]} %")
        pts.append({"temp": round(t), "fan": round(f)})
    pts.sort(key=lambda p: p["temp"])
    for a, b in zip(pts, pts[1:]):
        if a["temp"] == b["temp"]:
            raise ValueError(f"two points at {a['temp']} °C; each point needs its own temperature")
        if b["fan"] < a["fan"]:
            raise ValueError(f"the fan drops from {a['fan']} % to {b['fan']} % as it gets hotter "
                             f"({a['temp']} → {b['temp']} °C); a curve can only stay level or rise")

    def num(key: str, default: float) -> float:
        v = float(body.get(key, default))
        lo, hi = LIMITS[key]
        if not lo <= v <= hi:
            what = {"abort_c": "the safety temperature (°C)", "cooldown_s": "re-send every (s)",
                    "poll_s": "check temperature every (s)"}.get(key, key)
            raise ValueError(f"{what} must be {lo}-{hi}")
        return v

    abort_c = num("abort_c", CURVE_DEFAULTS["abort_c"])
    if abort_c <= pts[0]["temp"]:
        raise ValueError("the safety temperature must be above the first point")
    label = re.sub(r"[<>\"'`&]", "", str(body.get("label") or "")).strip()[:48]   # a name, never markup
    if not label:
        raise ValueError("give the curve a name")
    return {
        **CURVE_DEFAULTS,
        "label": label,
        "points": pts,
        "abort_c": abort_c,
        "cooldown_s": int(num("cooldown_s", CURVE_DEFAULTS["cooldown_s"])),
        "poll_s": num("poll_s", CURVE_DEFAULTS["poll_s"]),
        "smoothing": max(0, min(2, int(body.get("smoothing") or 0))),
        "force_tempcontrol_on": bool(body.get("force_tempcontrol_on", True)),
        "custom": True,
    }


def _slug(label: str, taken: set[str]) -> str:
    base = "my-" + (re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:30] or "curve")
    key, n = base, 2
    while key in taken:
        key, n = f"{base}-{n}", n + 1
    return key


# ---------------------------------------------------------------- live readings

_live: dict[str, dict[str, Any]] = {}
_live_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=6)
LIVE_MAX_AGE = 12.0


def _read_live(mid: str, client: Any) -> dict[str, Any]:
    """Board temperatures and fan RPM from the light port-4028 API (no login)."""
    out: dict[str, Any] = {"ts": time.time(), "online": False}
    try:
        devs = list(client.bfg("devs").get("DEVS") or [])
    except Exception as e:
        out["error"] = str(e)[:80]
        return out
    temps, rpm = [], []
    for d in devs:
        vals = []
        for k, v in d.items():
            if (str(k).startswith("tstemp-") or k == "Temperature") and v is not None:
                try:
                    vals.append(float(v))
                except (TypeError, ValueError):
                    pass
        if vals:
            temps.append(max(vals))
        for i in range(8):
            v = d.get(f"fan{i}")
            if v is not None:
                try:
                    rpm.append(int(float(v)))
                except (TypeError, ValueError):
                    pass
    out.update(online=True, board_temps=temps, temp_max=max(temps) if temps else None,
               fan_rpm=sorted(set(rpm)) if rpm else [])
    return out


def live_all(clients: dict[str, Any]) -> dict[str, dict[str, Any]]:
    now = time.time()
    with _live_lock:
        stale = [(mid, c) for mid, c in clients.items()
                 if now - float((_live.get(mid) or {}).get("ts") or 0) > LIVE_MAX_AGE]
    if stale:
        results = list(_pool.map(lambda mc: (mc[0], _read_live(*mc)), stale))
        with _live_lock:
            _live.update(dict(results))
    with _live_lock:
        return {mid: dict(_live.get(mid) or {}) for mid in clients}


# ---------------------------------------------------------------- presets

PRESET_KEYS = [("high", "High"), ("middle", "Middle"), ("low", "Low"), ("lowest", "Lowest power")]
PLAN_LIMITS = {"mhz": (200, 1200), "mv": (7000, 14000), "pv": (7000, 14500)}   # sanity band for a reported stock plan
FAN_MIN_PCT = 20          # no hand-set or preset fan below this (the fan curves never go below 20 % either)


def _tuned_key(mid: str, key: str) -> str:
    return f"tuned-{tuner_addon.safe_id(mid)}-{key}"


def import_presets() -> list[str]:
    """Take the presets of every finished tuning run that hasn't been imported yet."""
    srv = _srv()
    done: list[str] = []
    doc = None
    for mid, r in tuner_addon.runs().items():
        if r["running"]:
            continue
        f = tuner_addon.Files(mid)
        try:
            pdoc = json.loads(f.presets.read_text())
        except (FileNotFoundError, ValueError, OSError):
            continue
        run = str(pdoc.get("run") or "")
        if not run or run != str(r["info"].get("run") or ""):
            continue   # left over from an earlier run
        if doc is None:
            doc = srv.load_registry_doc()
        if (doc.get("presets_imported") or {}).get(mid) == run:
            continue
        row = next((m for m in doc.get("miners") or [] if _row_id(m) == mid), None)
        if row is None:
            continue
        name = str(row.get("name") or mid)
        curves = dict(doc.get("profiles") or {})
        old_all = dict((doc.get("presets") or {}).get(mid) or {})
        rebuilt = pdoc.get("mode") == "presets"
        # a search replaces all the tuner presets (your own stay). A rebuild (presets only) replaces only the
        # ones it tested, and a preset it couldn't pass keeps the old version if that one had passed.
        mine = dict(old_all) if rebuilt else {k: v for k, v in old_all.items() if v.get("source") != "tuner"}
        when = str(pdoc.get("created") or "")[:16].replace("T", " ")
        changed: set[str] = set()
        for key, label in PRESET_KEYS:
            e = (pdoc.get("presets") or {}).get(key)
            if not e:
                continue
            old = old_all.get(key) or {}
            if rebuilt and not e.get("ok") and old.get("source") == "tuner" and old.get("ok"):
                mine[key] = {**old, "rebuild_failed": {"mhz": e.get("mhz"), "mv": e.get("mv"), "target_c": e.get("target_c"),
                                                       "reason": e.get("reason"), "run": run, "tested": when}}
                continue
            changed.add(key)
            fan_key = None
            if e.get("ok") and e.get("curve"):
                fan_key = _tuned_key(mid, key)
                curves[fan_key] = {**CURVE_DEFAULTS, "label": f"Tuned {name} · {label} ({e.get('target_c'):g} °C)",
                                   "points": e["curve"], "smoothing": 1, "custom": True, "tuned": True,
                                   "abort_c": min(CURVE_DEFAULTS["abort_c"], float(e["curve"][-1]["temp"]) + 6)}
            mine[key] = {
                "label": label, "mhz": e.get("mhz"), "mv": e.get("mv"), "pv": e.get("pv"),
                "fan_profile": fan_key, "source": "tuner", "ok": bool(e.get("ok")),
                "target_c": e.get("target_c"), "avg_fan": e.get("avg_fan"), "avg_ths": e.get("avg_ths"),
                "max_t": e.get("max_t"), "watched_min": e.get("watched_min"),
                "reason": e.get("reason"), "run": run, "tested": when,
            }
        doc["profiles"] = curves
        doc.setdefault("presets", {})[mid] = mine
        doc.setdefault("presets_imported", {})[mid] = run
        high = mine.get("high")
        if rebuilt and row.get("active_preset") in changed:
            # a presets-only run puts back what the miner ran before: the OLD numbers of the tuner preset
            # that was on. Say no preset, so nothing claims the new numbers are on the miner.
            row["active_preset"] = None
        act = row.get("active_preset")
        if act and act != "idle" and not (mine.get(act) or {}).get("ok"):
            row["active_preset"] = None      # the preset it pointed at was replaced, dropped or didn't pass
        elif act and act != "idle":
            num = lambda p: tuple((p or {}).get(k) for k in ("mhz", "mv", "pv"))
            if num(old_all.get(act)) != num(mine.get(act)):
                row["active_preset"] = None  # same name, new numbers: the miner isn't on them (High is set below)
        if high and high.get("ok") and not rebuilt:
            row["active_preset"] = "high"   # the tuner leaves the miner on High
            if high.get("fan_profile"):
                fc = dict(row.get("fan_control") or {})
                fc.update(enabled=True, profile=high["fan_profile"], fan_offset=0)
                row["fan_control"] = fc
        done.append(mid)
    if done and doc is not None:
        srv.save_registry_doc(doc)
        print(f"[presets] imported tuner presets for {', '.join(done)}", flush=True)
    return done


def _plan_lims(hw: dict[str, Any] | None) -> dict[str, tuple[int, int]]:
    """The hard clock / voltage / PV range for one miner, around its own stock plan. Refused when the
    stock plan isn't known (not probed yet) or doesn't look like a real one: then nothing is allowed."""
    fp = tuner_addon.format_problem(hw)
    if fp:
        raise ValueError(fp[0].upper() + fp[1:])
    stock = (hw or {}).get("stock")
    if not stock:
        if (hw or {}).get("ok") and (hw or {}).get("plan_format") == "sc-lite":
            raise ValueError("this miner's firmware didn't report a stock (level 0) power plan, so clock and "
                             "voltage can't be changed")
        raise ValueError("this miner's stock setting isn't known yet, so clock and voltage can't be changed: "
                         "press Probe now on its page first")
    try:
        sm, sv, sp = int(stock["mhz"]), int(stock["mv"]), int(stock["pv"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("this miner's stock setting is unreadable (not whole numbers), so clock and voltage "
                         "changes are refused")
    lo_m, hi_m = PLAN_LIMITS["mhz"]; lo_v, hi_v = PLAN_LIMITS["mv"]
    if not (lo_m <= sm <= hi_m and lo_v <= sv <= hi_v and -500 <= sp - sv <= 1000):
        # a miner can report anything as its "stock" plan: don't let it widen its own limits
        raise ValueError(f"this miner reports a stock setting outside what the app knows how to handle "
                         f"({sm} MHz / {sv} mV / PV {sp}; it allows {lo_m}-{hi_m} MHz, {lo_v}-{hi_v} mV, PV within "
                         "-500/+1000 of mV), so clock and voltage changes are refused. That's a limit of the "
                         "app for this model, not a fault in the miner")
    t = tuner_addon.plan_limits(hw)["limits"]
    lim = {"mhz": (min(t["mhz"][0], 300), t["mhz"][1]), "mv": (t["mv"][0], t["mv"][1])}
    lim["pv"] = (lim["mv"][0] - 500, lim["mv"][1] + 1000)
    return lim


def _check_plan(body: dict[str, Any], hw: dict[str, Any] | None = None) -> tuple[int, int, int]:
    """A clock / voltage / PV within the miner's hard limits (around its own stock plan)."""
    lim = _plan_lims(hw)
    vals = []
    for k in ("mhz", "mv", "pv"):
        try:
            v = int(body.get(k))
        except (TypeError, ValueError):
            raise ValueError(f"{k} must be a whole number")
        lo, hi = lim[k]
        if not lo <= v <= hi:
            raise ValueError(f"{k} must be {lo}-{hi}")
        vals.append(v)
    mhz, mv, pv = vals
    if not -500 <= pv - mv <= 1000:
        raise ValueError("PV must be within -500 / +1000 of mV")
    return mhz, mv, pv


def _check_partial(body: dict[str, Any], hw: dict[str, Any] | None) -> None:
    """For the dashboard's own clock / voltage / fan action: every value that's given must be in range."""
    if any(body.get(k) not in (None, "") for k in ("mhz", "mv", "pv")):
        lim = _plan_lims(hw)
        for k in ("mhz", "mv", "pv"):
            if body.get(k) in (None, ""):
                continue
            try:
                v = int(body.get(k))
            except (TypeError, ValueError):
                raise ValueError(f"{k} must be a whole number")
            if not lim[k][0] <= v <= lim[k][1]:
                raise ValueError(f"{k} must be {lim[k][0]}-{lim[k][1]} for this miner")
        if body.get("mv") not in (None, "") and body.get("pv") not in (None, ""):
            if not -500 <= int(body["pv"]) - int(body["mv"]) <= 1000:
                raise ValueError("PV must be within -500 / +1000 of mV")
    if body.get("fan") not in (None, ""):
        try:
            f = int(body.get("fan"))
        except (TypeError, ValueError):
            raise ValueError("fan must be a whole number")
        if not FAN_MIN_PCT <= f <= 100:
            raise ValueError(f"fan must be {FAN_MIN_PCT}-100 %")


def save_preset(body: dict[str, Any]) -> dict[str, Any]:
    srv = _srv()
    mid = str(body.get("miner_id") or "")
    doc = srv.load_registry_doc()
    row = next((m for m in doc.get("miners") or [] if _row_id(m) == mid), None)
    if not row:
        raise ValueError(f"unknown miner {mid}")
    if str(body.get("key") or "") == "idle":
        raise ValueError("Idle is the firmware's own mode and can't be edited")
    mhz, mv, pv = _check_plan(body, row.get("hardware"))
    label = re.sub(r"[<>\"'`&]", "", str(body.get("label") or "")).strip()[:32]   # a name, never markup
    if not label:
        raise ValueError("give the preset a name")
    fan_key = body.get("fan_profile") or None
    if fan_key and fan_key not in fan_controller.merge_profiles(doc.get("profiles")):
        raise ValueError(f"no fan profile called {fan_key}")
    mine = dict((doc.get("presets") or {}).get(mid) or {})
    key = str(body.get("key") or "")
    old = mine.get(key) if key else None
    if key and old is None:
        raise ValueError(f"no preset {key} on this miner")
    if not key:
        base = "my-" + (re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:24] or "preset")
        key, n = base, 2
        while key in mine:
            key, n = f"{base}-{n}", n + 1
    changed_clock = not old or (old.get("mhz"), old.get("mv"), old.get("pv")) != (mhz, mv, pv)
    new = dict(old or {})
    new.update(label=label, mhz=mhz, mv=mv, pv=pv, fan_profile=fan_key)
    if changed_clock:   # hand-set clocks are not tested
        new.update(source="manual", ok=True, reason="set by hand, not tested", tested="")
        for k in ("avg_ths", "avg_fan", "max_t", "watched_min", "target_c"):
            new.pop(k, None)
    elif not old:
        new.update(source="manual", ok=True)
    mine[key] = new
    doc.setdefault("presets", {})[mid] = mine
    if old and changed_clock and row.get("active_preset") == key:
        row["active_preset"] = None      # the miner still runs the old numbers: apply the preset to run the new ones
    srv.save_registry_doc(doc)
    return {"ok": True, "key": key, "preset": new}


def delete_preset(body: dict[str, Any]) -> dict[str, Any]:
    srv = _srv()
    mid, key = str(body.get("miner_id") or ""), str(body.get("key") or "")
    doc = srv.load_registry_doc()
    mine = dict((doc.get("presets") or {}).get(mid) or {})
    if key not in mine:
        raise ValueError(f"no preset {key} on this miner")
    if (mine.get(key) or {}).get("idle"):
        raise ValueError("Idle is the firmware's own mode; it stays while the miner has one")
    del mine[key]
    doc.setdefault("presets", {})[mid] = mine
    for m in doc.get("miners") or []:
        if _row_id(m) == mid and m.get("active_preset") == key:
            m.pop("active_preset", None)
    srv.save_registry_doc(doc)
    return {"ok": True}


def apply_preset(body: dict[str, Any]) -> dict[str, Any]:
    srv = _srv()
    mid, key = str(body.get("miner_id") or ""), str(body.get("key") or "")
    if tuner_addon.running(mid):
        raise ValueError("a tuning run is going on this miner; stop it first")
    doc = srv.load_registry_doc()
    p = ((doc.get("presets") or {}).get(mid) or {}).get(key)
    if not p:
        raise ValueError(f"no preset {key} on this miner")
    if not p.get("ok"):
        raise ValueError(f"{p.get('label')} didn't pass its test, so it can't be applied")
    row = next((m for m in doc.get("miners") or [] if _row_id(m) == mid), {})
    if not (row.get("hardware") or {}).get("ok"):
        raise ValueError("this miner hasn't been probed yet; press Probe now first")
    if p.get("idle"):
        return _apply_idle(srv, mid, row)
    _check_plan(p, row.get("hardware"))      # again now: limits follow the miner's current probe
    client = srv.get_client(mid)
    s = client.api("GET", "/mcb/setting")
    if not isinstance(s, dict):
        raise ValueError("couldn't read the miner's settings")
    from miner_client import build_plan, parse_plan
    _m, _v, fa, fb, _pv = parse_plan(s.get("manualPowerplan") or "")
    s["manual"] = True
    s["select"] = 0
    s["manualPowerplan"] = build_plan(int(p["mhz"]), int(p["mv"]), fa, fb, int(p["pv"]))
    client.api("PUT", "/mcb/setting", s)
    if row.get("active_preset") == "idle":
        notify_addon.idle_changed(mid)      # waking up may restart its mining software
    doc = srv.load_registry_doc()   # re-read: the miner call can take a while
    for m in doc.get("miners") or []:
        if _row_id(m) == mid:
            m["active_preset"] = key
            if p.get("fan_profile"):
                fc = dict(m.get("fan_control") or {})
                fc.update(enabled=True, profile=p["fan_profile"], fan_offset=0)   # the preset's own curve, unshifted
                m["fan_control"] = fc
    srv.save_registry_doc(doc)
    return {"ok": True, "plan": s["manualPowerplan"], "fan_profile": p.get("fan_profile")}


def _apply_idle(srv: Any, mid: str, row: dict[str, Any]) -> dict[str, Any]:
    """Put the miner in the firmware's own Idle mode: manual off and the Idle plan selected, the way its
    own Miner settings page does. Its manual plan (clock / voltage) is left as it is, so any other preset
    wakes it (they write manual on, select 0 and their plan)."""
    hw = row.get("hardware") or {}
    idx = hw.get("idle_select")
    if idx is None:
        raise ValueError("this miner's firmware didn't list an Idle mode at the last probe; press Probe now")
    client = srv.get_client(mid)
    s = client.api("GET", "/mcb/setting")
    if not isinstance(s, dict):
        raise ValueError("couldn't read the miner's settings")
    plans = s.get("powerplans") or []
    info = str((plans[idx] or {}).get("info") or "") if isinstance(plans, list) and 0 <= idx < len(plans) and isinstance(plans[idx], dict) else ""
    if not info.startswith("0 MHz"):
        raise ValueError("the miner's plan list changed since the last probe (no Idle plan where it was); press Probe now")
    s["manual"] = False
    s["select"] = int(idx)
    client.api("PUT", "/mcb/setting", s)
    notify_addon.idle_changed(mid)
    doc = srv.load_registry_doc()
    for m in doc.get("miners") or []:
        if _row_id(m) == mid:
            m["active_preset"] = "idle"
    srv.save_registry_doc(doc)
    return {"ok": True, "plan": info, "idle": True}


def capture_state(mid: str) -> dict[str, Any]:
    """What the miner runs now, so a schedule can put it back after a painted stretch: its preset, or
    else its clock / voltage / PV, and its fan control."""
    srv = _srv()
    doc = srv.load_registry_doc()
    row = next((m for m in doc.get("miners") or [] if _row_id(m) == mid), {})
    held: dict[str, Any] = {"at": int(time.time()), "fan_control": dict(row.get("fan_control") or {})}
    key = row.get("active_preset")
    if key and ((doc.get("presets") or {}).get(mid) or {}).get(key):
        held["preset"] = key
        return held
    from miner_client import parse_plan
    s = srv.get_client(mid).api("GET", "/mcb/setting")
    if not isinstance(s, dict) or "manual" not in s:
        raise ValueError("couldn't read what the miner runs now (its settings didn't come back)")
    if not s.get("manual"):
        # it runs a firmware level (its stock plan or its Idle mode): manualPowerplan isn't what runs then,
        # so remember the fields that pick the level and put exactly those back
        held["raw"] = {k: s.get(k) for k in ("manual", "select", "manualPowerplan")}
        return held
    m, v, _a, _b, pv = parse_plan(str(s.get("manualPowerplan") or ""))
    held["plan"] = {"mhz": m, "mv": v, "pv": pv}
    return held


def restore_state(mid: str, held: dict[str, Any]) -> dict[str, Any]:
    """Put back what capture_state saved (a preset, or a clock / voltage / PV and the fan control)."""
    srv = _srv()
    if held.get("preset"):
        r = apply_preset({"miner_id": mid, "key": held["preset"], "_by": "schedule"})
    elif held.get("plan"):
        doc = srv.load_registry_doc()
        row = next((m for m in doc.get("miners") or [] if _row_id(m) == mid), {})
        mhz, mv, pv = _check_plan(held["plan"], row.get("hardware"))
        client = srv.get_client(mid)
        s = client.api("GET", "/mcb/setting")
        if not isinstance(s, dict):
            raise ValueError("couldn't read the miner's settings")
        from miner_client import build_plan, parse_plan
        _m, _v, fa, fb, _pv = parse_plan(s.get("manualPowerplan") or "")
        s.update(manual=True, select=0, manualPowerplan=build_plan(mhz, mv, fa, fb, pv))
        client.api("PUT", "/mcb/setting", s)
        r = {"ok": True, "plan": s["manualPowerplan"]}
    elif isinstance(held.get("raw"), dict):
        client = srv.get_client(mid)
        s = client.api("GET", "/mcb/setting")
        if not isinstance(s, dict):
            raise ValueError("couldn't read the miner's settings")
        raw = {k: v for k, v in held["raw"].items() if k in ("manual", "select", "manualPowerplan") and v is not None}
        if "manual" not in raw or "select" not in raw:
            raise ValueError("the saved setting to go back to is incomplete; set the miner by hand")
        s.update(raw)
        client.api("PUT", "/mcb/setting", s)
        r = {"ok": True, "plan": "its own firmware level"}
    else:
        return {"ok": True}
    doc = srv.load_registry_doc()
    for m in doc.get("miners") or []:
        if _row_id(m) == mid:
            if not held.get("preset"):
                m["active_preset"] = None
                raw = held.get("raw") or {}
                if raw and not raw.get("manual") and raw.get("select") is not None \
                        and raw.get("select") == (m.get("hardware") or {}).get("idle_select"):
                    m["active_preset"] = "idle"      # back in the firmware's Idle mode
            if held.get("fan_control"):
                m["fan_control"] = held["fan_control"]
    srv.save_registry_doc(doc)
    return r


# ---------------------------------------------------------------- hardware

PROBE_RETRY_S = 600
_probing: set[str] = set()
_probe_lock = threading.Lock()


def _chips_per_board(client: Any) -> list[int] | None:
    """Chips on each hash board, from /dbg/icinfo (None if the model doesn't offer it)."""
    try:
        raw = client.api("GET", "/dbg/icinfo")
        if isinstance(raw, dict) and isinstance(raw.get("body"), str):
            raw = json.loads(raw["body"])
        elif isinstance(raw, str):
            raw = json.loads(raw)
        dd = raw.get("drawdata") if isinstance(raw, dict) else None
        if isinstance(dd, list) and dd:
            return [len(b or []) for b in dd]
    except Exception:
        pass
    return None


_ANY_PLAN = re.compile(r"^\s*(\d{1,5})\s*MHz\s+(\d{1,5}(?:\.\d{1,4})?)\s*V\s+(\d{1,3})\s*RPM\s+(\d{1,3})\s*RPM"
                       r"(?:\s+PV\s+(\d{1,5}(?:\.\d{1,4})?))?\s*$", re.I)


def read_any_plan(text: Any) -> dict[str, Any] | None:
    """Any of the plan forms, for showing: {mhz, volts (as written), fan_a, fan_b, pv or None}."""
    m = _ANY_PLAN.match(str(text or "")[:200])
    if not m:
        return None
    return {"mhz": int(m.group(1)), "volts": m.group(2), "fan_a": int(m.group(3)), "fan_b": int(m.group(4)),
            "pv": m.group(5)}


def running_plan(s: dict[str, Any], flat: list[dict[str, Any]]) -> dict[str, Any]:
    """Which plan the miner is really running, from /mcb/setting.
    manual: true -> manualPowerplan. manual: false -> the selected level (select) of the stock plans,
    of the running algorithm on a miner with one plan list per algorithm (HS BOX: algoname). The HS
    BOX's manualPowerplan can then hold the other algorithm's plan, so it isn't what runs."""
    algo = s.get("algoname") if isinstance(s.get("algoname"), str) else None
    mine = [p for p in flat if not algo or not p.get("algo") or p.get("algo") == algo]
    manual = bool(s.get("manual"))
    try:
        sel = int(s.get("select") or 0)
    except (TypeError, ValueError):
        sel = 0
    lvl = lambda p: int(p.get("level") or 0) if str(p.get("level") or "0").lstrip("-").isdigit() else -1
    stock = next((str(p.get("info") or "") for p in mine if lvl(p) == 0), "")[:200]
    running = str(s.get("manualPowerplan") or "")[:200] if manual else \
        next((str(p.get("info") or "") for p in mine if lvl(p) == sel), "")[:200]
    texts = [t for t in (running, stock) if t and not t.startswith("0 MHz")]
    return {"plan_format": tuner_addon.plan_format(texts[0] if texts else ""),
            "algo": (algo or None) and algo[:40], "manual": manual,
            "stock_text": stock or None, "running_text": running or None,
            "live_text": str(s.get("manualPowerplan") or "")[:200] or None,
            "stock_read": read_any_plan(stock), "running_read": read_any_plan(running)}


def _stock_plans(client: Any, hw: dict[str, Any] | None = None) -> tuple[dict[str, int] | None, list[dict[str, int]]]:
    """The firmware's own power plans from /mcb/setting. Level 0 is the stock (normal) plan:
    625 MHz / 9100 mV / PV 9400 on an SC Lite, 700 / 11750 / 11900 on an SC5 Pro II.
    Also notes in hw how the miner writes its plans (plan_format), the stock and live plan text,
    and the live setting (current) when it's in the form this app can change."""
    from miner_client import parse_plan
    s = client.api("GET", "/mcb/setting") or {}
    raw = s.get("powerplans") or []
    nested = any(isinstance(p, dict) and isinstance(p.get("mode"), list) for p in raw)
    flat = []
    for p in raw if isinstance(raw, list) else []:
        if not isinstance(p, dict):
            continue
        if isinstance(p.get("mode"), list):      # HS BOX: one list of plans per algorithm
            flat += [dict(m, algo=p.get("algo")) for m in p["mode"] if isinstance(m, dict)]
        else:
            flat.append(p)
    if hw is not None:
        hw.update(running_plan(s, flat))
        hw["plan_nested"] = nested
        hw["fan_target"] = _fan_target_of(s)
        try:
            m, v, _a, _b, pv = parse_plan(hw.get("running_text") or "")
            hw["current"] = {"mhz": m, "mv": v, "pv": pv}
        except Exception:
            hw["current"] = None
        # the firmware's own Idle mode (level 3, "0 MHz 0 V ..."): select it to stop hashing. Only on a
        # miner with one flat list of plans (the SC Lite); select is that plan's place in the list
        hw["idle_select"], hw["idle_text"] = None, None
        if not nested and isinstance(raw, list):
            for i, p in enumerate(raw):
                info = str(p.get("info") or "").strip() if isinstance(p, dict) else ""
                if info.startswith("0 MHz"):
                    hw["idle_select"], hw["idle_text"] = i, info[:80]
                    break
        if hw["plan_format"] != "sc-lite":
            return None, []          # not a form this app can change: no stock setting to tune from
        flat = [p for p in flat if not hw.get("algo") or not p.get("algo") or p.get("algo") == hw["algo"]]
    plans = []
    for p in flat:
        try:
            mhz, mv, _a, _b, pv = parse_plan(str(p.get("info") or ""))
        except Exception:
            continue          # e.g. the "0 MHz 0 V" off plan has no PV
        if mhz > 0 and mv > 0:
            plans.append({"level": int(p.get("level") or 0), "mhz": mhz, "mv": mv, "pv": pv})
    stock = next((dict(mhz=p["mhz"], mv=p["mv"], pv=p["pv"]) for p in plans if p["level"] == 0), None)
    return stock, plans


def _fan_target_of(s: dict[str, Any]) -> dict[str, Any] | None:
    """The firmware fan loop's target from /mcb/setting: temp_target inside temp_targets ([low, high] in C),
    or None when the miner doesn't report one."""
    rng, val = s.get("temp_targets"), s.get("temp_target")
    try:
        lo, hi, v = float(rng[0]), float(rng[1]), float(val)
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    if not 0 < lo < hi <= 120:
        return None
    return {"value": v, "min": lo, "max": hi}


def _settings_format(s: dict[str, Any]) -> str:
    """The plan format of every plan text in /mcb/setting (manualPowerplan and each power plan, nested per
    algorithm on the HS Box): 'box' only when they're all box-style (the "0 MHz 0 V" off plans are left out)."""
    texts = [s.get("manualPowerplan")]
    for p in s.get("powerplans") or []:
        if isinstance(p, dict):
            texts += [m.get("info") for m in p.get("mode") or [] if isinstance(m, dict)] if isinstance(p.get("mode"), list) else [p.get("info")]
    forms = {tuner_addon.plan_format(t) for t in texts if t and not str(t).strip().startswith("0 MHz")}   # off plans say nothing
    return "box" if forms == {"box"} else ("mixed" if len(forms) > 1 else (forms.pop() if forms else "unknown"))


FAN_TARGET_GAP_S = 60     # between two fan-target writes to one miner (a double click, or two tabs)
_fan_target_last: dict[str, float] = {}
_fan_target_lock = threading.Lock()


def fan_target_problem(hw: dict[str, Any] | None) -> str:
    """Why this miner has no fan target control ('' if it has). Only the SC Box / HS Box family: their
    firmware ignores fan numbers and steers the fans to temp_target itself, so that target is the one fan
    setting the app writes there. Every other model keeps the app's own fan control (curves, fan %)."""
    hw = hw or {}
    if not hw.get("ok"):
        return "this miner hasn't been probed yet"
    if hw.get("plan_format") != "box":
        return "the fan target is only for the SC Box and HS Box (other models use the app's fan curves)"
    if not hw.get("fan_target"):
        return "this miner's firmware doesn't report a fan target range; press Probe now"
    return ""


def _fan_target_row(mid: str) -> dict[str, Any]:
    row = next((m for m in _srv().load_registry() if _row_id(m) == mid), None)
    if not row:
        raise ValueError(f"unknown miner {mid}")
    why = fan_target_problem(row.get("hardware"))
    if why:
        raise ValueError(why)
    return row


def fan_target_state(mid: str) -> dict[str, Any]:
    """The fan target as the miner reports it now (one settings read)."""
    _fan_target_row(mid)
    s = _srv().get_client(mid).api("GET", "/mcb/setting")
    ft = _fan_target_of(s if isinstance(s, dict) else {})
    if not ft:
        raise ValueError("the miner didn't report a fan target just now")
    return {"ok": True, "miner_id": mid, **ft}


def set_fan_target(body: dict[str, Any]) -> dict[str, Any]:
    """Write the SC Box / HS Box fan target. Only temp_target changes: the settings are read fresh and put
    back as the miner gave them (its clock, voltage, manual flag and plan stay exactly as they are; the
    stock page's own Save would reset a manual clock to the stock plan). Range and whole degrees as the
    firmware allows. Tested on both: the target holds and the fan loop steers to it within seconds; the SC
    Box's fans run near full speed for about 15 minutes after a write, the HS Box's don't."""
    mid = str(body.get("miner_id") or "")
    _fan_target_row(mid)
    try:
        want = float(body.get("target"))
    except (TypeError, ValueError):
        raise ValueError("give the fan target in whole degrees C") from None
    if not want.is_integer():
        raise ValueError("the fan target is in whole degrees C")
    with _fan_target_lock:      # two tabs or a double click: only one write gets through
        left = FAN_TARGET_GAP_S - (time.time() - _fan_target_last.get(mid, 0.0))
        if left > 0:
            raise ValueError(f"the fan target was just changed; try again in {int(left) + 1} s")
        prev, _fan_target_last[mid] = _fan_target_last.get(mid, 0.0), time.time()

    def refuse(msg: str) -> None:       # nothing was written: the next try needn't wait
        _fan_target_last[mid] = prev
        raise ValueError(msg)
    client = _srv().get_client(mid)
    s = client.api("GET", "/mcb/setting")
    ft = _fan_target_of(s if isinstance(s, dict) else {})
    if not ft:
        refuse("the miner didn't report a fan target just now; press Probe now")
    if not ft["min"] <= want <= ft["max"]:
        refuse(f"this miner's fan target must be {ft['min']:g} to {ft['max']:g} °C")
    if _settings_format(s) != "box":
        refuse("this miner's settings no longer look like an SC Box / HS Box; press Probe now")
    before = ft["value"]
    if want != before:
        s["temp_target"] = int(want)
        client.api("PUT", "/mcb/setting", s)
    after = _fan_target_of(client.api("GET", "/mcb/setting") or {}) or {}
    held = after.get("value") == want
    doc = _srv().load_registry_doc()
    for m in doc.get("miners") or []:
        if _row_id(m) == mid:
            if isinstance(m.get("hardware"), dict) and after:
                m["hardware"]["fan_target"] = after
            m["fan_target_set"] = {"value": want, "from": before, "held": held, "at": int(time.time())}
    _srv().save_registry_doc(doc)
    if not held:
        raise ValueError(f"the miner didn't keep {want:g} °C (it reports {after.get('value')}); nothing else was changed")
    return {"ok": True, "miner_id": mid, "value": want, "from": before, "min": ft["min"], "max": ft["max"],
            "changed": want != before}


def probe_hardware(mid: str) -> dict[str, Any]:
    """Detect what the miner is and store it on its row in miners.json."""
    srv = _srv()
    row = next((m for m in srv.load_registry() if _row_id(m) == mid), None)
    if not row:
        raise ValueError(f"unknown miner {mid}")
    ip, pw = str(row.get("ip") or ""), str(row.get("password") or "")
    hw: dict[str, Any] = {"ok": False, "ip": ip, "probed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                          "probed_ts": int(time.time())}      # (the retry timer uses this: no time zone in it)
    try:
        from probe import probe_miner
        r = probe_miner(ip, password=pw, try_common_passwords=False, deep=False)
        ident = r.get("identity") or {}
        cap = ident.get("capability") or {}
        obs = ident.get("boards_observed") or {}
        hw.update(
            model=ident.get("model"), firmware=ident.get("firmware"),
            profile=ident.get("suggested_profile") or "unknown", name=cap.get("name"),
            support=ident.get("support_level"), plan_dialect=(r.get("dialect") or {}).get("suggested_profile"),
            boards=obs.get("nboards"), fans=obs.get("nfans"),
            rated_ths=round(cap["rated_mhs"] / 1e6, 2) if cap.get("rated_mhs") else None,
            rated_watts=cap.get("rated_watts"),
        )
        ports = r.get("ports") or {}
        if not ports.get("80") and not ports.get("4028"):
            hw["error"] = f"no answer at {ip} (is it on, and is the IP right?)"
        elif not (r.get("login") or {}).get("ok"):
            hw["error"] = "couldn't sign in to the miner (check its password under Settings)"
        else:
            hw["ok"] = True
            from miner_client import MinerClient
            client = MinerClient(ip=ip, password=pw)
            try:
                hw["stock"], hw["stock_plans"] = _stock_plans(client, hw)
            except Exception as e:
                # signed in, but its settings didn't come back: not a finished probe (it's tried again in
                # PROBE_RETRY_S, not straight away, and nothing treats the model as known meanwhile)
                hw["stock"], hw["stock_plans"] = None, []
                hw["ok"] = False
                hw["error"] = f"signed in, but couldn't read its settings ({type(e).__name__}: {e})"[:160]
            chips = _chips_per_board(client)
            if chips:
                hw["chips_per_board"] = chips
                hw["chips"] = sum(chips)
                hw["boards"] = hw.get("boards") or len(chips)
        hw["tuner_tested"] = hw.get("profile") == "sc-lite"
    except Exception as e:
        hw["error"] = f"{type(e).__name__}: {e}"[:160]
    doc = srv.load_registry_doc()
    for m in doc.get("miners") or []:
        if _row_id(m) == mid:
            old = m.get("hardware") or {}
            if not hw.get("ok") and old.get("ok") and old.get("ip") == hw.get("ip"):
                # a probe that failed (miner busy, offline for a moment) doesn't wipe what an earlier probe
                # found: the model, its plan format (what keeps a box read only), its fan target, its stock plan
                m["hardware"] = {**old, "last_probe_error": hw.get("error"), "last_probe_at": hw.get("probed_at")}
            else:
                m["hardware"] = hw
            pace_for(m)
            _sync_idle_preset(doc, mid, hw)
    srv.save_registry_doc(doc)
    return hw


IDLE_PRESET = {"label": "Idle", "source": "firmware", "idle": True, "ok": True, "mhz": 0, "mv": 0, "pv": 0,
               "fan_profile": None, "reason": "the firmware's own Idle mode: no hashing"}


def _sync_idle_preset(doc: dict[str, Any], mid: str, hw: dict[str, Any]) -> None:
    """An Idle preset for a miner whose firmware has an Idle mode (and whose plans this app can change);
    removed again if a probe no longer finds it."""
    if not hw.get("ok"):
        return          # a probe that failed says nothing about the Idle mode: leave the preset as it is
    mine = (doc.setdefault("presets", {})).setdefault(mid, {})
    has = hw.get("idle_select") is not None and hw.get("plan_format") == "sc-lite"
    if has and "idle" not in mine:
        mine["idle"] = dict(IDLE_PRESET)
    elif not has and (mine.get("idle") or {}).get("source") == "firmware":
        del mine["idle"]
        for m in doc.get("miners") or []:
            if _row_id(m) == mid and m.get("active_preset") == "idle":
                m["active_preset"] = None


def hw_line(hw: dict[str, Any] | None) -> str:
    if not hw:
        return "not probed yet"
    if not hw.get("ok"):
        return "probe failed: " + str(hw.get("error") or "no answer")
    bits = [str(hw.get("name") or hw.get("model") or "unknown model")]
    if hw.get("chips_per_board"):
        cpb = hw["chips_per_board"]
        bits.append(f"{len(cpb)}×{cpb[0]} chips" if len(set(cpb)) == 1 else f"{hw.get('chips')} chips")
    elif hw.get("boards"):
        bits.append(f"{hw['boards']} boards")
    if hw.get("fans"):
        bits.append(f"{hw['fans']} fans")
    if hw.get("firmware"):
        bits.append(f"fw {hw['firmware']}")
    return " · ".join(bits)


def auto_probe() -> None:
    """Probe (in the background) any miner that hasn't been, one at a time."""
    if _probing:
        return
    srv = _srv()
    now = time.time()
    for m in srv.load_registry():
        mid = _row_id(m)
        if m.get("demo") or not m.get("ip") or (m.get("password") or "") in ("", "CHANGE_ME"):
            continue
        hw = m.get("hardware") or {}
        if hw.get("ok") and hw.get("ip", m.get("ip")) == m.get("ip") and "stock" in hw and "idle_select" in hw:
            continue                      # probed (and the IP hasn't changed since)
        if hw.get("ok") and "idle_select" not in hw:
            hw = {}                       # probed before 1.13: probe again to find its Idle mode
        if hw.get("ok") and "stock" not in hw:
            hw = {}                       # probed before 1.8.3: probe again to read its stock plan
        if hw.get("ip") and hw.get("ip") != m.get("ip"):
            hw = {}                       # moved to a new IP: probe again now
        try:
            last = float(hw.get("probed_ts") or 0) or (
                time.mktime(time.strptime(hw.get("probed_at", "")[:19], "%Y-%m-%dT%H:%M:%S")) if hw else 0)
        except (ValueError, TypeError):
            last = 0
        if hw and now - last < PROBE_RETRY_S:
            continue
        if tuner_addon.running(mid):
            continue

        def run(mid: str = mid) -> None:
            try:
                hw = probe_hardware(mid)
                print(f"[hardware] {mid}: {hw_line(hw)}", flush=True)
            except Exception as e:
                print(f"[hardware] {mid}: {e}", flush=True)
            finally:
                with _probe_lock:
                    _probing.discard(mid)
        with _probe_lock:
            _probing.add(mid)
        threading.Thread(target=run, daemon=True, name=f"probe-{mid}").start()
        return


# ---------------------------------------------------------------- health + power (Fleet cards)

HEALTH_EVERY_S = 600
_health: dict[str, dict[str, Any]] = {}       # mid -> short summary for the cards
_health_busy = [False]
_health_last = [0.0]


def _summarise(mid: str, d: dict[str, Any]) -> dict[str, Any]:
    weak = [f"b{b['board']}c{c}" for b in d["boards"] for c in b.get("weak") or []]
    watch = [f"b{b['board']}c{c}" for b in d["boards"] for c in b.get("watch") or []]
    bad = [b["bad_pct"] for b in d["boards"] if b.get("bad_pct") is not None]
    maxc = [b["max_c"] for b in d["boards"] if b.get("max_c") is not None]
    return {"weak": weak, "watch": watch, "at": int(time.time()),
            "max_chip_c": max(maxc) if maxc else None, "log": bool(d.get("log"))}


def health_tick() -> None:
    """Every HEALTH_EVERY_S, refresh each miner's chip health in the background (one at a time)."""
    if _health_busy[0] or time.time() - _health_last[0] < HEALTH_EVERY_S:
        return
    _health_last[0] = time.time()
    _health_busy[0] = True

    def run() -> None:
        try:
            for m in _srv().load_registry():
                mid = _row_id(m)
                if m.get("demo") or not (m.get("hardware") or {}).get("ok"):
                    continue
                try:
                    _health[mid] = _summarise(mid, chips(mid, force=True))
                except Exception as e:
                    print(f"[health] {mid}: {e}", flush=True)
        finally:
            _health_busy[0] = False
    threading.Thread(target=run, daemon=True, name="chip-health").start()


def power_model(doc: dict[str, Any], row: dict[str, Any]) -> dict[str, Any] | None:
    """What the power estimate needs: the model's rated watts / TH/s at its stock plan, and a
    calibration from a wall reading, if you gave one."""
    hw = row.get("hardware") or {}
    stock = hw.get("stock") or {}
    if not hw.get("rated_watts") or not stock.get("mhz") or not stock.get("pv"):
        return None
    pw = doc.get("power") or {}
    return {"rated_w": hw["rated_watts"], "rated_ths": hw.get("rated_ths"), "mhz": stock["mhz"], "pv": stock["pv"],
            "cal": float((row.get("power_cal") or {}).get("factor") or 1.0), "mains_v": float(pw.get("mains_v") or 110),
            "cal_info": row.get("power_cal")}


def estimate_watts(pm: dict[str, Any] | None, mhz: float, pv: float) -> dict[str, Any] | None:
    """Wall power estimate: chips scale with clock x voltage^2 (PV is the real board voltage); about
    7% (fans, control board) doesn't. Clocks run in 12.5 MHz steps."""
    if not pm or not mhz or not pv:
        return None
    eff = (int(mhz / 12.5)) * 12.5
    w = pm["rated_w"] * pm["cal"] * (0.07 + 0.93 * (eff / pm["mhz"]) * (pv / pm["pv"]) ** 2)
    ths = (pm["rated_ths"] or 0) * eff / pm["mhz"] if pm.get("rated_ths") else None
    return {"w": round(w), "amps": round(w / pm["mains_v"], 1), "ths": round(ths, 2) if ths else None,
            "w_per_th": round(w / ths) if ths else None, "mhz_actual": eff}


def calibrate_power(body: dict[str, Any]) -> dict[str, Any]:
    """You measured the wall power at the miner's current setting: scale the estimate to match."""
    mid = str(body.get("miner_id") or "")
    srv = _srv()
    doc = srv.load_registry_doc()
    row = next((m for m in doc.get("miners") or [] if _row_id(m) == mid), None)
    if not row:
        raise ValueError(f"unknown miner {mid}")
    if body.get("clear"):
        row.pop("power_cal", None)
        srv.save_registry_doc(doc)
        return {"ok": True}
    watts, mhz, pv = float(body.get("watts") or 0), float(body.get("mhz") or 0), float(body.get("pv") or 0)
    if not 50 <= watts <= 10000 or not mhz or not pv:
        raise ValueError("give the measured watts (50-10000) and the setting it was measured at")
    pm = power_model(doc, {**row, "power_cal": None})
    if not pm:
        raise ValueError("this miner's rated power isn't known, so there's nothing to calibrate")
    est = estimate_watts(pm, mhz, pv)
    row["power_cal"] = {"factor": round(watts / est["w"], 3), "watts": watts, "mhz": mhz, "pv": pv,
                        "at": time.strftime("%Y-%m-%d %H:%M")}
    srv.save_registry_doc(doc)
    return {"ok": True, "factor": row["power_cal"]["factor"]}


def set_mains(body: dict[str, Any]) -> dict[str, Any]:
    v = float(body.get("mains_v") or 0)
    if not 90 <= v <= 260:
        raise ValueError("mains voltage must be 90-260 V")
    srv = _srv()
    doc = srv.load_registry_doc()
    doc.setdefault("power", {})["mains_v"] = v
    srv.save_registry_doc(doc)
    return {"ok": True}


def pace_for(row: dict[str, Any]) -> None:
    """Slow requests down to a miner whose web backend can't take a burst (SC BOX, HS BOX)."""
    hw = row.get("hardware") or {}
    fast = bool(hw.get("ok")) and hw.get("plan_format", "sc-lite" if hw.get("stock") else None) == "sc-lite" and hw.get("profile") not in ("sc-box", "hs-box")
    if row.get("ip"):
        import miner_safety
        miner_safety.set_gap(str(row["ip"]), miner_safety.FAST_GAP_S if fast else miner_safety.BOX_GAP_S)


def hardware_list() -> dict[str, Any]:
    """Per miner: what it was detected as, plus its preset, schedule and tuning state (Fleet cards)."""
    doc = _srv().load_registry_doc()
    shares = shares_addon.load()
    rejects = rejects_addon.load()
    out = {}
    for m in doc.get("miners") or []:
        mid = _row_id(m)
        hw = m.get("hardware")
        pace_for(m)
        p = ((doc.get("presets") or {}).get(mid) or {}).get(m.get("active_preset") or "")
        out[mid] = {"hardware": hw, "line": hw_line(hw), "probing": mid in _probing,
                    "preset": (f"{p.get('label')} ({'tuned' if p.get('source') == 'tuner' and p.get('ok') else 'hand-set'})"
                               if p else None),
                    "presets": {k: {"label": v.get("label") or k, "mhz": v.get("mhz"), "mv": v.get("mv"), "pv": v.get("pv"),
                                    "tuned": v.get("source") == "tuner", "idle": bool(v.get("idle")),
                                    "power": estimate_watts(power_model(doc, m), v.get("mhz"), v.get("pv"))}
                                for k, v in ((doc.get("presets") or {}).get(mid) or {}).items() if v.get("ok") is not False},
                    "active": m.get("active_preset"),
                    "schedule": schedule_addon.short_line(doc, mid),
                    "schedule_paused": bool(((doc.get("schedules") or {}).get(mid) or {}).get("paused")),
                    "tuning": bool(tuner_addon.running(mid)),
                    "tuner": tuner_addon.plan_limits(hw),
                    "in_charge": in_charge(doc, mid),
                    "power_model": power_model(doc, m),
                    "health": _health.get(mid),
                    "temp_source": (m.get("fan_control") or {}).get("temp_source") or "board",
                    "chip_offset": chip_offset(mid),
                    "restart": restart_view(mid),
                    "share": shares_addon.card(shares.get(mid)),
                    "rejects": rejects_addon.card(rejects.get(mid)),
                    "hashrate": hashrate_addon.card(mid, rejects_addon.card(rejects.get(mid)))}
    # the app's own time zone (Settings -> Time zone), so the page prints times the way the app counts them
    return {"ok": True, "miners": out, "utc_offset_min": int(time.localtime().tm_gmtoff // 60)}


# ---------------------------------------------------------------- chips (Miner page)

_chip_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def chips(mid: str, force: bool = False) -> dict[str, Any]:
    """Every chip's hardware errors and perf since the boards last started (/dbg/icinfo), with
    its health from the miner's log (share of bad results, board chip temperatures).
    Cached (60 s, 120 s while tuning; the log 5 min) so opening the page can't flood the miner."""
    ttl = 120 if tuner_addon.running(mid) else 60
    ts, val = _chip_cache.get(mid, (0.0, {}))
    if val and not force and time.time() - ts < ttl:
        return {**val, "age_s": int(time.time() - ts)}
    srv = _srv()
    row = next((m for m in srv.load_registry() if _row_id(m) == mid), None)
    if not row:
        raise ValueError(f"unknown miner {mid}")
    from miner_client import MinerClient
    c = MinerClient(ip=str(row.get("ip")), password=str(row.get("password") or ""))
    raw = c.api("GET", "/dbg/icinfo")
    if isinstance(raw, dict) and isinstance(raw.get("body"), str):
        raw = json.loads(raw["body"])
    elif isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict) or not isinstance(raw.get("drawdata"), list):
        raise ValueError("this miner doesn't report per-chip data (/dbg/icinfo)")
    boards = []
    for b, board in enumerate(raw["drawdata"]):
        tab = (raw.get("tabledata") or [{}] * (b + 1))
        info = tab[b] if b < len(tab) and isinstance(tab[b], dict) else {}
        up_s = float(info.get("time") or 0)
        cl = []
        for chip in board or []:
            idx = int(chip.get("chipindex", chip.get("nr", 0) + 1))
            cl.append({"chip": idx, "hwerr": int(chip.get("hwerr") or 0),
                       "perf": int(chip.get("perf") or 0) or None, "bist": chip.get("bist")})
        boards.append({"board": b, "name": info.get("name") or f"board {b}", "uptime_s": up_s,
                       "reboots": int(info.get("reboot") or 0), "chips": sorted(cl, key=lambda x: x["chip"])})
    log = health_addon.read(c, mid, force=force)
    hwi = row.get("hardware") or {}
    box = bool(tuner_addon.format_problem(hwi)) or hwi.get("profile") in ("sc-box", "hs-box")
    health_addon.grade(boards, log, hw_fallback=box)
    _health[mid] = _summarise(mid, {"boards": boards, "log": log})
    val = {"ok": True, "boards": boards, "read_at": int(time.time()),
           "log": ({"from": log.get("from"), "to": log.get("to"), "dtfs_events": log.get("dtfs_events"),
                    "age_s": log.get("age_s")} if log else None)}
    _chip_cache[mid] = (time.time(), val)
    return {**val, "age_s": 0}


# ---------------------------------------------------------------- guard (dashboard's own routes)

LOCKED_WHILE_TUNING = {"fan", "fan_nudge", "tempcontrol", "plan", "restart", "fan_control",
                       "failback", "make_preferred", "set_pool_order",   # these three restart the miner
                       "add_pool"}                                        # (and this one can: preferred + restart)
# while a preset or a running schedule is in charge of a miner, its clock and fans are locked
# too, until you take manual control (the Miner page switch, /api/quick/manual)
LOCKED_BY_PRESET = {"fan", "fan_nudge", "tempcontrol", "plan", "fan_control"}


def in_charge(doc: dict[str, Any], mid: str) -> list[str]:
    """What controls this miner's clock and fans right now: a preset and/or a running schedule."""
    out = []
    row = next((m for m in doc.get("miners") or [] if _row_id(m) == mid), None) or {}
    key = row.get("active_preset")
    if key:
        p = ((doc.get("presets") or {}).get(mid) or {}).get(key) or {}
        out.append(f"the {p.get('label') or key} preset")
    sch = (doc.get("schedules") or {}).get(mid) or {}
    if sch.get("enabled") and not sch.get("paused") and sch.get("rules") and not schedule_addon.in_back_time(sch):
        out.append("its schedule")
    return out


def take_manual(body: dict[str, Any]) -> dict[str, Any]:
    """Manual control of the clock and fans: the preset turns off and the schedule pauses."""
    ids = [str(x) for x in (body.get("miner_ids") or ([body["miner_id"]] if body.get("miner_id") else []))]
    if not ids:
        raise ValueError("no miner given")
    srv = _srv()
    doc = srv.load_registry_doc()
    known = {_row_id(m) for m in doc.get("miners") or []}
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise ValueError(f"unknown miner {', '.join(unknown)}")
    for m in doc.get("miners") or []:
        mid = _row_id(m)
        if mid in ids:
            m.pop("active_preset", None)
            sch = (doc.get("schedules") or {}).get(mid)
            if sch and sch.get("enabled") and not sch.get("paused"):
                sch["paused"] = {"reason": "manual control", "at": int(time.time())}
    srv.save_registry_doc(doc)
    return {"ok": True}


_HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")


def check_address(ip: Any) -> str:
    """A miner address must be a bare IPv4 address or host name: no port, path, user or query (so the
    probe and the miner client can't be pointed at other services), and not this machine or a
    link-local / unspecified address. SCLITE_ALLOW_LOOPBACK=1 allows 127.x (for testing with fakes)."""
    import ipaddress
    import os
    ip = str(ip or "").strip()
    if not ip:
        raise ValueError("ip required")
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        if not _HOST_RE.match(ip) or ip.replace(".", "").isdigit():
            raise ValueError("the address must be a plain IPv4 address or host name (no port, path or http://)")
        if ip.lower() in ("localhost", "localhost.localdomain"):
            raise ValueError("that address is this machine, not a miner")
        if re.fullmatch(r"(?:0x[0-9a-f]+|\d+)(?:\.(?:0x[0-9a-f]+|\d+)){0,3}", ip.lower()):
            # hex / short forms like 0x7f000001 or 0x7f.1 aren't names: they're IPv4 in disguise
            raise ValueError("write the address as four numbers, like 192.168.1.50")
        # a name: it must lead to an address a miner can have (not this machine, not link-local)
        try:
            import socket as _socket
            for fam, _t, _p, _c, sa in _socket.getaddrinfo(ip, 80, _socket.AF_INET):
                r = ipaddress.ip_address(sa[0])
                if r.is_unspecified or r.is_link_local or r.is_multicast or (r.is_loopback and os.environ.get("SCLITE_ALLOW_LOOPBACK") != "1"):
                    raise ValueError("that name leads to an address that can't be a miner on your network")
        except ValueError:
            raise
        except OSError:
            pass          # not resolvable now: it fails later with a clear "no answer"
        return ip
    if a.version != 4:
        raise ValueError("use the miner's IPv4 address")
    if a.is_unspecified or a.is_link_local or a.is_multicast or (a.is_loopback and os.environ.get("SCLITE_ALLOW_LOOPBACK") != "1"):
        raise ValueError("that address can't be a miner on your network")
    return ip


def check_miner_id(mid: Any) -> str:
    mid = str(mid or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,47}", mid):
        raise ValueError("the miner id may only use letters, digits, '.', '_' and '-' (up to 48)")
    return mid


def guard_post(handler: Any, path: str, body: Any, json_response: Callable) -> bool:
    """Runs before the dashboard's POST routes. True = answered here (refused)."""
    body = body if isinstance(body, dict) else {}
    if path in ("/api/miners", "/api/probe"):       # adding / probing a miner: a plain address and id only
        try:
            check_address(body.get("ip"))
            if body.get("id") not in (None, ""):
                check_miner_id(body.get("id"))
            if path == "/api/probe":
                body["kick_fan"] = max(FAN_MIN_PCT, min(100, int(body.get("kick_fan") or 80)))
                if body.get("add_to_registry") and body.get("id") not in (None, "") and any(
                        _row_id(m) == str(body["id"]).strip() for m in _srv().load_registry()):
                    raise ValueError(f"a miner with the id {body['id']} already exists")
        except (ValueError, TypeError) as e:
            json_response(handler, 400, {"ok": False, "error": str(e)})
            return True
        return False
    mids: list[str] = []
    action = None
    if path.startswith("/api/miners/") and "/action/" in path:
        mid, _, action = path[len("/api/miners/"):].partition("/action/")
        mids = [mid]
    elif path.startswith("/api/miners/") and path.endswith("/fan_nudge"):
        mids, action = [path[len("/api/miners/"):-len("/fan_nudge")]], "fan_nudge"
    elif path.startswith("/api/miners/") and path.endswith("/fan_control"):
        mids, action = [path[len("/api/miners/"):-len("/fan_control")]], "fan_control"
    elif path == "/api/fleet/action":
        action = str(body.get("action") or "")
        mids = [str(x) for x in (body.get("ids") or [])] or [_row_id(m) for m in _srv().load_registry()]
    else:
        return False
    mids = [m for m in mids if not m.startswith("demo-")]
    if action in ("plan", "fan", "fan_nudge", "tempcontrol") or (action == "fan_control" and body.get("enabled")):
        # a model whose power plan the app can't write yet (SC BOX, HS BOX): clock, voltage and fans stay read only
        doc = _srv().load_registry_doc()
        rows = {_row_id(r): r for r in doc.get("miners") or []}
        ro = [m for m in mids if tuner_addon.format_problem((rows.get(m) or {}).get("hardware"))]
        if ro:
            names = ", ".join(str((rows.get(m) or {}).get("name") or m) for m in ro)
            json_response(handler, 400, {"ok": False, "error": f"{names}: " + tuner_addon.format_problem(
                (rows.get(ro[0]) or {}).get("hardware")).replace("so the tuner, presets and clock settings are off",
                                                                 "so the tuner, presets, clock and fan settings are off")})
            return True
    if action in ("plan", "fan"):    # hand-set clock / voltage / PV / fan: the same hard limits as presets
        doc = _srv().load_registry_doc()
        rows = {_row_id(r): r for r in doc.get("miners") or []}
        try:
            for m in mids:
                _check_partial(body, (rows.get(m) or {}).get("hardware"))
        except ValueError as e:
            json_response(handler, 400, {"ok": False, "error": str(e)})
            return True
    if action in LOCKED_WHILE_TUNING:
        busy = [m for m in mids if tuner_addon.running(m)]
        if busy and path == "/api/fleet/action" and len(busy) < len(mids):
            body["ids"] = [m for m in mids if m not in busy]   # the miners being tuned are skipped, the rest go
            busy = []
        if busy:
            json_response(handler, 409, {"ok": False, "locked": busy,
                                         "error": f"locked while tuning: {', '.join(busy)}. The tuner controls "
                                                  "the clock and fans of a miner it is tuning; stop the run on the "
                                                  "Tuner page first."})
            return True
    if action in LOCKED_BY_PRESET:
        doc = _srv().load_registry_doc()
        held = {m: in_charge(doc, m) for m in mids}
        held = {m: v for m, v in held.items() if v}
        if held:
            names = {_row_id(r): r.get("name") or _row_id(r) for r in doc.get("miners") or []}
            what = "; ".join(f"{names.get(m, m)} is run by {' and '.join(v)}" for m, v in held.items())
            json_response(handler, 409, {"ok": False, "locked": list(held), "by": held,
                                         "error": f"locked: {what}. Take manual control on the miner's page first "
                                                  "(that turns the preset off and pauses the schedule), or change the preset."})
            return True
    return False


# ---------------------------------------------------------------- restarting miners

# The firmware restarts on PUT /mcb/restart (the dashboard used GET, which the SC Lite ignores).
# Replace the client's soft_restart everywhere (Restart, Failback, pool switch), keeping GET as a
# fallback for firmware that wants it.
import urllib.error
import urllib.request

import miner_client as _mc


def _soft_restart(self: Any, timeout: float = 8.0) -> str:
    """Restart the mining software (PUT /mcb/restart; GET on firmware that wants it). Signs in again once on
    a 401 (an old token after the miner rebooted), and keeps the per-miner pacing like every other request."""
    import miner_safety
    notify_addon.note_app_restart(self.ip)     # an uptime drop now isn't "restarted on its own"
    last: Exception | None = None
    relogged = False
    with self._lock:
        if self._token is None:
            self.login()
        methods = ["PUT", "GET"]
        while methods:
            method = methods[0]
            req = urllib.request.Request(self.host() + "/mcb/restart", data=b"" if method == "PUT" else None,
                                         headers={"Authorization": self._token, "Accept": "*/*"}, method=method)
            try:
                with miner_safety._Paced(self.ip), urllib.request.urlopen(req, timeout=timeout) as r:
                    r.read()
                    return f"restart HTTP {getattr(r, 'status', 200)} ({method})"
            except urllib.error.HTTPError as e:
                if e.code == 401 and not relogged:
                    relogged = True
                    self._token = None
                    self.login()
                    continue
                if method == "PUT" and e.code in (400, 404, 405, 501):
                    last = e
                    methods.pop(0)
                    continue
                raise
            except Exception as e:   # the connection usually drops as the restart starts
                msg = str(e).lower()
                if any(x in msg for x in ("closed", "reset", "timed out", "timeout", "refused", "remote end", "aborted")):
                    return f"restart requested ({type(e).__name__})"
                raise
    raise last or RuntimeError("restart not accepted")


_mc.MinerClient.soft_restart = _soft_restart


# Bounded reads from miners, and miner-reported text cleaned before it reaches a page (miner_safety.py)
import miner_safety  # noqa: E402
miner_safety.install(_mc.MinerClient)

_restarts: dict[str, dict[str, Any]] = {}
RESTART_WAIT_S = 300
RESTART_GUESS_S = 75          # until a miner has been restarted here once: send, go down, come back, hash
_restart_times_lock = threading.Lock()


def _restart_times_file() -> Any:
    import os
    from pathlib import Path
    return Path(os.environ.get("SCLITE_WEBUI_MINERS", "miners.json")).with_name("restart_times.json")


def _restart_times() -> dict[str, list[float]]:
    try:
        return json.loads(_restart_times_file().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _remember_restart(mid: str, secs: float) -> None:
    """Keep each miner's last 5 restart times, so the next progress bar matches how long it really takes."""
    with _restart_times_lock:
        d = _restart_times()
        d[mid] = (d.get(mid) or [])[-4:] + [round(secs, 1)]
        try:
            _restart_times_file().write_text(json.dumps(d), encoding="utf-8")
        except Exception:
            pass


def expected_restart_s(mid: str) -> float:
    t = sorted(_restart_times().get(mid) or [])
    return t[len(t) // 2] if t else RESTART_GUESS_S


def restart_view(mid: str) -> dict[str, Any] | None:
    """The restart's state for the Fleet card's progress bar (times relative to now, so clocks don't matter)."""
    st = _restarts.get(mid)
    if not st:
        return None
    now = time.time()
    return {**st, "elapsed_s": round(now - st.get("since", now), 1),
            "ago_s": round(now - st["at"], 1) if st.get("at") else None}


def _uptime_hash(client: Any) -> tuple[float, float]:
    s = (client.bfg("summary").get("SUMMARY") or [{}])[0]
    mhs = s.get("MHS 20s") or s.get("MHS 5s") or s.get("MHS av") or 0
    return float(s.get("Elapsed") or 0), float(mhs or 0)


def _restart_worker(mid: str) -> None:
    st = _restarts[mid]
    t0 = time.time()
    try:
        c = _srv().get_client(mid)
        try:
            before, _h = _uptime_hash(c)
        except Exception:
            before = None
        st.update(state="restarting", phase="send", msg="sending the restart…")
        c.soft_restart()
        st.update(phase="wait", msg="restarting…")
        restarted = down = False
        while time.time() - t0 < RESTART_WAIT_S:
            time.sleep(5)
            try:
                up, mhs = _uptime_hash(c)
            except Exception:
                down = True
                st.update(phase="down", msg="restarting… (not answering yet)")
                continue
            if (before is not None and up + 5 < before) or (before is None and up < 180) or (down and up < 600):
                restarted = True
            if restarted and mhs > 0:
                took = time.time() - t0
                st.update(state="done", phase="hashing", took_s=round(took), msg=f"restarted, hashing again after {int(took)} s", at=time.time())
                _remember_restart(mid, took)
                return
            if restarted:
                st.update(phase="up", msg="back up, waiting for hashing to start…")
        st.update(state="failed" if not restarted else "warn", at=time.time(),
                  msg=("no sign of a restart after 5 minutes (its uptime kept counting)" if not restarted
                       else "restarted, but not hashing yet after 5 minutes"))
    except Exception as e:
        st.update(state="failed", msg=f"{type(e).__name__}: {e}"[:160], at=time.time())
    finally:
        print(f"[restart] {mid}: {st.get('msg')}", flush=True)
        try:
            notify_addon.app_restart_done(mid, st)
        except Exception as e:
            print(f"[notify] {e}", flush=True)


def restart_miners(body: dict[str, Any]) -> dict[str, Any]:
    ids = [str(x) for x in (body.get("ids") or []) if not str(x).startswith("demo-")]
    if not ids:
        raise ValueError("say which miners to restart")
    started, skipped = [], {}
    for mid in ids:
        if tuner_addon.running(mid):
            skipped[mid] = "being tuned"
            continue
        if (_restarts.get(mid) or {}).get("state") == "restarting":
            skipped[mid] = "already restarting"
            continue
        _restarts[mid] = {"state": "restarting", "phase": "send", "msg": "starting…", "since": time.time(),
                          "expect_s": expected_restart_s(mid)}
        threading.Thread(target=_restart_worker, args=(mid,), daemon=True, name=f"restart-{mid}").start()
        started.append(mid)
    return {"ok": bool(started), "started": started, "skipped": skipped,
            **({} if started else {"error": "; ".join(f"{k}: {v}" for k, v in skipped.items())})}


# ---------------------------------------------------------------- demo miners (Settings)

def demo_rows(doc: dict[str, Any], snap: Callable[[str, str, str], dict]) -> list[dict[str, Any]]:
    """Any number of UI-only demo miners (doc["demo_count"], or 2 for the old switch)."""
    if not doc.get("demo"):
        return []
    n = max(0, min(60, int(doc.get("demo_count") or 2)))
    rows = []
    for i in range(1, n + 1):
        mid, name, ip = f"demo-{i:02d}", f"Demo {i:02d}", f"192.168.0.{200 + i}"
        on = i % 3 != 0
        rows.append({"id": mid, "name": name, "ip": ip, "demo": True,
                     "fan_control": {"enabled": on, "profile": "curve-60" if on else "steps-default", "fan_offset": 0},
                     "snapshot": snap(mid, name, ip),
                     "fan_runtime": {"enabled": on, "last_status": "CURVE fan=62 temp=61.0 (demo)" if on else "OFF (demo)"}})
    return rows


def set_demo(body: dict[str, Any]) -> dict[str, Any]:
    n = int(body.get("count") or 0)
    if not 0 <= n <= 60:
        raise ValueError("0 to 60 demo miners")
    srv = _srv()
    doc = srv.load_registry_doc()
    doc["demo"] = n > 0
    doc["demo_count"] = n
    srv.save_registry_doc(doc)
    return {"ok": True, "count": n}


def probe_now(body: dict[str, Any]) -> dict[str, Any]:
    mid = str(body.get("miner_id") or "")
    if tuner_addon.running(mid):
        raise ValueError("a tuning run is going on this miner; probe it when the run has ended")
    with _probe_lock:
        if mid in _probing:
            raise ValueError("already probing this miner; try again in a few seconds")
        _probing.add(mid)
    try:
        hw = probe_hardware(mid)
    finally:
        with _probe_lock:
            _probing.discard(mid)
    return {"ok": True, "hardware": hw, "line": hw_line(hw)}


# ---------------------------------------------------------------- routes


def state() -> dict[str, Any]:
    srv = _srv()
    try:
        import_presets()
    except Exception as e:
        print(f"[presets] import: {e}", flush=True)
    doc = srv.load_registry_doc()
    custom = doc.get("profiles") or {}
    profiles = fan_controller.merge_profiles(custom)
    defaults = doc.get("fan_defaults") or {}
    runtime = srv._fan_ctrl.status_blob() if getattr(srv, "_fan_ctrl", None) else {}
    clients = srv.sync_clients()
    live = live_all(clients)
    miners = []
    for mid, c in clients.items():
        row = next((m for m in (doc.get("miners") or []) if _row_id(m) == mid), {})
        fc = row.get("fan_control") or {"enabled": bool(defaults.get("enabled_default")),
                                         "profile": defaults.get("profile") or "steps-default",
                                         "fan_offset": 0}
        rt = dict(runtime.get(mid) or {})
        rt.pop("history", None)
        miners.append({"id": mid, "name": c.name, "ip": c.ip, "fan_control": fc,
                       "runtime": rt, "live": live.get(mid) or {},
                       "presets": {k: {**v, "power": estimate_watts(power_model(doc, row), v.get("mhz"), v.get("pv"))}
                                   for k, v in ((doc.get("presets") or {}).get(mid) or {}).items()},
                       "active_preset": row.get("active_preset"),
                       "tuning": bool(tuner_addon.running(mid)),
                       "hardware": row.get("hardware"), "hardware_line": hw_line(row.get("hardware")),
                       "probing": mid in _probing,
                       "tuner_fans": tuner_addon.fan_owner(mid),
                       "chip_offset": chip_offset(mid)})
    return {
        "profiles": {k: {**v, "builtin": k in fan_controller.BUILTIN_PROFILES}
                     for k, v in profiles.items()},
        "defaults": defaults,
        "miners": miners,
        "limits": LIMITS,
    }


def save_profile(body: dict[str, Any]) -> dict[str, Any]:
    srv = _srv()
    prof = validate_curve(body)
    doc = srv.load_registry_doc()
    custom = dict(doc.get("profiles") or {})
    key = str(body.get("key") or "").strip()
    if key:
        if key in fan_controller.BUILTIN_PROFILES:
            raise ValueError("built-in profiles can't be changed; save a copy instead")
        if key not in custom:
            raise ValueError(f"no custom curve called {key}")
    else:
        key = _slug(prof["label"], set(custom) | set(fan_controller.BUILTIN_PROFILES))
    custom[key] = prof
    doc["profiles"] = custom
    srv.save_registry_doc(doc)
    return {"ok": True, "key": key, "profile": prof}


def delete_profile(body: dict[str, Any]) -> dict[str, Any]:
    srv = _srv()
    key = str(body.get("key") or "")
    if key in fan_controller.BUILTIN_PROFILES:
        raise ValueError("built-in profiles can't be deleted")
    doc = srv.load_registry_doc()
    custom = dict(doc.get("profiles") or {})
    if key not in custom:
        raise ValueError(f"no custom curve called {key}")
    users = [str(m.get("name") or _row_id(m)) for m in doc.get("miners") or []
             if (m.get("fan_control") or {}).get("profile") == key]
    if users:
        raise ValueError(f"in use by {', '.join(users)}; give them another profile first")
    in_presets = [f"{p.get('label')} ({mid})" for mid, ps in (doc.get("presets") or {}).items()
                  for p in ps.values() if p.get("fan_profile") == key]
    if in_presets:
        raise ValueError(f"used by the preset {', '.join(in_presets)}; change its fan curve first")
    if (doc.get("fan_defaults") or {}).get("profile") == key:
        raise ValueError("this is the default profile for new miners; pick another default first")
    del custom[key]
    doc["profiles"] = custom
    srv.save_registry_doc(doc)
    return {"ok": True}


# ---------------------------------------------------------------- time zone (Settings)
# Schedules, restarts, logs and the tuner's CSV times use the app's local time. The container starts on
# TZ from its compose file (UTC by default); a time zone chosen in Settings is kept in miners.json
# ("timezone") and applied to this process (and so to the tuner runs it starts).

import os as _os
from pathlib import Path as _Path

_TZ_DIR = _Path("/usr/share/zoneinfo")
_TZ_DEFAULT = _os.environ.get("TZ") or "UTC"
_tz_applied = [None]


def _tz_ok(name: str) -> bool:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_+\-]*(/[A-Za-z0-9_+\-]+){0,2}", name or "") or ".." in name:
        return False
    return (_TZ_DIR / name).is_file() if _TZ_DIR.is_dir() else name in ("UTC",)


def _apply_tz(name: str) -> None:
    if not hasattr(time, "tzset"):          # Windows (run-local.ps1): the OS time zone is used
        return
    _os.environ["TZ"] = name
    time.tzset()
    _tz_applied[0] = name


def apply_saved_tz() -> None:
    try:
        name = str((_srv().load_registry_doc() or {}).get("timezone") or "")
    except Exception:
        return
    want = name if name and _tz_ok(name) else _TZ_DEFAULT
    if want != _tz_applied[0]:
        _apply_tz(want)


def _zones() -> list[str]:
    if not _TZ_DIR.is_dir():
        return ["UTC"]
    out = []
    for area in ("Africa", "America", "Antarctica", "Asia", "Atlantic", "Australia", "Europe", "Indian", "Pacific"):
        d = _TZ_DIR / area
        if d.is_dir():
            for p in d.rglob("*"):
                if p.is_file():
                    out.append(str(p.relative_to(_TZ_DIR)))
    return ["UTC"] + sorted(out)


def tz_state() -> dict[str, Any]:
    doc = _srv().load_registry_doc()
    return {"ok": True, "timezone": _tz_applied[0] or _TZ_DEFAULT, "saved": doc.get("timezone") or None,
            "default": _TZ_DEFAULT, "now": time.strftime("%Y-%m-%d %H:%M %Z (UTC%z)"), "zones": _zones(),
            "can_set": hasattr(time, "tzset")}


def set_tz(body: dict[str, Any]) -> dict[str, Any]:
    name = str(body.get("timezone") or "").strip()
    if name and not _tz_ok(name):
        raise ValueError(f"unknown time zone {name!r}: pick one from the list (like Asia/Tokyo or America/New_York)")
    srv = _srv()
    doc = srv.load_registry_doc()
    old = doc.get("timezone")
    if name:
        doc["timezone"] = name
    else:
        doc.pop("timezone", None)
    if (name or None) != old:
        for sch in (doc.get("schedules") or {}).values():   # the week's blocks now sit at other times: the
            if isinstance(sch, dict):                         # rule in force in the new zone applies next tick
                sch["last_slot"], sch["last_index"] = None, None
    srv.save_registry_doc(doc)
    _apply_tz(name or _TZ_DEFAULT)
    return tz_state()


# ---------------------------------------------------------------- best share numbers
_scale_loaded = [False]
SCALE_LABELS = {"hashes": "Like DATUM and mempool", "miner": "The miner's own numbers", "both": "Both"}


def apply_saved_scale() -> None:
    try:
        shares_addon.set_scale(str((_srv().load_registry_doc() or {}).get("share_scale") or "hashes"))
        _scale_loaded[0] = True
    except Exception:
        pass


def scale_state() -> dict[str, Any]:
    ex = 1261948.2
    return {"ok": True, "share_scale": shares_addon.scale(), "labels": SCALE_LABELS,
            "examples": {"hashes": shares_addon.fmt_hashes(ex), "miner": shares_addon.fmt_miner(ex),
                         "both": f"{shares_addon.fmt_hashes(ex)} ({shares_addon.fmt_miner(ex)})"}}


def set_scale(body: dict[str, Any]) -> dict[str, Any]:
    mode = str(body.get("share_scale") or "")
    if mode not in shares_addon.SCALES:
        raise ValueError("share_scale must be hashes, miner or both")
    srv = _srv()
    doc = srv.load_registry_doc()
    if mode == "hashes":
        doc.pop("share_scale", None)          # the default
    else:
        doc["share_scale"] = mode
    srv.save_registry_doc(doc)
    shares_addon.set_scale(mode)
    return scale_state()


def handle_get(handler: Any, path: str, json_response: Callable) -> bool:
    if path in notify_addon.ROUTES_GET:
        json_response(handler, 200, notify_addon.ROUTES_GET[path]())
        return True
    if path == "/api/fans/state":
        json_response(handler, 200, state())
        return True
    if path == "/api/hardware/list":
        json_response(handler, 200, hardware_list())
        return True
    if path == "/api/hardware/chips":
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(handler.path).query)
        mid = (q.get("miner") or [""])[0]
        try:
            json_response(handler, 200, chips(mid, force=(q.get("force") or [""])[0] == "1"))
        except ValueError as e:      # an unknown miner, or bad input
            json_response(handler, 400, {"ok": False, "error": str(e).strip("'")[:200]})
        except Exception as e:
            json_response(handler, 502, {"ok": False, "error": str(e).strip("'")[:200]})
        return True
    if path == "/api/hardware/shares":
        json_response(handler, 200, {"ok": True, "miners": shares_addon.all_cards()})
        return True
    if path == "/api/schedule/state":
        json_response(handler, 200, schedule_addon.state())
        return True
    if path == "/api/settings/timezone":
        json_response(handler, 200, tz_state())
        return True
    if path == "/api/settings/share_scale":
        json_response(handler, 200, scale_state())
        return True
    if path == "/api/hardware/fan_target":
        from urllib.parse import parse_qs, urlparse
        mid = (parse_qs(urlparse(handler.path).query).get("miner") or [""])[0]
        try:
            json_response(handler, 200, fan_target_state(mid))
        except ValueError as e:
            json_response(handler, 400, {"ok": False, "error": str(e).strip("'")})
        except Exception as e:
            json_response(handler, 502, {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]})
        return True
    if path in ("/api/hardware/report", "/api/hardware/report/download"):
        from urllib.parse import parse_qs, urlparse
        mid = (parse_qs(urlparse(handler.path).query).get("miner") or [""])[0]
        if path == "/api/hardware/report":
            json_response(handler, 200, report_addon.status(mid))
            return True
        got = report_addon.zip_of(mid)
        if not got:
            json_response(handler, 404, {"ok": False, "error": "no report ready for this miner (make one first)"})
            return True
        tuner_addon._send_file(handler, got[1], got[0], "application/zip")
        return True
    return False


def handle_post(handler: Any, path: str, body: dict[str, Any], json_response: Callable) -> bool:
    fn = {"/api/fans/profile": save_profile, "/api/fans/profile_delete": delete_profile,
          "/api/presets/apply": apply_preset, "/api/presets/save": save_preset,
          "/api/presets/delete": delete_preset, "/api/hardware/probe": probe_now,
          "/api/hardware/report": report_addon.start,
          "/api/quick/demo": set_demo, "/api/hardware/shares_reset": shares_addon.reset, "/api/quick/manual": take_manual,
          "/api/fans/temp_source": set_temp_source, "/api/hardware/power_cal": calibrate_power,
          "/api/hardware/mains": set_mains, "/api/quick/restart": restart_miners, **schedule_addon.ROUTES_POST,
          "/api/settings/timezone": set_tz, "/api/settings/share_scale": set_scale,
          "/api/hardware/fan_target": set_fan_target, **notify_addon.ROUTES_POST}.get(path)
    if not fn:
        return False
    try:
        json_response(handler, 200, fn(body if isinstance(body, dict) else {}))
    except (ValueError, TypeError, KeyError) as e:
        json_response(handler, 400, {"ok": False, "error": str(e).strip("'")})
    except Exception as e:   # the miner didn't answer, etc.
        json_response(handler, 502, {"ok": False, "error": f"{type(e).__name__}: {e}"})
    return True


schedule_addon._srv = _srv
shares_addon._srv = _srv
shares_addon._running = lambda mid: bool(tuner_addon.running(mid))
schedule_addon._apply = apply_preset
schedule_addon._restart = restart_miners
schedule_addon._restart_s = expected_restart_s
schedule_addon._capture = capture_state
schedule_addon._restore = restore_state
schedule_addon._notify = notify_addon.schedule_problem
shares_addon._notify = notify_addon.best_share
shares_addon._observe = rejects_addon.observe
shares_addon._history = hashrate_addon.observe
rejects_addon._srv = _srv
rejects_addon._notify = notify_addon.reject_problem


def _reject_client(mid: str) -> Any:
    row = next((m for m in _srv().load_registry() if _row_id(m) == mid), None)
    if not row or not row.get("ip"):
        return None
    from miner_client import MinerClient
    return MinerClient(ip=str(row["ip"]), password=str(row.get("password") or ""))


rejects_addon._client = _reject_client


def _report_probe(mid: str) -> dict[str, Any]:
    with _probe_lock:
        if mid in _probing:
            raise ValueError("already probing this miner")
        _probing.add(mid)
    try:
        return probe_hardware(mid)
    finally:
        with _probe_lock:
            _probing.discard(mid)


def _report_busy(mid: str) -> str:
    if not any(_row_id(m) == mid for m in _srv().load_registry()):
        return f"unknown miner {mid}"
    if tuner_addon.running(mid):
        return "a tuning run is going on this miner; make the report when the run has ended"
    if mid in _probing:
        return "the miner is being probed; try again in a few seconds"
    if (_restarts.get(mid) or {}).get("state") == "restarting":
        return "the miner is restarting; try again when it's back"
    return ""


report_addon._probe = _report_probe
report_addon._client = _reject_client
report_addon._busy = _report_busy
notify_addon._srv = _srv
notify_addon._tuner_running = lambda mid: bool(tuner_addon.running(mid))
notify_addon._tuner_finished = lambda mid: str((tuner_addon.status(mid) or {}).get("finished_line") or "")
notify_addon._health = lambda mid: _health.get(mid)
notify_addon._restarting = lambda mid: (_restarts.get(mid) or {}).get("state") == "restarting"


def _tighten_permissions() -> None:
    """miners.json holds the miners' passwords: make it and the app's other data files owner-only
    (older versions wrote them readable by everyone)."""
    import os
    from pathlib import Path
    reg = Path(os.environ.get("SCLITE_WEBUI_MINERS", "miners.json"))
    for f in list(reg.parent.glob("*.json")) + [reg]:
        try:
            if f.is_file() and not f.is_symlink():      # never through a link: chmod follows it
                os.chmod(f, 0o600)
        except OSError:
            pass
    # only the tuner's own data folder, and only when it's set (not "." when it isn't), never through a link
    tdir = (os.environ.get("SCLITE_TUNER_DATA") or "").strip()
    if tdir and os.path.isdir(tdir) and not os.path.islink(tdir):
        try:
            os.chmod(tdir, 0o700)
        except OSError:
            pass          # (owned by another user, e.g. made as root by an older image): don't stop the app
        for root, dirs, files in os.walk(tdir, followlinks=False):
            for name in dirs + files:
                p = os.path.join(root, name)
                try:
                    if not os.path.islink(p):
                        os.chmod(p, 0o700 if os.path.isdir(p) else 0o600)
                except OSError:
                    pass


_tighten_permissions()
