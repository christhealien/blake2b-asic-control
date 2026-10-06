"""Chip health for Blake2b ASIC Control, from the miner's own log (/dbg/minersyslog).

The miner logs every bad result (a "HW error") with the board, chip and core it came
from, and that chip's running totals since its board last started:

    [2026-10-02 13:13:57] C3: nonce#(9/9) 0x7a30... ntime#0 from 32.17  BAD(139/236)
                          board 3          chip 32, core 17   139 bad of 236 results

and, every few seconds per board, its chips' temperatures and the measured board voltage:

    [...] C0: Chip Avgtemp 69.000000'C, MaxTemp 76.000000'C, CpbTemp 68.750000'C
    [...] watchdog dev0: read cpb voltage to 8980 success.

So unlike the plain HW error count, it gives each chip's share of bad results (bad / all),
which also catches a chip that is doing little work. Chip numbers are the same as
/dbg/icinfo's chipindex. Read on demand and cached, as the log can be a few MB.
"""
from __future__ import annotations

import math
import re
import threading
import time
from typing import Any

CACHE_S = 300.0
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_lock = threading.Lock()

# Anchored to a line start, with bounded fields, so a crafted log can't make matching take forever
# (an unbounded "[...]" with re.findall over a log full of "[" is quadratic).
BAD = re.compile(r"^ ?\[([^\]\n]{1,40})\] C(\d{1,3}): nonce#\(\d{1,4}/\d{1,4}\) \S{1,80} ntime#\d{1,6} from (\d{1,3})\.(\d{1,3})\s{1,8}BAD\((\d{1,12})/(\d{1,12})\)", re.M)
TEMP = re.compile(r"^ ?\[([^\]\n]{1,40})\] C(\d{1,3}): Chip Avgtemp ([\d.]{1,12})'C, MaxTemp ([\d.]{1,12})'C(?:, CpbTemp ([\d.]{1,12})'C)?", re.M)   # the SC BOX writes no CpbTemp
MAX_LOG_CHARS = 4_000_000          # read the newest 4 MB at most
VOLT = re.compile(r"watchdog dev(\d{1,3}): read cpb voltage to (\d{1,6}) success")
DTFS = re.compile(r"C(\d{1,3}): (?:DTFS chip\((\d{1,4})\)|chip\((\d{1,4})\) Individual)")

# How a chip is judged (grade() below). Two signals, and a chip is only flagged when the signal is far
# bigger than the random scatter you'd expect from the number of results counted so far:
#  - bad results: its share of bad results (log), against the board's typical share.  weak: 10%+ bad
#    and clearly above its board; watch: 3%+ and clearly above.  This is the real "is the chip
#    failing" signal: a bad result is work the chip got wrong.
#  - hardware errors (/dbg/icinfo), only on an SC BOX / HS BOX board whose log has no bad-result lines:
#    watch at 10+ errors that are 5x the board's typical chip and at least 4 standard deviations out.
#    Never weak on this alone: the count covers the whole time since the boards started.
#  - perf: how much work it reports (/dbg/icinfo) against the board's typical chip. Perf is a count,
#    so with small numbers (after a restart) it scatters a lot: 43 against a typical 55 is only about
#    1.6 standard deviations, normal luck. A slow chip is only marked watch when it's both 15%+ below
#    and at least 4 standard deviations out; it's never weak on perf alone unless it does under half
#    the work (and that's statistically certain), since a slow chip with no bad results isn't failing.
WEAK_PCT, WATCH_PCT, MIN_RESULTS = 10.0, 3.0, 50
HW_WATCH_MIN, HW_WATCH_X = 10, 5          # no bad-result lines: watch at 10+ hardware errors and 5x the board's typical
BAD_Z_WEAK, BAD_Z_WATCH = 4.0, 3.0        # standard deviations above the board's typical bad share
PERF_SLOW, PERF_DEAD = 0.85, 0.50         # share of the board's typical perf
PERF_Z_SLOW, PERF_Z_DEAD = 4.0, 6.0       # ...and this many standard deviations below it

def parse(text: str) -> dict[str, Any]:
    if len(text) > MAX_LOG_CHARS:
        text = text[-MAX_LOG_CHARS:]
    chips: dict[tuple[int, int], dict[str, Any]] = {}
    for ts, b, c, core, bad, tot in BAD.findall(text):
        k = (int(b), int(c))
        e = chips.setdefault(k, {"bad": 0, "total": 0, "cores": {}, "last": ""})
        bad, tot = int(bad), int(tot)
        if bad > tot:                         # a garbled line: can't be right, skip it
            continue
        if tot < e["total"]:                 # the board restarted: counters start again
            e["cores"] = {}
        e["bad"], e["total"], e["last"] = bad, tot, ts
        e["cores"][int(core)] = e["cores"].get(int(core), 0) + 1
    boards: dict[int, dict[str, Any]] = {}
    for ts, b, avg, mx, cpb in TEMP.findall(text):
        boards.setdefault(int(b), {}).update(avg_c=float(avg), max_c=float(mx), board_c=float(cpb) if cpb else None, at=ts)
    for b, mv in VOLT.findall(text):
        boards.setdefault(int(b), {})["volt_mv"] = int(mv)
    dtfs = len(DTFS.findall(text))
    first = re.search(r"\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\]", text[:100_000])
    last = None
    for m in re.finditer(r"\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\]", text[-4000:]):
        last = m.group(1)
    return {"chips": {f"{b}:{c}": {"board": b, "chip": c, "bad": v["bad"], "total": v["total"],
                                   "bad_pct": round(100.0 * v["bad"] / v["total"], 2) if v["total"] else None,
                                   "cores": len(v["cores"]), "last": v["last"]}
                      for (b, c), v in chips.items()},
            "boards": {str(b): v for b, v in boards.items()},
            "dtfs_events": dtfs,
            "from": first.group(1) if first else None, "to": last}


def read(client: Any, key: str, force: bool = False) -> dict[str, Any] | None:
    """The miner log, parsed (cached CACHE_S). None if this miner has no such log."""
    with _lock:
        ts, val = _cache.get(key, (0.0, None))
        if val is not None and not force and time.time() - ts < CACHE_S:
            return {**val, "age_s": int(time.time() - ts)}
    try:
        raw = client.api("GET", "/dbg/minersyslog")
    except Exception:
        return None
    if isinstance(raw, dict):
        raw = raw.get("body") or raw.get("data") or ""
    if not isinstance(raw, str) or "C0:" not in raw and "C1:" not in raw:
        return None
    val = parse(raw)
    with _lock:
        _cache[key] = (time.time(), val)
    return {**val, "age_s": 0}


def _z_count(value: float, typical: float) -> float:
    """How many standard deviations a count is below the typical count (Poisson: sd = sqrt(typical))."""
    return (typical - value) / math.sqrt(typical) if typical > 0 else 0.0


def _z_share(bad: int, total: int, p0: float) -> float:
    """How many standard deviations a chip's bad count is above what the board's typical share predicts."""
    p0 = min(max(p0, 0.005), 0.5)             # never assume less than 0.5% bad
    sd = math.sqrt(total * p0 * (1 - p0))
    return (bad - total * p0) / sd if sd > 0 else 0.0


def grade(boards: list[dict[str, Any]], log: dict[str, Any] | None, hw_fallback: bool = False) -> None:
    """Add health to each chip (dicts with chip, hwerr, perf) in place: ok / watch / weak, and why."""
    lchips = (log or {}).get("chips") or {}
    for b in boards:
        perfs = sorted(c["perf"] for c in b["chips"] if c.get("perf"))
        med = perfs[len(perfs) // 2] if perfs else None
        for c in b["chips"]:
            lc = lchips.get(f"{b['board']}:{c['chip']}")
            if lc:
                c.update(bad=lc["bad"], total=lc["total"], bad_pct=lc["bad_pct"], cores=lc["cores"])
        # the board's typical bad share: the median chip's (so one bad chip doesn't move it)
        shares = sorted(c["bad"] / c["total"] for c in b["chips"] if c.get("total"))
        p0 = shares[len(shares) // 2] if shares else 0.0
        b["typical_bad_pct"] = round(100 * p0, 2) if shares else None
        b["typical_perf"] = med
        # no bad-result lines for this board in the log (the SC BOX and HS BOX hardly write them): fall
        # back on the hardware-error counts, against the board's typical chip
        hws = sorted(int(c.get("hwerr") or 0) for c in b["chips"])
        hw_med = hws[len(hws) // 2] if hws else 0
        use_hw = hw_fallback and bool(hws) and len(shares) < len(b["chips"]) / 2   # (a stray line or two doesn't count)
        b["judged_on_hw"] = use_hw
        for c in b["chips"]:
            why, notes, state = [], [], "ok"
            bp, n, bad = c.get("bad_pct"), c.get("total") or 0, c.get("bad") or 0
            if bp is not None and n >= MIN_RESULTS:
                z = _z_share(bad, n, p0)
                c["bad_z"] = round(z, 1)
                if bp >= WEAK_PCT and z >= BAD_Z_WEAK:
                    state = "weak"; why.append(f"{bp:.0f}% of its results are bad (typical on this board: {100 * p0:.1f}%)")
                elif bp >= WATCH_PCT and z >= BAD_Z_WATCH:
                    state = "watch"; why.append(f"{bp:.1f}% of its results are bad (typical on this board: {100 * p0:.1f}%)")
                elif bp >= WATCH_PCT:
                    notes.append(f"{bp:.1f}% bad, but from only {n} results: could still be chance")
            elif bp is not None and bad:
                notes.append(f"only {n} results so far: too few to judge")
            if use_hw:
                h, exp = int(c.get("hwerr") or 0), max(float(hw_med), 1.0)
                zh = (h - exp) / math.sqrt(exp)
                if h >= HW_WATCH_MIN and h >= HW_WATCH_X * exp and zh >= BAD_Z_WEAK:
                    state = "watch"
                    why.append(f"{h} hardware errors, {h / exp:.0f}x the board's typical chip ({hw_med})")
            c["perf_rel"] = round(c["perf"] / med, 3) if med and c.get("perf") else None
            pr = c["perf_rel"]
            if pr is not None and pr < 1:
                z = _z_count(c["perf"], med)
                c["perf_z"] = round(z, 1)
                spread = 100 / math.sqrt(med)             # one standard deviation, in % of typical
                if pr < PERF_DEAD and z >= PERF_Z_DEAD:
                    state = "weak"; why.append(f"does only {pr * 100:.0f}% of the work of the board's typical chip")
                elif pr < PERF_SLOW and z >= PERF_Z_SLOW:
                    if state == "ok":
                        state = "watch"
                    why.append(f"slow: {pr * 100:.0f}% of the board's typical chip")
                elif pr < 0.92:
                    notes.append(f"perf {pr * 100:.0f}% of typical is normal scatter at these numbers (±{spread:.0f}% is one standard deviation)")
            c["health"], c["why"], c["note"] = state, "; ".join(why), "; ".join(notes)
        lb = ((log or {}).get("boards") or {}).get(str(b["board"])) or {}
        b.update({k: lb[k] for k in ("avg_c", "max_c", "board_c", "volt_mv") if k in lb})
        b["weak"] = [c["chip"] for c in b["chips"] if c["health"] == "weak"]
        b["watch"] = [c["chip"] for c in b["chips"] if c["health"] == "watch"]
        tb = sum(c.get("bad") or 0 for c in b["chips"]); tt = sum(c.get("total") or 0 for c in b["chips"])
        b["bad_pct"] = round(100.0 * tb / tt, 2) if tt else None


def chip_temp_offset(log: dict[str, Any] | None) -> float | None:
    """How much hotter the hottest chip runs than the board sensor (for the fan curve)."""
    best = None
    for v in ((log or {}).get("boards") or {}).values():
        if v.get("max_c") is not None and v.get("board_c") is not None:
            d = v["max_c"] - v["board_c"]
            best = d if best is None else max(best, d)
    return best
