"""Clock writes for the SC Box / HS Box (power plans like "550 MHz 0.44 V 90 RPM 90 RPM": decimal volts, no PV).

Shared by the dashboard (presets, the clock control, schedules) and the box tuner (asic_tuner_box.py), so both
write and put back a box's clock the same way. Nothing here talks to the network itself: callers pass
get() -> the miner's /mcb/setting and put(settings) -> PUT it back.

What testing on both boxes showed (fw 2.2.5 / 2.2.6):
  - a manual plan (manual true, manualPowerplan) applies at once, without a restart;
  - only the clock is worth changing: 0.01 V less made no measurable difference in power at the wall, so the
    app keeps the voltage (and the fan fields, which these firmwares ignore) exactly as the miner has them;
  - turning manual off again does NOT make the firmware run its level plan: the HS Box kept running the last
    clock written. Going back to a firmware level therefore first writes that level's plan as a manual plan
    (applied at once), then the original fields (manual, select, manualPowerplan).
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable

PLAN = re.compile(r"^\s*(\d{2,4})\s*MHz\s+(\d+\.\d{1,4})\s*V\s+(\d{1,3})\s*RPM\s+(\d{1,3})\s*RPM\s*$", re.I)
GRID = 25                   # clocks on the 25 MHz grid
FIELDS = ("manual", "select", "manualPowerplan")


def parse(text: Any) -> dict[str, Any] | None:
    """{mhz, volts (text, as the miner wrote it), fan_a, fan_b} or None (not a box plan, or the 0 MHz off plan)."""
    m = PLAN.match(str(text or "")[:200])
    if not m or int(m.group(1)) <= 0:
        return None
    return {"mhz": int(m.group(1)), "volts": m.group(2), "fan_a": int(m.group(3)), "fan_b": int(m.group(4))}


def millivolts(volts: Any) -> int:
    """0.44 -> 440 (for the tuner's CSV columns, which are in mV)."""
    try:
        return int(round(float(volts) * 1000))
    except (TypeError, ValueError):
        return 0


def with_clock(text: str, mhz: int) -> str:
    """The same plan with only the clock changed (voltage and fan fields kept exactly as written)."""
    p = parse(text)
    if not p:
        raise ValueError(f"not an SC Box / HS Box plan: {str(text)[:60]!r}")
    return f"{int(mhz)} MHz {p['volts']} V {p['fan_a']} RPM {p['fan_b']} RPM"


def _modes(s: dict[str, Any]) -> list[dict[str, Any]]:
    """The power plans of the running algorithm (the HS Box keeps one list per algorithm)."""
    algo = s.get("algoname") if isinstance(s.get("algoname"), str) else None
    out: list[dict[str, Any]] = []
    for p in s.get("powerplans") or []:
        if not isinstance(p, dict):
            continue
        if isinstance(p.get("mode"), list):
            if algo and p.get("algo") != algo:
                continue
            out += [m for m in p["mode"] if isinstance(m, dict)]
        else:
            out.append(p)
    return out


def _level(p: dict[str, Any]) -> int:
    try:
        return int(p.get("level") or 0)
    except (TypeError, ValueError):
        return -1


def stock_text(s: dict[str, Any]) -> str:
    """The firmware's own level-0 plan of the running algorithm."""
    return next((str(p.get("info") or "") for p in _modes(s) if _level(p) == 0), "")[:200]


def running_text(s: dict[str, Any]) -> str:
    """The plan the miner really runs: manualPowerplan with manual on, else the selected level's plan."""
    if s.get("manual"):
        return str(s.get("manualPowerplan") or "")[:200]
    try:
        sel = int(s.get("select") or 0)
    except (TypeError, ValueError):
        sel = 0
    return next((str(p.get("info") or "") for p in _modes(s) if _level(p) == sel), "")[:200]


STOCK_RANGE = (300, 1000)   # a box reporting a stock clock outside this isn't trusted to set its own limits


def limits(stock_mhz: int) -> dict[str, int]:
    """The clock range for one box, around its own stock clock (SC Box 725, HS Box 850):
      lo     the lowest clock a preset or the clock control may set (half of stock, on the grid, at least 300)
      hi     the highest: stock. Both boxes made errors 25 MHz above what they ran, so nothing is set above stock
      tune   the highest a tuning run may test (stock + 25): it stops at the first step with errors"""
    s = int(stock_mhz)
    if not STOCK_RANGE[0] <= s <= STOCK_RANGE[1]:
        raise ValueError(f"this miner reports a stock clock of {s} MHz, outside what the app handles "
                         f"({STOCK_RANGE[0]}-{STOCK_RANGE[1]}), so its clock isn't changed")
    return {"lo": max(300, int(round(s * 0.5 / GRID)) * GRID), "hi": s, "tune": s + GRID, "stock": s}


def check_clock(mhz: Any, stock_mhz: int, tuning: bool = False) -> int:
    try:
        v = int(mhz)
    except (TypeError, ValueError):
        raise ValueError("the clock must be a whole number of MHz") from None
    lim = limits(stock_mhz)
    hi = lim["tune"] if tuning else lim["hi"]
    if v % GRID:
        raise ValueError(f"the clock must be on the {GRID} MHz grid (like {v // GRID * GRID} or {v // GRID * GRID + GRID})")
    if not lim["lo"] <= v <= hi:
        raise ValueError(f"the clock must be {lim['lo']}-{hi} MHz for this miner (stock {lim['stock']} MHz)")
    return v


def write_clock(get: Callable[[], dict], put: Callable[[dict], Any], mhz: int, base_text: str | None = None,
                stock_mhz: int | None = None, headroom: int = 0) -> str:
    """Run mhz as a manual plan: the running plan's voltage and fan fields stay, select stays, every other setting
    goes back as the miner gave it. Checked by reading it back. Returns the plan text written.
    stock_mhz: the stock clock the limits were worked out from (the last probe). It's read again here from the
    same settings: if the miner now reports another one (the HS Box switched to its other algorithm: stock 750
    instead of 850), nothing is written until it's probed again. mhz may be at most stock + headroom."""
    s = get()
    if not isinstance(s, dict) or "manual" not in s:
        raise RuntimeError("couldn't read the miner's settings")
    if stock_mhz is not None:
        fresh = parse(stock_text(s))
        if not fresh:
            raise RuntimeError("the miner didn't report its stock plan just now; press Probe now")
        if fresh["mhz"] != int(stock_mhz):
            raise RuntimeError(f"the miner's stock plan now reads {fresh['mhz']} MHz, not {int(stock_mhz)} as at the last "
                               "probe (another algorithm?): nothing was changed. Press Probe now")
        if int(mhz) > fresh["mhz"] + int(headroom):
            raise RuntimeError(f"{int(mhz)} MHz is above what the app sets on this miner (stock {fresh['mhz']} MHz)")
    text = with_clock(base_text or running_text(s), mhz)
    s["manual"], s["manualPowerplan"] = True, text
    put(s)
    back = get()
    if not isinstance(back, dict) or back.get("manual") is not True or back.get("manualPowerplan") != text:
        raise RuntimeError(f"the miner didn't keep {text} (it reads "
                           f"{(back or {}).get('manualPowerplan') if isinstance(back, dict) else '?'})")
    return text


def put_back(get: Callable[[], dict], put: Callable[[dict], Any], raw: dict[str, Any], run_text: str,
             pause_s: float = 3.0) -> bool:
    """Put a box back on what it ran: raw = its manual / select / manualPowerplan fields from before, run_text =
    the plan it was really running then. Writes run_text as a manual plan first (so the boards run it again
    even when manual goes off: see the module note), then the raw fields. True when the fields read back as raw."""
    raw = {k: raw.get(k) for k in FIELDS if k in raw}
    if "manual" not in raw:
        raise ValueError("the setting to go back to is incomplete")
    s = get()
    if not isinstance(s, dict):
        raise RuntimeError("couldn't read the miner's settings")
    if not parse(run_text) and not raw.get("manual") and not str(run_text or "").strip().startswith("0 MHz"):
        # without its plan written first, turning manual off would leave the current clock running (HS Box)
        raise ValueError(f"the plan to go back to isn't readable ({str(run_text)[:60]!r}); set the miner by hand")
    if parse(run_text):
        s["manual"], s["manualPowerplan"] = True, run_text
        put(s)
        time.sleep(pause_s)
        s = get()
        if not isinstance(s, dict):
            raise RuntimeError("couldn't read the miner's settings")
    if any(s.get(k) != v for k, v in raw.items()):
        s.update(raw)
        put(s)
    back = get()
    return isinstance(back, dict) and all(back.get(k) == v for k, v in raw.items())
