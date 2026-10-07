"""Best share tracking for Blake2b ASIC Control.

Every miner reports its best share since its mining software last started ("Best Share"
in the port-4028 summary; no password needed). The miner forgets it on every restart,
so this keeps, per miner, in best_shares.json next to miners.json:

  session   the best share since the last restart. It starts again from 0 whenever the
            miner restarts (uptime goes back, or the miner's own best drops).
  history   every new best the miner reaches, logged when it's found (a new best since restart
            is a new line, even within the same restart), with what the miner was on then
            (clock, preset or tuning). The KEEP biggest are kept; the biggest is the record.
            Shown biggest first, each with when it was found. Only a Reset on the Miner page
            clears it. (Before 1.10.2 a restart kept one line, its best, so a bigger share later
            in the same restart replaced the earlier one.)

The widget server reads the same file.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

POLL_S = 30.0
KEEP = 20
BOOT_SLACK_S = 120        # "started at" can wobble by this much without being a restart

_lock = threading.Lock()
_busy = [False]
_last = [0.0]

# set by fan_addon: the dashboard module (registry) and "what is this miner on" helpers
_srv: Callable[[], Any] | None = None
_running: Callable[[str], bool] = lambda mid: False
_notify: Callable[[str, str, str, str], None] = lambda mid, best, prev, on: None   # notify_addon.best_share
_observe: Callable[[str, dict], Any] = lambda mid, summary: None                  # rejects_addon.observe
_history: Callable[[str, dict, str], Any] = lambda mid, summary, on: None         # hashrate_addon.observe


def path() -> Path:
    reg = Path(os.environ.get("SCLITE_WEBUI_MINERS", "miners.json"))
    return reg.with_name("best_shares.json")


def load() -> dict[str, Any]:
    try:
        doc = json.loads(path().read_text())
        return doc if isinstance(doc, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def _save(doc: dict[str, Any]) -> None:
    p = path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1))
    os.replace(tmp, p)


# The miner counts share difficulty in the old unit where difficulty 1 = 2^32 hashes (checked on an SC Lite:
# MHS av x Elapsed / Difficulty Accepted came to 4.287e9). DATUM and mempool show difficulty in hashes, so a
# share is shown here x 2^32: an SC Lite share of 16384 is 70.37T, as DATUM shows it. Stored values stay in the
# miner's own unit; only what's shown is converted.
MINER_DIFF_UNIT = 2 ** 32
_SI = (("Y", 1e24), ("Z", 1e21), ("E", 1e18), ("P", 1e15), ("T", 1e12), ("G", 1e9), ("M", 1e6), ("k", 1e3))


SCALES = ("hashes", "miner", "both")
_scale = ["hashes"]          # Settings -> Best share numbers (miners.json "share_scale"), set by fan_addon


def set_scale(mode: str) -> None:
    _scale[0] = mode if mode in SCALES else "hashes"


def scale() -> str:
    return _scale[0]


def fmt_hashes(v: Any) -> str:
    """On DATUM's and mempool's scale and in their style: two decimals at most, trailing zeros dropped, the
    letter right after the number. 1261948.2 -> '5.42P', 16384 -> '70.37T'."""
    try:
        v = float(v) * MINER_DIFF_UNIT
    except (TypeError, ValueError):
        return "–"
    for unit, div in _SI:
        if v >= div:
            return f"{v / div:.2f}".rstrip("0").rstrip(".") + unit
    return f"{v:.0f}"


def fmt_miner(v: Any) -> str:
    """The miner's own number (units of 2^32 hashes), as the app showed it before 1.15.5: 1261948.2 -> '1.26 M'."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "–"
    for unit, div in (("P", 1e15), ("T", 1e12), ("G", 1e9), ("M", 1e6), ("k", 1e3)):
        if v >= div:
            x = v / div
            return f"{x:.3g} {unit}" if x < 100 else f"{x:.0f} {unit}"
    return f"{v:.0f}"


def fmt(v: Any, short: bool = False) -> str:
    """A share difficulty from the miner, the way Settings -> Best share numbers says: like DATUM and mempool
    ('5.42P'), the miner's own number ('1.26 M'), or both ('5.42P (1.26 M)'). short: one of them only (the
    widget), the DATUM one when both are picked."""
    mode = _scale[0]
    if mode == "miner":
        return fmt_miner(v)
    if mode == "both" and not short and fmt_hashes(v) != "–":
        return f"{fmt_hashes(v)} ({fmt_miner(v)})"
    return fmt_hashes(v)


def _on_what(row: dict[str, Any], presets: dict[str, Any], clock: float | None, mid: str) -> str:
    if _running(mid):
        return f"tuning at {clock:.0f} MHz" if clock else "tuning"
    p = presets.get(row.get("active_preset") or "")
    bits = [f"{clock:.0f} MHz"] if clock else []
    if p:
        bits.append(str(p.get("label") or row.get("active_preset")))
    return " · ".join(bits) or ""


def update(doc: dict[str, Any], mid: str, best: float, elapsed: float, on: str, now: float | None = None) -> dict[str, Any]:
    """Fold one reading into this miner's entry (pure: easy to test). Returns the entry."""
    now = time.time() if now is None else now
    e = doc.setdefault(mid, {})
    s = e.get("session") or {}
    boot = now - max(0.0, elapsed)
    restarted = (not s) or abs(boot - float(s.get("boot") or 0)) > BOOT_SLACK_S or best < float(s.get("best") or 0)
    if restarted:
        s = {"sid": now, "boot": boot, "best": 0.0, "at": None, "on": ""}
    else:
        s["boot"] = min(float(s["boot"]), boot) if s.get("boot") else boot
    if best > float(s.get("best") or 0):
        # The miner doesn't say when it found its best share, only what it is now, so a new one was found
        # some time since the last reading. Each line keeps the setting from the reading that saw it (it is
        # never updated later). If the setting changed since the last reading, it could be either one; if
        # the app wasn't reading for a while (stopped, offline), the time is only known to a window.
        label = on
        prev_on, seen = e.get("last_on"), float(e.get("seen") or 0)
        if not restarted and prev_on and prev_on != on:
            label = f"{prev_on} or {on} (changed between two readings)"
        found = {"best": best, "at": now, "on": label, "sid": s["sid"]}
        since = max(boot, seen) if seen else boot
        if now - since > 3 * POLL_S:
            found["after"] = since          # found somewhere between `after` and `at`
        s.update(best=best, at=now, on=label)
        # a new best since restart: its own line (it never replaces an earlier one)
        hist = list(e.get("history") or [])
        hist.append(found)
        hist.sort(key=lambda h: -float(h.get("best") or 0))
        e["history"] = hist[:KEEP]
    e["session"] = s
    e["seen"] = now
    e["last_on"] = on
    return e


def _read(client: Any, mid: str = "") -> tuple[float, float, float | None, dict]:
    s = (client.bfg("summary").get("SUMMARY") or [{}])[0]
    try:
        _observe(mid, s)          # the same reading counts accepted / rejected shares
    except Exception as e:
        print(f"[rejects] {mid}: {e}", flush=True)
    clock = None
    try:
        clocks = [float(d["clock"]) for d in client.bfg("devs").get("DEVS") or [] if d.get("clock") is not None]
        clock = max(clocks) if clocks else None
    except Exception:
        pass
    return float(s.get("Best Share") or 0), float(s.get("Elapsed") or 0), clock, s


def _poll() -> None:
    from miner_client import MinerClient
    try:
        rdoc = _srv().load_registry_doc() if _srv else {}
        presets = rdoc.get("presets") or {}
        readings = []
        for m in rdoc.get("miners") or []:
            if m.get("demo") or not m.get("ip"):
                continue
            mid = str(m.get("id") or m.get("ip"))
            try:
                c = MinerClient(ip=str(m["ip"]), password="", name=mid, id=mid)
                best, elapsed, clock, summ = _read(c, mid)
            except Exception:
                continue          # offline: keep what we have
            if elapsed <= 0:
                continue
            on = _on_what(m, presets.get(mid) or {}, clock, mid)
            try:
                _history(mid, summ, on)    # the Fleet card's hashrate graph
            except Exception as e:
                print(f"[hashrate] {mid}: {e}", flush=True)
            readings.append((mid, best, elapsed, on))
        if readings:
            with _lock:
                doc = load()
                for r in readings:
                    old = max((float(h.get("best") or 0) for h in (doc.get(r[0]) or {}).get("history") or []), default=0.0)
                    e = update(doc, *r)
                    new = max((float(h.get("best") or 0) for h in e.get("history") or []), default=0.0)
                    if old > 0 and new > old:
                        try:
                            _notify(r[0], fmt(new), fmt(old), r[3])
                        except Exception:
                            pass
                _save(doc)
    except Exception as e:
        print(f"[shares] {e}", flush=True)
    finally:
        _busy[0] = False


def tick() -> None:
    """Called from the fan loop: read every miner's best share every POLL_S, in the background."""
    now = time.time()
    if _busy[0] or now - _last[0] < POLL_S:
        return
    _last[0] = now
    _busy[0] = True
    threading.Thread(target=_poll, daemon=True, name="best-shares").start()


def card(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    """What the Fleet card, Miner page and widgets show for one miner."""
    if not entry:
        return None
    s = entry.get("session") or {}
    hist = sorted(entry.get("history") or [], key=lambda h: -float(h.get("best") or 0))
    top = hist[0] if hist else None

    return {"now": s.get("best") or 0, "now_text": fmt(s.get("best")) if s.get("best") else "–",
            "now_at": s.get("at"), "now_on": s.get("on") or "", "since": s.get("boot"),
            "record": top and top.get("best"), "record_text": fmt(top.get("best")) if top else "–",
            "record_at": top and top.get("at"), "record_on": (top or {}).get("on") or "",
            "history": [{"best": h.get("best"), "text": fmt(h.get("best")), "at": h.get("at"), "after": h.get("after"),
                         "on": h.get("on") or "",
                         "rank": hist.index(h) + 1, "record": h is top,
                         "current": h.get("sid") == s.get("sid") and h.get("best") == s.get("best")}
                        for h in hist],                    # the table: biggest first
            "reset_at": entry.get("reset_at"), "scale": _scale[0]}


def all_cards() -> dict[str, Any]:
    return {mid: card(e) for mid, e in load().items()}


def reset(body: dict[str, Any]) -> dict[str, Any]:
    """Clear the record and history (the since-restart best stays: it's the miner's own)."""
    ids = body.get("miner_ids") or ([body["miner_id"]] if body.get("miner_id") else [])
    if not ids:
        raise ValueError("no miner given")
    with _lock:
        doc = load()
        for mid in ids:
            e = doc.setdefault(str(mid), {})
            s = e.get("session") or {}
            e["history"] = ([{"best": s["best"], "at": s.get("at"), "on": s.get("on") or "", "sid": s.get("sid")}]
                            if s.get("best") else [])
            e["reset_at"] = time.time()
        _save(doc)
    return {"ok": True}
