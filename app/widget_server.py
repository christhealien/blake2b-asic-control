#!/usr/bin/env python3
"""Umbrel home-screen widgets for Blake2b ASIC Control.

Umbrel reads these from inside the app's network (web:8788); the port is not
published through the app's web address, so it needs no login.

  GET /widgets/overview   four-stats: total hashrate (and miners online), hottest ASIC (which miner),
                          best share since restart (which miner), tuner
  GET /widgets/miners     list: one line per miner: what it's doing (hashing, tuning, hot, offline),
                          then its hashrate, hottest board and best share since restart

Data comes from each miner's port-4028 status API (light, no password), refreshed in
the background every SCLITE_WIDGET_POLL_S seconds (default 30), plus the clock / voltage /
PV setting from the web API every SCLITE_WIDGET_PLAN_S seconds (default 120; while a miner
is being tuned it comes from the run's files instead). Requests are answered from that
cache so a slow or offline miner never delays the home screen.
"""
from __future__ import annotations

import json
import os
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


def _registry() -> list[dict[str, Any]]:
    try:
        doc = json.loads(REGISTRY.read_text())
    except (FileNotFoundError, ValueError):
        return []
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


def _read(row: dict[str, Any]) -> dict[str, Any]:
    name = row.get("name") or row.get("id") or row.get("ip")
    mid = row["_mid"]
    out: dict[str, Any] = {"name": name, "ip": row.get("ip"), "online": False}
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
    plan = (tune or {}).get("plan")   # (the list no longer shows MHz / mV / PV, so no web-API read)
    out["plan"] = plan
    if tune:
        out["status"], out["icon"] = f"Tuning · {tune['what']}", "🔧"
    elif out["ths"] < 0.05:
        out["status"], out["icon"] = "Not hashing", "🟠"
    elif out["max_t"] is not None and out["max_t"] >= HOT_C:
        out["status"], out["icon"] = f"Hot · {out['max_t']:.0f} °C", "🟠"
    else:
        out["status"], out["icon"] = "Hashing", "🟢"

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


def overview() -> dict[str, Any]:
    with _lock:
        miners = list(_cache["miners"])
    online = [m for m in miners if m.get("online")]
    ths = sum(m.get("ths") or 0 for m in online)
    hot = max((m for m in online if m.get("max_t") is not None), key=lambda m: m["max_t"], default=None)
    t_text, t_sub = _tuner()
    top = max((m for m in online if m.get("best")), key=lambda m: m["best"], default=None)
    return {
        "type": "four-stats",
        "refresh": REFRESH,
        "link": "",
        "items": [
            {"title": "Hashrate", "text": f"{ths:.2f}",
             "subtext": f"TH/s · {len(online)}/{len(miners)} online" if miners else "add miners in Settings"},
            {"title": "Hottest ASIC", "text": f"{hot['max_t']:.0f}" if hot else "–",
             "subtext": f"°C · {hot['name']}" if hot else ""},
            {"title": "Best share", "text": shares_addon.fmt(top["best"], short=True).replace(" ", "") if top else "–",
             "subtext": f"since restart · {top['name']}" if top else ""},
            {"title": "Tuner", "text": t_text, "subtext": t_sub},
        ],
    }


def miner_list() -> dict[str, Any]:
    """One short line per miner: what it's doing, then hashrate and hottest board."""
    with _lock:
        miners = list(_cache["miners"])
    items = []
    for m in miners[:5]:   # Umbrel's list widget shows at most 5 lines
        if not m.get("online"):
            items.append({"text": f"🔴 {m['name']} · Offline", "subtext": str(m.get("ip") or "")})
            continue
        bits = [f"{m['ths']:.2f} TH/s"]
        if m.get("max_t") is not None:
            bits.append(f"{m['max_t']:.0f} °C")
        if m.get("best"):
            bits.append(f"best {shares_addon.fmt(m['best'], short=True)}")
        items.append({"text": f"{m['icon']} {m['name']} · {m['status']}", "subtext": " · ".join(bits)})
    if not items:
        items.append({"text": "No miners yet", "subtext": "Add one under Settings"})
    return {"type": "list", "refresh": REFRESH, "link": "", "items": items}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/")
        if path == "/widgets/overview":
            body = overview()
        elif path == "/widgets/miners":
            body = miner_list()
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
