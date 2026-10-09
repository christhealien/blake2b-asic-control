"""Rejected shares for Blake2b ASIC Control: which kind they were.

The port-4028 summary counts accepted and rejected shares since the mining software started, but not
why a share was rejected. Almost always it's a stale share: a correct answer that reached the pool just
after the pool had moved on to the next block ("Rejected ... (stale-prevblk)" in the miner's log, in the
same second as a "Work restart!"). That's pool timing, not the miner. A share rejected for any other
reason (or with none) is rare and worth a look at the miner's log.

So, per miner (in rejects.json next to miners.json):
  - every reading (the best-share poll, every 30 s) adds the new accepted / rejected shares to an hourly
    count (8 days kept): the rate over 24 hours and this miner's own usual rate over 7 days;
  - when the rejected count goes up, the miner's log (/dbg/minersyslog) is read once (at most every
    LOG_GAP_S) and each new "Rejected" line is sorted: stale (exactly "(stale-prevblk)") or not stale
    (anything else, or no reason). A reject the log didn't account for after LOG_GIVE_UP_S (the miner
    trims its own log) is kept as "not seen in the log", never guessed;
  - the events of the last 7 days are kept (time read, the miner's own log time, kind, count, and for a
    not-stale one the short reason word, like "duplicate"; no other log text is stored).

Idea and wording from crProductGuy's stale-share proposal for gbox (MIT, docs/stale-shares-proposal.md in
crProductGuy's box tools); this is a separate implementation for this app.
"""
from __future__ import annotations

import json

import miner_safety
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

HOURS_KEPT = 8 * 24
EVENTS_KEPT_S = 7 * 86400
LOG_GAP_S = 300.0           # read a miner's log at most this often
LOG_WAIT_S = 20.0           # wait this long after the count went up (the line is written at once, but be kind)
LOG_GIVE_UP_S = 900.0       # rejects the log hasn't shown after this long are "not seen in the log"
KEYS_KEPT = 400
HIGH_PCT, HIGH_MIN = 1.0, 20    # "above the usual" wording: at least 1 % of accepted and 20 rejects in 24 h

_lock = threading.Lock()
_reading: set[str] = set()

# set by fan_addon
_srv: Callable[[], Any] | None = None
_client: Callable[[str], Any] = lambda mid: None            # a signed-in MinerClient for this miner (or None)
_notify: Callable[[str, str], None] = lambda mid, text: None   # notify_addon.reject_problem

_LINE = re.compile(r"\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\]\s+Rejected\s+([0-9a-fA-F]+)\s*(.{0,200})")
_REASON = re.compile(r"\(([a-z][a-z0-9 _-]{0,40})\)\s*$", re.I)


def path() -> Path:
    reg = Path(os.environ.get("SCLITE_WEBUI_MINERS", "miners.json"))
    return reg.with_name("rejects.json")


def load() -> dict[str, Any]:
    return miner_safety.read_json(path())


def _save(doc: dict[str, Any]) -> None:
    miner_safety.write_json(path(), doc)


def classify(text: str) -> tuple[str, str]:
    """What follows "Rejected <id>" on a log line -> (kind, reason word). Only "(stale-prevblk)" is stale;
    anything else (or nothing) is "other", so a reason never seen before is noticed, not assumed."""
    m = _REASON.search(text.strip())
    reason = m.group(1).strip().lower() if m else ""
    return ("stale" if reason == "stale-prevblk" else "other"), reason


def parse_log(raw: str) -> list[dict[str, str]]:
    """Every "Rejected" line of a miner log: its time stamp, share id, kind and reason word."""
    out = []
    for m in _LINE.finditer(raw or ""):
        kind, reason = classify(m.group(3))
        out.append({"at": m.group(1), "id": m.group(2).lower(), "kind": kind, "reason": reason,
                    "key": f"{m.group(1)}|{m.group(2).lower()}|{reason}"})
    return out


def observe(mid: str, summary: dict[str, Any], now: float | None = None) -> dict[str, Any] | None:
    """Fold one port-4028 summary reading in (from the best-share poll). Returns the miner's entry."""
    now = time.time() if now is None else now
    try:
        acc, rej = int(summary.get("Accepted") or 0), int(summary.get("Rejected") or 0)
        elapsed = float(summary.get("Elapsed") or 0)
    except (TypeError, ValueError):
        return None
    if elapsed <= 0:
        return None
    with _lock:
        doc = load()
        e = doc.setdefault(mid, {})
        seen = e.get("seen") or {}
        boot = now - elapsed
        restarted = (not seen) or abs(boot - float(seen.get("boot") or 0)) > 120 or acc < int(seen.get("acc") or 0) \
            or rej < int(seen.get("rej") or 0)
        if not seen:
            d_acc = d_rej = 0                      # first reading: a start point, nothing counted yet
            e["since"] = now
        elif restarted:
            d_acc, d_rej = acc, rej                # counted since the restart
        else:
            d_acc, d_rej = acc - int(seen["acc"]), rej - int(seen["rej"])
        e["seen"] = {"boot": boot if restarted else min(boot, float(seen.get("boot") or boot)), "acc": acc, "rej": rej, "at": now}
        hour = int(now // 3600 * 3600)
        hours = [h for h in e.get("hours") or [] if h[0] > now - HOURS_KEPT * 3600]
        if hours and hours[-1][0] == hour:
            hours[-1][1] += d_acc
            hours[-1][2] += d_rej
        elif d_acc or d_rej or not hours:
            hours.append([hour, d_acc, d_rej])
        e["hours"] = hours
        if d_rej > 0:
            p = e.get("pending") or {"n": 0, "since": now}
            p["n"] = int(p.get("n") or 0) + d_rej
            p.setdefault("since", now)
            e["pending"] = p
        _expire(e, now)
        _save(doc)
        need = bool(e.get("pending")) and now - float(e.get("log_at") or 0) >= LOG_GAP_S \
            and now - float(e["pending"].get("since") or now) >= LOG_WAIT_S
    if need and mid not in _reading:
        _reading.add(mid)
        threading.Thread(target=_read_log, args=(mid,), daemon=True, name=f"rejects-{mid}").start()
    return e


def _expire(e: dict[str, Any], now: float) -> None:
    e["events"] = [x for x in e.get("events") or [] if float(x.get("t") or 0) > now - EVENTS_KEPT_S]
    p = e.get("pending")
    if p and p.get("n", 0) > 0 and now - float(p.get("since") or now) > LOG_GIVE_UP_S:
        e["events"].append({"t": float(p["since"]), "kind": "unseen", "n": int(p["n"])})
        e["pending"] = None
    elif p and p.get("n", 0) <= 0:
        e["pending"] = None


def fold_log(e: dict[str, Any], lines: list[dict[str, str]], now: float) -> list[dict[str, Any]]:
    """Match the log's Rejected lines to the rejects counted but not explained yet (pure: easy to test).
    Returns the new events. A line seen before is never counted twice."""
    known = set(e.get("keys") or [])
    fresh = [ln for ln in lines if ln["key"] not in known]
    # the newest unseen lines are the rejects just counted; older ones (from before the app was counting,
    # or already explained) are only remembered so they're never counted later
    p = e.get("pending") or {"n": 0, "since": now}
    take = fresh[-int(p.get("n") or 0):] if p.get("n") else []
    new: list[dict[str, Any]] = []
    for ln in take:
        last = new[-1] if new else None
        if last and last["at"] == ln["at"] and last["kind"] == ln["kind"] \
                and (last.get("reason") or "") == (ln["reason"] if ln["kind"] == "other" else ""):
            last["n"] += 1                         # two in the same second: one mark, x2
            continue
        ev = {"t": now, "at": ln["at"], "kind": ln["kind"], "n": 1}
        if ln["kind"] == "other":
            ev["reason"] = ln["reason"]
        new.append(ev)
    e["keys"] = (list(e.get("keys") or []) + [ln["key"] for ln in lines if ln["key"] not in known])[-KEYS_KEPT:]
    left = int(p.get("n") or 0) - len(take)
    e["pending"] = {"n": left, "since": p.get("since", now)} if left > 0 else None
    e["events"] = list(e.get("events") or []) + new
    return new


def _read_log(mid: str) -> None:
    try:
        c = _client(mid)
        raw = None
        if c is not None:
            try:
                raw = c.api("GET", "/dbg/minersyslog")
            except Exception:
                raw = None
        if isinstance(raw, dict):
            raw = raw.get("body") or raw.get("data") or ""
        now = time.time()
        with _lock:
            doc = load()
            e = doc.setdefault(mid, {})
            e["log_at"] = now
            new = fold_log(e, parse_log(raw), now) if isinstance(raw, str) else []
            _expire(e, now)
            _save(doc)
        for ev in new:
            if ev["kind"] == "other":
                try:
                    _notify(mid, f"⚠️ {ev['n']} share{'s' if ev['n'] > 1 else ''} rejected for a reason other than stale"
                                 f" ({ev.get('reason') or 'no reason given'}) at {ev['at'][11:16]} (miner's clock): "
                                 "worth a look at the miner's log")
                except Exception:
                    pass
    except Exception as ex:
        print(f"[rejects] {mid}: {ex}", flush=True)
    finally:
        _reading.discard(mid)


def card(e: dict[str, Any] | None, now: float | None = None) -> dict[str, Any] | None:
    """What the Fleet card and Miner page show for one miner."""
    if not e or not e.get("seen"):
        return None
    now = time.time() if now is None else now
    hours = e.get("hours") or []
    day = [h for h in hours if h[0] > now - 24 * 3600]
    week = [h for h in hours if h[0] > now - 7 * 86400]
    acc24, rej24 = sum(h[1] for h in day), sum(h[2] for h in day)
    acc7, rej7 = sum(h[1] for h in week), sum(h[2] for h in week)
    evs = [x for x in e.get("events") or [] if float(x.get("t") or 0) > now - 3 * 86400]
    ev24 = [x for x in evs if float(x.get("t") or 0) > now - 24 * 3600]
    n = lambda xs, k: sum(int(x.get("n") or 0) for x in xs if x.get("kind") == k)
    stale, other, unseen = n(ev24, "stale"), n(ev24, "other"), n(ev24, "unseen")
    pending = int((e.get("pending") or {}).get("n") or 0)
    pct = 100.0 * rej24 / acc24 if acc24 else 0.0
    usual = 100.0 * rej7 / acc7 if acc7 else None
    covered = (now - float(e.get("since") or now)) / 3600
    others = [x for x in ev24 if x.get("kind") == "other"]
    return {"accepted": e["seen"]["acc"], "rejected": e["seen"]["rej"],
            "day": {"accepted": acc24, "rejected": rej24, "stale": stale, "other": other, "unseen": unseen,
                    "pending": pending, "pct": round(pct, 3)},
            "usual_pct": round(usual, 3) if usual is not None else None,
            "high": other == 0 and rej24 >= HIGH_MIN and pct >= HIGH_PCT,
            "other_last": max((x.get("at") or "" for x in others), default="") or None,
            "covered_h": round(min(covered, 24 * 8), 1),
            "events": [{"t": x["t"], "at": x.get("at"), "kind": x["kind"], "n": x.get("n", 1),
                        **({"reason": x.get("reason")} if x.get("kind") == "other" else {})} for x in evs]}


def all_cards() -> dict[str, Any]:
    return {mid: card(e) for mid, e in load().items()}
