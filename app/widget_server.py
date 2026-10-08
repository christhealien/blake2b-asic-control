#!/usr/bin/env python3
"""Umbrel home-screen widgets for Blake2b ASIC Control.

Umbrel reads these from inside the app's network (web:8788); the port is not
published through the app's web address, so it needs no login.

  GET /widgets/overview   four-stats: total hashrate, miners online (the tuning step instead while a run
                          is on), hottest board, best share since restart. Each value is a number and a
                          short unit only: Umbrel puts them on one line and cuts off whatever doesn't fit.
  GET /widgets/miners     list: per miner a short top line (name, status, preset) and the numbers on the
                          bright line below, which may wrap onto two lines (hashrate, hottest, best share)
  GET /widgets/health     two gauges: hashrate against its own 24-hour average (full = mining as usual),
                          hottest board against the heat alert (Settings -> Notifications; 80 C by default)

Data comes from each miner's port-4028 status API (light, no password), refreshed in
the background every SCLITE_WIDGET_POLL_S seconds (default 30), plus the clock / voltage /
PV setting from the web API every SCLITE_WIDGET_PLAN_S seconds (default 120; while a miner
is being tuned it comes from the run's files instead). Requests are answered from that
cache so a slow or offline miner never delays the home screen.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from miner_client import MinerClient, parse_plan  # noqa: E402
import miner_safety  # noqa: E402  (bounded reads from miners)
miner_safety.install(MinerClient)
import tuner_addon  # noqa: E402  (reads each miner's tuning-run files; no miner traffic)
import shares_addon  # noqa: E402  (best shares: fmt only; the live value comes from the summary below)

REGISTRY = Path(os.environ.get("SCLITE_WEBUI_MINERS", str(HERE / "miners.json")))
PORT = int(os.environ.get("SCLITE_WIDGET_PORT", "8788"))
POLL_S = max(10.0, float(os.environ.get("SCLITE_WIDGET_POLL_S", "30")))
# umbrelOS reads "refresh" from every widget response (it converts it with ms() and
# errors if it is missing, which shows the widget as dashes), so always send it.
REFRESH = os.environ.get("SCLITE_WIDGET_REFRESH", "30s")

_cache: dict[str, Any] = {"miners": [], "updated": 0.0}
_lock = threading.Lock()


_settings: dict[str, Any] = {"hot_c": 80.0}


def _registry() -> list[dict[str, Any]]:
    try:
        doc = json.loads(REGISTRY.read_text())
    except (FileNotFoundError, ValueError):
        return []
    shares_addon.set_scale(str(doc.get("share_scale") or "hashes"))     # Settings -> Best share numbers
    try:
        _settings["hot_c"] = float(((doc.get("notify") or {}).get("hot_c")) or 80)
    except (TypeError, ValueError):
        _settings["hot_c"] = 80.0
    try:
        _settings["mains_v"] = float(((doc.get("power") or {}).get("mains_v")) or 110)
    except (TypeError, ValueError):
        _settings["mains_v"] = 110.0
    rows = [dict(r) for r in doc.get("miners") or [] if r.get("ip") and (r.get("password") or "") != "CHANGE_ME"]
    for r in rows:   # the preset the miner was last put on, if any
        mid = str(r.get("id") or r.get("ip"))
        r["_mid"] = mid
        r["_preset"] = ((doc.get("presets") or {}).get(mid) or {}).get(r.get("active_preset") or "")
    return rows


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


PLAN_S = max(60.0, float(os.environ.get("SCLITE_WIDGET_PLAN_S", "120")))
_plans: dict[str, tuple[float, tuple[int, int, int] | None]] = {}   # mid -> (read at, (MHz, mV, PV))
_clients: dict[tuple[str, str, str], MinerClient] = {}
_seen: dict[str, Any] = {}


def _plan(row: dict[str, Any]) -> tuple[int, int, int] | None:
    """MHz / mV / PV from the miner's settings (web API, needs the password): read every PLAN_S."""
    mid = row["_mid"]
    ts, val = _plans.get(mid, (0.0, None))
    seen = (row.get("active_preset"), (row.get("_preset") or {}).get("mhz"))
    if time.time() - ts < PLAN_S and _seen.get(mid) == seen:
        return val            # (a newly applied preset triggers a fresh read)
    _seen[mid] = seen
    try:
        key = (mid, str(row["ip"]), str(row.get("password") or ""))
        c = _clients.get(key)
        if c is None:   # keep the client so its login token is reused
            c = _clients[key] = MinerClient(ip=key[1], password=key[2], name=mid, id=mid)
        s = c.api("GET", "/mcb/setting")
        m, v, _a, _b, pv = parse_plan(str((s or {}).get("manualPowerplan") or ""))
        val = (m, v, pv)
    except Exception:
        pass   # keep the last known value
    _plans[mid] = (time.time(), val)
    return val


PHASES = {"baseline": "baseline", "test": "testing", "preset high": "preset High", "preset middle": "preset Middle",
          "preset low": "preset Low", "preset lowest": "preset Lowest power"}


def _tuning(mid: str) -> dict[str, Any] | None:
    """What this miner's tuning run is doing, or None (reads the run's files only)."""
    try:
        if not tuner_addon.running(mid):
            return None
        st = tuner_addon.status(mid)
    except Exception:
        return None
    step = st.get("step") or []
    if not step:
        return {"what": "starting"}
    last = step[-1]
    ph = PHASES.get(str(last.get("phase") or ""), str(last.get("phase") or ""))
    try:
        plan = (int(last["mhz"]), int(last["mv"]), int(last["pv"]))
    except (KeyError, TypeError, ValueError):
        plan = None
    what = f"testing {last.get('mhz')} MHz" if ph == "testing" else ph
    return {"what": what, "plan": plan}


def _watts(row: dict[str, Any], plan: tuple[int, int, int] | None) -> float | None:
    """Wall power estimate for one hashing miner, the same sum as the Fleet card's (fan_addon.estimate_watts:
    the chips scale with clock x PV^2 from the model's rated watts at its stock plan, about 7 % doesn't, and the
    wall-reading calibration applies). Copied rather than imported: importing fan_addon patches the dashboard's
    fan controller. None when the model's rated power or stock plan isn't known."""
    hw = row.get("hardware") or {}
    stock = hw.get("stock") or {}
    rated = _num(hw.get("rated_watts"))
    if not rated:
        return None
    cal = _num((row.get("power_cal") or {}).get("factor")) or 1.0
    s_mhz, s_pv = _num(stock.get("mhz")), _num(stock.get("pv"))
    if not s_mhz or not s_pv:
        # no stock plan on record: a read-only model (SC Box, HS Box) runs its firmware's own setting, so its
        # rated power is the estimate; anything else is left out
        return rated * cal if (hw.get("plan_format") or "sc-lite") != "sc-lite" else None
    mhz, pv = (plan[0], plan[2]) if plan and plan[0] and plan[2] else (s_mhz, s_pv)
    eff = int(mhz / 12.5) * 12.5
    return rated * cal * (0.07 + 0.93 * (eff / s_mhz) * (pv / s_pv) ** 2)


def _power_plan(row: dict[str, Any], tune_plan: tuple[int, int, int] | None) -> tuple[int, int, int] | None:
    """The clock / PV the estimate uses: the tuning run's step, else the miner's own setting (SC Lite: read over
    its web API every PLAN_S), else its preset, else None (stock)."""
    if tune_plan:
        return tune_plan
    if ((row.get("hardware") or {}).get("plan_format") or "sc-lite") == "sc-lite":
        got = _plan(row)
        if got:
            return got
    p = row.get("_preset") or {}
    try:
        return (int(p["mhz"]), int(p["mv"]), int(p["pv"]))
    except (KeyError, TypeError, ValueError):
        return None   # SC Box / HS Box are read only here, so they run their stock plan


def _read(row: dict[str, Any]) -> dict[str, Any]:
    name = row.get("name") or row.get("id") or row.get("ip")
    mid = row["_mid"]
    out: dict[str, Any] = {"name": name, "ip": row.get("ip"), "mid": mid, "online": False}
    c = MinerClient(ip=str(row["ip"]), password="", name=str(name), id=mid)
    try:
        summary = (c.bfg("summary").get("SUMMARY") or [{}])[0]
        devs = list(c.bfg("devs").get("DEVS") or [])
    except Exception as e:  # offline or 4028 closed
        out["error"] = str(e)[:80]
        out["status"], out["icon"] = "Offline", "🔴"
        return out
    mhs = _num(summary.get("MHS 20s")) or _num(summary.get("MHS 5s")) or _num(summary.get("MHS av")) or 0.0
    temps, clocks = [], []
    for d in devs:
        for k, v in d.items():
            if (str(k).startswith("tstemp-") or k == "Temperature") and _num(v):
                temps.append(float(v))
        if _num(d.get("clock")):
            clocks.append(float(d["clock"]))
    out["best"] = _num(summary.get("Best Share")) or 0.0   # since the miner last restarted
    out.update(online=True, ths=mhs / 1e6, boards=len(devs),
               max_t=max(temps) if temps else None,
               clock=round(max(clocks)) if clocks else None)

    tune = _tuning(mid)
    plan = (tune or {}).get("plan")   # the tuning step's plan; the power estimate reads the miner's own otherwise
    out["plan"] = plan
    if tune:
        out["status"], out["icon"] = f"Tuning · {tune['what']}", "🔧"
    elif out["ths"] < 0.05:
        out["status"], out["icon"] = "Not hashing", "🟠"
    elif out["max_t"] is not None and out["max_t"] >= HOT_C:
        out["status"], out["icon"] = f"Hot · {out['max_t']:.0f} °C", "🟠"
    else:
        out["status"], out["icon"] = "Hashing", "🟢"
    # power only while it hashes (an idle miner draws little, and the card shows no estimate for it either)
    out["watts"] = _watts(row, _power_plan(row, plan)) if out["ths"] >= 0.05 else 0.0

    # the profile it's running: a preset only while the miner is still on that preset's clock
    p = row.get("_preset") or None
    if tune:
        out["profile"] = None
    elif p and (plan is None or (plan[0], plan[1]) == (p.get("mhz"), p.get("mv"))):
        out["profile"] = f"{p.get('label')} ({'tuned' if p.get('source') == 'tuner' and p.get('ok') else 'hand-set'})"
    elif plan:
        out["profile"] = "custom clock" if p else "no preset"
    else:
        out["profile"] = None
    return out


HOT_C = float(os.environ.get("SCLITE_WIDGET_HOT_C", "85"))


def _poll_forever() -> None:
    pool = ThreadPoolExecutor(max_workers=8)
    while True:
        rows = _registry()
        miners = list(pool.map(_read, rows)) if rows else []
        with _lock:
            _cache["miners"] = miners
            _cache["updated"] = time.time()
        time.sleep(POLL_S)


def _tuner() -> tuple[str, str]:
    """Short tuner state for the overview tile: (text, subtext)."""
    try:
        live = [k for k, r in tuner_addon.runs().items() if r["running"]]
    except Exception:
        return "–", "tuner"
    if len(live) > 1:
        return str(len(live)), "miners tuning"
    if live:
        t = _tuning(live[0]) or {}
        plan = t.get("plan")
        return (str(plan[0]) if plan else "On"), ("MHz · " + t.get("what", "") if plan else "tuner starting")
    return "Off", "not tuning"


def _usual_ths(mids: list[str]) -> float | None:
    """The fleet's usual hashrate: the sum of each miner's own 24-hour average (hashrate.json, which the app
    keeps next to miners.json), or None while there's no history yet."""
    try:
        doc = json.loads(REGISTRY.with_name("hashrate.json").read_text())
    except (FileNotFoundError, ValueError, OSError):
        return None
    now, total, any_ = time.time(), 0.0, False
    for mid in mids:
        pts = [p for p in (doc.get(mid) or {}).get("points") or [] if p[0] > now - 86400 and p[2]]
        if len(pts) >= 6:                               # at least half an hour of points
            total += sum(p[1] / p[2] for p in pts) / len(pts) / 1e6
            any_ = True
    return total if any_ else None


def _ths(v: float) -> tuple[str, str]:
    """A hashrate as (number, unit) short enough for a widget: 4.73 TH/s, 546 GH/s."""
    return (f"{v:.2f}", "TH/s") if v >= 1 else (f"{v * 1000:.0f}", "GH/s")


def _power_tile(online: list[dict[str, Any]]) -> dict[str, Any]:
    """The whole fleet's estimated wall power. A '+' after the number means some hashing miners aren't counted
    (their model's rated power isn't known), so the real total is higher."""
    hashing = [m for m in online if (m.get("ths") or 0) >= 0.05]
    known = [m for m in hashing if m.get("watts") is not None]
    if not known:
        return {"title": "Est. power", "text": "–", "subtext": ""}
    w = sum(m["watts"] for m in known)
    text, sub = (f"{w / 1000:.2f}", "kW") if w >= 1000 else (f"{w:.0f}", "W")
    return {"title": "Est. power", "text": text + ("+" if len(known) < len(hashing) else ""), "subtext": sub}


def overview() -> dict[str, Any]:
    with _lock:
        miners = list(_cache["miners"])
    online = [m for m in miners if m.get("online")]
    ths = sum(m.get("ths") or 0 for m in online)
    top = max((m for m in online if m.get("best")), key=lambda m: m["best"], default=None)
    num, unit = _ths(ths)
    power = _power_tile(online)
    if not miners:
        second = {"title": "Miners", "text": "0", "subtext": "add one"}
    elif any(m.get("icon") == "🔧" for m in online):
        t_text, t_sub = _tuner()
        second = {"title": "Tuning", "text": t_text, "subtext": t_sub.split(" · ")[0]}
    else:
        down = len(miners) - len(online)
        second = {"title": "Online", "text": f"{len(online)}/{len(miners)}", "subtext": f"{down} down" if down else "miners"}
    return {
        "type": "four-stats",
        "refresh": REFRESH,
        "link": "",
        "items": [
            {"title": "Hashrate", "text": num, "subtext": unit},
            second,
            power,
            {"title": "Best share", "text": _best3(top["best"]) if top else "–",
             "subtext": ""},
        ],
    }


def _best3(v: Any) -> str:
    """A best share for the small widgets: three figures at most and no space, so a miner's numbers stay on one
    line on a phone. '307.51P' -> '308P', '1.36E' stays, '1.26 M' -> '1.26M'."""
    txt = shares_addon.fmt(v, short=True).replace(" ", "")
    m = re.fullmatch(r"([0-9.]+)(\D*)", txt)
    if not m:
        return txt
    x = float(m.group(1))
    return (f"{x:.3g}" if x < 1000 else f"{x:.0f}") + m.group(2)


def miner_list() -> dict[str, Any]:
    """Per miner: the name and what it's doing on the small top line, the numbers on the bright line below.
    Kept short ('4.80 TH · 56° · best 308P') so the numbers fit one line on a phone, where the widget is 160 px
    wide: Umbrel truncates the top line and wraps the bright one, and only the first three rows are clear."""
    with _lock:
        miners = list(_cache["miners"])
    items = []
    for m in miners[:5]:   # Umbrel's list widget shows at most 5
        if not m.get("online"):
            items.append({"subtext": f"🔴 {m['name']} · Offline", "text": "not answering"})
            continue
        head = f"{m['icon']} {m['name']} · {m['status']}"
        prof = m.get("profile")
        if prof and m["icon"] == "🟢":
            head += " · " + prof.split(" (")[0]
        num, unit = _ths(m.get("ths") or 0)
        bits = [f"{num} {unit[:2]}"]
        if m.get("max_t") is not None:
            bits.append(f"{m['max_t']:.0f}°")
        if m.get("best"):
            bits.append(f"best {_best3(m['best'])}")
        items.append({"subtext": head, "text": " · ".join(bits)})
    return {"type": "list", "refresh": REFRESH, "link": "", "items": items, "noItemsText": "No miners yet: add one under Settings"}


def health() -> dict[str, Any]:
    """Two gauges: hashrate against its own usual (full when it's mining as usual), hottest board against the
    heat alert (full at the alert)."""
    with _lock:
        miners = list(_cache["miners"])
    online = [m for m in miners if m.get("online")]
    ths = sum(m.get("ths") or 0 for m in online)
    usual = _usual_ths([m.get("mid") or "" for m in online])
    num, unit = _ths(ths)
    u_num = (f"{usual:.3g}" if unit == "TH/s" else f"{usual * 1000:.0f}") if usual else ""   # in the gauge's unit
    hot = max((m.get("max_t") for m in online if m.get("max_t") is not None), default=None)
    alert = _settings["hot_c"]
    return {
        "type": "two-stats-with-guage",
        "refresh": REFRESH,
        "link": "",
        "items": [
            {"title": f"avg {u_num}" if usual else ("hashrate" if online else "offline"), "text": num, "subtext": unit[:2],
             "progress": round(min(1.0, ths / usual), 3) if usual else (1.0 if ths > 0 else 0.0)},
            {"title": f"alert {alert:.0f}°", "text": f"{hot:.0f}" if hot is not None else "–", "subtext": "°C" if hot is not None else "",
             "progress": round(max(0.0, min(1.0, hot / alert)), 3) if hot is not None and alert else 0.0},
        ],
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/")
        if path == "/widgets/overview":
            body = overview()
        elif path == "/widgets/miners":
            body = miner_list()
        elif path == "/widgets/health":
            body = health()
        elif path == "/health":
            body = {"ok": True, "updated": _cache["updated"]}
        else:
            self.send_response(404)
            self.end_headers()
            return
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt: str, *args: Any) -> None:
        pass  # Umbrel polls often; keep the app log quiet


def main() -> None:
    threading.Thread(target=_poll_forever, daemon=True).start()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[widgets] serving on :{PORT} (polling miners every {POLL_S:g} s)", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
