"""Hashrate history for Blake2b ASIC Control: the last 24 hours per miner, for the Fleet card graph.

The best-share poll reads every miner's port-4028 summary every 30 s; the same reading is folded in here:
the 20-second hashrate ("MHS 20s") averaged into 5-minute points, and markers where something happened:

  R  the mining software started again (its uptime went back): a restart, by the app or on its own
  P  the setting changed (a preset, the schedule, or a clock set by hand), with what it is now
  T  a tuning run started (its many steps aren't marked one by one)

A rejected share that wasn't stale (rejects_addon) is added as an X when the card is built. Kept in
hashrate.json next to miners.json, so the graph survives an app restart; a stretch with no readings
(the miner was off, or the app wasn't running) is drawn as a gap, not a line.
"""
from __future__ import annotations

import json

import miner_safety
import os
import threading
import time
from pathlib import Path
from typing import Any

KEEP_S = 24 * 3600
STEP_S = 300                   # one point per 5 minutes
MARKERS_KEPT = 60
SAVE_EVERY_S = 60.0

_lock = threading.Lock()
_doc: dict[str, Any] | None = None
_saved = [0.0]


def path() -> Path:
    reg = Path(os.environ.get("SCLITE_WEBUI_MINERS", "miners.json"))
    return reg.with_name("hashrate.json")


def _load() -> dict[str, Any]:
    global _doc
    if _doc is None:
        _doc = miner_safety.read_json(path())
    return _doc


def _save(force: bool = False) -> None:
    now = time.time()
    if not force and now - _saved[0] < SAVE_EVERY_S:
        return
    _saved[0] = now
    try:
        miner_safety.write_json(path(), _doc or {})
    except OSError:
        pass


def _setting(on: str) -> str:
    """What the miner is on, for the P markers: 'tuning' for any tuning step, else the label as is."""
    on = (on or "").strip()
    return "tuning" if on.startswith("tuning") else on


def observe(mid: str, summary: dict[str, Any], on: str = "", now: float | None = None) -> None:
    """Fold one port-4028 summary reading in (from the best-share poll)."""
    now = time.time() if now is None else now
    try:
        mhs = float(summary.get("MHS 20s") or summary.get("MHS 5s") or summary.get("MHS av") or 0)
        elapsed = float(summary.get("Elapsed") or 0)
    except (TypeError, ValueError):
        return
    if elapsed <= 0:
        return
    with _lock:
        doc = _load()
        e = doc.setdefault(mid, {})
        pts = [p for p in e.get("points") or [] if p[0] > now - KEEP_S]
        t = int(now // STEP_S * STEP_S)
        if pts and pts[-1][0] == t:
            pts[-1][1] += mhs
            pts[-1][2] += 1
        else:
            pts.append([t, mhs, 1])
        e["points"] = pts
        e["last"] = [now, mhs, float(summary.get("MHS av") or 0)]
        marks = [m for m in e.get("markers") or [] if m[0] > now - KEEP_S]
        boot = now - elapsed
        last_boot = e.get("boot")
        if last_boot is not None and boot - float(last_boot) > 120:
            marks.append([now - min(elapsed, 600), "R", "the mining software started again"])
        e["boot"] = boot if last_boot is None or boot - float(last_boot) > 120 else min(boot, float(last_boot))
        cur = _setting(on)
        prev = e.get("on")
        if prev is not None and cur and cur != prev:
            if cur == "tuning":
                marks.append([now, "T", "a tuning run started"])
            elif prev == "tuning":
                marks.append([now, "P", f"tuning ended: {cur}"])
            else:
                marks.append([now, "P", f"now {cur}"])
        if cur:
            e["on"] = cur
        e["markers"] = marks[-MARKERS_KEPT:]
        _save()


def card(mid: str, rejects: dict[str, Any] | None = None, now: float | None = None) -> dict[str, Any] | None:
    """The Fleet card graph: 5-minute points (TH/s) and markers, last 24 h."""
    now = time.time() if now is None else now
    with _lock:
        e = (_load().get(mid) or {})
        pts = [[p[0], round(p[1] / max(1, p[2]) / 1e6, 4)] for p in e.get("points") or [] if p[0] > now - KEEP_S]
        marks = [list(m) for m in e.get("markers") or [] if m[0] > now - KEEP_S]
        last = e.get("last")
    for ev in (rejects or {}).get("events") or []:
        if ev.get("kind") == "other" and float(ev.get("t") or 0) > now - KEEP_S:
            why = ev.get("reason") or "no reason given"
            marks.append([float(ev["t"]), "X", f"a share rejected, not stale ({why})"])
    if not pts:
        return None
    vals = [p[1] for p in pts]
    fresh = last and now - float(last[0]) < 180
    return {"points": pts, "markers": sorted(marks), "step": STEP_S,
            "now": round(float(last[1]) / 1e6, 4) if fresh else None,
            "since_restart": round(float(last[2]) / 1e6, 4) if fresh and len(last) > 2 and last[2] else None,
            "avg": round(sum(vals) / len(vals), 4), "from": pts[0][0]}


def flush() -> None:
    with _lock:
        _save(force=True)
