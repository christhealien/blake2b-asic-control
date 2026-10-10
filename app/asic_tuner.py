#!/usr/bin/env python3
"""Blake2b ASIC Control: automatic per-chip clock / voltage tuner for the SC Lite (fw 2.2.0).

It judges every setting by each chip's own hardware errors (from /dbg/icinfo),
not by board totals, so the normal background errors of healthy chips do not
fail good settings.

How it works
  1. BASELINE: runs the baseline setting (--start-mhz / --mv / --base-pv: stock by default,
     or the current setting with --baseline-current) for --baseline-min minutes and records
     every chip's normal error rate there. A board reset ends it at once.
  2. Each TEST: applies a plan (MHz + mV, PV follows mV), checks the miner really took it,
     waits --settle-min, then watches every chip for --hold-min minutes.
     A chip is over its limit when its new errors are more than a clean chip would make by chance:
     errors are random (Poisson) around expected = that chip's baseline rate x the hold time x
     (test MHz / baseline MHz) (a faster clock does more work, so it makes proportionally more errors
     at the same error rate per hash; --no-clock-scale turns that off). Because every chip is checked,
     the false-fail chance (5 % per test at --sigma 2.5) is shared out between them, so a clean miner
     with 184 chips doesn't fail by luck every few tests. The same check runs on each board's total. A board reset also fails the test, and so does a
     hashrate more than --hash-tol below what the clock should give.
     A test FAILS the moment anything goes over, so bad settings end early.
  3. VOLTAGE CURVE: every clock from --min-mhz (default 100 MHz below stock) up to --max-mhz
     in --step steps, each at its own lowest clean voltage. A clock starts at the voltage that
     was lowest one clock slower (the lowest at stock voltage), goes up --mv-step at a time
     until it's clean (up to --max-mv), then down until it fails (to --min-mv; two fails in a
     row, or one where a slower clock failed too). Nothing is assumed from another clock. The
     curve ends at the first clock that isn't clean at any allowed voltage; the fastest clean
     clock is the best setting. So the curve covers underclocking and overclocking alike.
  4. A failed setting is retested once (--retries) unless it went over its whole limit in
     under half the test (not a fluke); --retest-all retests those too.
  5. CONFIRM: the best setting is tested once more, for longer. If that fails it falls
     back to the next best passing setting and confirms that instead.
  6. It leaves the miner on the confirmed best plan. If nothing was confirmed, or the run
     stops early (Ctrl+C, a safety stop, an error before a confirm), the miner goes back to
     the setting it ran before the run, which is not necessarily stock.

Confirm mode (--confirm-mhz, --confirm-mv, --confirm-hours): after the baseline it
skips the search and runs that one setting for hours, in --hold-min rounds judged the
same way (each failed round gets one retest). It ends with a CONFIRMED or REJECTED
row, and leaves the miner on the setting if confirmed, otherwise on what it ran before.

PV is never tuned on its own: it always sits a fixed gap above V. By default that is the
gap the miner already has (9100 mV / PV 9400 on a stock SC Lite, so +300); --pv-offset
sets it yourself.

Fans: with --fan-hold-c 0 (the default) fans are never changed, so a fan manager can keep
running alongside. With --fan-hold-c 60 the tuner takes over the fans and holds the hottest
board at 60 C for the whole run (never below --fan-min %), so every setting is judged at the
same temperature, and it records how much fan each setting needed.

Presets (--presets, search mode): after the curve it builds and tests four presets from it,
each held at its own temperature (High at --fan-hold-c, the others warmer, up to --preset-max-c)
for --preset-min minutes: High is the confirmed best setting, Lowest power the curve's lowest
clock (or --preset-min-mhz), and Middle and Low are spread evenly between them, each at the
lowest clean voltage the curve measured at its clock. A preset that fails gets more voltage,
then a lower clock (up to 3 tries). Each passing preset gets a fan curve built from the fan it
needed. They are written to asic_tuner_presets.json for the dashboard.

Presets only (--presets-only --source FILE, since 1.13): builds and tests the four presets again from
an earlier search's voltage curve, without mapping the curve again, for example at other temperatures
(--preset-temps 60,64,68,72). FILE (written by the dashboard from that search's CSV files) holds the
curve, the setting that was confirmed (High), and every chip's normal error rate from that search's
baseline. With --baseline-min above 0 a fresh baseline runs first (today's normal rates); with
--baseline-min 0 the saved rates are used. High gets its own test (and more voltage or one clock less
if it fails, like the others). The miner ends on what it ran before the run.
If any board reaches --abort-c it puts the baseline plan back and stops.
Close the web dashboard while it runs.

Files written next to this script (flushed after every row):
  asic_tuner_results.csv   one row per finished test
  asic_tuner_live.csv      one row every poll
  asic_tuner_chips.csv     per-chip new errors for every finished test
(An older file with different columns is renamed to *.old-<number>.csv.)

Examples
  python asic_tuner.py --check          # show chip errors once, change nothing
  python asic_tuner.py                  # full run
  python asic_tuner.py --hold-min 20    # quicker tests
  python asic_tuner.py --max-mv 9200    # smaller voltage raises
  python asic_tuner.py --no-boost --no-trim-voltage   # clock only
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import signal
import sys
import time
from typing import Any

import sclite_common as sc

# where the CSV/JSON files go: next to this script, or SCLITE_TUNER_DATA (used by the Umbrel app)
HERE = os.environ.get("SCLITE_TUNER_DATA") or os.path.dirname(os.path.abspath(__file__))
RESULTS_CSV = os.path.join(HERE, "asic_tuner_results.csv")
LIVE_CSV = os.path.join(HERE, "asic_tuner_live.csv")
CHIPS_CSV = os.path.join(HERE, "asic_tuner_chips.csv")
CHIPMAP_JSON = os.path.join(HERE, "asic_tuner_chipmap.json")  # latest per-chip reading
PRESETS_JSON = os.path.join(HERE, "asic_tuner_presets.json")  # presets built by this run
PLAN_JSON = os.path.join(HERE, "asic_tuner_plan.json")        # this run's steps and how far it got

RESULTS_HEADER = ["time", "mhz", "mv", "pv", "result", "reason", "watched_min",
                  "worst_chip", "worst_chip_new", "worst_chip_limit",
                  "all_chips_new", "board_resets", "avg_ths", "max_temp"]
LIVE_HEADER = ["time", "phase", "mhz", "mv", "pv", "elapsed_min", "hold_min",
               "worst_chip", "worst_chip_new", "worst_chip_limit",
               "all_chips_new", "board_resets", "total_ths", "max_temp", "fan", "target_c"]
CHIPS_HEADER = ["time", "mhz", "mv", "pv", "result", "watched_min", "board", "chip",
                "new_hw", "baseline_per_hour", "limit"]


class Abort(Exception):
    """Stop the whole run."""


# ------------------------------------------------------------------- files ---

_checked: set[str] = set()


def append_row(path: str, header: list[str], row: list[Any]) -> None:
    """Append a row, flushing to disk. An old file with other columns is renamed once."""
    if path not in _checked:
        _checked.add(path)
        if os.path.exists(path):
            with open(path, newline="") as f:
                first = next(csv.reader(f), None)
            if first != header:
                os.replace(path, path[:-4] + f".old-{int(time.time())}.csv")
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerow(row)
        f.flush()
        os.fsync(f.fileno())


def write_chipmap(plan: "Plan", phase: str, elapsed_min: float, hold_min: float, judge: bool,
                  gained: dict, limits: dict, base_rate: dict, resets: int, weak: dict | None = None) -> None:
    """Latest per-chip reading for the dashboard's chip map (written atomically)."""
    doc = {"time": now(), "phase": phase, "mhz": plan.mhz, "mv": plan.mv, "pv": plan.pv,
           "elapsed_min": round(elapsed_min, 1), "hold_min": hold_min, "judged": judge,
           "board_resets": resets,
           "chips": [[k[0], k[1], gained[k], round(limits[k], 2) if judge else None,
                      round(base_rate.get(k, 0.0), 2)] for k in sorted(gained)],
           "weak": [[k[0], k[1]] for k in sorted(weak or {})]}
    tmp = CHIPMAP_JSON + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f)
    os.replace(tmp, CHIPMAP_JSON)


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


OFFLINE_S = 300.0   # how long the miner's web API may be unreachable before the run stops


def _with_reply(e: Exception) -> Exception:
    """For an HTTP error, add what the miner said (its reply body) so the log shows the reason."""
    if hasattr(e, "read") and hasattr(e, "code") and not getattr(e, "_b2ac_said", False):
        try:
            said = e.read(300).decode("utf-8", "replace")
        except Exception:
            said = ""
        said = " ".join(said.replace("<", " <").split())[:160]
        out = RuntimeError(f"{e}" + (f" (the miner said: {said})" if said else ""))
        out._b2ac_said = True  # type: ignore[attr-defined]
        return out
    return e


def patient(fn: Any, what: str) -> Any:
    """Call fn(); if the miner's web API is briefly down (it restarts under load), wait and retry."""
    start: float | None = None
    wait = 5.0
    while True:
        try:
            out = fn()
            if start is not None:
                log(f"miner answering again after {time.time() - start:.0f} s")
            return out
        except (Abort, KeyboardInterrupt):
            raise
        except Exception as e:
            now_t = time.time()
            e = _with_reply(e)
            if start is None:
                start = now_t
                log(f"miner not answering ({what}: {str(e)[:220]}); "
                    f"waiting up to {OFFLINE_S / 60:g} min")
            if now_t - start >= OFFLINE_S:
                raise RuntimeError(f"miner unreachable for {OFFLINE_S / 60:g} min ({what}): {e}")
            time.sleep(wait)
            wait = min(30.0, wait * 2)


# ----------------------------------------------------------------- reading ---

Key = tuple[int, int]          # (board, chip)
Chips = dict[Key, int]         # -> hardware error count


def read_icinfo() -> tuple[Chips, dict[int, int]]:
    return patient(_read_icinfo, "chip counters")


def _read_icinfo() -> tuple[Chips, dict[int, int]]:
    """Per-chip hardware errors and per-board reset counters from /dbg/icinfo."""
    raw: Any = sc.api("GET", "/dbg/icinfo")
    if isinstance(raw, dict) and isinstance(raw.get("body"), str):
        raw = json.loads(raw["body"])
    elif isinstance(raw, str):
        raw = json.loads(raw)
    if isinstance(raw, dict) and isinstance(raw.get("body"), dict) and "drawdata" not in raw:
        raw = raw["body"]      # the SC Box / HS Box send it as an object, the SC Lite as text
    if not isinstance(raw, dict) or not isinstance(raw.get("drawdata"), list):
        raise RuntimeError(f"unexpected /dbg/icinfo response: {str(raw)[:300]}")
    chips: Chips = {}
    for b, board in enumerate(raw["drawdata"]):
        for chip in board or []:
            idx = int(chip.get("chipindex", chip.get("nr", 0) + 1))
            chips[(b, idx)] = int(chip.get("hwerr") or 0)
    resets: dict[int, int] = {}
    for b, row in enumerate(raw.get("tabledata") or []):
        resets[b] = int(row.get("reboot") or 0)
    if not chips:
        raise RuntimeError("/dbg/icinfo returned no chips")
    return chips, resets


def up_from(mhz: int, step: int) -> int:
    """The first clock above mhz on the step grid (615 with 25 MHz steps -> 625, 625 -> 650): the miner
    runs clocks in 12.5 MHz steps, so off-grid clocks like 640 would really run at 637.5."""
    return (mhz // step + 1) * step


def down_from(mhz: int, step: int) -> int:
    """The first clock below mhz on the step grid (615 -> 600, 625 -> 600)."""
    return ((mhz - 1) // step) * step


def chip_name(k: Key) -> str:
    return f"b{k[0]}c{k[1]}"


# ----------------------------------------------------------------- writing ---

class Plan:
    def __init__(self, mhz: int, mv: int, pv: int):
        self.mhz, self.mv, self.pv = mhz, mv, pv

    def with_(self, mhz: int | None = None, mv: int | None = None) -> "Plan":
        new_mv = self.mv if mv is None else mv
        return Plan(self.mhz if mhz is None else mhz, new_mv, self.pv + (new_mv - self.mv))

    def text(self) -> str:
        return f"{self.mhz} MHz {self.mv} mV PV {self.pv}"


def current_plan() -> Plan:
    mhz, mv, _a, _b, pv = sc.parse_plan(str(patient(sc.get_setting, "read plan").get("manualPowerplan")))
    return Plan(mhz, mv, pv)


def running_state() -> tuple[Plan, dict, str, bool]:
    """What the miner really runs before a run: (plan, the raw fields to put back, a description).
    manual on: manualPowerplan. manual off: the selected firmware level (its stock plan, or its Idle mode),
    and manualPowerplan may hold an old setting that isn't running at all, so putting the miner "back" on
    it would be wrong: the raw fields (manual, select, manualPowerplan) are what goes back then."""
    s = patient(sc.get_setting, "read plan")
    raw = {k: s.get(k) for k in ("manual", "select", "manualPowerplan")}
    if s.get("manual"):
        mhz, mv, _a, _b, pv = sc.parse_plan(str(s.get("manualPowerplan")))
        return Plan(mhz, mv, pv), raw, "its own manual setting", True
    try:
        sel = int(s.get("select") or 0)
    except (TypeError, ValueError):
        sel = 0
    plans = [p for p in (s.get("powerplans") or []) if isinstance(p, dict)]
    info = next((str(p.get("info") or "") for p in plans if str(p.get("level")) == str(sel)), "")
    if not info and 0 <= sel < len(plans):
        info = str(plans[sel].get("info") or "")
    try:
        mhz, mv, _a, _b, pv = sc.parse_plan(info)
        return Plan(mhz, mv, pv), raw, f"the firmware's level {sel} plan", mhz > 0
    except Exception:
        # its Idle mode ("0 MHz 0 V ..."): no clock to tune from; the manual plan stands in for the baseline
        mhz, mv, _a, _b, pv = sc.parse_plan(str(s.get("manualPowerplan")))
        return Plan(mhz, mv, pv), raw, f"the firmware's level {sel} plan ({info or 'unknown'})", False


def restore_raw(raw: dict) -> None:
    """Put the miner back on exactly the fields it had before the run (manual, select, manualPowerplan)."""
    def write() -> None:
        payload = dict(sc.get_setting())
        payload.update(raw)
        sc.put_setting(payload)
    patient(write, "put back the miner's own setting")


def bfg_devs() -> list[dict]:
    """The boards from the miner's port-4028 API (no login): clock and voltage as the boards report them."""
    import socket
    ip = os.environ.get("SCLITE_IP", "")
    with socket.create_connection((ip, 4028), timeout=8) as s:
        s.sendall(json.dumps({"command": "devs"}).encode())
        raw = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            raw += chunk
    text = raw.decode("utf-8", "replace").strip("\x00\n ")
    return list((json.loads(text[text.find("{"):text.rfind("}") + 1]) or {}).get("DEVS") or [])


def _uptime() -> float | None:
    """The mining software's uptime from port 4028 (the longest board's Device Elapsed), or None."""
    try:
        v = [float(d.get("Device Elapsed")) for d in bfg_devs() if d.get("Device Elapsed") is not None]
        return max(v) if v else None
    except Exception:
        return None


def pll(mhz: float) -> float:
    """The clock the chips really run: the miner sets clocks in 12.5 MHz steps (rounding down)."""
    return int(mhz / 12.5) * 12.5


def not_applied(plan: Plan) -> tuple[str, str]:
    """(hard, soft): hard = the miner's setting isn't the plan; soft = a board reports another clock."""
    try:
        cur = current_plan()
        if (cur.mhz, cur.mv, cur.pv) != (plan.mhz, plan.mv, plan.pv):
            return f"the miner's setting reads {cur.text()}", ""
    except Exception as e:
        return f"couldn't read the setting back ({e})", ""
    try:
        devs = bfg_devs()
    except Exception:
        return "", ""                   # no port-4028 data: the setting read-back has to do
    want = pll(plan.mhz)
    off = [f"board {i} at {float(d.get('clock') or 0):g} MHz" for i, d in enumerate(devs)
           if d.get("clock") is not None and abs(float(d["clock"]) - want) > 1.0 and abs(float(d["clock"]) - plan.mhz) > 1.0]
    return "", ("boards report another clock: " + ", ".join(off)) if off else ""


def apply_plan(plan: Plan) -> None:
    """Write MHz / V / PV; the fan fields are copied from the live setting untouched."""
    def write() -> None:
        payload = dict(sc.get_setting())
        _m, _v, fan_a, fan_b, _pv = sc.parse_plan(str(payload.get("manualPowerplan")))
        payload["manual"] = True
        payload["manualPowerplan"] = sc.build_plan(plan.mhz, plan.mv, fan_a, fan_b, plan.pv)
        payload["select"] = 0
        sc.put_setting(payload)
    patient(write, f"apply {plan.text()}")


class FanHold:
    """Holds the hottest board at a target temperature by setting the fan (PI control).

    Called once per reading (every --poll-s). The stock firmware slowly walks the fan back,
    so the value is re-sent at least every RESEND_S even when it hasn't changed."""
    KP = 4.0        # fan % per degree above target
    KI = 0.6        # fan % added per reading per degree (slowly removes a steady offset)
    RESEND_S = 60.0

    def __init__(self, target: float, fan_min: int, fan_max: int = 100):
        self.target = float(target)
        self.fan_min, self.fan_max = int(fan_min), int(fan_max)
        self.base: float | None = None
        self.i = 0.0
        self.fan: int | None = None
        self.sent = 0.0
        self.samples: list[int] = []

    def set_target(self, target: float) -> None:
        if float(target) != self.target:
            log(f"fans: now holding {target:g} C")
        self.target = float(target)

    def tick(self, temp: float | None) -> None:
        if temp is None:
            return
        if self.base is None:  # start from the fan the miner is on now
            _m, _v, fa, _fb, _pv = sc.parse_plan(str(patient(sc.get_setting, "read fan").get("manualPowerplan")))
            self.base = float(max(self.fan_min, min(self.fan_max, fa or 70)))
            self.fan = int(self.base)
        err = temp - self.target
        if abs(err) > 0.5:
            self.i = max(-60.0, min(60.0, self.i + self.KI * err))
        want = self.base + self.KP * err + self.i
        cur = self.fan if self.fan is not None else int(self.base)
        if err >= 8:
            want = self.fan_max            # far too hot: full fan now (not limited to one step up)
        else:
            step = 20 if err > 3 else 10   # faster up than down
            want = max(cur - 10, min(cur + step, want))
        want = int(round(max(self.fan_min, min(self.fan_max, want))))
        if want != self.fan or time.time() - self.sent >= self.RESEND_S:
            self.send(want)
        self.samples.append(self.fan if self.fan is not None else want)

    def send(self, fan: int) -> None:
        def write() -> None:
            payload = dict(sc.get_setting())
            m, v, _fa, _fb, pv = sc.parse_plan(str(payload.get("manualPowerplan")))
            payload["manual"] = True
            payload["manualPowerplan"] = sc.build_plan(m, v, fan, fan, pv)
            payload["select"] = 0
            sc.put_setting(payload)
        patient(write, f"set fan {fan}")
        self.fan, self.sent = fan, time.time()


# ----------------------------------------------------------------- testing ---

def poisson_limit(lam: float, p: float) -> int:
    """The largest count k with P(X > k) >= p for a Poisson count with mean lam: more than k errors
    would happen by chance less than p of the time."""
    lam = max(0.0, lam)
    if lam > 400:          # normal approximation (and no underflow) for big totals
        z = math.sqrt(2) * _erfcinv(2 * p)
        return int(math.ceil(lam + z * math.sqrt(lam)))
    term = math.exp(-lam)  # P(X = 0)
    cdf, k = term, 0
    while 1 - cdf >= p and k < 100000:
        k += 1
        term *= lam / k
        cdf += term
    return k


def _erfcinv(y: float) -> float:
    lo, hi = 0.0, 10.0     # erfc is decreasing; bisect (only used for very large counts)
    for _ in range(100):
        mid = (lo + hi) / 2
        if math.erfc(mid) > y:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


class Tester:
    def __init__(self, a: argparse.Namespace):
        self.a = a
        self.base_rate: dict[Key, float] = {}   # errors per hour per chip at baseline
        self.base_mhz = 0                          # the baseline clock (limits scale with MHz / this)
        self.weak: dict[Key, float] = {}           # chips already noisy at the baseline -> times the median rate
        self.base_ths = 0.0                        # hashrate during the baseline (a pass must hash in proportion)
        self.applied_ok = 0
        self.notes: list[str] = []
        self.fans = FanHold(a.fan_hold_c, a.fan_min) if a.fan_hold_c > 0 else None

    def false_fail(self) -> float:
        """The chance a clean test still fails somewhere by bad luck (5 % at the default sensitivity 2.5;
        a lower sensitivity is stricter: 2.0 gives about 18 %, 3.0 about 1 %)."""
        tail = lambda z: 0.5 * math.erfc(z / math.sqrt(2))
        return min(0.5, 0.05 * tail(self.a.sigma) / tail(2.5))

    def limit(self, rate_per_hour: float, hours: float, scale: float = 1.0, weak: bool = False,
              n: int = 1) -> float:
        """The most errors allowed in the whole test. Errors are random (Poisson), and every one of the n
        chips (or boards) is checked, so each gets its share of the false-fail chance: a clean miner with
        184 chips then fails a test by luck only about 1 time in 20, not every few tests."""
        e = rate_per_hour * hours * scale
        if weak:   # an already-noisy chip: only fail when it gets clearly worse than its own normal
            e *= self.a.weak_factor
        return max(float(self.a.margin), float(poisson_limit(e, self.false_fail() / max(1, n))))

    def chip_limit(self, k: Key, hours: float, scale: float, n: int = 1) -> float:
        return self.limit(self.base_rate.get(k, 0.0), hours, scale, weak=k in self.weak, n=n)

    NO_TEMP_LIMIT = 10      # readings in a row without any temperature before the run stops (about 5 min)

    def heat(self) -> dict:
        snap = patient(sc.board_snapshot, "temperatures")
        t = snap.get("max_t")
        if t is None:
            # no temperature means no heat abort and no fan hold: don't keep testing blind
            self._no_t = getattr(self, "_no_t", 0) + 1
            if self._no_t >= self.NO_TEMP_LIMIT:
                raise Abort(f"the miner reported no board temperature for {self._no_t} readings in a row")
        else:
            self._no_t = 0
        if t is not None and t >= self.a.abort_c:
            raise Abort(f"board reached {t:.1f} C (abort at {self.a.abort_c} C)")
        if self.fans:
            self.fans.tick(t)
        return snap

    def settle(self, plan: Plan, minutes: float | None = None) -> None:
        apply_plan(plan)
        end = time.time() + (self.a.settle_min if minutes is None else minutes) * 60
        while time.time() < end:
            self.heat()
            time.sleep(min(self.a.poll_s, max(0.0, end - time.time())))
        # judge only what the miner is really running: check the setting and every board's clock
        hard, soft = not_applied(plan)
        if hard or soft:
            log(f"  {plan.text()} not in effect yet ({hard or soft}); applying it again")
            apply_plan(plan)
            for _ in range(6):
                time.sleep(10)
                self.heat()
                hard, soft = not_applied(plan)
                if not (hard or soft):
                    break
            if hard:
                raise Abort(f"the miner didn't take {plan.text()}: {hard}")
            if soft:   # the setting is right; a board's reported clock may just lag: note it, carry on
                log(f"  WARNING: {plan.text()} is set, but {soft}")
                self.notes.append(f"{plan.text()}: {soft}")
        self.applied_ok += 1

    def watch(self, plan: Plan, phase: str, hold_min: float, judge: bool) -> tuple[bool, str, dict]:
        """Watch for hold_min. With judge=False nothing can fail (baseline)."""
        a = self.a
        prev, prev_resets = read_icinfo()
        gained: dict[Key, int] = {k: 0 for k in prev}
        board_resets = 0
        hr: list[float] = []
        max_t = 0.0
        start = time.time()
        hold_h = hold_min / 60.0
        if self.fans:
            self.fans.samples = []
        # more clock = more hashes = more errors at the same per-hash error rate
        scale = 1.0 if (a.no_clock_scale or not self.base_mhz) else max(0.5, plan.mhz / self.base_mhz)
        limits = {k: self.chip_limit(k, hold_h, scale, n=len(gained)) for k in gained}
        # a board's total is judged on its normal chips (noisy chips are judged on their own)
        boards_ = {k[0] for k in gained}
        board_limits = {
            b: self.limit(sum(self.base_rate.get(k, 0.0) for k in gained if k[0] == b and k not in self.weak), hold_h, scale,
                          n=len(boards_))
            for b in boards_}

        up0 = _uptime()
        born0 = time.time() - up0 if up0 is not None else None   # when the mining software last started
        while True:
            time.sleep(a.poll_s)
            snap = self.heat()
            up = _uptime()
            # compare the implied start time, not the uptime: a restart behind a long outage can already show
            # more uptime than the last reading, but it started later
            if up is not None and born0 is not None and (time.time() - up) > born0 + 90:
                # the mining software restarted: its error counters started again from zero, so the
                # test can't be judged clean (and a setting that crashes the miner isn't one to keep)
                return False, f"the miner restarted during the test (its mining software has been up only {up:.0f} s)", {
                    "gained": dict(gained), "limits": dict(limits), "worst": max(gained, key=lambda k: gained[k]) if gained else None,
                    "total": sum(gained.values()), "resets": board_resets, "watched_min": (time.time() - start) / 60.0,
                    "hold_min": hold_min, "avg_ths": (sum(hr) / len(hr)) if hr else 0.0, "max_t": max_t,
                    "avg_fan": None, "target_c": self.fans.target if self.fans else None, "decisive": False}
            if up is not None:
                up0 = up
                if born0 is None:
                    born0 = time.time() - up
            cur, cur_resets = read_icinfo()
            for k, v in cur.items():
                p = prev.get(k, 0)
                gained[k] = gained.get(k, 0) + (v - p if v >= p else v)  # lower = counters reset
                limits.setdefault(k, self.chip_limit(k, hold_h, scale))
            for b, r in cur_resets.items():
                if r > prev_resets.get(b, r):
                    board_resets += r - prev_resets[b]
            prev, prev_resets = cur, cur_resets

            elapsed_min = (time.time() - start) / 60.0
            hr.append(sum(x.get("hr_ths") or 0 for x in snap.get("boards", [])))
            max_t = max(max_t, snap.get("max_t") or 0.0)
            worst = max(gained, key=lambda k: gained[k] - limits[k]) if judge \
                else max(gained, key=lambda k: gained[k])
            total = sum(gained.values())
            fs = self.fans.samples if self.fans else []
            late = fs[len(fs) // 2:] or fs          # second half: after the fan has settled
            stats = {"gained": dict(gained), "limits": dict(limits), "worst": worst,
                     "total": total, "resets": board_resets, "watched_min": elapsed_min, "hold_min": hold_min,
                     "avg_ths": sum(hr) / len(hr), "max_t": max_t,
                     "avg_fan": round(sum(late) / len(late)) if late else None,
                     "target_c": self.fans.target if self.fans else None}

            write_chipmap(plan, phase, elapsed_min, hold_min, judge, gained, limits,
                          self.base_rate, board_resets, self.weak)
            append_row(LIVE_CSV, LIVE_HEADER, [
                now(), phase, plan.mhz, plan.mv, plan.pv, f"{elapsed_min:.1f}", f"{hold_min:g}",
                chip_name(worst), gained[worst], f"{limits[worst]:.1f}" if judge else "",
                total, board_resets, f"{hr[-1]:.3f}", snap.get("max_t"),
                self.fans.fan if self.fans else "", f"{self.fans.target:g}" if self.fans else ""])
            top = sorted(gained, key=lambda k: -gained[k])[:3]
            log(f"  {phase} {elapsed_min:5.1f}/{hold_min:g} min  {hr[-1]:.2f} TH/s  "
                f"maxT={snap.get('max_t')}"
                + (f" fan={self.fans.fan}%->{self.fans.target:g}C" if self.fans else "")
                + f"  all chips +{total}  top: "
                + ", ".join(f"{chip_name(k)} +{gained[k]}" for k in top))

            if judge:
                # over the whole test's limit before half the test: at least twice the allowed rate,
                # so a retest would only repeat it (it's not a fluke)
                stats["decisive"] = elapsed_min <= 0.5 * hold_min
                if gained[worst] > limits[worst]:
                    return False, (f"chip {chip_name(worst)} +{gained[worst]} errors "
                                   f"(limit {limits[worst]:.1f})"), stats
                for b, lim in sorted(board_limits.items()):
                    g = sum(v for k, v in gained.items() if k[0] == b and k not in self.weak)
                    if g > lim:
                        return False, f"board {b} +{g} errors (limit {lim:.1f})", stats
                stats["decisive"] = False
            if board_resets:
                # also during the baseline: a board that resets there won't get better by waiting
                return False, "board reset", stats
            if elapsed_min >= hold_min:
                if judge and self.base_ths > 0 and self.base_mhz and a.hash_tol > 0:
                    want = self.base_ths * pll(plan.mhz) / pll(self.base_mhz)
                    got = stats["avg_ths"]
                    if got < want * (1 - a.hash_tol):
                        return False, (f"no chip over its limit, but hashrate {got:.2f} TH/s is "
                                       f"{(1 - got / want) * 100:.0f}% below the {want:.2f} this clock should give"), stats
                hard, _soft = not_applied(plan)
                if hard:   # something changed the setting during the test: what was watched isn't this plan
                    return False, f"the setting changed during the test ({hard})", stats
                return True, "clean", stats

    def baseline(self, plan: Plan) -> None:
        a = self.a
        log(f"BASELINE {plan.text()} for {a.baseline_min:g} min (records each chip's normal rate)")
        self.settle(plan, max(a.settle_min, a.fan_settle_min) if self.fans else None)  # time for the fan to reach the target
        _ok, _why, st = self.watch(plan, "baseline", a.baseline_min, judge=False)
        if st["resets"]:
            record(plan, "BASELINE", f"board reset after {st['watched_min']:.0f} min", st, {})
            raise Abort(f"a board reset {st['watched_min']:.1f} min into the baseline at {plan.mhz} MHz / "
                        f"{plan.mv} mV; this miner can't run its baseline. Pick a baseline it runs "
                        "steadily (e.g. its current setting) and try again")
        hours = max(st["watched_min"], 1e-6) / 60.0
        # +1 pseudo-error per chip: a chip that happened to show 0 in a short
        # baseline is not assumed to be perfect (avoids false fails later)
        self.base_rate = {k: (g + a.pseudo) / hours for k, g in st["gained"].items()}
        self.base_mhz = plan.mhz
        self.base_ths = float(st.get("avg_ths") or 0)
        self._find_weak()
        record(plan, "BASELINE", f"{st['total']} errors across all chips", st, self.base_rate)
        noisy = sorted(self.base_rate, key=lambda k: -self.base_rate[k])[:5]
        log("baseline done. Noisiest chips (errors/hour): "
            + ", ".join(f"{chip_name(k)} {self.base_rate[k]:.0f}" for k in noisy))

    def load_baseline(self, rates: dict[Key, float], base_mhz: int, base_ths: float, when: str) -> None:
        """Use an earlier search's baseline (each chip's normal rate) instead of running one."""
        self.base_rate = dict(rates)
        self.base_mhz = int(base_mhz)
        self.base_ths = float(base_ths or 0)
        self._find_weak()
        noisy = sorted(self.base_rate, key=lambda k: -self.base_rate[k])[:5]
        log(f"BASELINE reused from {when} ({len(self.base_rate)} chips, {base_mhz} MHz). Noisiest chips (errors/hour): "
            + ", ".join(f"{chip_name(k)} {self.base_rate[k]:.0f}" for k in noisy))

    def _find_weak(self) -> None:
        a = self.a
        # chips that are already far noisier than the rest (a weak or damaged chip): their errors
        # hardly depend on clock or voltage, so judging them like the others makes every step
        # fail on their random ups and downs. They're judged on getting clearly worse instead.
        self.weak = {}
        if a.weak_x > 0 and self.base_rate:
            rates = sorted(self.base_rate.values())
            med = max(rates[len(rates) // 2], 1e-6)
            floor = max(a.weak_x * med, a.weak_min)
            self.weak = {k: r / med for k, r in self.base_rate.items() if r >= floor}
            if self.weak:
                log(f"noisy chips (at least {a.weak_x:g}x the median {med:.1f}/h): "
                    + ", ".join(f"{chip_name(k)} {self.base_rate[k]:.0f}/h ({x:.0f}x)" for k, x in sorted(self.weak.items(), key=lambda kv: -kv[1]))
                    + f"; they fail a step only above {a.weak_factor:g}x their own normal rate")

    def test(self, plan: Plan, hold: float | None = None) -> tuple[bool, str, dict]:
        hold = hold or self.a.hold_min
        log(f"TEST {plan.text()}  (settle {self.a.settle_min:g} min, hold {hold:g} min)")
        self.settle(plan)
        return self.watch(plan, "test", hold, judge=True)


def record(plan: Plan, result: str, reason: str, st: dict, base_rate: dict[Key, float]) -> None:
    w = st["worst"]
    is_base = result == "BASELINE"
    t = now()
    known = w is not None and w in st["gained"]
    append_row(RESULTS_CSV, RESULTS_HEADER, [
        t, plan.mhz, plan.mv, plan.pv, result, reason, f"{st['watched_min']:.1f}",
        chip_name(w) if known else "", st["gained"][w] if known else "",
        "" if is_base or not known else f"{st['limits'][w]:.1f}",
        st["total"], st["resets"], f"{st['avg_ths']:.3f}", f"{st['max_t']:.1f}"])
    for k in sorted(st["gained"]):
        append_row(CHIPS_CSV, CHIPS_HEADER, [
            t, plan.mhz, plan.mv, plan.pv, result, f"{st['watched_min']:.1f}", k[0], k[1],
            st["gained"][k], f"{base_rate.get(k, 0.0):.1f}",
            "" if is_base else f"{st['limits'][k]:.1f}"])


# ---------------------------------------------------------------- progress ---

class Progress:
    """Every step of the run, written to asic_tuner_plan.json after each change so the Tuner
    page can tick them off. A step (and each clock / voltage / preset in it) is one of:
    todo (not done yet), run (going now), pass, fail, skip (not needed), stop (cut short)."""

    def __init__(self, run_id: str):
        self.doc: dict[str, Any] = {"run": run_id, "started": now(), "finished": None,
                                    "outcome": None, "stages": []}
        self.stage = ""            # the step tests are counted under right now
        self._cur: dict | None = None

    def add(self, sid: str, title: str, detail: str = "") -> dict:
        st = {"id": sid, "title": title, "detail": detail, "state": "todo", "note": "", "items": []}
        self.doc["stages"].append(st)
        return st

    def get(self, sid: str) -> dict | None:
        return next((s for s in self.doc["stages"] if s["id"] == sid), None)

    def item(self, sid: str, key: Any, label: str, detail: str = "") -> dict:
        st = self.get(sid)
        if st is None:
            st = self.add(sid, sid)
        it = next((i for i in st["items"] if i["key"] == str(key)), None)
        if it is None:
            it = {"key": str(key), "label": label, "detail": detail, "state": "todo", "note": "", "attempts": []}
            st["items"].append(it)
        return it

    def set(self, sid: str, state: str, note: str | None = None) -> None:
        st = self.get(sid)
        if st is not None:
            st["state"] = state
            if note is not None:
                st["note"] = note
            self.write()

    def _key(self, plan: "Plan", key: Any = None) -> tuple[Any, str]:
        sid = self.stage
        if key is None:
            key = plan.mhz if sid in ("climb", "map", "curve") else plan.mv if sid == "trim" else f"{plan.mhz}/{plan.mv}"
        return key, (f"{plan.mhz} MHz" if sid in ("climb", "map", "curve") else f"{plan.mv} mV" if sid == "trim"
                     else f"{plan.mhz} MHz · {plan.mv} mV")

    def cached(self, plan: "Plan", ok: bool) -> None:
        """A setting already tested earlier in the run (not tested again)."""
        key, label = self._key(plan)
        it = self.item(self.stage, key, label)
        it["state"] = "pass" if ok else "fail"
        it["note"] = f"{plan.mhz} MHz · {plan.mv} mV already {'passed' if ok else 'failed'} earlier in this run"
        self.write()

    def begin(self, plan: "Plan", label: str = "", key: Any = None, item_label: str = "") -> None:
        """A test starts: mark its step and item as going, and add the attempt."""
        sid = self.stage
        key, default = self._key(plan, key)
        it = self.item(sid, key, item_label or default)
        it["state"] = "run"
        a = {"mhz": plan.mhz, "mv": plan.mv, "pv": plan.pv, "label": label.strip(" ()") or "test",
             "state": "run", "why": "", "started": now()}
        it["attempts"].append(a)
        self._cur = a
        st = self.get(sid)
        if st and st["state"] in ("todo", "pass", "fail"):
            st["state"] = "run"
        self.write()

    def end(self, ok: bool, why: str) -> None:
        a = self._cur
        if a is None:
            return
        a["state"], a["why"], a["ended"] = ("pass" if ok else "fail"), why, now()
        st = self.get(self.stage)
        for it in (st or {}).get("items", []):
            if a in it["attempts"]:
                it["state"] = a["state"]
        self._cur, self._last = None, a
        self.write()

    def note_last(self, text: str) -> None:
        """Add a remark to the attempt that just ended (shown in the Run plan)."""
        a = getattr(self, "_last", None)
        if a is not None:
            a["why"] = (a.get("why") + "; " if a.get("why") else "") + text
            self.write()

    def skip_rest(self, sid: str, note: str) -> None:
        st = self.get(sid)
        for it in (st or {}).get("items", []):
            if it["state"] == "todo":
                it["state"], it["note"] = "skip", note
        self.write()

    def finish(self, outcome: str, note: str) -> None:
        """End of the run: anything still going was cut short, anything not started is skipped."""
        cut = outcome != "done"
        for st in self.doc["stages"]:
            for it in st["items"]:
                for a in it["attempts"]:
                    if a["state"] == "run":
                        a["state"], a["why"] = "stop", note
                if it["state"] == "run":
                    it["state"] = "stop"
                elif it["state"] == "todo" and cut:
                    it["state"] = "skip"
            if st["state"] == "run":
                st["state"] = "stop" if cut else "pass"
            elif st["state"] == "todo" and cut and st["id"] != "finish":
                st["state"] = "skip"
        self.doc["finished"], self.doc["outcome"] = now(), outcome
        self.write()

    def write(self) -> None:
        try:
            tmp = PLAN_JSON + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.doc, f, indent=1)
            os.replace(tmp, PLAN_JSON)
        except OSError:
            pass   # the page just won't update; never stop a run over this


PROG = Progress("")


# -------------------------------------------------------------------- main ---

# ----------------------------------------------------------------- presets ---

PRESET_ORDER = [("high", "High"), ("middle", "Middle"), ("low", "Low"), ("lowest", "Lowest power")]


def preset_curve(target: float, fan: int | None, fan_min: int, abort_c: float) -> list[dict] | None:
    """A fan curve through (target, fan it needed), steeper above, gentle below."""
    if fan is None:
        return None
    f = max(fan_min, min(100, int(fan)))
    pts = [(target - 10, f - 20), (target - 4, f - 8), (target, f),
           (target + 4, f + 15), (min(target + 8, abort_c - 2), 100)]
    out, last_f = [], 0
    for t, v in pts:
        v = int(round(max(fan_min, min(100, v))))
        v = max(v, last_f)
        t = int(round(max(20, min(100, t))))
        if out and t <= out[-1]["temp"]:
            continue
        out.append({"temp": t, "fan": v})
        last_f = v
    return out


def write_presets(doc: dict) -> None:
    tmp = PRESETS_JSON + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=1)
    os.replace(tmp, PRESETS_JSON)


def curve_clocks(a: argparse.Namespace, base: "Plan") -> list[int]:
    """The clocks the curve tests: from --min-mhz (default 100 MHz below stock), on the step grid, up to --max-mhz."""
    lo = a.min_mhz or (base.mhz - 100)
    lo = max(300, (lo // a.step) * a.step)
    clocks = list(range(lo, a.max_mhz + 1, a.step))
    if clocks and clocks[-1] != a.max_mhz and a.max_mhz > lo:
        clocks.append(a.max_mhz)          # Highest MHz off the step grid: it's still the last clock tested
    return clocks


def build_presets(a: argparse.Namespace, tester: "Tester", base: Plan, best: Plan,
                  passed: list[Plan], last_stats: dict, gap: int) -> None:
    """Build and test High / Middle / Low / Lowest power, each held at its own temperature."""
    hold = a.fan_hold_c if tester.fans else None
    warm = max(hold, min(a.preset_max_c, a.abort_c - 12)) if hold else None
    temps = {}
    for i, (key, _l) in enumerate(PRESET_ORDER):
        temps[key] = round(hold + (warm - hold) * i / 3) if hold else None
    if hold and getattr(a, "preset_temps", None):
        temps = {key: float(t) for (key, _l), t in zip(PRESET_ORDER, a.preset_temps)}

    pmin = a.preset_min_mv or a.min_mv     # the lowest voltage a preset may use

    def mk(mhz: int, mv: int) -> Plan:
        mv = max(pmin, min(a.max_mv, mv))
        return Plan(max(300, mhz), mv, mv + gap)

    def low_mv(mhz: int) -> int | None:
        # the lowest voltage that was tested and passed at this clock (nothing assumed from other clocks)
        mvs = [p.mv for p in passed if p.mhz == mhz]
        return min(mvs) if mvs else None

    lowest_clock = a.preset_min_mhz or curve_clocks(a, base)[0]
    if lowest_clock < best.mhz:
        # a chosen Lowest power clock: Middle and Low are spread evenly between High and it,
        # with voltages spread the same way (or the lowest that passed at that clock)
        lo_mhz = lowest_clock
        r = lambda x, s: int(round(x / s) * s)
        def between(f: float) -> Plan:
            m = r(best.mhz - (best.mhz - lo_mhz) * f, a.step)
            v = low_mv(m) or r(best.mv - (best.mv - pmin) * f, a.mv_step)
            return mk(m, v)
        mid, low = between(1 / 3), between(2 / 3)
        lo_v = low_mv(lo_mhz) or pmin               # mapped: its lowest clean voltage
        lowest = mk(lo_mhz, lo_v)
        lowest_tries = [lowest, mk(lo_mhz, lo_v + a.mv_step), mk(lo_mhz, lo_v + 2 * a.mv_step)]
    else:
        mid = mk(best.mhz - a.step, low_mv(best.mhz - a.step) or best.mv)
        low_mhz = best.mhz - 2 * a.step
        # mapped clocks already have their lowest clean voltage; otherwise try one step below what's known
        low = (mk(low_mhz, low_mv(low_mhz)) if low_mv(low_mhz)
               else mk(low_mhz, (low_mv(low_mhz) or mid.mv) - a.mv_step))
        lowest_mhz = min(down_from(base.mhz, a.step), low_mhz - a.step)
        lowest = mk(lowest_mhz, pmin)
        lowest_tries = [lowest, mk(lowest.mhz - a.step, pmin), mk(lowest.mhz - a.step, pmin + a.mv_step)]
    tries = {
        # presets only: High wasn't just confirmed at its temperature, so it may need help like the others
        "high": ([best, mk(best.mhz, best.mv + a.mv_step),
                  mk(best.mhz - a.step, low_mv(best.mhz - a.step) or best.mv)]
                 if getattr(a, "presets_only", False) else [best]),
        # a preset that fails gets more voltage, then one clock step less at that clock's measured voltage
        "middle": [mid, mk(mid.mhz, mid.mv + a.mv_step),
                   mk(mid.mhz - a.step, low_mv(mid.mhz - a.step) or mid.mv + a.mv_step)],
        "low": [low, mk(low.mhz, low.mv + a.mv_step),
                mk(low.mhz - a.step, low_mv(low.mhz - a.step) or low.mv + a.mv_step)],
        "lowest": lowest_tries,
    }
    doc = {"run": a.run_id, "created": now(), "complete": False, "fan_hold_c": hold,
           "fan_min": a.fan_min, "preset_min": a.preset_min, "presets": {},
           "mode": "presets" if getattr(a, "presets_only", False) else "search",
           "source": getattr(a, "source_label", "") or None}
    write_presets(doc)
    log(f"PRESETS: testing each for {a.preset_min:g} min"
        + (f", held at {', '.join(f'{temps[k]} C' for k, _ in PRESET_ORDER)}" if hold else ""))

    for key, label in PRESET_ORDER:
        target = temps[key]
        if tester.fans and target is not None:
            tester.fans.set_target(target)
        entry: dict[str, Any] = {"label": label, "ok": False, "target_c": target}
        pit = PROG.item("presets", key, label)
        if target is not None:
            pit["detail"] = f"held at {target} °C"
        # High was just confirmed at the hold temperature: reuse that test
        st0 = last_stats.get((best.mhz, best.mv))
        if key == "high" and not a.no_confirm and st0 and st0.get("why") == "clean" and best is not base:
            plan, ok, st = best, True, st0
            pit["attempts"].append({"mhz": best.mhz, "mv": best.mv, "pv": best.pv, "label": "confirm test",
                                    "state": "pass", "why": "reuses the confirm test", "started": now()})
            pit["state"] = "pass"
            PROG.write()
        else:
            seen: set[tuple[int, int]] = set()
            ok, plan, st = False, tries[key][0], {}
            for plan in tries[key]:
                if (plan.mhz, plan.mv) in seen:
                    continue
                seen.add((plan.mhz, plan.mv))
                log(f"PRESET {label}: {plan.text()}" + (f" at {target} C" if target else ""))
                for attempt in range(1 + max(0, a.retries)):
                    tag = " retest" if attempt else ""
                    PROG.begin(plan, "retest" if attempt else "test", key=key, item_label=label)
                    try:
                        tester.settle(plan, max(a.settle_min, a.fan_settle_min if tester.fans else 0))
                        ok, why, st = tester.watch(plan, f"preset {key}", a.preset_min, judge=True)
                    except Abort as e:
                        PROG.end(False, str(e))
                        raise
                    PROG.end(ok, why)
                    st["why"] = why
                    record(plan, ("PASS" if ok else "FAIL") + f" (preset {key}{tag})", why, st, tester.base_rate)
                    log(f"{'PASS' if ok else 'FAIL'} preset {label}{tag} {plan.text()}: {why}")
                    if ok or (st.get("decisive") and not a.retest_all):
                        break          # passed, or failed clearly (a retest would only repeat it)
                    if attempt < a.retries:
                        log(f"retest preset {label} {plan.text()} once, in case that was a fluke")
                if ok:
                    break
        entry.update(mhz=plan.mhz, mv=plan.mv, pv=plan.pv, ok=ok, reason=st.get("why", ""),
                     avg_ths=round(st.get("avg_ths") or 0, 3), max_t=st.get("max_t"),
                     avg_fan=st.get("avg_fan"), watched_min=round(st.get("watched_min") or 0, 1))
        if ok and target is not None:
            entry["curve"] = preset_curve(target, st.get("avg_fan"), a.fan_min, a.abort_c)
        doc["presets"][key] = entry
        write_presets(doc)
    doc["complete"] = True
    write_presets(doc)
    log("PRESETS: " + ", ".join(
        f"{v['label']} {v['mhz']} MHz/{v['mv']} mV {'ok' if v['ok'] else 'FAILED'}"
        for v in doc["presets"].values()))


def main() -> None:
    ap = argparse.ArgumentParser(description="Automatic SC Lite clock/voltage tester (per-chip)")
    ap.add_argument("--check", action="store_true", help="show chip errors once and exit (no changes)")
    ap.add_argument("--start-mhz", type=int, default=625, help="baseline clock (the dashboard passes stock, the current setting or yours)")
    ap.add_argument("--baseline-current", action="store_true",
                    help="use the setting the miner is running now as the baseline (ignores --start-mhz / --mv / --base-pv)")
    ap.add_argument("--stock-mhz", type=int, default=0, help="the firmware's stock clock, only used to warn about a turned-down miner")
    ap.add_argument("--max-mhz", type=int, default=700, help="highest clock the curve tries")
    ap.add_argument("--step", type=int, default=25, help="MHz per step")
    ap.add_argument("--mv", type=int, default=9100, help="baseline voltage in mV")
    ap.add_argument("--mv-step", type=int, default=100, help="mV per voltage step (boost and trim)")
    ap.add_argument("--max-mv", type=int, default=9300, help="highest voltage any clock may get")
    ap.add_argument("--min-mv", type=int, default=8800, help="lowest voltage tried at any clock")
    ap.add_argument("--no-boost", action="store_true", help="never raise voltage above a clock's starting voltage")
    ap.add_argument("--no-trim-voltage", action="store_true", help="don't lower the voltage at each clock (only raise it until clean): a quicker, rougher curve")
    ap.add_argument("--baseline-min", type=float, default=25, help="minutes for the baseline")
    ap.add_argument("--hold-min", type=float, default=25, help="minutes watched per test")
    ap.add_argument("--settle-min", type=float, default=3, help="minutes ignored after each change")
    ap.add_argument("--sigma", type=float, default=2.5,
                    help="sensitivity: 2.5 = a clean test fails by chance about 5 %% of the time; lower is stricter (2.0 about 18 %%, 3.0 about 1 %%)")
    ap.add_argument("--margin", type=float, default=2, help="errors always allowed per chip, however low its normal rate")
    ap.add_argument("--pseudo", type=float, default=1, help="errors added to each chip's baseline count")
    ap.add_argument("--base-pv", type=int, default=0, help="the baseline's PV (default: mV + the PV gap)")
    ap.add_argument("--confirm-min", type=float, default=0, help="minutes for the final confirmation (default: twice --hold-min)")
    ap.add_argument("--hash-tol", type=float, default=0.04,
                    help="a clean test still fails if its hashrate is this much below baseline x clock ratio (0 = off)")
    ap.add_argument("--weak-x", type=float, default=8,
                    help="a chip at least this many times the median baseline rate counts as noisy (0 = off)")
    ap.add_argument("--weak-min", type=float, default=20, help="...and at least this many errors per hour")
    ap.add_argument("--weak-factor", type=float, default=1.5,
                    help="a noisy chip fails a step only above this many times its own normal rate")
    ap.add_argument("--no-clock-scale", action="store_true",
                    help="don't allow more errors at higher clocks (judge every clock by the baseline's errors per hour)")
    ap.add_argument("--retries", type=int, default=1, help="retests before a setting counts as failed")
    ap.add_argument("--retest-all", action="store_true",
                    help="also retest a setting that went over its whole limit in under half the test (normally not)")
    ap.add_argument("--no-predict-mv", action="store_true",
                    help="curve: start every clock at the last clock's voltage (normally it adds what the last step needed, at most one --mv-step)")
    ap.add_argument("--no-confirm", action="store_true", help="skip the final confirmation test")
    ap.add_argument("--confirm-mhz", type=int, default=0,
                    help="confirm mode: skip the search and run this clock for --confirm-hours")
    ap.add_argument("--confirm-mv", type=int, default=0, help="confirm mode voltage (default: --mv)")
    ap.add_argument("--confirm-hours", type=float, default=3, help="confirm mode length")
    ap.add_argument("--poll-s", type=float, default=30, help="seconds between readings")
    ap.add_argument("--abort-c", type=float, default=88, help="stop the run and restore the best setting if a board reaches this C")
    ap.add_argument("--any-model", action="store_true", help="allow miner models other than the SC Lite")
    ap.add_argument("--pv-offset", type=int, default=None,
                    help="PV = mV + this (default: keep the gap the miner already has, +300 on a stock SC Lite)")
    ap.add_argument("--fan-hold-c", type=float, default=0,
                    help="take over the fans and hold the hottest board at this C (0 = never touch fans)")
    ap.add_argument("--fan-min", type=int, default=30, help="lowest fan %% while holding a temperature")
    ap.add_argument("--fan-settle-min", type=float, default=5,
                    help="minutes to settle when the held temperature changes (baseline, each preset)")
    ap.add_argument("--presets", action="store_true",
                    help="after the search, build and test High / Middle / Low / Lowest power presets")
    ap.add_argument("--preset-min", type=float, default=20, help="minutes each preset is tested")
    ap.add_argument("--min-mhz", type=int, default=0,
                    help="lowest clock of the curve (default: 100 MHz below the baseline, on the step grid)")
    ap.add_argument("--preset-min-mhz", type=int, default=0,
                    help="clock for the Lowest power preset (default 0: one step below the baseline / Low); "
                         "Middle and Low are then spread evenly between High and it")
    ap.add_argument("--preset-min-mv", type=int, default=0,
                    help="lowest voltage a preset may use (Lowest power starts here; default: --min-mv)")
    ap.add_argument("--preset-max-c", type=float, default=70,
                    help="warmest a preset is held at (Lowest power); High is held at --fan-hold-c")
    ap.add_argument("--run-id", default=os.environ.get("SCLITE_TUNER_RUN", ""),
                    help="id written into the presets file (set by the dashboard)")
    ap.add_argument("--presets-only", action="store_true",
                    help="build and test the presets from an earlier search's curve (--source), without a new search")
    ap.add_argument("--source", default="", help="presets only: the JSON file with the earlier search's curve and baseline")
    ap.add_argument("--preset-temps", default="",
                    help="the four preset temperatures, High,Middle,Low,Lowest (default: spread from --fan-hold-c to --preset-max-c)")
    ap.add_argument("--offline-min", type=float, default=5,
                    help="minutes the miner's web API may be down before the run stops")
    a = ap.parse_args()
    if a.preset_temps:
        try:
            a.preset_temps = [float(x) for x in a.preset_temps.split(",")]
        except ValueError:
            ap.error("--preset-temps: four numbers, High,Middle,Low,Lowest")
        if len(a.preset_temps) != 4 or not all(30 <= t <= a.abort_c - 6 for t in a.preset_temps):
            ap.error(f"--preset-temps: four temperatures from 30 to {a.abort_c - 6:g} C (6 C below --abort-c)")
    else:
        a.preset_temps = None
    if a.presets_only and not a.source:
        ap.error("--presets-only needs --source")
    global OFFLINE_S
    OFFLINE_S = max(0.5, a.offline_min) * 60

    # Always treat Ctrl+C / stop requests as "stop and restore the best plan",
    # even when started by a background service that ignores Ctrl+C.
    def _stop(_sig: int, _frame: Any) -> None:
        raise KeyboardInterrupt
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    patient(sc.login, "log in")

    if a.check:
        print(f"plan={sc.get_setting().get('manualPowerplan')}")
        chips, resets = read_icinfo()
        for b in sorted({k[0] for k in chips}):
            n = [k for k in chips if k[0] == b]
            print(f"board {b}: {len(n)} chips, {sum(chips[k] for k in n)} hardware errors, "
                  f"resets={resets.get(b)}")
        print("chips with the most hardware errors (since the counters last reset):")
        for k in sorted(chips, key=lambda k: -chips[k])[:10]:
            print(f"  board {k[0]} chip {k[1]:>2}: {chips[k]}")
        print(f"hottest board {sc.board_snapshot().get('max_t')} C")
        return

    # Only the SC Lite is proven. Other models may use a different plan format
    # or chip layout, so they need an explicit --any-model.
    try:
        status = patient(lambda: sc.api("GET", "/mcb/status"), "read model")
        model = str((status or {}).get("model") or "unknown") if isinstance(status, dict) else "unknown"
    except Exception:
        model = "unknown"
    log(f"miner model: {model}")
    if "SCLITE" not in model.upper().replace(" ", "").replace("-", ""):
        if not a.any_model:
            log("this tuner is only tested on the SC Lite; nothing was changed. "
                "Tick 'allow untested models' (or pass --any-model) to try it anyway.")
            print(f"\nNOT RUN: untested model {model}")
            sys.exit(2)
        log("WARNING: untested model; watch the first steps closely")

    cur, pre_raw, pre_how, pre_live = running_state()
    pre = cur   # what the miner ran before this run: where it goes back to unless a setting is confirmed
    log(f"before the run the miner is on {pre.text()} ({pre_how})")
    # PV follows mV with a fixed gap: the miner's own gap unless --pv-offset sets one
    gap = a.pv_offset if a.pv_offset is not None else cur.pv - cur.mv
    log(f"PV = mV {'+' if gap >= 0 else '-'} {abs(gap)} "
        f"({(os.environ.get('SCLITE_PV_NOTE') or 'set by you') if a.pv_offset is not None else 'kept from the miner: ' + str(cur.mv) + ' mV / PV ' + str(cur.pv)})")
    if a.baseline_current:
        base = pre
        log("baseline: the setting the miner is running now")
    else:
        base = Plan(a.start_mhz, a.mv, a.base_pv or a.mv + gap)
    if a.stock_mhz and pre.mhz <= a.stock_mhz - 50:
        log(f"NOTE: the miner runs {pre.mhz} MHz, {a.stock_mhz - pre.mhz} MHz below its stock {a.stock_mhz} MHz. "
            "It may have been turned down for a reason; whatever happens, it goes back to that setting "
            "unless a better one is confirmed")
    src: dict[str, Any] = {}
    if a.presets_only:
        # an earlier search's curve, confirmed best and baseline, from the dashboard (see --source)
        with open(a.source) as f:
            src = json.load(f)
        a.presets = True
        a.no_confirm = False
        a.source_label = str(src.get("label") or "an earlier search")
        log(f"PRESETS ONLY from {a.source_label}: curve "
            + ", ".join(f"{m} {v}" for m, v in src.get("curve") or [])
            + f"; High {src['best'][0]} MHz / {src['best'][1]} mV")
    tester = Tester(a)
    best: Plan = base
    earned: Plan | None = None   # a setting that passed everything (the confirm, if on): only this replaces `pre`
    presets_stop = ""            # set if a safety stop cut the presets short
    log(f"files: {RESULTS_CSV}")
    log(f"       {LIVE_CSV}")
    log(f"       {CHIPS_CSV}")
    if tester.fans:
        log(f"fans: holding the hottest board at {a.fan_hold_c:g} C (fan never below {a.fan_min}%)")
    else:
        log("fans are never changed")
    log(f"stopping puts the miner back on {pre.text()} (what it ran before)"
        + ("" if a.presets_only else ", unless a setting was already confirmed"))

    global PROG
    PROG = prog = Progress(a.run_id)
    hold_txt = f", fans holding {a.fan_hold_c:g} °C" if tester.fans else ""
    if a.presets_only and a.baseline_min <= 0:
        prog.add("baseline", "Baseline (reused)", f"each chip's normal error rate from {a.source_label}'s baseline "
                 f"({base.text()}): nothing runs here")
    else:
        prog.add("baseline", "Baseline", f"{base.text()} for {a.baseline_min:g} min{hold_txt}: "
                 "learns every chip's normal error rate. Nothing can fail here.")
    if a.presets_only:
        prog.add("presets", "Presets", f"rebuilds High, Middle, Low and Lowest power from {a.source_label}'s voltage curve, "
                 f"{a.preset_min:g} min each, each with its own fan curve")
        for k, lbl in PRESET_ORDER:
            prog.item("presets", k, lbl)
    elif a.confirm_mhz:
        target0 = base.with_(mhz=a.confirm_mhz, mv=a.confirm_mv or base.mv)
        n0 = max(1, round(a.confirm_hours * 60 / a.hold_min))
        st = prog.add("confirm_mode", f"Confirm {target0.text()}",
                      f"{n0} rounds of {a.hold_min:g} min (about {a.confirm_hours:g} h); a failed round gets one retest")
        for i in range(1, n0 + 1):
            prog.item("confirm_mode", i, f"Round {i}")
    else:
        prog.add("curve", "Voltage curve", f"every clock from {curve_clocks(a, base)[0]} up to {a.max_mhz} MHz in {a.step} MHz steps, "
                 f"{a.hold_min:g} min per test. Each clock: up from the voltage that was lowest one clock slower until "
                 f"clean (max {a.max_mv} mV), then down {a.mv_step} mV at a time until it fails (min {a.min_mv} mV). "
                 "Every voltage is tested at its own clock. It ends at the first clock that isn't clean at any allowed voltage.")
        for m in curve_clocks(a, base):
            prog.item("curve", m, f"{m} MHz")
        if not a.no_confirm:
            prog.add("confirm", "Confirm", f"runs the best setting once more, for {a.confirm_min or 2 * a.hold_min:g} min; "
                     "if it fails, the next best is confirmed instead")
        if a.presets:
            prog.add("presets", "Presets", f"builds and tests High, Middle, Low and Lowest power, {a.preset_min:g} min each, "
                     "each with its own fan curve")
            for k, lbl in PRESET_ORDER:
                prog.item("presets", k, lbl)
    if a.presets_only:
        prog.add("finish", "Finish", f"puts back what the miner ran before the run ({pre.mhz} MHz · {pre.mv} mV); "
                 "the new presets go to the Profiles page")
    else:
        prog.add("finish", "Finish", f"leaves the miner on the confirmed best setting; if nothing is confirmed, or the run "
                 f"stops early, it goes back to what it ran before ({pre.mhz} MHz · {pre.mv} mV)")
    prog.write()

    done: dict[tuple[int, int], bool] = {}

    passed: list[Plan] = [base]
    last_stats: dict[tuple[int, int], dict] = {}

    def once(plan: Plan, label: str = "", key: Any = None, hold: float | None = None) -> bool:
        prog.begin(plan, label, key)
        try:
            ok, why, st = tester.test(plan, hold)
        except Abort as e:
            prog.end(False, str(e))
            raise
        prog.end(ok, why)
        record(plan, ("PASS" if ok else "FAIL") + label, why, st, tester.base_rate)
        log(f"{'PASS' if ok else 'FAIL'}{label} {plan.text()}: {why}")
        st["why"] = why
        last_stats[(plan.mhz, plan.mv)] = st
        return ok

    def run(plan: Plan, fresh: bool = False) -> bool:
        """Test a setting (with one retest on a fail). An earlier result is reused, except
        that fresh=True tests an earlier failure again (it may have been one noisy chip)."""
        key = (plan.mhz, plan.mv)
        if key in done and not (fresh and not done[key]):
            log(f"skip {plan.text()} (already {'passed' if done[key] else 'failed'})")
            prog.cached(plan, done[key])
            return done[key]
        ok = once(plan)
        for _ in range(a.retries):
            if ok:
                break
            st0 = last_stats.get(key) or {}
            if st0.get("decisive") and not a.retest_all:
                w = st0.get("watched_min", 0)
                log(f"no retest: it went over its whole limit in {w:.1f} of {st0.get('hold_min', a.hold_min):g} min "
                    f"(at least twice the allowed rate), so it wasn't a fluke")
                prog.note_last("over its limit in under half the test: not retested")
                break
            log(f"retest {plan.text()} once, in case that was a fluke")
            ok = once(plan, " (retest)")
        done[key] = ok
        if ok:
            passed.append(plan)
        return ok

    try:
        prog.set("baseline", "run")
        if a.presets_only and a.baseline_min <= 0:
            rates = {}
            for k, v in (src.get("base_rate") or {}).items():
                b_, c_ = k.split("-")
                rates[(int(b_), int(c_))] = float(v)
            if not rates:
                raise Abort("the earlier search has no saved baseline; run a fresh baseline instead")
            tester.load_baseline(rates, base.mhz, float(src.get("base_ths") or 0), str(src.get("base_when") or a.source_label))
        else:
            tester.baseline(base)
        prog.set("baseline", "pass", (f"reused every chip's normal rate from {src.get('base_when') or a.source_label}"
                                      if a.presets_only and a.baseline_min <= 0 else "learned every chip's normal rate") + (
            "; noisy chips judged on getting clearly worse: " + ", ".join(
                f"{chip_name(k)} ({tester.base_rate[k]:.0f}/h, {x:.0f}x normal)"
                for k, x in sorted(tester.weak.items(), key=lambda kv: -kv[1])) if tester.weak else ""))
        done[(base.mhz, base.mv)] = True

        if a.presets_only:
            for m, v in src.get("passed") or []:
                passed.append(Plan(int(m), int(v), int(v) + gap))
            bm, bv = int(src["best"][0]), int(src["best"][1])
            best = Plan(bm, bv, bv + gap)
            prog.stage = "presets"
            prog.set("presets", "run")
            try:
                build_presets(a, tester, base, best, passed, last_stats, gap)
                n_ok = sum(1 for i in prog.get("presets")["items"] if i["state"] == "pass")
                prog.set("presets", "pass" if n_ok else "fail", f"{n_ok} of 4 presets passed")
            except Abort as e:
                log(f"PRESETS stopped: {e}")
                prog.skip_rest("presets", f"not tested: {e}")
                prog.set("presets", "stop", f"stopped: {e}")
        elif a.confirm_mhz:
            # CONFIRM MODE: run one chosen setting for a long time, in test-length rounds
            target = base.with_(mhz=a.confirm_mhz, mv=a.confirm_mv or base.mv)
            rounds = max(1, round(a.confirm_hours * 60 / a.hold_min))
            log(f"CONFIRM MODE: {target.text()} for {rounds} rounds of {a.hold_min:g} min")
            t0 = time.time()
            ok = True
            prog.stage = "confirm_mode"
            for i in range(1, rounds + 1):
                ok = once(target, f" (confirm {i}/{rounds})", key=i)
                if not ok:
                    log(f"retest round {i} once, in case that was a fluke")
                    ok = once(target, f" (confirm {i}/{rounds} retest)", key=i)
                if not ok:
                    break
            prog.skip_rest("confirm_mode", "not needed: an earlier round failed")
            prog.set("confirm_mode", "pass" if ok else "fail",
                     f"CONFIRMED after {rounds} clean rounds" if ok else f"REJECTED in round {i}")
            hours = (time.time() - t0) / 3600
            verdict = "CONFIRMED" if ok else "REJECTED"
            append_row(RESULTS_CSV, RESULTS_HEADER, [
                now(), target.mhz, target.mv, target.pv, verdict,
                (f"clean for all {rounds} rounds" if ok else f"failed in round {i} of {rounds}")
                + f", {hours:.1f} h", f"{hours * 60:.1f}", "", "", "", "", "", "", ""])
            log(f"{verdict}: {target.text()}")
            best = target if ok else base
            earned = target if ok else None
        else:
            # the curve: every clock from --min-mhz up to --max-mhz, each at its own lowest clean voltage.
            # Nothing is assumed from another clock: each voltage is tested at its own clock.
            prog.stage = "curve"
            prog.set("curve", "run")
            curve: list[Plan] = []
            prev_low, lift = None, 0
            stopped = None
            for m in curve_clocks(a, base):
                prog.item("curve", m, f"{m} MHz")
                # where to start: the lowest clocks start at the stock voltage; a higher clock starts at the
                # previous clock's lowest clean voltage, plus what the step before needed but at most one
                # voltage step. Either way it then goes down until it fails, so the start is only the order of
                # the tests, not an assumption. Starting too high costs a full passing test that the way down
                # makes pointless (on a real run: 575 needed +200 only because 550 scraped through at 8800, so
                # 600 started at 9200 and spent 45 min proving what 9100 then showed); starting one step too low
                # costs a failed test, which usually ends early.
                if prev_low is None:
                    start = base.mv
                else:
                    start = prev_low.mv + (min(lift, a.mv_step) if not a.no_predict_mv else 0)
                start = max(a.min_mv, min(a.max_mv, start))
                if prev_low is not None and start != prev_low.mv:
                    log(f"curve {m} MHz: start at {start} mV (the last clock step needed +{lift} mV"
                        + (f"; one step at most" if lift > a.mv_step else "") + ")")
                low, v = None, start
                while True:                      # up from the start until it's clean
                    plan = base.with_(mhz=m, mv=v)
                    if run(plan, fresh=not (last_stats.get((m, v)) or {}).get("decisive")):
                        low = plan
                        break
                    if a.no_boost or v + a.mv_step > a.max_mv:
                        break
                    v += a.mv_step
                    log(f"curve {m} MHz: more voltage, {v} mV")
                if low is None:
                    stopped = (m, v)
                    it = prog.item("curve", m, f"{m} MHz")
                    it["state"], it["note"] = "fail", f"not clean up to {v} mV (the highest allowed): the top of the curve"
                    prog.skip_rest("curve", f"not tried: {m} MHz wasn't clean at any allowed voltage, so the curve ends below it")
                    break
                if not a.no_trim_voltage:        # then down until it fails (two in a row, or one where a slower clock failed too)
                    misses = 0
                    for mv in range(low.mv - a.mv_step, a.min_mv - 1, -a.mv_step):
                        slower = next((k for k, ok_ in done.items() if k[0] < m and k[1] == mv and not ok_), None)
                        if slower:
                            # a slower clock already failed at this voltage (after its retest): a faster clock
                            # needs at least as much, so this test could only fail. Stop here.
                            log(f"curve {m} MHz: not testing {mv} mV, {slower[0]} MHz already failed there")
                            break
                        plan = base.with_(mhz=m, mv=mv)
                        if done.get((m, mv)) is False:
                            # it failed at this very clock a moment ago (on the way up): don't test it again
                            log(f"curve {m} MHz: {mv} mV already failed at this clock")
                            ok_ = False
                        else:
                            ok_ = run(plan, fresh=not (last_stats.get((m, mv)) or {}).get("decisive"))
                        if ok_:
                            low, misses = plan, 0
                            continue
                        misses += 1
                        if misses >= 2:
                            break
                it = prog.item("curve", m, f"{m} MHz")
                it["state"] = "pass"
                it["note"] = (f"lowest clean voltage {low.mv} mV"
                              + (" (the lowest allowed)" if low.mv - a.mv_step < a.min_mv else ""))
                if prev_low is not None:
                    lift = max(0, low.mv - prev_low.mv)
                prev_low = low
                curve.append(low)
                prog.write()
            if curve:
                best = curve[-1]
                if a.no_confirm:
                    earned = best
                prog.set("curve", "pass", "lowest clean voltage per clock: "
                         + ", ".join(f"{p.mhz} {p.mv}" for p in curve)
                         + (f"; {stopped[0]} MHz wasn't clean up to {stopped[1]} mV" if stopped else ""))
                log("VOLTAGE CURVE: " + ", ".join(f"{p.mhz} MHz {p.mv} mV" for p in curve))
            else:
                prog.set("curve", "fail", "no clock in the range was clean: the miner goes back to what it ran before")

            # confirm: re-test the winner once; if it fails, fall back and confirm that
            if not a.no_confirm:
                confirm_min = a.confirm_min or 2 * a.hold_min
                prog.stage = "confirm"
                prog.set("confirm", "run")
                for cand in sorted(passed[1:], key=lambda p: (p.mhz, -p.mv), reverse=True):
                    log(f"CONFIRM {cand.text()} for {confirm_min:g} min")
                    ok = once(cand, " (confirm)", hold=confirm_min)
                    st0 = last_stats.get((cand.mhz, cand.mv)) or {}
                    if not ok and a.retries and (not st0.get("decisive") or a.retest_all):
                        # a close call over a long test is likely luck: one more go before giving up on it
                        log(f"retest the confirm of {cand.text()} once, in case that was a fluke")
                        ok = once(cand, " (confirm retest)", hold=confirm_min)
                    if ok:
                        best = earned = cand
                        break
                    best = base
                else:
                    best = base  # nothing confirmed: back to the baseline
                prog.set("confirm", "pass" if best is not base else "fail",
                         f"confirmed {best.mhz} MHz · {best.mv} mV" if best is not base
                         else f"nothing confirmed: back to what it ran before ({pre.mhz} MHz · {pre.mv} mV)")

            if a.presets:
                prog.stage = "presets"
                prog.set("presets", "run")
                try:
                    build_presets(a, tester, base, best, passed, last_stats, gap)
                    n_ok = sum(1 for i in prog.get("presets")["items"] if i["state"] == "pass")
                    prog.set("presets", "pass" if n_ok else "fail", f"{n_ok} of 4 presets passed")
                except Abort as e:   # too hot during a preset: keep the search result
                    log(f"PRESETS stopped: {e}")
                    prog.skip_rest("presets", f"not tested: {e}")
                    prog.set("presets", "stop", f"stopped: {e}")
                    presets_stop = f"safety stop during the presets: {e}"
                if earned:
                    apply_plan(earned)      # end on High
                elif pre_raw.get("manual"):
                    apply_plan(pre)
                # (a miner that ran a firmware level is put back below, in the finally block)
                if tester.fans:
                    tester.fans.set_target(a.fan_hold_c)

        outcome = ("aborted", presets_stop) if presets_stop else ("done", "")
    except Abort as e:
        log(f"ABORT: {e}")
        earned = None   # a safety stop: back to what it ran before
        outcome = ("aborted", f"safety stop: {e}")
    except KeyboardInterrupt:
        log("stopped by you")
        outcome = ("stopped", "stopped by you")
    except Exception as e:
        log(f"ERROR: {type(e).__name__}: {e}")
        outcome = ("error", f"error: {type(e).__name__}: {e}")
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)   # don't let a 2nd stop interrupt the restore
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        final = earned or pre
        raw_back = final is pre and not pre_raw.get("manual")   # it ran a firmware level (stock or Idle)
        left = f"its own setting again ({pre_how})" if raw_back else final.text()
        if final is pre:
            log(f"going back to what the miner ran before the run: {left if raw_back else pre.text()}")
        try:
            if raw_back:
                # Turning manual off doesn't make the firmware run its level again (seen on the HS Box: it kept
                # the last clock written), so first write the level's own plan, which takes effect at once,
                # then put the fields back (manual off, its level selected). Idle is picked by `select` alone.
                if pre_live:
                    apply_plan(pre)
                restore_raw(pre_raw)
            else:
                apply_plan(final)
            # leave the fan where the setting needed it until the dashboard's auto fan takes over again; not
            # after putting a firmware level back: a fan write turns manual on again (with the old manual plan)
            if tester.fans and tester.fans.fan is not None and not raw_back:
                # leave the fan where this setting needed it (or high if unknown) until the
                # dashboard's auto fan takes over again
                need = (last_stats.get((final.mhz, final.mv)) or {}).get("avg_fan")
                tester.fans.send(int(max(a.fan_min, min(100, (need or 75) + 5))))
                log(f"fan left at {tester.fans.fan}%")
            log(f"miner left on {left}")
            try:
                oc = locals().get("outcome") or ("stopped", "stopped")
                prog.set("finish", "pass", f"miner left on {left}")
                prog.finish(oc[0], oc[1])
            except Exception:
                pass
        except Exception as e:
            fix = (f"select its {pre_how.replace('the firmware', 'firmware')} again on the miner's own page" if raw_back
                   else f"set {final.mhz} MHz / {final.mv} mV on the Miner page")
            log(f"could not restore the plan ({e}); {fix}")
            try:   # say so on the Tuner page too: the miner may still be on a test setting
                prog.set("finish", "fail", f"COULD NOT put the miner back on {left}: {fix}")
                prog.finish("error", f"the miner may still be on a test setting: {fix}")
            except Exception:
                pass
            sys.exit(1)

    if a.presets_only:
        try:
            with open(PRESETS_JSON) as f:
                pd = json.load(f)
            n_ok = sum(1 for v in (pd.get("presets") or {}).values() if v.get("ok"))
        except (OSError, ValueError):
            n_ok = 0
        oc = locals().get("outcome") or ("stopped", "stopped")
        head = "PRESETS DONE" if oc[0] == "done" else f"PRESETS STOPPED ({oc[1]})"
        back = left if raw_back else f"{final.mhz} MHz at {final.mv} mV (PV {final.pv})"
        print(f"\n{head}: {n_ok} of 4 passed; miner back on {back}, what it ran before")
    elif earned:
        print(f"\nBEST: {final.mhz} MHz at {final.mv} mV (PV {final.pv})")
    else:
        back = left if raw_back else f"{final.mhz} MHz at {final.mv} mV (PV {final.pv})"
        print(f"\nRESTORED: {back}, what it ran before: nothing new was confirmed")


if __name__ == "__main__":
    main()
