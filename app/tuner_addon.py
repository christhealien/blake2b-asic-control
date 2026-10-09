"""Auto-tuner add-on for the web dashboard: one tuning run per miner, side by side.

Each run is ../python/asic_tuner.py in its own process (so it keeps going if the
dashboard or the browser is closed), with its own folder under the data folder:
    <data>/<miner id>/asic_tuner_*.csv / .json, asic_tuner.log, asic_tuner.pid.json
Several miners can be tuned at the same time; each run only touches its own miner.

Routes (wired into server.py by install_addons.py):
  GET  /api/tuner/status?miner=<id>    that miner's state, live progress, results, log tail,
                                       plus a short summary of every miner's run
  GET  /api/tuner/chipmap?miner=<id>   that miner's chip map (live, or one finished test)
  POST /api/tuner/start                {"miner_id": ..., options...}
  POST /api/tuner/stop                 {"miner_id": ...}  Ctrl+C that run (it puts back what the miner ran before, or the confirmed best)
"""
from __future__ import annotations

import csv
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
PYDIR = Path(os.environ.get("SCLITE_TUNER_DIR", str(HERE.parent / "python")))   # where the scripts are
DATA = Path(os.environ.get("SCLITE_TUNER_DATA") or PYDIR)                        # where results are kept
SCRIPT = PYDIR / "asic_tuner.py"
SCRIPT_TAG = b"asic_tuner"     # how a running tuner is recognised in /proc/<pid>/cmdline
PREFIX = "asic_tuner"             # data file names: asic_tuner.log, asic_tuner_results.csv, ...
OLD_PREFIX = "sclite_autotune"    # names used before 1.8 (renamed on start)

# option name -> (command-line flag, type, min, max, default)
NUM_OPTS: dict[str, tuple[str, type, float, float, float]] = {
    "start_mhz":    ("--start-mhz",    int,   400, 700,  625),
    "max_mhz":      ("--max-mhz",      int,   400, 700,  700),
    "step":         ("--step",         int,   5,   100,  25),
    "mv":           ("--mv",           int,   8500, 9400, 9100),
    "max_mv":       ("--max-mv",       int,   8500, 9400, 9300),
    "min_mv":       ("--min-mv",       int,   8500, 9400, 8700),
    "preset_min_mv": ("--preset-min-mv", int,  8500, 9400, 8700),
    "mv_step":      ("--mv-step",      int,   25,  300,  100),
    "baseline_min": ("--baseline-min", float, 5,   180,  60),
    "hold_min":     ("--hold-min",     float, 5,   180,  30),
    "sigma":        ("--sigma",        float, 1,   5,    2.5),
    "abort_c":      ("--abort-c",      float, 60,  92,   88),
}
# Clock and voltage options follow each miner's own stock plan (read by the probe from the
# firmware's power plans); the numbers above are the SC Lite's, used when it isn't known.
SC_LITE_STOCK = {"mhz": 625, "mv": 9100, "pv": 9400}
MHZ_OPTS = ("start_mhz", "max_mhz")
MV_OPTS = ("mv", "max_mv", "min_mv", "preset_min_mv")


# How a miner writes its power plan. Only the SC Lite form can be changed by this app so far.
_PLAN_FORMS = (
    ("sc-lite", re.compile(r"^\s*\d+\s*MHz\s+\d+\s*V\s+\d+\s*RPM\s+\d+\s*RPM\s+PV\s+\d+\s*$", re.I)),
    ("box", re.compile(r"^\s*\d+\s*MHz\s+\d+\.\d+\s*V\s+\d+\s*RPM\s+\d+\s*RPM\s*$", re.I)),          # SC BOX, HS BOX
    ("float-pv", re.compile(r"^\s*\d+\s*MHz\s+\d+(\.\d+)?\s*V\s+\d+\s*RPM\s+\d+\s*RPM\s+PV\s+\d+(\.\d+)?\s*$", re.I)),
)
FORMAT_NOTE = {
    "box": ("this model writes its power plan with volts as a decimal and no PV (like \"725 MHz 0.41 V 70 RPM 70 RPM\", "
            "as the SC BOX and HS BOX do). The app can read it but can't change clock or voltage in that format yet, "
            "so the tuner, presets and clock settings are off for this miner. Probing again won't change that"),
    "float-pv": ("this model writes its power plan with decimal volts and a PV term. The app can't change clock or "
                 "voltage in that format yet. Probing again won't change that"),
    "unknown": ("this miner's power plan is in a format the app doesn't recognise, so clock and voltage can't be "
                "changed. Probing again won't change that"),
}


def plan_format(text: Any) -> str:
    """'sc-lite', 'box', 'float-pv' or 'unknown' (also the "0 MHz 0 V" off plan of a box: 'box')."""
    t = str(text or "")[:200]
    for name, rx in _PLAN_FORMS:
        if rx.match(t):
            return name
    return "unknown"


def format_problem(hw: dict[str, Any] | None) -> str:
    """Why clock / voltage can't be changed on this miner because of its plan format ('' if it can)."""
    f = (hw or {}).get("plan_format")
    return FORMAT_NOTE.get(f, "") if f and f != "sc-lite" else ""


def plan_limits(hw: dict[str, Any] | None) -> dict[str, Any]:
    """Tuner defaults and ranges for one miner, from its stock plan.
      advised  the usual range (outside it the Tuner page warns, but you can still start):
               SC Lite (625 / 9100): 400-700 MHz, 8500-9400 mV
      limits   the hard range (the run won't start outside it): SC Lite 300-775 MHz, 8100-9600 mV
    Defaults for an SC Lite: start 625 / 9100, climb to 700, boost to 9300, trim to 8800."""
    s = (hw or {}).get("stock") or None
    src = "firmware" if s else "assumed"
    try:
        s = {k: int(s[k]) for k in ("mhz", "mv", "pv")} if s else dict(SC_LITE_STOCK)
    except (KeyError, TypeError, ValueError):
        s, src = dict(SC_LITE_STOCK), "assumed"
    r25 = lambda x: int(round(x / 25.0) * 25)
    fp = format_problem(hw)
    if fp:
        src = "unsupported"
    return {
        "stock": s, "source": src, "pv_gap": s["pv"] - s["mv"], "format_note": fp,
        "plan_format": (hw or {}).get("plan_format"), "stock_text": (hw or {}).get("stock_text"),
        "running_text": (hw or {}).get("running_text"),
        "current": (hw or {}).get("current"),
        "advised": {"mhz": (r25(s["mhz"] * 0.64), s["mhz"] + 75), "mv": (s["mv"] - 600, s["mv"] + 300)},
        "limits": {"mhz": (max(300, r25(s["mhz"] * 0.5)), s["mhz"] + 150), "mv": (s["mv"] - 1000, s["mv"] + 500)},
        "defaults": {"start_mhz": s["mhz"], "max_mhz": s["mhz"] + 75, "mv": s["mv"],
                     "max_mv": s["mv"] + 200, "min_mv": s["mv"] - 400, "preset_min_mv": s["mv"] - 400,
                     "confirm_mhz": s["mhz"] + 25, "confirm_mv": s["mv"]},
    }


BOOL_OPTS = {"boost": "--no-boost", "trim": "--no-trim-voltage", "confirm": "--no-confirm"}
# fans and presets (checked separately: they depend on each other)
FAN_OPTS: dict[str, tuple[str, type, float, float, float]] = {
    "fan_hold_c":   ("--fan-hold-c",   float, 45,  80,   60),
    "fan_min":      ("--fan-min",      int,   20,  80,   30),
    "preset_max_c": ("--preset-max-c", float, 45,  85,   70),
    "preset_min":   ("--preset-min",   float, 5,   60,   20),
}


# ------------------------------------------------------------------ folders ---

def safe_id(mid: Any) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", str(mid or "")).strip("._")
    return s[:64] or "miner"


class Files:
    """Where one miner's run keeps its files."""

    def __init__(self, mid: Any):
        self.mid = str(mid)
        self.dir = DATA / safe_id(mid)
        self.pid = self.dir / f"{PREFIX}.pid.json"
        self.log = self.dir / f"{PREFIX}.log"
        self.results = self.dir / f"{PREFIX}_results.csv"
        self.live = self.dir / f"{PREFIX}_live.csv"
        self.chips = self.dir / f"{PREFIX}_chips.csv"
        self.chipmap = self.dir / f"{PREFIX}_chipmap.json"
        self.presets = self.dir / f"{PREFIX}_presets.json"
        self.runs = self.dir / f"{PREFIX}_runs.json"        # every run's options (for presets-only runs)
        self.source = self.dir / f"{PREFIX}_source.json"    # what a presets-only run builds from
        self.plan = self.dir / f"{PREFIX}_plan.json"      # the run's steps and how far it got


_DATA_FILE = r"(\.pid\.json|\.log|_(results|live|chips)(\.old-\d+)?\.csv|_chipmap\.json|_presets\.json|_plan\.json)"


def _migrate() -> None:
    """Keep older results (nothing is deleted):
    - before 1.6 there was one run, kept straight in the data folder: move it into the folder of
      the miner it was for;
    - before 1.8 the files were called sclite_autotune*: rename them to asic_tuner*."""
    if not DATA.is_dir():
        return
    old = re.compile(rf"^{OLD_PREFIX}{_DATA_FILE}$")
    new_name = lambda n: PREFIX + n[len(OLD_PREFIX):]
    loose = [p for p in DATA.glob(f"{OLD_PREFIX}*") if p.is_file() and old.match(p.name)]
    if loose:
        try:
            mid = json.loads((DATA / f"{OLD_PREFIX}.pid.json").read_text()).get("miner_id")
        except (FileNotFoundError, ValueError, OSError):
            mid = None
        dest = Files(mid or "previous-run").dir
        try:
            dest.mkdir(parents=True, exist_ok=True)
            for p in loose:
                if not (dest / new_name(p.name)).exists():
                    shutil.move(str(p), str(dest / new_name(p.name)))
        except OSError as e:
            print(f"[tuner] could not move old results into {dest}: {e}", flush=True)
    for p in DATA.glob(f"*/{OLD_PREFIX}*"):
        if p.is_file() and old.match(p.name) and not (p.parent / new_name(p.name)).exists():
            try:
                p.rename(p.parent / new_name(p.name))
            except OSError as e:
                print(f"[tuner] could not rename {p}: {e}", flush=True)


try:
    _migrate()
except Exception as e:  # never stop the dashboard over this
    print(f"[tuner] migrate: {e}", flush=True)


# ------------------------------------------------------------------ process ---

def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError, OSError):
        return None


def _alive(pid: int, data_dir: Any = None) -> bool:
    """Is this pid a running tuner (of this miner, when data_dir is given)? After a container restart pids
    start from low numbers again, so an old pid file can name another miner's run: each run gets its own
    SCLITE_TUNER_DATA folder in its environment, which tells them apart."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    cmd = Path(f"/proc/{pid}/cmdline")
    if cmd.exists():  # guard against the pid being reused by something else
        try:
            if SCRIPT_TAG not in cmd.read_bytes():
                return False
        except OSError:
            return True
        if data_dir is not None:
            try:
                env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
                return f"SCLITE_TUNER_DATA={data_dir}".encode() in env
            except OSError:
                return True
    return True


_procs: dict[str, subprocess.Popen] = {}   # runs we started, so their exit can be collected


def _reap() -> None:
    for k, p in list(_procs.items()):
        if p.poll() is not None:   # finished: collect it (no zombie)
            del _procs[k]


def running(mid: Any) -> dict[str, Any] | None:
    _reap()
    f = Files(mid)
    info = _read_json(f.pid)
    if info and _alive(int(info.get("pid", 0)), f.dir):
        return info
    return None


def runs() -> dict[str, dict[str, Any]]:
    """Every miner that has a run folder: {miner id: {running, info}}."""
    _reap()
    out: dict[str, dict[str, Any]] = {}
    if not DATA.is_dir():
        return out
    for pidfile in DATA.glob(f"*/{PREFIX}.pid.json"):
        info = _read_json(pidfile) or {}
        mid = str(info.get("miner_id") or pidfile.parent.name)
        out[mid] = {"running": bool(info) and _alive(int(info.get("pid", 0)), pidfile.parent), "info": info}
    return out


def fan_owner(mid: Any) -> bool:
    """True while a run on this miner is holding its fans (the dashboard's auto fan waits)."""
    info = running(mid)
    return bool(info and float((info.get("options") or {}).get("fan_hold_c") or 0) > 0)


def _num(body: dict[str, Any], name: str, spec: tuple[str, type, float, float, float]) -> Any:
    _flag, typ, lo, hi, default = spec
    val = body.get(name, default)
    if val in (None, ""):
        val = default
    try:
        val = typ(val)
    except (TypeError, ValueError):
        raise RuntimeError(f"{name} must be a number")
    if not lo <= val <= hi:
        raise RuntimeError(f"{name} must be between {lo:g} and {hi:g}")
    return val


def http_error_text(e: Exception) -> str:
    """An error as text, with what the miner said for an HTTP error (its reply body, cut short)."""
    msg = str(e)
    if hasattr(e, "read") and hasattr(e, "code"):
        try:
            said = e.read(300).decode("utf-8", "replace").strip()  # type: ignore[attr-defined]
        except Exception:
            said = ""
        said = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", said)).strip()[:160]
        if said:
            msg += f" (the miner said: {said})"
    return msg


def sign_in_problem(ip: str, pw: str) -> str | None:
    """Sign in to the miner fresh, the way the tuner will. None if that works, else why not."""
    try:
        from miner_client import MinerClient
        MinerClient(ip=ip, password=pw).login()
        return None
    except Exception as e:
        return http_error_text(e)


def read_live_plan(ip: str, pw: str) -> dict[str, int] | None:
    """The clock / voltage the miner runs now (None if it can't be read in the SC Lite form)."""
    try:
        from miner_client import MinerClient, parse_plan
        s = MinerClient(ip=ip, password=pw).api("GET", "/mcb/setting") or {}
        s = s if isinstance(s, dict) else {}
        text = str(s.get("manualPowerplan") or "")
        if not s.get("manual"):
            # it runs a firmware level: its stock plan (or Idle), not whatever manualPowerplan still holds
            sel = str(s.get("select") or 0)
            plans = [p for p in s.get("powerplans") or [] if isinstance(p, dict)]
            text = next((str(p.get("info") or "") for p in plans if str(p.get("level")) == sel), "")
        m, v, _a, _b, pv = parse_plan(text[:200])
        return {"mhz": int(m), "mv": int(v), "pv": int(pv)}
    except Exception:
        return None


# ------------------------------------------------------- presets from the last search ---

_CURVE_RESULTS = ("PASS", "FAIL", "PASS (retest)", "FAIL (retest)")
_CONFIRM_PASS = ("PASS (confirm)", "PASS (confirm retest)")


def _step_of(values: list[int], default: int) -> int:
    """The curve's step: the most common gap between its clocks (an off-grid top clock, such as 690 after
    675, would make the smallest gap 15 and put presets on clocks the curve never measured)."""
    from collections import Counter
    v = sorted(set(values))
    d = [b - a for a, b in zip(v, v[1:]) if b > a]
    if not d:
        return default
    c = Counter(d).most_common()
    top = c[0][1]
    return max(g for g, n in c if n == top)


def search_source(mid: Any) -> dict[str, Any]:
    """What a presets-only run builds from: this miner's latest search that mapped a voltage curve, read
    back from its CSV files (the results keep every run). Raises RuntimeError saying why there's none."""
    f = Files(mid)
    rows = _csv(f.results)
    if not rows or "worst_chip" not in rows[0]:
        raise RuntimeError("this miner has no tuning results yet: run 'Map every clock and voltage' first")
    # split into runs: every run starts with its BASELINE row (a presets-only run that reuses a baseline
    # has none, but it only adds preset rows, which are ignored here)
    segs: list[list[dict[str, str]]] = []
    for r in rows:
        if r.get("result") == "BASELINE" or not segs:
            segs.append([])
        segs[-1].append(r)
    seg = next((g for g in reversed(segs)
                if g[0].get("result") == "BASELINE" and any(r["result"] in ("PASS", "PASS (retest)") for r in g)), None)
    if not seg:
        raise RuntimeError("no finished voltage curve found for this miner: run 'Map every clock and voltage' first")
    b = seg[0]
    passed = []
    for r in seg[1:]:
        res = r.get("result") or ""
        if res.startswith("PASS") and "(preset" not in res and not re.search(r"\(confirm \d", res):
            k = (int(r["mhz"]), int(r["mv"]))
            if k not in passed:
                passed.append(k)
    if not passed:
        raise RuntimeError("the last search has no passing setting to build presets from")
    curve: dict[int, int] = {}
    for m, v in passed:
        curve[m] = min(v, curve.get(m, v))
    confirms = [r for r in seg if r["result"] in _CONFIRM_PASS]
    tried_confirm = any("(confirm" in r["result"] and not re.search(r"\(confirm \d", r["result"]) for r in seg)
    if confirms:
        best = (int(confirms[-1]["mhz"]), int(confirms[-1]["mv"]))
    elif tried_confirm:
        raise RuntimeError("the last search confirmed nothing (every confirm test failed), so there's no High to "
                           "build presets from: run a new search")
    else:                                   # confirm was off: the fastest clean clock at its lowest voltage
        m = max(curve)
        best = (m, curve[m])
    base = {"mhz": int(b["mhz"]), "mv": int(b["mv"]), "pv": int(b["pv"])}
    btime = b.get("time") or ""
    # every chip's normal rate, as the search judged it (the chips CSV writes it on every row of the run)
    rates: dict[str, float] = {}
    try:
        with open(f.chips, newline="") as fh:
            for r in csv.DictReader(fh):
                if r.get("time") == btime and r.get("result") == "BASELINE":
                    rates[f"{int(r['board'])}-{int(r['chip'])}"] = float(r.get("baseline_per_hour") or 0)
    except (FileNotFoundError, KeyError, ValueError):
        rates = {}
    # the options that search ran with (kept since 1.13), else what the files show
    opts: dict[str, Any] = {}
    hist = _read_json(f.runs) or {}
    pid = _read_json(f.pid) or {}
    cands = list(hist.values()) + ([{"started": pid.get("started"), "options": pid.get("options") or {}}] if pid else [])
    stamp = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(float(t)))
    before = [c for c in cands if c.get("started") and stamp(c["started"]) <= btime
              and (c.get("options") or {}).get("mode", "search") == "search"]
    if before:
        opts = max(before, key=lambda c: float(c["started"]))["options"] or {}
    hold_c = opts.get("fan_hold_c") if opts.get("fans", "hold") == "hold" else None
    if not opts:            # before 1.13: the temperature the baseline was held at, from the live readings
        try:
            t0 = time.mktime(time.strptime(btime[:19], "%Y-%m-%dT%H:%M:%S")) - float(b.get("watched_min") or 60) * 60 - 900
            lo = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t0))
            tc = [r.get("target_c") for r in _csv(f.live) if r.get("phase") == "baseline" and lo <= (r.get("time") or "") <= btime]
            tc = [float(x) for x in tc if x not in (None, "")]
            hold_c = tc[-1] if tc and tc[-1] > 0 else None
        except (ValueError, OverflowError):
            hold_c = None
    tested_mv = [int(r["mv"]) for r in seg[1:] if (r.get("mv") or "").isdigit()]
    clocks = sorted(curve)
    when = btime[:16].replace("T", " ")
    try:
        label = "the search of " + time.strftime("%b %d", time.strptime(btime[:10], "%Y-%m-%d")).replace(" 0", " ")
    except ValueError:
        label = "the last search"
    prev = _read_json(f.presets) or {}
    prev_temps = [((prev.get("presets") or {}).get(k) or {}).get("target_c") for k in ("high", "middle", "low", "lowest")]
    return {
        "label": label, "base_when": when, "baseline": base,
        "base_ths": float(b.get("avg_ths") or 0), "base_minutes": float(b.get("watched_min") or 0),
        "base_rate": rates, "passed": [list(k) for k in passed],
        "curve": [[m, curve[m]] for m in clocks], "best": [best[0], best[1]],
        "best_confirmed": bool(confirms), "step": _step_of(clocks, 25),
        "mv_step": _step_of(tested_mv, 100) if tested_mv else 100,
        "min_mhz": clocks[0],
        "min_mv": int(opts.get("min_mv") or min(tested_mv + [base["mv"]])),
        "max_mv": int(opts.get("max_mv") or max(tested_mv + [base["mv"]])),
        "hold_c": hold_c,
        "options": {k: opts.get(k) for k in ("fan_hold_c", "fan_min", "preset_max_c", "preset_min", "sigma",
                                              "abort_c", "weak", "fans") if k in opts},
        "last_temps": prev_temps if all(isinstance(t, (int, float)) for t in prev_temps) else None,
        "last_presets_mode": prev.get("mode") or "search",
        "rows": len(seg),
    }


def source_summary(mid: Any) -> dict[str, Any]:
    """For the Tuner page: what 'Rebuild the presets' would build from (no per-chip data)."""
    try:
        src = search_source(mid)
    except RuntimeError as e:
        return {"ok": True, "available": False, "why": str(e)}
    return {"ok": True, "available": True,
            **{k: v for k, v in src.items() if k not in ("base_rate", "passed")},
            "base_chips": len(src["base_rate"])}


def _start_presets(row: dict[str, Any], body: dict[str, Any], mid: str, ip: str, pw: str,
                   pl: dict[str, Any]) -> dict[str, Any]:
    """Build and test the four presets again from the last search's curve (no new search)."""
    src = search_source(mid)
    lim = pl["limits"]
    base, (bm, bv) = src["baseline"], src["best"]
    for what, m, v in (("the baseline", base["mhz"], base["mv"]), ("High", bm, bv)):
        if not (lim["mhz"][0] <= m <= lim["mhz"][1] and lim["mv"][0] <= v <= lim["mv"][1]):
            raise RuntimeError(f"{what} of {src['label']} ({m} MHz / {v} mV) is outside this miner's hard limits now "
                               f"({lim['mhz'][0]}-{lim['mhz'][1]} MHz, {lim['mv'][0]}-{lim['mv'][1]} mV)")
    reuse = str(body.get("baseline") or "fresh") == "reuse"
    if reuse and not src["base_rate"]:
        raise RuntimeError(f"{src['label']} has no saved per-chip baseline, so it can't be reused: pick a fresh baseline")
    opts: dict[str, Any] = {"mode": "presets", "source": src["label"], "source_when": src["base_when"],
                            "baseline": "reuse" if reuse else "fresh", "high": {"mhz": bm, "mv": bv},
                            "start_mhz": base["mhz"], "mv": base["mv"], "base_pv": base["pv"]}
    opts["baseline_min"] = 0 if reuse else _num(body, "baseline_min", NUM_OPTS["baseline_min"])
    opts["sigma"] = _num(body, "sigma", NUM_OPTS["sigma"])
    opts["abort_c"] = _num(body, "abort_c", NUM_OPTS["abort_c"])
    opts["preset_min"] = _num(body, "preset_min", FAN_OPTS["preset_min"])
    opts["weak"] = bool(body.get("weak", True))
    opts["any_model"] = bool(body.get("any_model", False))
    opts["fans"] = "hold" if body.get("fans", "hold") == "hold" else "leave"
    gap = base["pv"] - base["mv"]
    min_mv = max(lim["mv"][0], min(src["min_mv"], base["mv"]))
    max_mv = min(lim["mv"][1], max(src["max_mv"], bv))
    args = ["--presets-only", "--source", str(Files(mid).source),
            "--start-mhz", str(base["mhz"]), "--mv", str(base["mv"]), "--base-pv", str(base["pv"]),
            "--pv-offset", str(gap), "--stock-mhz", str(pl["stock"]["mhz"]),
            "--min-mhz", str(src["min_mhz"]), "--max-mhz", str(bm), "--step", str(src["step"]),
            "--mv-step", str(src["mv_step"]), "--min-mv", str(min_mv), "--preset-min-mv", str(min_mv),
            "--max-mv", str(max_mv), "--baseline-min", f"{opts['baseline_min']:g}",
            "--sigma", f"{opts['sigma']:g}", "--abort-c", f"{opts['abort_c']:g}",
            "--preset-min", f"{opts['preset_min']:g}"]
    if not opts["weak"]:
        args += ["--weak-x", "0"]
    if opts["any_model"]:
        args.append("--any-model")
    if opts["fans"] == "hold":
        opts["fan_min"] = _num(body, "fan_min", FAN_OPTS["fan_min"])
        temps = body.get("preset_temps")
        try:
            temps = [float(t) for t in temps]
        except (TypeError, ValueError):
            raise RuntimeError("give four preset temperatures (High, Middle, Low, Lowest power)")
        if len(temps) != 4:
            raise RuntimeError("give four preset temperatures (High, Middle, Low, Lowest power)")
        for t, (k, lbl) in zip(temps, (("high", "High"), ("middle", "Middle"), ("low", "Low"), ("lowest", "Lowest power"))):
            if not 45 <= t <= opts["abort_c"] - 10:
                raise RuntimeError(f"the {lbl} temperature must be from 45 °C to {opts['abort_c'] - 10:g} °C "
                                   "(10 °C below the abort temperature)")
        opts["preset_temps"] = temps
        # the fresh baseline is held where the search held it, so the error rates compare like for like
        opts["fan_hold_c"] = float(src.get("hold_c") or temps[0])
        args += ["--fan-hold-c", f"{opts['fan_hold_c']:g}", "--fan-min", str(opts["fan_min"]),
                 "--preset-temps", ",".join(f"{t:g}" for t in temps)]
    else:
        opts["fan_hold_c"] = 0
    f = Files(mid)
    f.dir.mkdir(parents=True, exist_ok=True)
    tmp = f.source.with_suffix(".tmp")
    tmp.write_text(json.dumps(src))
    os.replace(tmp, f.source)
    return _spawn(row, mid, ip, pw, args, opts)


_start_lock = __import__("threading").Lock()


def start(row: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    """Start a run. One start at a time: two tabs pressing Start together would otherwise both pass the
    "already running" check during the seconds the start takes, and start two runs on one miner."""
    with _start_lock:
        return _start(row, body)


def _start(row: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    mid = str(row.get("id") or row.get("ip") or "")
    if running(mid):
        raise RuntimeError(f"a tuning run is already going on {row.get('name') or mid}")
    if not SCRIPT.is_file():
        raise RuntimeError(f"{SCRIPT} not found; put asic_tuner.py in the python folder")
    ip = (row.get("ip") or "").strip()
    pw = row.get("password") or ""
    if not ip or not pw or pw == "CHANGE_ME":
        raise RuntimeError("this miner has no IP or password in miners.json")
    hw = row.get("hardware") or {}
    if not hw.get("ok") or hw.get("ip", ip) != ip:
        raise RuntimeError("this miner hasn't been probed yet, so the tuner doesn't know what it is. "
                           "Press Probe (on this page or on Profiles) and start again. New miners are "
                           "probed automatically within a minute or so of being added.")
    if hw.get("profile") != "sc-lite" and not body.get("any_model"):
        raise RuntimeError(f"this miner was detected as {hw.get('name') or hw.get('model') or hw.get('profile') or 'an unknown model'}. "
                           "The tuner is only tested on the SC Lite; tick 'Allow miner models other than the "
                           "SC Lite' to try anyway.")
    for other, r in runs().items():
        if r["running"] and other != mid and (r["info"].get("ip") or "") == ip:
            raise RuntimeError(f"{ip} is already being tuned (as {other})")

    pl = plan_limits(hw)
    lim, dflt = pl["limits"], pl["defaults"]
    if pl["format_note"]:
        raise RuntimeError(pl["format_note"][0].upper() + pl["format_note"][1:] + ".")
    if pl["source"] != "firmware":
        raise RuntimeError("this miner's stock setting hasn't been read from its firmware yet; the tuner's hard "
                           "limits are worked out from it. Press Probe and start again.")
    body = dict(body)
    stock = pl["stock"]
    # the baseline: stock (default), what the miner runs now, or a clock and voltage you choose.
    # The hard limits stay worked out from the stock setting either way.
    kind = str(body.get("baseline") or "stock")
    if body.get("mode") != "presets" and kind not in ("stock", "current", "custom"):
        raise RuntimeError("baseline must be stock, current or custom")
    why = sign_in_problem(ip, pw)        # the tuner signs in fresh; check that works before starting
    if why:
        raise RuntimeError(f"the miner at {ip} refused a fresh sign-in, so the tuner couldn't start: {why}. "
                           "The dashboard can keep showing it online on its existing session (and from port "
                           "4028) while new sign-ins fail. Open http://" + ip + " and sign in there: if that "
                           "fails too, the miner's web service is stuck; turn the miner off and on again (a "
                           "Restart from the app needs a sign-in too), then start the run again. If it says "
                           "the password is wrong, fix the password for this miner in the app.")
    if body.get("mode") == "presets":
        return _start_presets(row, body, mid, ip, pw, pl)
    live = read_live_plan(ip, pw)        # also where the miner goes back to if nothing is confirmed
    if kind == "stock":
        base = dict(stock)
    elif kind == "current":
        if not live:
            raise RuntimeError("couldn't read the setting this miner runs now, so it can't be the baseline")
        base = dict(live)
    else:
        try:
            base = {"mhz": int(body.get("start_mhz")), "mv": int(body.get("mv"))}
        except (TypeError, ValueError):
            raise RuntimeError("the baseline clock and voltage must be whole numbers")
        base["pv"] = base["mv"] + pl["pv_gap"]
    for k, (lo, hi) in (("mhz", lim["mhz"]), ("mv", lim["mv"])):
        if not lo <= base[k] <= hi:
            what = {"stock": "the stock", "current": "the current", "custom": "your"}[kind]
            raise RuntimeError(f"{what} baseline {base['mhz']} MHz / {base['mv']} mV is outside this miner's hard "
                               f"limits ({lim['mhz'][0]}-{lim['mhz'][1]} MHz, {lim['mv'][0]}-{lim['mv'][1]} mV)")
    if not -500 <= base["pv"] - base["mv"] <= 1000:
        raise RuntimeError(f"the baseline's PV {base['pv']} is too far from its {base['mv']} mV")
    body["start_mhz"], body["mv"] = base["mhz"], base["mv"]
    if body.get("min_mv") not in (None, ""):
        body["preset_min_mv"] = body["min_mv"]     # one lowest voltage for the trim, the map and the presets
    args: list[str] = []
    opts: dict[str, Any] = {}
    for name, spec in NUM_OPTS.items():
        if name in MHZ_OPTS or name in MV_OPTS:   # around this miner's stock plan
            lo, hi = lim["mhz"] if name in MHZ_OPTS else lim["mv"]
            spec = (spec[0], spec[1], lo, hi, dflt[name])
        opts[name] = _num(body, name, spec)
        args += [spec[0], str(opts[name])]
    args += ["--base-pv", str(base["pv"]), "--stock-mhz", str(stock["mhz"])]
    opts["base_pv"] = base["pv"]
    opts["baseline"] = kind
    opts["before"] = live          # what the miner ran when the run started
    cm = body.get("confirm_min")
    if cm not in (None, "", 0, "0"):
        try:
            cm = float(cm)
        except (TypeError, ValueError):
            raise RuntimeError("confirm minutes must be a number (or empty: twice the test length)")
        if not 5 <= cm <= 240:
            raise RuntimeError("confirm minutes must be between 5 and 240")
        opts["confirm_min"] = cm
        args += ["--confirm-min", f"{cm:g}"]
    if not opts["min_mv"] <= opts["mv"] <= opts["max_mv"]:
        raise RuntimeError("voltages must satisfy Lowest mV <= baseline mV <= Highest mV")
    if opts["preset_min_mv"] > opts["mv"]:
        raise RuntimeError("the lowest preset voltage must be at or below the baseline mV")
    body["boost"] = True      # the voltage curve always adds voltage where a clock needs it
    for name, flag in BOOL_OPTS.items():
        on = bool(body.get(name, True))
        opts[name] = on
        if not on:
            args.append(flag)
    opts["weak"] = bool(body.get("weak", True))     # judge already-noisy chips on getting worse
    if not opts["weak"]:
        args += ["--weak-x", "0"]
    pvo = body.get("pv_offset")
    if pvo not in (None, ""):
        try:
            pvo = int(pvo)
        except (TypeError, ValueError):
            raise RuntimeError("PV offset must be a whole number (or empty for auto)")
        if not -500 <= pvo <= 1000:
            raise RuntimeError("PV offset must be between -500 and 1000")
        opts["pv_offset"] = pvo
        args += ["--pv-offset", str(pvo)]
        # the PV you set applies from the baseline on (the search and confirm steps follow the baseline's gap)
        base["pv"] = base["mv"] + pvo
        i = args.index("--base-pv")
        args[i + 1] = str(base["pv"])
        opts["base_pv"] = base["pv"]
    else:
        opts["pv_offset"] = None
        if pl["source"] == "firmware" and -500 <= pl["pv_gap"] <= 1000:
            # auto: the gap of the miner's stock plan (not whatever it happens to be set to now)
            args += ["--pv-offset", str(pl["pv_gap"])]
            opts["pv_auto"] = pl["pv_gap"]
    # Lowest MHz (search only): where the voltage curve starts (default 100 MHz below stock, on the step grid).
    # (Before 1.9 this was "Lowest preset MHz" in the Presets section.)
    pmh = body.get("min_mhz", body.get("preset_min_mhz"))
    opts["min_mhz"] = None
    if body.get("mode") != "confirm":
        if pmh in (None, "", 0, "0"):
            st = int(opts.get("step") or 25)
            pmh = max(300, (int(opts["start_mhz"]) - 100) // st * st)
        try:
            pmh = int(pmh)
        except (TypeError, ValueError):
            raise RuntimeError("Lowest MHz must be a whole number")
        if not lim["mhz"][0] <= pmh <= lim["mhz"][1]:
            raise RuntimeError(f"Lowest MHz must be between {lim['mhz'][0]} and {lim['mhz'][1]}")
        if pmh >= int(opts.get("max_mhz") or 0):
            raise RuntimeError("Lowest MHz must be below Highest MHz")
        opts["min_mhz"] = pmh
        args += ["--min-mhz", str(pmh)]
    opts["any_model"] = bool(body.get("any_model", False))
    if opts["any_model"]:
        args.append("--any-model")
    opts["mode"] = "confirm" if body.get("mode") == "confirm" else "search"
    if opts["mode"] == "confirm":
        for name, flag, typ, lo, hi in (("confirm_mhz", "--confirm-mhz", int, *lim["mhz"]),
                                         ("confirm_mv", "--confirm-mv", int, *lim["mv"]),
                                         ("confirm_hours", "--confirm-hours", float, 0.5, 48)):
            try:
                val = typ(body.get(name))
            except (TypeError, ValueError):
                raise RuntimeError(f"{name} must be a number")
            if not lo <= val <= hi:
                raise RuntimeError(f"{name} must be between {lo:g} and {hi:g}")
            opts[name] = val
            args += [flag, str(val)]

    # fans: hold a temperature, or leave them alone
    opts["fans"] = "hold" if body.get("fans", "hold") == "hold" else "leave"
    if opts["fans"] == "hold":
        opts["fan_hold_c"] = _num(body, "fan_hold_c", FAN_OPTS["fan_hold_c"])
        opts["fan_min"] = _num(body, "fan_min", FAN_OPTS["fan_min"])
        if opts["fan_hold_c"] > opts["abort_c"] - 10:
            raise RuntimeError("the target temperature must be at least 10 °C below the abort temperature")
        args += ["--fan-hold-c", f"{opts['fan_hold_c']:g}", "--fan-min", str(opts["fan_min"])]
    else:
        opts["fan_hold_c"] = 0
    # presets (search only)
    opts["presets"] = opts["mode"] == "search" and bool(body.get("presets", True))
    if opts["presets"]:
        opts["preset_min"] = _num(body, "preset_min", FAN_OPTS["preset_min"])
        args += ["--presets", "--preset-min", f"{opts['preset_min']:g}"]
        if opts["fans"] == "hold":
            opts["preset_max_c"] = _num(body, "preset_max_c", FAN_OPTS["preset_max_c"])
            if opts["preset_max_c"] < opts["fan_hold_c"]:
                raise RuntimeError("the warmest preset temperature can't be below the target temperature")
            if opts["preset_max_c"] > opts["abort_c"] - 12:
                raise RuntimeError("the warmest preset temperature must be at least 12 °C below the abort temperature")
            args += ["--preset-max-c", f"{opts['preset_max_c']:g}"]

    extra = {}
    if opts.get("pv_auto") is not None:
        s = pl["stock"]
        extra["SCLITE_PV_NOTE"] = f"the gap of the miner's stock setting, {s['mv']} mV / PV {s['pv']}"
    return _spawn(row, mid, ip, pw, args, opts, extra)


def _spawn(row: dict[str, Any], mid: str, ip: str, pw: str, args: list[str], opts: dict[str, Any],
           extra_env: dict[str, str] | None = None) -> dict[str, Any]:
    f = Files(mid)
    f.dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    env = dict(os.environ, SCLITE_IP=ip, SCLITE_PASSWORD=pw, PYTHONUNBUFFERED="1",
               SCLITE_TUNER_DATA=str(f.dir), SCLITE_TUNER_RUN=f"{started:.0f}", **(extra_env or {}))
    log = open(f.log, "a", buffering=1)
    log.write(f"\n===== started from dashboard {time.strftime('%Y-%m-%d %H:%M:%S')} "
              f"miner {mid} ({ip}) =====\n")
    proc = subprocess.Popen(
        [sys.executable, "-u", str(SCRIPT), *args],
        cwd=str(PYDIR), env=env, stdout=log, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )
    log.close()
    _procs[mid] = proc
    info = {"pid": proc.pid, "miner_id": mid, "miner_name": row.get("name"),
            "ip": ip, "started": started, "run": f"{started:.0f}", "options": opts}
    f.pid.write_text(json.dumps(info))
    _remember_run(f, info)
    return info


def _remember_run(f: "Files", info: dict[str, Any]) -> None:
    """Keep every run's options (asic_tuner_runs.json, the last 50), so a later presets-only run can find
    what an earlier search used (its temperatures, its lowest and highest mV) after other runs came."""
    try:
        doc = _read_json(f.runs) or {}
        doc[str(info["run"])] = {"started": info["started"], "options": info.get("options") or {}}
        keep = sorted(doc, key=lambda k: -float(doc[k].get("started") or 0))[:50]
        tmp = f.runs.with_suffix(".tmp")
        tmp.write_text(json.dumps({k: doc[k] for k in keep}))
        os.replace(tmp, f.runs)
    except (OSError, ValueError, TypeError):
        pass


def stop(mid: Any) -> dict[str, Any]:
    info = running(mid)
    if not info:
        return {"ok": True, "stopped": False, "message": "no tuning run on this miner"}
    os.kill(int(info["pid"]), signal.SIGINT)  # the tuner catches this and restores the setting
    return {"ok": True, "stopped": True,
            "message": "stop sent; the tuner is putting back what the miner ran before (or the confirmed best)"}


# ------------------------------------------------------------------ reading ---

def _csv(path: Path) -> list[dict[str, str]]:
    try:
        with open(path, newline="") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


def _tail(path: Path, n: int = 40) -> list[str]:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 16000))
            lines = f.read().decode("utf-8", "replace").splitlines()
        return lines[-n:]
    except FileNotFoundError:
        return []


def _since(info: dict[str, Any] | None) -> str | None:
    if info and info.get("started"):
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(float(info["started"])))
    return None


def _run_plan(f: "Files", info: dict[str, Any] | None) -> dict[str, Any] | None:
    """The steps of this miner's latest run (asic_tuner_plan.json), if it belongs to that run."""
    doc = _read_json(f.plan)
    if not doc or not info:
        return None
    if str(doc.get("run") or "") != str(info.get("run") or ""):
        return None
    return doc


def status(mid: Any = None) -> dict[str, Any]:
    all_runs = runs()
    if mid in (None, ""):   # no miner asked for: a running one, else the latest run
        live_ones = [k for k, r in all_runs.items() if r["running"]]
        latest = sorted(all_runs, key=lambda k: -float(all_runs[k]["info"].get("started") or 0))
        mid = (live_ones or latest or [""])[0]
    f = Files(mid)
    info = running(mid)
    last_info = info or _read_json(f.pid)
    live = _csv(f.live)
    live = live if live and "worst_chip" in live[0] else []
    results = _csv(f.results)
    results = results if results and "worst_chip" in results[0] else []
    # only this run's rows (the CSV keeps every run); the tuner writes local-time ISO stamps
    since = _since(last_info)
    if since:
        results = [r for r in results if r.get("time", "") >= since]
        live = [r for r in live if r.get("time", "") >= since]

    # rows of the step in progress (same plan + phase as the last row, contiguous)
    step: list[dict[str, str]] = []
    if live:
        key = (live[-1]["phase"], live[-1]["mhz"], live[-1]["mv"])
        prev_elapsed = float("inf")
        for r in reversed(live):
            e = float(r.get("elapsed_min") or 0)
            if (r["phase"], r["mhz"], r["mv"]) != key or e > prev_elapsed:
                break
            step.append(r)
            prev_elapsed = e
        step.reverse()

    fails: dict[str, int] = {}
    for r in results:
        if r["result"].startswith("FAIL") and r.get("worst_chip"):
            fails[r["worst_chip"]] = fails.get(r["worst_chip"], 0) + 1
    search_rows = [r for r in results if "(preset" not in r["result"]]
    confirmed = [r for r in search_rows if r["result"] in ("PASS (confirm)", "CONFIRMED")]
    rejected = {(r["mhz"], r["mv"]) for r in search_rows if r["result"] == "REJECTED"}
    passes = [r for r in search_rows if r["result"].startswith("PASS") and (r["mhz"], r["mv"]) not in rejected]
    best = confirmed[-1] if confirmed else (
        max(passes, key=lambda r: (int(r["mhz"]), -int(r["mv"]))) if passes else None)

    log = _tail(f.log)
    finished = None
    for line in reversed(log):
        if line.startswith(("BEST:", "RESTORED:", "NOT RUN:", "PRESETS DONE:", "PRESETS STOPPED")) or "miner left on" in line:
            finished = line.strip()
            break
        if line.startswith("====="):
            break
    # once a run has ended, the tuner's own BEST line is the answer
    if not info and finished and finished.startswith("BEST:"):
        m = re.match(r"BEST: (\d+) MHz at (\d+) mV \(PV (\d+)\)", finished)
        if m:
            mhz, mv, pv = m.groups()
            same = [r for r in results if r["mhz"] == mhz and r["mv"] == mv and r.get("avg_ths")]
            best = {"mhz": mhz, "mv": mv, "pv": pv,
                    "avg_ths": same[-1]["avg_ths"] if same else ""}
            confirmed = [r for r in confirmed if r["mhz"] == mhz and r["mv"] == mv]
    if not info and finished and finished.startswith("RESTORED:"):
        best, confirmed = None, []   # nothing new confirmed: the miner is back on what it ran before

    presets = _read_json(f.presets)
    if presets and last_info and str(presets.get("run") or "") != str(last_info.get("run") or ""):
        presets = None   # from an earlier run

    return {
        "ok": True,
        "miner_id": mid,
        "running": bool(info),
        "info": last_info,
        "script_found": SCRIPT.is_file(),
        "folder": str(f.dir),
        "step": step[-60:],
        "results": results[-40:],
        "fail_chips": sorted(fails.items(), key=lambda kv: -kv[1]),
        "best": best,
        "best_confirmed": bool(confirmed),
        "finished_line": finished,
        "presets": presets,
        "plan": _run_plan(f, last_info),
        "log": log,
        "runs": {k: {"running": r["running"], "name": r["info"].get("miner_name"),
                     "started": r["info"].get("started")} for k, r in all_runs.items()},
        "defaults": ({k: v[4] for k, v in NUM_OPTS.items()} | {k: True for k in BOOL_OPTS} | {"weak": True}
                     | {k: v[4] for k, v in FAN_OPTS.items()} | {"fans": "hold", "presets": True}),
    }


def chipmap(mid: Any, q: dict[str, list[str]]) -> dict[str, Any]:
    """Per-chip map: the live reading, or one finished test (?t=&mhz=&mv=&r=)."""
    f = Files(mid)
    if not q.get("t"):
        try:
            doc = json.loads(f.chipmap.read_text())
        except (FileNotFoundError, ValueError):
            return {"ok": True, "chips": []}
        since = _since(_read_json(f.pid))
        if since and doc.get("time", "") < since:
            return {"ok": True, "chips": []}
        doc.update(ok=True, source="live")
        return doc
    t, mhz, mv, r = (q.get(k, [""])[0] for k in ("t", "mhz", "mv", "r"))
    chips = []
    watched = None
    for row in _csv(f.chips):
        if row["time"] == t and row["mhz"] == mhz and row["mv"] == mv and row["result"] == r:
            lim = row.get("limit") or ""
            chips.append([int(row["board"]), int(row["chip"]), int(float(row["new_hw"] or 0)),
                          float(lim) if lim else None, float(row.get("baseline_per_hour") or 0)])
            watched = row.get("watched_min")
    return {"ok": True, "source": "test", "time": t, "mhz": mhz, "mv": mv, "result": r,
            "phase": "baseline" if r == "BASELINE" else "test", "judged": r != "BASELINE",
            "elapsed_min": float(watched or 0), "chips": chips}


# ------------------------------------------------------------------ routing ---

def _q(handler: Any) -> dict[str, list[str]]:
    return parse_qs(urlparse(handler.path).query)


def _send_file(handler: Any, data: bytes, name: str, ctype: str) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Content-Disposition", f'attachment; filename="{name}"')
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(data)


def download(handler: Any, q: dict[str, list[str]], json_response: Callable) -> None:
    """A miner's tuner files: log (this run, or all), the CSVs, the run plan, or all of them as a zip."""
    import io
    import zipfile
    mid = q.get("miner", [""])[0]
    what = q.get("what", ["log"])[0]
    f = Files(mid)
    stamp = time.strftime("%Y%m%d-%H%M")
    base = f"asic_tuner-{safe_id(mid)}-{stamp}"
    files = {"results": f.results, "chips": f.chips, "live": f.live, "plan": f.plan, "presets": f.presets}
    if what in ("log", "fulllog"):
        try:
            text = f.log.read_text(errors="replace")
        except FileNotFoundError:
            return json_response(handler, 404, {"ok": False, "error": "no tuner log for this miner yet"})
        if what == "log":   # only the latest run (each run starts with a "=====" line)
            i = text.rfind("\n===== started")
            text = text[i + 1:] if i >= 0 else text
        return _send_file(handler, text.encode(), f"{base}{'' if what == 'log' else '-all'}.log.txt", "text/plain; charset=utf-8")
    if what in files:
        p = files[what]
        if not p.is_file():
            return json_response(handler, 404, {"ok": False, "error": f"no {what} file for this miner yet"})
        ext = ".json" if p.suffix == ".json" else ".csv"
        return _send_file(handler, p.read_bytes(), f"{base}-{what}{ext}",
                          "application/json" if ext == ".json" else "text/csv; charset=utf-8")
    if what == "zip":
        if not f.dir.is_dir():
            return json_response(handler, 404, {"ok": False, "error": "no tuner files for this miner yet"})
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted(f.dir.iterdir()):
                if p.is_file() and not p.name.endswith((".tmp", ".pid.json")):
                    z.write(p, p.name)
        return _send_file(handler, buf.getvalue(), f"{base}.zip", "application/zip")
    json_response(handler, 400, {"ok": False, "error": "what must be log, fulllog, results, chips, live, plan, presets or zip"})


def handle_get(handler: Any, path: str, json_response: Callable) -> bool:
    if path == "/api/tuner/download":
        download(handler, _q(handler), json_response)
        return True
    if path == "/api/tuner/status":
        json_response(handler, 200, status(_q(handler).get("miner", [""])[0]))
        return True
    if path == "/api/tuner/source":
        json_response(handler, 200, source_summary(_q(handler).get("miner", [""])[0]))
        return True
    if path == "/api/tuner/chipmap":
        q = _q(handler)
        mid = q.get("miner", [""])[0] or status()["miner_id"]
        json_response(handler, 200, chipmap(mid, q))
        return True
    return False


def handle_post(handler: Any, path: str, body: dict[str, Any], json_response: Callable,
                load_registry: Callable[[], list[dict[str, Any]]]) -> bool:
    if path == "/api/tuner/start":
        mid = body.get("miner_id")
        row = next((m for m in load_registry() if (m.get("id") or m.get("ip")) == mid), None)
        if not row:
            json_response(handler, 404, {"ok": False, "error": f"unknown miner id: {mid}"})
            return True
        try:
            info = start(row, body)
        except RuntimeError as e:
            json_response(handler, 400, {"ok": False, "error": str(e)})
            return True
        json_response(handler, 200, {"ok": True, "info": info})
        return True
    if path == "/api/tuner/stop":
        mid = (body or {}).get("miner_id")
        if not mid:   # older pages: stop the only running run
            live_ones = [k for k, r in runs().items() if r["running"]]
            if len(live_ones) != 1:
                json_response(handler, 400, {"ok": False, "error": "say which miner to stop"})
                return True
            mid = live_ones[0]
        json_response(handler, 200, stop(mid))
        return True
    return False
