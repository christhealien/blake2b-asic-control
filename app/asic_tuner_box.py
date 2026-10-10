#!/usr/bin/env python3
"""Blake2b ASIC Control: clock tuner for the SC Box and HS Box (power plans like "550 MHz 0.44 V 90 RPM 90 RPM").

The SC Lite tuner (asic_tuner.py) maps clock AND voltage. On these boxes only the clock is worth tuning: in testing
(both boxes, a plug-in meter) a 0.01 V step made no measurable difference in wall power, while every 25 MHz moved
power (about 7 W on the SC Box, 4-5 W on the HS Box) and hashrate in step, at the same J/TH. So this tuner keeps the
voltage and fan fields exactly as the miner has them and changes the clock only. It judges every setting the same
way as the SC Lite tuner, by each chip's own hardware errors from /dbg/icinfo (the boxes report it in the same form).

  1. BASELINE: the baseline clock (--start-mhz, default what it runs now) for --baseline-min: every chip's normal
     error rate. Nothing can fail here except a board reset.
  2. CLIMB: one --step (25 MHz) at a time up to --max-mhz (the dashboard allows up to stock + 25), each held
     --hold-min after --settle-min (15: the SC Box's fans run near full speed for about 15 minutes after any
     settings write). It stops at the first clock that isn't clean (after one retest unless it failed clearly).
     Both boxes made errors 25 MHz above what they ran in testing, so this is usually a short step.
  3. CONFIRM: the fastest clean clock above the baseline once more, for --confirm-min; if it fails, the next one down.
  4. PRESETS: High (the confirmed clock, or the baseline), Middle, Low and Lowest power at 25 / 50 / 75 MHz below it,
     each held --preset-min (a preset that fails tries one step lower). Written to asic_tuner_presets.json.
  5. It ends on High when High passed; otherwise (or after a stop, a safety stop or an error) it puts back what the
     miner ran before, the reliable way (box_plan.put_back: the plan it ran first, as a manual plan, then its fields).

--presets-only skips the climb and the confirm: the four presets from the baseline clock down.
Safety: any reading at --abort-c (88 C) stops the run and puts the setting back; so do 10 readings with no
temperature, a board reset during the baseline, and Ctrl+C / the dashboard's Stop.
Fans are never written (these firmwares steer them to their own fan target).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from typing import Any

import asic_tuner as at
import sclite_common as sc

try:
    import box_plan
except ImportError:          # an older layout: it lives next to the dashboard
    sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "webui"))
    import box_plan

log, now, patient, Abort = at.log, at.now, at.patient, at.Abort


class BoxPlan(at.Plan):
    """A box setting: the clock, plus the voltage and fan fields kept from the miner's own plan. mv (for the CSV
    columns and the Tuner page) is the voltage in mV (0.44 V -> 440); there is no PV."""

    def __init__(self, mhz: int, volts: str, fan_a: int, fan_b: int):
        self.mhz, self.volts, self.fan_a, self.fan_b = int(mhz), str(volts), int(fan_a), int(fan_b)
        self.mv, self.pv = box_plan.millivolts(volts), ""

    def with_(self, mhz: int | None = None, mv: int | None = None) -> "BoxPlan":
        return BoxPlan(self.mhz if mhz is None else mhz, self.volts, self.fan_a, self.fan_b)

    def plan_text(self) -> str:
        return f"{self.mhz} MHz {self.volts} V {self.fan_a} RPM {self.fan_b} RPM"

    def text(self) -> str:
        return f"{self.mhz} MHz at {self.volts} V"


STOCK = {"mhz": None}      # the stock clock this run's limits come from (checked again on every write)


def box_apply(plan: BoxPlan) -> None:
    """Write the clock as a manual plan (voltage, fan fields and every other setting as the miner has them). Never
    above stock + 25, and nothing at all if the miner's stock plan changed during the run (algorithm switch)."""
    def write() -> None:
        try:
            box_plan.write_clock(sc.get_setting, sc.put_setting, plan.mhz, base_text=plan.plan_text(),
                                 stock_mhz=STOCK["mhz"], headroom=box_plan.GRID)
        except RuntimeError as e:
            if "stock plan" in str(e) or "above what" in str(e):
                raise Abort(str(e)) from None      # not the miner being briefly unreachable: stop the run
            raise
    patient(write, f"apply {plan.text()}")


def box_not_applied(plan: BoxPlan) -> tuple[str, str]:
    """(hard, soft) like asic_tuner.not_applied: hard = the setting isn't this plan; soft = a board reports another clock."""
    try:
        s = sc.get_setting()
        if s.get("manual") is not True or s.get("manualPowerplan") != plan.plan_text():
            return f"the miner's setting reads {box_plan.running_text(s) or '?'}", ""
    except Exception as e:
        return f"couldn't read the setting back ({e})", ""
    try:
        devs = at.bfg_devs()
    except Exception:
        return "", ""
    want = at.pll(plan.mhz)
    off = [f"board {i} at {float(d.get('clock') or 0):g} MHz" for i, d in enumerate(devs)
           if d.get("clock") is not None and abs(float(d["clock"]) - want) > 1.0 and abs(float(d["clock"]) - plan.mhz) > 1.0]
    return "", ("boards report another clock: " + ", ".join(off)) if off else ""


# the SC Lite tester's settle / watch call these by name: point them at the box versions
at.apply_plan = box_apply
at.not_applied = box_not_applied


def box_snapshot() -> dict:
    """Hottest chip-sensor reading and hashrate from port 4028 (devs: tstemp-*, MHS 20s); the web API's devs
    (one temperature per board) if port 4028 doesn't answer."""
    try:
        devs = at.bfg_devs()
        boards, temps = [], []
        for d in devs:
            ts = [float(v) for k, v in d.items()
                  if (str(k).lower().startswith("tstemp") or k == "Temperature") and isinstance(v, (int, float)) and 0 < v < 150]
            mhs = d.get("MHS 20s") if isinstance(d.get("MHS 20s"), (int, float)) else d.get("MHS 5s")
            boards.append({"temp": max(ts) if ts else None, "hr_ths": float(mhs or 0) / 1e6})
            temps += ts
        if boards:
            return {"boards": boards, "max_t": max(temps) if temps else None}
    except Exception:
        pass
    return sc.board_snapshot()


class BoxTester(at.Tester):
    def heat(self) -> dict:
        snap = patient(box_snapshot, "temperatures")
        t = snap.get("max_t")
        if t is None:
            self._no_t = getattr(self, "_no_t", 0) + 1
            if self._no_t >= self.NO_TEMP_LIMIT:
                raise Abort(f"the miner reported no temperature for {self._no_t} readings in a row")
        else:
            self._no_t = 0
        if t is not None and t >= self.a.abort_c:
            raise Abort(f"a chip sensor reached {t:.1f} C (abort at {self.a.abort_c} C)")
        return snap


def main() -> None:
    ap = argparse.ArgumentParser(description="Clock tuner for the SC Box and HS Box (per-chip errors, clock only)")
    ap.add_argument("--start-mhz", type=int, default=0, help="baseline clock (default: what the miner runs now)")
    ap.add_argument("--max-mhz", type=int, default=0, help="highest clock the climb tries (default: stock)")
    ap.add_argument("--stock-mhz", type=int, default=0, help="the firmware's stock clock (default: read from the miner)")
    ap.add_argument("--step", type=int, default=25, help="MHz per step")
    ap.add_argument("--baseline-min", type=float, default=60)
    ap.add_argument("--hold-min", type=float, default=30)
    ap.add_argument("--settle-min", type=float, default=15)
    ap.add_argument("--confirm-min", type=float, default=60)
    ap.add_argument("--preset-min", type=float, default=30)
    ap.add_argument("--no-confirm", action="store_true")
    ap.add_argument("--no-presets", action="store_true")
    ap.add_argument("--presets-only", action="store_true", help="no climb: test the four presets from the baseline clock down")
    ap.add_argument("--sigma", type=float, default=2.5)
    ap.add_argument("--margin", type=float, default=2)
    ap.add_argument("--pseudo", type=float, default=1)
    ap.add_argument("--hash-tol", type=float, default=0.04)
    ap.add_argument("--weak-x", type=float, default=8)
    ap.add_argument("--weak-min", type=float, default=20)
    ap.add_argument("--weak-factor", type=float, default=1.5)
    ap.add_argument("--retries", type=int, default=1)
    ap.add_argument("--poll-s", type=float, default=60, help="seconds between readings (the boxes' web API is slow)")
    ap.add_argument("--abort-c", type=float, default=88)
    ap.add_argument("--offline-min", type=float, default=5)
    ap.add_argument("--run-id", default=os.environ.get("SCLITE_TUNER_RUN", ""))
    a = ap.parse_args()
    # what the SC Lite tester reads that has no meaning here
    a.fan_hold_c, a.fan_min, a.fan_settle_min, a.no_clock_scale, a.retest_all = 0, 30, 0, False, False
    at.OFFLINE_S = max(0.5, a.offline_min) * 60

    def _stop(_sig: int, _frame: Any) -> None:
        raise KeyboardInterrupt
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    patient(sc.login, "log in")
    try:
        status = patient(lambda: sc.api("GET", "/mcb/status"), "read model")
        model = str((status or {}).get("model") or "unknown") if isinstance(status, dict) else "unknown"
    except Exception:
        model = "unknown"
    log(f"miner model: {model}")
    s0 = patient(sc.get_setting, "read the setting")
    pre_text, stock_text = box_plan.running_text(s0), box_plan.stock_text(s0)
    pre_p, stock_p = box_plan.parse(pre_text), box_plan.parse(stock_text)
    if "BOX" not in model.upper().replace(" ", "").replace("-", "") or not pre_p:
        log(f"this tuner is for the SC Box and HS Box; this miner ({model}) runs \"{pre_text[:60]}\". Nothing was changed.")
        print(f"\nNOT RUN: not an SC Box / HS Box setting ({model})")
        sys.exit(2)
    if not stock_p:
        print("\nNOT RUN: the miner didn't report its stock plan")
        sys.exit(2)
    stock = stock_p["mhz"]
    if a.stock_mhz and a.stock_mhz != stock:
        print(f"\nNOT RUN: the miner's stock plan reads {stock} MHz, not {a.stock_mhz} as at the last probe (another algorithm?)")
        sys.exit(2)
    try:
        lim = box_plan.limits(stock)
    except ValueError as e:
        print(f"\nNOT RUN: {e}")
        sys.exit(2)
    STOCK["mhz"] = stock
    pre_raw = {k: s0.get(k) for k in box_plan.FIELDS}
    pre = BoxPlan(pre_p["mhz"], pre_p["volts"], pre_p["fan_a"], pre_p["fan_b"])
    pre_how = "its own manual setting" if s0.get("manual") else "its stock plan"
    base = pre.with_(mhz=a.start_mhz or pre.mhz)
    a.max_mhz = a.max_mhz or stock
    if not lim["lo"] <= base.mhz <= lim["hi"] or base.mhz % a.step:
        # never above stock: the baseline isn't judged, and High can be the baseline clock
        print(f"\nNOT RUN: the baseline {base.mhz} MHz must be {lim['lo']}-{lim['hi']} MHz on the {a.step} MHz grid")
        sys.exit(2)
    if a.max_mhz > lim["tune"]:
        print(f"\nNOT RUN: the highest clock {a.max_mhz} MHz is above stock + {box_plan.GRID} ({lim['tune']})")
        sys.exit(2)
    log(f"before the run the miner is on {pre.text()} ({pre_how}); stock {stock_text or '?'}")
    log(f"only the clock changes: voltage {pre.volts} V and the fan fields stay as the miner has them; fans are never written")
    log(f"stopping puts the miner back on {pre.text()} ({pre_how})")

    tester = BoxTester(a)
    at.PROG = prog = at.Progress(a.run_id)
    climb = [] if a.presets_only else list(range(base.mhz + a.step, a.max_mhz + 1, a.step))
    prog.add("baseline", "Baseline", f"{base.text()} for {a.baseline_min:g} min (after {a.settle_min:g} min to settle): "
             "learns every chip's normal error rate. Nothing can fail here.")
    if not a.presets_only:
        prog.add("climb", "Clock climb", (f"{climb[0]} up to {climb[-1]} MHz, {a.step} MHz at a time, {a.hold_min:g} min each "
                 f"after {a.settle_min:g} min to settle; it stops at the first clock that isn't clean") if climb
                 else "nothing to climb: the baseline is already the highest clock allowed")
        for m in climb:
            prog.item("climb", m, f"{m} MHz")
        if not a.no_confirm:
            prog.add("confirm", "Confirm", f"the fastest clean clock once more, for {a.confirm_min:g} min; if it fails, the next one down")
    if not a.no_presets:
        prog.add("presets", "Presets", f"High, Middle, Low and Lowest power, 25 MHz apart, {a.preset_min:g} min each (clock only)")
        for k, lbl in at.PRESET_ORDER:
            prog.item("presets", k, lbl)
    prog.add("finish", "Finish", f"leaves the miner on High when it passed; otherwise it goes back to what it ran before ({pre.text()})")
    prog.write()

    done: dict[int, bool] = {}
    stats: dict[int, dict] = {}

    def once(plan: BoxPlan, label: str = "", key: Any = None, hold: float | None = None) -> bool:
        prog.begin(plan, label, key, item_label=f"{plan.mhz} MHz")
        try:
            ok, why, st = tester.test(plan, hold)
        except Abort as e:
            prog.end(False, str(e))
            raise
        prog.end(ok, why)
        at.record(plan, ("PASS" if ok else "FAIL") + label, why, st, tester.base_rate)
        log(f"{'PASS' if ok else 'FAIL'}{label} {plan.text()}: {why}")
        st["why"] = why
        stats[plan.mhz] = st
        return ok

    def run(plan: BoxPlan, label: str = "", key: Any = None, hold: float | None = None) -> bool:
        ok = once(plan, label, key, hold)
        for _ in range(a.retries):
            if ok:
                break
            if (stats.get(plan.mhz) or {}).get("decisive"):
                log("no retest: it went over its whole limit in under half the test, so it wasn't a fluke")
                prog.note_last("over its limit in under half the test: not retested")
                break
            log(f"retest {plan.text()} once, in case that was a fluke")
            ok = once(plan, (label[:-1] + " retest)") if label.endswith(")") else " (retest)", key, hold)
        return ok

    best: BoxPlan = base
    confirmed = False
    final: BoxPlan | None = None      # what the run ends on (None: back to what it ran before)
    presets_stop = ""
    try:
        prog.set("baseline", "run")
        tester.baseline(base)
        prog.set("baseline", "pass", "learned every chip's normal rate" + (
            "; noisy chips judged on getting clearly worse: " + ", ".join(
                f"{at.chip_name(k)} ({tester.base_rate[k]:.0f}/h)" for k in tester.weak) if tester.weak else ""))
        done[base.mhz] = True

        if not a.presets_only:
            prog.stage = "climb"
            if climb:
                prog.set("climb", "run")
            passed = [base]
            for m in climb:
                plan = base.with_(mhz=m)
                ok = run(plan, key=m)
                done[m] = ok
                if not ok:
                    prog.skip_rest("climb", f"not tried: {m} MHz wasn't clean")
                    break
                passed.append(plan)
            best = passed[-1]
            prog.set("climb", "pass" if best is not base else ("fail" if climb else "skip"),
                     f"fastest clean clock {best.mhz} MHz" if best is not base
                     else (f"nothing above {base.mhz} MHz was clean" if climb else "nothing to climb"))
            if not a.no_confirm:
                prog.stage = "confirm"
                if best is base:
                    prog.set("confirm", "skip", f"nothing to confirm: High is the baseline clock, {base.mhz} MHz")
                else:
                    prog.set("confirm", "run")
                    for cand in reversed(passed[1:]):
                        log(f"CONFIRM {cand.text()} for {a.confirm_min:g} min")
                        if run(cand, " (confirm)", key=f"c{cand.mhz}", hold=a.confirm_min):
                            best, confirmed = cand, True
                            break
                    else:
                        best = base
                    prog.set("confirm", "pass" if confirmed else "fail",
                             f"confirmed {best.mhz} MHz" if confirmed else f"nothing confirmed: High is the baseline clock, {base.mhz} MHz")
            else:
                confirmed = best is not base

        # presets and the end of the run never go above stock: a clean stock + 25 only says there's headroom
        top = best if best.mhz <= lim["hi"] else base.with_(mhz=lim["hi"])
        if top is not best:
            log(f"{best.mhz} MHz was clean, but presets stay at stock or below: High is {top.mhz} MHz")
        if not a.no_presets:
            prog.stage = "presets"
            prog.set("presets", "run")
            doc = {"run": a.run_id, "created": now(), "complete": False, "box": True, "fan_hold_c": None,
                   "preset_min": a.preset_min, "presets": {}, "mode": "presets" if a.presets_only else "search",
                   "volts": base.volts}
            at.write_presets(doc)
            try:
                for i, (key, label) in enumerate(at.PRESET_ORDER):
                    pit = prog.item("presets", key, label)
                    entry: dict[str, Any] = {"label": label, "ok": False, "box": True, "mv": None, "pv": None,
                                             "volts": base.volts, "target_c": None, "avg_fan": None}
                    want = top.mhz - i * a.step
                    tries = [m for m in (want, want - a.step) if m >= lim["lo"]]
                    if not tries:
                        entry.update(mhz=want, reason=f"below the lowest clock the app sets ({lim['lo']} MHz)")
                        pit["state"], pit["note"] = "skip", entry["reason"]
                        doc["presets"][key] = entry
                        at.write_presets(doc)
                        continue
                    ok, plan, st = False, base.with_(mhz=tries[0]), {}
                    reuse = stats.get(top.mhz) if key == "high" and confirmed and top is best else None
                    if key == "high" and top.mhz == base.mhz:
                        # the baseline ran for a full baseline without a board reset: High needs no test of its own
                        ok, plan, st = True, base, {"why": "the baseline clock", "avg_ths": tester.base_ths,
                                                    "watched_min": a.baseline_min, "max_t": None}
                        pit["attempts"].append({"mhz": base.mhz, "mv": base.mv, "pv": "", "label": "baseline", "state": "pass",
                                                "why": "the baseline clock, watched for the whole baseline", "started": now()})
                        pit["state"] = "pass"
                        prog.write()
                    elif reuse and reuse.get("why") == "clean":
                        ok, plan, st = True, top, reuse
                        pit["attempts"].append({"mhz": top.mhz, "mv": top.mv, "pv": "", "label": "confirm test", "state": "pass",
                                                "why": "reuses the confirm test", "started": now()})
                        pit["state"] = "pass"
                        prog.write()
                    else:
                        for m in tries:
                            plan = base.with_(mhz=m)
                            log(f"PRESET {label}: {plan.text()}")
                            for attempt in range(1 + max(0, a.retries)):
                                prog.begin(plan, "retest" if attempt else "test", key=key, item_label=label)
                                try:
                                    tester.settle(plan)
                                    ok, why, st = tester.watch(plan, f"preset {key}", a.preset_min, judge=True)
                                except Abort as e:
                                    prog.end(False, str(e))
                                    raise
                                prog.end(ok, why)
                                st["why"] = why
                                at.record(plan, ("PASS" if ok else "FAIL") + f" (preset {key}{' retest' if attempt else ''})",
                                          why, st, tester.base_rate)
                                log(f"{'PASS' if ok else 'FAIL'} preset {label} {plan.text()}: {why}")
                                if ok or st.get("decisive"):
                                    break
                            if ok:
                                break
                    entry.update(mhz=plan.mhz, ok=ok, reason=st.get("why", ""), avg_ths=round(st.get("avg_ths") or 0, 3),
                                 max_t=st.get("max_t"), watched_min=round(st.get("watched_min") or 0, 1))
                    doc["presets"][key] = entry
                    at.write_presets(doc)
                doc["complete"] = True
                at.write_presets(doc)
                n_ok = sum(1 for v in doc["presets"].values() if v.get("ok"))
                prog.set("presets", "pass" if n_ok else "fail", f"{n_ok} of 4 presets passed")
                log("PRESETS: " + ", ".join(f"{v['label']} {v.get('mhz')} MHz {'ok' if v['ok'] else 'FAILED'}"
                                            for v in doc["presets"].values()))
                high = doc["presets"].get("high") or {}
                if high.get("ok"):
                    final = base.with_(mhz=int(high["mhz"]))
            except Abort as e:
                log(f"PRESETS stopped: {e}")
                prog.skip_rest("presets", f"not tested: {e}")
                prog.set("presets", "stop", f"stopped: {e}")
                presets_stop = f"safety stop during the presets: {e}"
        elif confirmed:
            final = top
        outcome = ("aborted", presets_stop) if presets_stop else ("done", "")
    except Abort as e:
        log(f"ABORT: {e}")
        final = None
        outcome = ("aborted", f"safety stop: {e}")
    except KeyboardInterrupt:
        log("stopped by you")
        final = None
        outcome = ("stopped", "stopped by you")
    except Exception as e:
        log(f"ERROR: {type(e).__name__}: {e}")
        final = None
        outcome = ("error", f"error: {type(e).__name__}: {e}")
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            if final is not None:
                box_apply(final)
                left = f"High, {final.text()}"
            else:
                log(f"going back to what the miner ran before the run: {pre.text()} ({pre_how})")
                kept = patient(lambda: box_plan.put_back(sc.get_setting, sc.put_setting, pre_raw, pre_text),
                               "put back the miner's own setting")
                if not kept:
                    raise RuntimeError("the miner's fields didn't read back as they were")
                left = f"{pre.text()} ({pre_how}) again"
            log(f"miner left on {left}")
            try:
                oc = locals().get("outcome") or ("stopped", "stopped")
                prog.set("finish", "pass", f"miner left on {left}")
                prog.finish(oc[0], oc[1])
            except Exception:
                pass
        except Exception as e:
            fix = f"set {pre.mhz} MHz on its Miner page, or {pre_how.replace('its ', 'its own ')} on the miner's own page"
            log(f"could not restore the setting ({e}); {fix}")
            try:
                prog.set("finish", "fail", f"COULD NOT put the miner back: {fix}")
                prog.finish("error", f"the miner may still be on a test setting: {fix}")
            except Exception:
                pass
            sys.exit(1)

    if final is not None:
        print(f"\nBEST: {final.mhz} MHz at {final.volts} V (clock only)")
    else:
        print(f"\nRESTORED: {pre.text()} ({pre_how}), what it ran before")


if __name__ == "__main__":
    main()
