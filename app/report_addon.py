"""Miner report for Blake2b ASIC Control: one zip with what it takes to support a model the app hasn't
been tested on (a different SC or HS unit, newer firmware), for the owner to look over and send in.

Every request is a read; nothing is changed on the miner. They go one at a time with a pause between
(the firmware's web service falls over under a burst), so a report takes about half a minute.

What's read (the same list as crProductGuy's capture guide, docs/capture-request.md in his box tools,
plus the fan controller's log and, if ticked, the miner's own log):
  port 4028: version, summary, devs (no login)
  web API:   /mcb/status /mcb/setting /mcb/cgminer?cgminercmd=devs /mcb/algosetting
             /dbg/minerinfo /dbg/icinfo /dbg/fanctrllog /cpb/hshistory   [/dbg/minersyslog]

Never read: /mcb/pools (pool URL, wallet, worker password), /mcb/wifisetting (WiFi passwords), the
port-4028 "pools" command. Removed from what is kept: the MAC address, IP addresses, pool URLs, e-mail
addresses, anything that looks like a wallet or a token (any unbroken run of 24+ letters and digits),
string values under keys like user, pass, wallet, worker, pool, url, ssid, host, ip, mac, serial, and
any string value that mentions a pool, a user, a worker, a wallet or the network.
From the miner's log, every line that mentions a pool, a user, a worker, a wallet or the network is
dropped whole. The login token is only ever in a request header and is never written.
"""
from __future__ import annotations

import io
import json
import os
import re
import threading
import time
import zipfile
from typing import Any, Callable

PAUSE_S = 2.0
KEEP_S = 1800                    # a finished report can be downloaded for 30 min
LOG_MAX = 1_500_000

HTTP_READS = [
    ("mcb_status.json", "/mcb/status", "model, firmware, hardware and control-board strings"),
    ("mcb_setting.json", "/mcb/setting", "the power plans (how this model writes clock and voltage), fan and temperature settings"),
    ("cgminer_devs.json", "/mcb/cgminer?cgminercmd=devs", "per-board hashrate, temperature, fans and errors (some models answer an error here; that's useful too)"),
    ("mcb_algosetting.json", "/mcb/algosetting", "the algorithm list"),
    ("dbg_minerinfo.txt", "/dbg/minerinfo", "per-board data as text, one [PGAn] block per board"),
    ("dbg_icinfo.json", "/dbg/icinfo", "per-chip counts on each board (what the chip map and the tuner use)"),
    ("dbg_fanctrllog.txt", "/dbg/fanctrllog", "the firmware's own fan controller log"),
    ("cpb_hshistory.json", "/cpb/hshistory", "the miner's own hashrate history (how often it samples)"),
]
LOG_READ = ("miner_log.txt", "/dbg/minersyslog", "the miner's own log, with every pool, user, wallet and network line taken out")
PORT_READS = [
    ("port4028_version.json", "version", "software versions"),
    ("port4028_summary.json", "summary", "totals: hashrate, accepted / rejected shares, uptime"),
    ("port4028_devs.json", "devs", "every board's hashrate, temperatures, fans, errors (and on some models voltage and current)"),
]

# set by fan_addon
_probe: Callable[[str], dict[str, Any]] = lambda mid: {}
_client: Callable[[str], Any] = lambda mid: None
_busy: Callable[[str], str] = lambda mid: ""          # why a report can't run now ("" = it can)
_version = os.environ.get("B2AC_VERSION") or "unknown"

_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}

# ---------------------------------------------------------------- removing private details

_SECRET_KEY = re.compile(r"pass|pwd|token|jwt|secret|wallet|worker|user|pool|url|ssid|wifi|host|e-?mail|"
                         r"(?:^|[_\s-])(?:ip|mac|sn)(?:$|[_\s-]|addr|v[46])|inet6|addr|serial|gateway|dns|account|apikey|api_key",
                         re.I)
_URL = re.compile(r"\b(?:stratum\+?\w*|tcp|ssl|tls|https?|wss?)://\S+", re.I)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_IPV4 = re.compile(r"\b(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?::\d{1,5})?\b")
_MAC = re.compile(r"\b[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}\b")
# IPv6 (a link-local fe80:: address carries the MAC): eight groups, or any form with "::". Not clock times
# like 07:24:31, which have neither.
def _ipv6_out(m: "re.Match[str]") -> str:
    """Replace a candidate only if it really is an IPv6 address (not a clock time like 07:24:31)."""
    import ipaddress
    try:
        ipaddress.IPv6Address(m.group(0).split("%")[0])
        return "<ip removed>"
    except ValueError:
        return m.group(0)


_IPV6 = re.compile(r"(?<![\w:.])[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}(?:%\w+)?(?![\w:])")   # checked below
# a host with a port and no scheme, like a pool address in a log line ("sc-us.example.tech:700")
_HOSTPORT = re.compile(r"\b[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.(?!(?:py|js|c|h|cc|cpp|hpp|go|rs|sh|so|log|txt|json|cfg|conf|ini|html?|css|lua)\b)[A-Za-z]{2,}:\d{2,5}\b")   # not file.py:123
_LONG = re.compile(r"[A-Za-z0-9_\-=]{24,}")
_MAC12 = re.compile(r"\b[0-9A-Fa-f]{12}\b")
_LOG_DROP = re.compile(r"pool|stratum|user|worker|wallet|passw|authori[sz]e|subscri|url|https?:|ssid|wifi|wlan|"
                       r"dns|gateway|dhcp|hostname|email|account|token|login", re.I)


def scrub_text(s: str) -> str:
    s = _URL.sub("<url removed>", s)
    s = _EMAIL.sub("<email removed>", s)
    s = _MAC.sub("00:11:22:33:44:55", s)
    s = _MAC12.sub("001122334455", s)
    s = _IPV6.sub(_ipv6_out, s)
    s = _HOSTPORT.sub("<host removed>", s)
    s = _IPV4.sub(lambda m: m.group(0) if m.group(0).startswith(("127.0.0.1", "0.0.0.0")) else "<ip removed>", s)
    return _LONG.sub("<removed>", s)


def scrub(obj: Any, key: str = "") -> Any:
    """A copy with private details removed: string values under private-sounding keys, and anything in
    any string that looks like an address, a URL, a wallet or a token. Numbers are kept as they are."""
    if isinstance(obj, dict):
        return {k: scrub(v, str(k)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(v, key) for v in obj]
    if isinstance(obj, str):
        if key and _SECRET_KEY.search(key) and obj.strip():
            return "<removed>"
        if _LOG_DROP.search(obj):            # free text that mentions a pool, a user, a wallet, the network
            return "<removed>"
        return scrub_text(obj)
    return obj


def scrub_log(text: str) -> tuple[str, int, int]:
    """The miner's log without any line that mentions a pool, a user, a wallet or the network, and with the
    rest scrubbed. Returns (text, lines kept, lines dropped)."""
    kept, dropped = [], 0
    for line in text.splitlines():
        if _LOG_DROP.search(line):
            dropped += 1
            continue
        kept.append(scrub_text(line))
    out = "\n".join(kept)
    if len(out) > LOG_MAX:
        out = "(older lines cut)\n" + out[-LOG_MAX:]
    return out, len(kept), dropped


def _as_text(raw: Any) -> str:
    if isinstance(raw, dict) and isinstance(raw.get("body"), str) and len(raw) <= 3:
        raw = raw["body"]
    if isinstance(raw, (dict, list)):
        return json.dumps(scrub(raw), indent=1, ensure_ascii=False)
    if raw is None:
        return ""
    text = str(raw)
    try:
        return json.dumps(scrub(json.loads(text)), indent=1, ensure_ascii=False)
    except (ValueError, TypeError):
        return scrub_text(text)


# ---------------------------------------------------------------- the report

def _set(mid: str, **kw: Any) -> None:
    with _lock:
        _jobs.setdefault(mid, {}).update(kw)


def _expire() -> None:
    now = time.time()
    with _lock:
        for mid in [m for m, j in _jobs.items() if j.get("state") != "running" and now - j.get("at", now) > KEEP_S]:
            del _jobs[mid]


def _run(mid: str, name: str, with_log: bool) -> None:
    files: dict[str, str] = {}
    reads: list[dict[str, Any]] = []
    steps = 1 + len(PORT_READS) + len(HTTP_READS) + (1 if with_log else 0)
    done = [0]

    def step(what: str) -> None:
        done[0] += 1
        _set(mid, step=what, done=done[0], total=steps)

    try:
        step("probing the miner")
        try:
            hw = _probe(mid) or {}
        except Exception as e:
            hw = {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}
        files["app_probe.json"] = json.dumps(scrub(hw), indent=1, ensure_ascii=False)
        client = _client(mid)
        if client is None:
            raise RuntimeError("this miner has no address saved")
        for fname, cmd, why in PORT_READS:
            time.sleep(PAUSE_S)
            step(f"port 4028: {cmd}")
            t0 = time.time()
            try:
                raw = client.bfg(cmd)
                files[fname] = _as_text(raw)
                reads.append({"file": fname, "read": f"port 4028 {cmd}", "ok": True, "ms": int((time.time() - t0) * 1000)})
            except Exception as e:
                reads.append({"file": fname, "read": f"port 4028 {cmd}", "ok": False, "error": scrub_text(str(e))[:200]})
        todo = HTTP_READS + ([LOG_READ] if with_log else [])
        for fname, path, why in todo:
            time.sleep(PAUSE_S)
            step(path)
            t0 = time.time()
            rec: dict[str, Any] = {"file": fname, "read": f"GET {path}"}
            try:
                raw = client.api("GET", path)
                if fname == LOG_READ[0]:
                    if isinstance(raw, dict):
                        raw = raw.get("body") or raw.get("data") or json.dumps(raw)
                    text, kept, dropped = scrub_log(str(raw or ""))
                    rec.update(lines_kept=kept, lines_dropped=dropped)
                    files[fname] = text
                else:
                    files[fname] = _as_text(raw)
                rec.update(ok=True, ms=int((time.time() - t0) * 1000), bytes=len(files[fname]))
            except Exception as e:
                code = getattr(e, "code", None)
                rec.update(ok=False, http=code, error=scrub_text(f"{type(e).__name__}: {e}")[:200])
                files[fname.rsplit(".", 1)[0] + ".error.txt"] = rec["error"] + "\n"
            reads.append(rec)
        stamp = time.strftime("%Y%m%d-%H%M")
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", str(hw.get("name") or "miner"))[:40]
        files["summary.txt"] = _summary(hw, reads, with_log)
        files["README.txt"] = _readme(with_log)
        files["reads.json"] = json.dumps({"app_version": _version, "made": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                          "pause_s": PAUSE_S, "reads": reads}, indent=1)
        buf = io.BytesIO()
        base = f"b2ac-report-{safe}-{stamp}"
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for fn in ["README.txt", "summary.txt"] + sorted(k for k in files if k not in ("README.txt", "summary.txt")):
                z.writestr(f"{base}/{fn}", files[fn])
        ok = sum(1 for r in reads if r.get("ok"))
        if not ok:
            raise RuntimeError("the miner didn't answer any read (is it on, and is its address right?)")
        _set(mid, state="done", step=f"{ok} of {len(reads)} reads answered", zip=buf.getvalue(),
             name=f"{base}.zip", at=time.time(), done=steps)
    except Exception as e:
        msg = str(e) if isinstance(e, (RuntimeError, ValueError)) else f"{type(e).__name__}: {e}"
        _set(mid, state="error", step=scrub_text(msg)[:200], at=time.time())


def _summary(hw: dict[str, Any], reads: list[dict[str, Any]], with_log: bool) -> str:
    g = lambda k: hw.get(k) if hw.get(k) not in (None, "") else "?"
    cpb = hw.get("chips_per_board")
    lines = [
        "Blake2b ASIC Control: miner report",
        f"app version {_version}, made {time.strftime('%Y-%m-%d %H:%M %Z')}",
        "",
        f"model (as the miner names it): {g('model')}",
        f"firmware: {g('firmware')}",
        f"recognised as: {g('name')} (profile {g('profile')}, support {g('support')})",
        f"boards: {g('boards')}   chips: {g('chips')}{f' ({cpb})' if cpb else ''}   fans: {g('fans')}",
        f"plan format: {g('plan_format')}{'  (one plan list per algorithm)' if hw.get('plan_nested') else ''}",
        f"stock plan: {g('stock_text')}",
        f"running plan: {g('running_text')}",
        f"Idle mode found: {'yes' if hw.get('idle_select') is not None else 'no'}",
        f"tuner tested on this model: {'yes' if hw.get('tuner_tested') else 'no'}",
    ]
    if not hw.get("ok"):
        lines.append(f"probe problem: {scrub_text(str(hw.get('error') or 'no answer'))}")
    lines += ["", "reads (one at a time, nothing changed on the miner):"]
    for r in reads:
        extra = f", {r['lines_kept']} log lines kept, {r['lines_dropped']} dropped" if "lines_kept" in r else ""
        lines.append(f"  {'ok  ' if r.get('ok') else 'FAIL'} {r['read']}"
                     + (f"  {r.get('ms')} ms{extra}" if r.get("ok") else f"  {r.get('error')}"))
    if not with_log:
        lines.append("  (the miner's log wasn't included)")
    return "\n".join(lines) + "\n"


def _readme(with_log: bool) -> str:
    what = "\n".join(f"  {f:24} {why}" for f, _p, why in PORT_READS + HTTP_READS + ([LOG_READ] if with_log else []))
    return f"""Blake2b ASIC Control: miner report

This folder is what the app read from one of your miners, so support for a model (or firmware) it
hasn't been tested on can be added: how it writes its clock and voltage, how many boards and chips it
has, what its own pages answer. Everything here is plain text: please look through it before you
send it.

Nothing was changed on the miner. Every request was a read, one at a time with a {PAUSE_S:.0f} s pause.

Not read at all: the pool settings (pool URL, wallet, worker password), the WiFi settings, and the
port-4028 "pools" command.

Taken out of what was read: the MAC address (shown as 00:11:22:33:44:55), IP addresses, URLs (pool
addresses), e-mail addresses, anything that looks like a wallet address or a token (any unbroken run
of 24 or more letters and digits), and the text of any setting named like user, password, wallet,
worker, pool, url, ssid, host, ip, mac or serial, or that mentions a pool, a user, a wallet or the
network.{'''
From the miner's own log, every line that mentions a pool, a user, a worker, a wallet, a login or the
network was dropped whole (summary.txt says how many).''' if with_log else ''}
Your miner's password and the login token are never written to any file.

Files:
  summary.txt              what the app found, and which reads answered
  app_probe.json           the app's own probe of the miner
  reads.json               each read: answered or not, how long it took
{what}
A read that failed has a .error.txt file instead; that's useful to know too.

To send it: open an issue at https://github.com/christhealien/blake2b-asic-control/issues, say
which unit it is (as its label or the stock web page names it), and attach this zip.
"""


# ---------------------------------------------------------------- routes

def start(body: dict[str, Any]) -> dict[str, Any]:
    mid = str(body.get("miner_id") or "")
    if not mid:
        raise ValueError("no miner picked")
    why = _busy(mid)
    if why:
        raise ValueError(why)
    _expire()
    with _lock:
        j = _jobs.get(mid)
        if j and j.get("state") == "running":
            raise ValueError("a report for this miner is already being made")
        _jobs[mid] = {"state": "running", "step": "starting", "done": 0, "total": 1, "at": time.time()}
    threading.Thread(target=_run, args=(mid, mid, bool(body.get("log", True))), daemon=True,
                     name=f"report-{mid}").start()
    return status(mid)


def status(mid: str) -> dict[str, Any]:
    _expire()
    with _lock:
        j = dict(_jobs.get(mid) or {})
    j.pop("zip", None)
    return {"ok": True, "miner_id": mid, "state": j.get("state") or "none", "step": j.get("step"),
            "done": j.get("done", 0), "total": j.get("total", 0), "name": j.get("name")}


def zip_of(mid: str) -> tuple[str, bytes] | None:
    with _lock:
        j = _jobs.get(mid) or {}
        return (j["name"], j["zip"]) if j.get("state") == "done" and j.get("zip") else None
