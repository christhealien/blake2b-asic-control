"""Per-miner schedules: which preset each miner runs, by time of day and day of the week.

A schedule is a list of rules, "at HH:MM on these days, switch to this preset". At any moment
the rule that started most recently is the one in force (it wraps round the week). When a
new rule comes into force the preset is applied once (clock + voltage + PV and its fan curve),
so a preset you apply by hand in between stays on until the next scheduled change.

A miner's schedule waits while that miner is being tuned, and it is paused (not deleted) when
its clock or voltage is set by hand on the Miner page; Resume on the Schedule page (or the
Fleet quick bar) starts it again straight away.

Scheduled restarts (each miner on its own, separate from the preset changes): restart the miner
software every hour / 6 h / 12 h / day / week / month / 3 months at a set time, or at the half-hour
blocks you mark on the week. A restart that comes due while the miner is being tuned is skipped; one
that was missed while the app wasn't running is skipped too (only up to 10 minutes late counts).

Stored in miners.json under "schedules": {miner id: {enabled, rules, paused, last_slot, restart, ...}}.

Routes (behind the login, wired through fan_addon):
  GET  /api/schedule/state
  POST /api/schedule/save    {miner_id, enabled, rules: [{preset, start: "HH:MM", days: [0-6, 0 = Monday]}],
                              restart: {every, at: "HH:MM", day: 0-6, mday: 1-28, marks: [minute of the week]}, base}
  POST /api/schedule/pause   {miner_id, paused: true|false}
  POST /api/schedule/copy    {from, to: [miner ids]}
"""
from __future__ import annotations

import datetime as dt
import re
import threading
import datetime as _dt
import time
from typing import Any, Callable

import tuner_addon

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MAX_RULES = 28
WEEK_MIN = 7 * 1440
RETRY_S = 300            # a preset that couldn't be applied is tried again after this long

_srv: Callable[[], Any] = lambda: None          # set by fan_addon
_apply: Callable[[dict], dict] = lambda b: {}   # fan_addon.apply_preset
_restart: Callable[[dict], dict] = lambda b: {}  # fan_addon.restart_miners
_capture: Callable[[str], dict] = lambda mid: {}             # fan_addon.capture_state: what the miner runs now
_restore: Callable[[str, dict], dict] = lambda mid, h: {}   # fan_addon.restore_state: put that back
BACK = "__back"          # a rule that ends a painted stretch: back to what the miner ran before it
_restart_s: Callable[[str], float] = lambda mid: 75.0
_notify: Callable[[str, str], None] = lambda mid, text: None   # notify_addon.schedule_problem   # fan_addon.expected_restart_s: how long this miner's restart takes
_busy: set[str] = set()
_lock = threading.Lock()
_last_tick = [0.0]


def _row_id(m: dict[str, Any]) -> str:
    return str(m.get("id") or m.get("ip") or "")


def _hm(s: str) -> int:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(s or "").strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(f"{s!r} isn't a time (use HH:MM, 00:00-23:59)")
    return int(m.group(1)) * 60 + int(m.group(2))


def _occurrences(rules: list[dict[str, Any]]) -> list[tuple[int, int]]:
    """(minute of the week, rule index), sorted. Monday 00:00 = 0."""
    out = []
    for i, r in enumerate(rules):
        start = _hm(r.get("start"))
        for d in sorted(set(int(x) for x in r.get("days") or [])):
            out.append((d * 1440 + start, i))
    return sorted(out)


def _now_mow(t: float | None = None) -> tuple[int, float]:
    lt = time.localtime(t if t is not None else time.time())
    mow = lt.tm_wday * 1440 + lt.tm_hour * 60 + lt.tm_min
    return mow, (t if t is not None else time.time())


def current(rules: list[dict[str, Any]], t: float | None = None) -> dict[str, Any] | None:
    """The rule in force now, when it started, and the next change."""
    occ = _occurrences(rules)
    if not occ:
        return None
    mow, now = _now_mow(t)
    past = [o for o in occ if o[0] <= mow]
    cur = past[-1] if past else occ[-1]                     # wrap: last one of the week
    ago = (mow - cur[0]) % WEEK_MIN
    # when this occurrence started, counted in local wall-clock time: across a daylight-saving change a
    # plain "now minus N minutes" would move the start (and the slot id) by an hour, and the rule in force
    # would be applied again, over a preset you applied by hand
    start_ts = _wall(now, -ago)
    fut = [o for o in occ if o[0] > mow]
    nxt = fut[0] if fut else occ[0]
    ahead = (nxt[0] - mow) % WEEK_MIN or WEEK_MIN
    return {"rule": rules[cur[1]], "index": cur[1], "slot": start_ts // 60,
            "since": start_ts, "next_rule": rules[nxt[1]], "next_at": _wall(now, ahead)}


def _same_or_back(last_slot: float, last_index: Any, c: dict[str, Any]) -> bool:
    """True when the "new" occurrence is only a daylight-saving shift: the same rule an hour off, or an
    earlier one coming back in the repeated hour when clocks go back. Then nothing is applied again.
    (Saving a schedule or changing the time zone clears last_slot, so this never blocks those.)"""
    d = c["slot"] - last_slot
    if last_index is not None and c["index"] == last_index and abs(d) <= 60:
        return True
    return d < 0       # an occurrence older than the one applied last: the clock went back, not forward


def _wall(now: float, minutes: int) -> int:
    """The time `minutes` of local wall-clock time from now (to the minute), as a Unix time."""
    lt = time.localtime(now)
    base = _dt.datetime(lt.tm_year, lt.tm_mon, lt.tm_mday, lt.tm_hour, lt.tm_min)
    t = (base + _dt.timedelta(minutes=minutes)).timetuple()
    return int(time.mktime((t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, 0, 0, 0, -1)))


def validate(rules_in: Any, presets: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(rules_in, list):
        raise ValueError("rules must be a list")
    if len(rules_in) > MAX_RULES:
        raise ValueError(f"up to {MAX_RULES} rules")
    rules, seen = [], {}
    for r in rules_in:
        key = str((r or {}).get("preset") or "")
        if key == BACK:
            pass
        elif key not in presets:
            raise ValueError(f"this miner has no preset called {key or '(none)'}")
        if key != BACK and presets[key].get("ok") is False:
            raise ValueError(f"the preset {presets[key].get('label') or key} didn't pass its test, so it can't be scheduled")
        start = r.get("start")
        m = _hm(start)
        days = sorted(set(int(d) for d in (r.get("days") or []) if 0 <= int(d) <= 6))
        if not days:
            raise ValueError(f"the {start} rule needs at least one day")
        for d in days:
            if (d, m) in seen:
                raise ValueError(f"two rules start at {start} on {DAY_NAMES[d]}")
            seen[(d, m)] = True
        rules.append({"preset": key, "start": f"{m // 60:02d}:{m % 60:02d}", "days": days})
    return rules


# ---------------------------------------------------------------- scheduled restarts

EVERY = {"off": "Off", "hour": "Every hour", "6h": "Every 6 hours", "12h": "Every 12 hours", "day": "Every day",
         "week": "Every week", "month": "Every month", "3months": "Every 3 months"}
MAX_MARKS = 7 * 24        # marked restarts, at most one an hour
MIN_GAP_MIN = 60          # marked restarts at least an hour apart (a restart stops hashing for a minute or two)
LATE_S = 600              # a restart up to 10 minutes late still happens (the app checks every 30 s)


def _on(cfg: dict[str, Any] | None) -> bool:
    return bool(cfg) and (cfg.get("every") not in (None, "off") or bool(cfg.get("marks")))


def validate_restart(r: Any, now: float | None = None) -> dict[str, Any]:
    """A repeat (every hour ... every 3 months, or off) plus any restarts marked on the week (minute of the
    week, each repeating weekly). Both can be used together; their times are added up."""
    r = r if isinstance(r, dict) else {}
    every = str(r.get("every") or "off")
    if every == "marks":                     # 1.11.0: "at the blocks I mark" was one of the repeats
        every = "off"
    if every not in EVERY:
        raise ValueError(f"restart: {every!r} isn't one of {', '.join(EVERY)}")
    out: dict[str, Any] = {"every": every}
    marks = sorted(set(int(m) for m in (r.get("marks") or []) if 0 <= int(m) < WEEK_MIN and int(m) % 30 == 0))
    if len(marks) > MAX_MARKS:
        raise ValueError(f"restart: up to {MAX_MARKS} marked restarts a week")
    if len(marks) > 1:
        for a, b in zip(marks, marks[1:] + [marks[0] + WEEK_MIN]):
            if b - a < MIN_GAP_MIN:
                raise ValueError(f"restart: marked restarts must be at least {MIN_GAP_MIN // 60} hour apart "
                                 f"({DAY_NAMES[a // 1440]} {a % 1440 // 60:02d}:{a % 60:02d} is too close to the next one)")
    if marks:
        out["marks"] = marks
    if every == "off":
        return out
    m = _hm(r.get("at") or "04:00")
    out["at"] = f"{m // 60:02d}:{m % 60:02d}"
    if every == "week":
        d = int(r.get("day") if r.get("day") is not None else 0)
        if not 0 <= d <= 6:
            raise ValueError("restart: pick a day of the week")
        out["day"] = d
    if every in ("month", "3months"):
        md = int(r.get("mday") or 1)
        if not 1 <= md <= 28:
            raise ValueError("restart: the day of the month must be 1-28 (so it's in every month)")
        out["mday"] = md
    if every == "3months":
        # which months: the first one from now (then every third month after it)
        now_d = dt.datetime.fromtimestamp(now if now is not None else time.time())
        anchor = now_d.year * 12 + now_d.month - 1
        first = now_d.replace(day=out["mday"], hour=m // 60, minute=m % 60, second=0, microsecond=0)
        if first <= now_d:
            anchor += 1
        out["anchor"] = int(r.get("anchor")) if isinstance(r.get("anchor"), int) else anchor
    return out


def _restart_times_on(cfg: dict[str, Any], day: dt.date) -> list[dt.datetime]:
    wd = day.weekday()
    out = [dt.datetime.combine(day, dt.time((m % 1440) // 60, m % 60)) for m in cfg.get("marks") or [] if m // 1440 == wd]
    every = cfg.get("every")
    if every in (None, "off", "marks"):
        return out
    try:
        hh, mm = divmod(_hm(cfg.get("at") or "04:00"), 60)
    except ValueError:
        return out
    at = lambda h: dt.datetime.combine(day, dt.time(h, mm))
    if every == "hour":
        out += [at(h) for h in range(24)]
    elif every in ("6h", "12h"):
        step = 6 if every == "6h" else 12
        out += [at(h) for h in range(24) if (h - hh) % step == 0]
    elif every == "day":
        out.append(at(hh))
    elif every == "week":
        if wd == int(cfg.get("day") or 0):
            out.append(at(hh))
    elif every in ("month", "3months"):
        if day.day == int(cfg.get("mday") or 1) and not (
                every == "3months" and (day.year * 12 + day.month - 1 - int(cfg.get("anchor") or 0)) % 3):
            out.append(at(hh))
    return out


def restart_times(cfg: dict[str, Any] | None, t0: float, t1: float) -> list[float]:
    """Every scheduled restart between t0 and t1 (timestamps, local time rules); a repeat and a mark at the
    same minute are one restart."""
    if not _on(cfg):
        return []
    d0, d1 = dt.date.fromtimestamp(t0), dt.date.fromtimestamp(t1)
    out, d = set(), d0
    while d <= d1:
        out.update(x.timestamp() for x in _restart_times_on(cfg, d))
        d += dt.timedelta(days=1)
    return sorted(x for x in out if t0 <= x <= t1)


def next_restart(cfg: dict[str, Any] | None, now: float | None = None) -> float | None:
    now = time.time() if now is None else now
    for days in (2, 8, 40, 100):
        nx = restart_times(cfg, now + 1, now + days * 86400)
        if nx:
            return nx[0]
    return None


def restart_line(cfg: dict[str, Any] | None) -> str:
    """'every day at 04:00 + 2 marked a week' and the like."""
    if not _on(cfg):
        return ""
    e, at = cfg.get("every"), cfg.get("at") or ""
    rep = {"hour": f"every hour at :{at[3:]}", "6h": f"every 6 hours from {at}", "12h": f"every 12 hours from {at}",
           "day": f"every day at {at}", "week": f"every {DAY_NAMES[int(cfg.get('day') or 0)]} at {at}",
           "month": f"on the {cfg.get('mday')}{_th(cfg.get('mday'))} of every month at {at}",
           "3months": f"on the {cfg.get('mday')}{_th(cfg.get('mday'))} every 3 months at {at}"}.get(e, "")
    n = len(cfg.get("marks") or [])
    mk = f"{n} marked on the week" if n else ""
    return " + ".join(x for x in (rep, mk) if x)


def _th(n: Any) -> str:
    n = int(n or 0)
    return "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def _restart_tick(doc: dict[str, Any], now: float) -> set[str]:
    """Start the restarts that are due. Returns the miners whose restart state changed."""
    dirty: set[str] = set()
    for mid, sch in list((doc.get("schedules") or {}).items()):
        cfg = (sch or {}).get("restart")
        if not _on(cfg):
            continue
        due = [t for t in restart_times(cfg, now - LATE_S, now) if t > float(cfg.get("last") or 0)]
        if not due:
            continue
        cfg["last"] = due[-1]
        dirty.add(mid)
        if tuner_addon.running(mid):
            cfg["last_result"] = {"at": int(now), "ok": False, "msg": "skipped: the miner was being tuned"}
            continue
        try:
            r = _restart({"ids": [mid]})
            ok = mid in (r.get("started") or [])
            cfg["last_result"] = {"at": int(now), "ok": ok,
                                  "msg": "restarted" if ok else "skipped: " + str((r.get("skipped") or {}).get(mid) or r.get("error") or "")}
        except Exception as e:
            cfg["last_result"] = {"at": int(now), "ok": False, "msg": f"failed: {str(e)[:120]}"}
        print(f"[schedule] {mid}: scheduled restart {cfg['last_result']['msg']}", flush=True)
        if not cfg["last_result"]["ok"]:
            try:
                _notify(mid, f"scheduled restart {cfg['last_result']['msg']}")
            except Exception:
                pass
    return dirty


def _presets_of(doc: dict[str, Any], mid: str) -> dict[str, Any]:
    return (doc.get("presets") or {}).get(mid) or {}


def summary(doc: dict[str, Any], mid: str) -> dict[str, Any]:
    sch = (doc.get("schedules") or {}).get(mid) or {}
    presets = _presets_of(doc, mid)
    out: dict[str, Any] = {"enabled": bool(sch.get("enabled")), "paused": sch.get("paused"),
                           "rules": sch.get("rules") or [], "error": sch.get("error"),
                           "last_applied": sch.get("last_applied")}
    out["base"] = sch.get("base")
    rcfg = sch.get("restart") or {"every": "off"}
    out["restart"] = {k: v for k, v in rcfg.items() if k != "last"}
    out["restart_line"] = restart_line(rcfg)
    out["restart_next"] = next_restart(rcfg)
    out["restart_last"] = (rcfg.get("last_result") or None)
    try:
        c = current(sch.get("rules") or [])
    except ValueError:
        c = None
    if c:
        lbl = lambda k: "back to what it ran before" if k == BACK else (presets.get(k) or {}).get("label") or k
        out.update(now=lbl(c["rule"]["preset"]), now_key=c["rule"]["preset"], since=c["since"],
                   next=lbl(c["next_rule"]["preset"]), next_key=c["next_rule"]["preset"], next_at=c["next_at"])
    return out


def short_line(doc: dict[str, Any], mid: str) -> str:
    s = summary(doc, mid)
    rs = f"restart {time.strftime('%a %H:%M', time.localtime(s['restart_next']))}" if s.get("restart_next") else ""
    line = _preset_line(s)
    return " · ".join(x for x in (line, rs) if x)


def _preset_line(s: dict[str, Any]) -> str:
    if not s["rules"]:
        return ""
    if not s["enabled"]:
        return "schedule off"
    if s["paused"]:
        return "schedule paused"
    if s.get("next"):
        return f"schedule: {s['next']} at {time.strftime('%a %H:%M', time.localtime(s['next_at']))}"
    return "schedule on"


# ---------------------------------------------------------------- engine

def tick() -> None:
    """Called about every 5 s from the fan loop; does its work every 30 s."""
    now = time.time()
    if now - _last_tick[0] < 30:
        return
    _last_tick[0] = now
    srv = _srv()
    doc = srv.load_registry_doc()
    try:
        touched = _restart_tick(doc, now)
        if touched:
            # the restarts can take a while: save only what they changed (when each last ran, and how it went)
            # onto a fresh copy, so anything saved meanwhile (a preset change, new restart times) isn't lost
            fresh = srv.load_registry_doc()
            for mid in touched:
                done = ((doc.get("schedules") or {}).get(mid) or {}).get("restart") or {}
                tgt = ((fresh.get("schedules") or {}).get(mid) or {}).get("restart")
                if isinstance(tgt, dict) and _on(tgt):
                    tgt["last"] = max(float(tgt.get("last") or 0), float(done.get("last") or 0))
                    if done.get("last_result"):
                        tgt["last_result"] = done["last_result"]
            srv.save_registry_doc(fresh)
            doc = fresh
    except Exception as e:
        print(f"[schedule] restarts: {e}", flush=True)
    for mid, sch in list((doc.get("schedules") or {}).items()):
        if not sch.get("enabled") or sch.get("paused") or not sch.get("rules"):
            continue
        if mid in _busy or tuner_addon.running(mid):
            continue
        try:
            c = current(sch["rules"])
        except ValueError:
            continue
        if not c or sch.get("last_slot") == c["slot"]:
            continue
        last = sch.get("last_slot")
        if isinstance(last, (int, float)) and _same_or_back(last, sch.get("last_index"), c):
            continue
        if sch.get("retry_after", 0) > now:
            continue
        key = c["rule"]["preset"]
        has_back = any(r.get("preset") == BACK for r in sch["rules"])
        held = sch.get("held")

        def run(mid: str = mid, key: str = key, slot: int = c["slot"], has_back: bool = has_back,
                held: dict | None = held, index: int = c["index"], rules0: list = list(sch["rules"])) -> None:
            err, new_held, keep = None, held, True
            try:
                if key == BACK:
                    # the end of a painted stretch: back to what the miner ran before it (if anything was saved)
                    if held:
                        _restore(mid, held)
                    new_held, keep = None, False
                else:
                    if has_back and not held:
                        new_held = _capture(mid)      # the start of a painted stretch: remember what it runs now
                    _apply({"miner_id": mid, "key": key, "_by": "schedule"})
            except Exception as e:
                err = str(e).strip("'")[:160]
            d = srv.load_registry_doc()
            s = (d.get("schedules") or {}).get(mid)
            if s is not None and s.get("rules") != rules0:
                # the schedule was saved anew meanwhile: its rule in force is applied on the next tick, but
                # what the miner ran before this stretch (if just captured) still belongs to it
                if key != BACK and new_held and not s.get("held"):
                    s["held"] = new_held
                    srv.save_registry_doc(d)
                elif key == BACK and held and not err and s.get("held") == held:
                    s.pop("held", None)    # it was just put back: don't let the new schedule put it back again
                    srv.save_registry_doc(d)
                s = None
            if s is not None:
                if err:
                    if not s.get("error"):
                        try:
                            _notify(mid, f"couldn't switch to {key if key != BACK else 'what it ran before'}: {err}")
                        except Exception:
                            pass
                    s["error"] = err
                    s["retry_after"] = time.time() + RETRY_S
                    if key != BACK and new_held and not held:
                        s["held"] = new_held          # keep what it ran before even if this change failed
                else:
                    s.update(last_slot=slot, last_index=index, error=None, retry_after=0,
                             last_applied={"preset": key, "at": int(time.time())})
                    if keep:
                        s["held"] = new_held
                    else:
                        s.pop("held", None)
                srv.save_registry_doc(d)
            print(f"[schedule] {mid}: {key} " + (f"failed: {err}" if err else "applied"), flush=True)
            with _lock:
                _busy.discard(mid)

        with _lock:
            _busy.add(mid)
        threading.Thread(target=run, daemon=True, name=f"schedule-{mid}").start()


def in_back_time(sch: dict[str, Any] | None) -> bool:
    """True while an unpainted stretch is in force (the rest of the time: nothing): the schedule isn't in
    charge of the miner then, so hand changes are free."""
    if not sch or not sch.get("rules"):
        return False
    try:
        c = current(sch["rules"])
    except ValueError:
        return False
    return bool(c) and c["rule"].get("preset") == BACK


def pause_for_hand_clock(doc: dict[str, Any], mid: str) -> bool:
    """Clock/voltage set by hand: pause the schedule (kept, not deleted). Returns True if paused."""
    sch = (doc.get("schedules") or {}).get(mid)
    if sch and sch.get("enabled") and not sch.get("paused"):
        sch["paused"] = {"reason": "clock or voltage set by hand", "at": int(time.time())}
        return True
    return False


# ---------------------------------------------------------------- routes

def state() -> dict[str, Any]:
    srv = _srv()
    doc = srv.load_registry_doc()
    miners = []
    for m in doc.get("miners") or []:
        if m.get("demo") or not m.get("ip"):
            continue
        mid = _row_id(m)
        presets = _presets_of(doc, mid)
        miners.append({
            "id": mid, "name": m.get("name") or mid, "ip": m.get("ip"),
            "presets": {k: {"label": p.get("label") or k, "ok": p.get("ok", True), "mhz": p.get("mhz"),
                            "mv": p.get("mv"), "pv": p.get("pv"), "source": p.get("source"),
                            "fan_profile": p.get("fan_profile"), "idle": bool(p.get("idle")), "box": bool(p.get("box"))} for k, p in presets.items()},
            "active_preset": m.get("active_preset"),
            "tuning": bool(tuner_addon.running(mid)),
            "restart_s": round(float(_restart_s(mid) or 75)),
            "schedule": summary(doc, mid),
        })
    return {"ok": True, "miners": miners, "now": int(time.time()),
            "timezone": time.strftime("%Z"), "utc_offset": time.strftime("%z"), "days": DAY_NAMES}


def save(body: dict[str, Any]) -> dict[str, Any]:
    srv = _srv()
    mid = str(body.get("miner_id") or "")
    doc = srv.load_registry_doc()
    if not any(_row_id(m) == mid for m in doc.get("miners") or []):
        raise ValueError(f"unknown miner {mid}")
    rules = validate(body.get("rules"), _presets_of(doc, mid))
    enabled = bool(body.get("enabled")) and bool(rules)
    old = (doc.get("schedules") or {}).get(mid) or {}
    oldr = old.get("restart") or {}
    restart = validate_restart(body.get("restart") if "restart" in body else oldr)
    if _on(restart):
        restart["last"] = max(time.time(), float(oldr.get("last") or 0))   # nothing in the past comes due on saving
        if oldr.get("last_result"):
            restart["last_result"] = oldr["last_result"]
    base = body.get("base") if "base" in body else old.get("base")
    base = str(base) if base and (str(base) in _presets_of(doc, mid) or str(base) == BACK) else None   # what the unpainted blocks run
    new = {"enabled": enabled, "rules": rules, "paused": old.get("paused") if enabled else None,
           "last_slot": None, "error": None, "retry_after": 0, "last_applied": old.get("last_applied"),
           "restart": restart, "base": base}
    if old.get("held") and any(r.get("preset") == BACK for r in rules):
        new["held"] = old["held"]    # what the miner ran before a painted stretch it's in now: still goes back
    doc.setdefault("schedules", {})[mid] = new     # last_slot reset: the rule in force is applied next tick
    srv.save_registry_doc(doc)
    _last_tick[0] = 0
    return {"ok": True, "schedule": summary(doc, mid)}


def pause(body: dict[str, Any]) -> dict[str, Any]:
    srv = _srv()
    mid = str(body.get("miner_id") or "")
    doc = srv.load_registry_doc()
    sch = (doc.get("schedules") or {}).get(mid)
    if not sch or not sch.get("rules"):
        raise ValueError("this miner has no schedule yet; make one on the Schedule page")
    if body.get("paused"):
        sch["paused"] = {"reason": "paused by you", "at": int(time.time())}
    else:
        sch.update(paused=None, enabled=True, last_slot=None, error=None, retry_after=0)
    srv.save_registry_doc(doc)
    _last_tick[0] = 0
    return {"ok": True, "schedule": summary(doc, mid)}


def copy(body: dict[str, Any]) -> dict[str, Any]:
    srv = _srv()
    src = str(body.get("from") or "")
    doc = srv.load_registry_doc()
    sch = (doc.get("schedules") or {}).get(src)
    if not sch or not (sch.get("rules") or _on(sch.get("restart"))):
        raise ValueError("that miner has no schedule to copy")
    done, skipped = [], {}
    for mid in body.get("to") or []:
        mid = str(mid)
        if mid == src or not any(_row_id(m) == mid for m in doc.get("miners") or []):
            continue
        try:
            rules = validate(sch["rules"], _presets_of(doc, mid))
        except ValueError as e:
            skipped[mid] = str(e)
            continue
        rs = {k: v for k, v in (sch.get("restart") or {"every": "off"}).items() if k != "last_result"}
        if _on(rs):
            rs["last"] = time.time()
        mine = (doc.get("schedules") or {}).get(mid) or {}
        doc.setdefault("schedules", {})[mid] = {"enabled": bool(sch.get("enabled")), "rules": rules, "paused": None,
                                                "last_slot": None, "error": None, "retry_after": 0, "restart": rs,
                                                "base": sch.get("base") if (sch.get("base") in _presets_of(doc, mid) or sch.get("base") == BACK) else None}
        if mine.get("held") and any(r.get("preset") == BACK for r in rules):   # this miner's own "what it ran
            doc["schedules"][mid]["held"] = mine["held"]                         # before", if it's mid-stretch
        done.append(mid)
    srv.save_registry_doc(doc)
    _last_tick[0] = 0
    return {"ok": True, "copied": done, "skipped": skipped}


ROUTES_POST = {"/api/schedule/save": save, "/api/schedule/pause": pause, "/api/schedule/copy": copy}
