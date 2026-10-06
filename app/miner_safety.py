"""Talking to miners safely: bounded reads, and nothing a miner reports can act as HTML.

A miner, or anything answering at a miner's address, could send endless or crafted replies:
  - every web-API reply is capped (MAX_API_BYTES) and every port-4028 reply is capped in size and time;
  - snapshot() values are cleaned of the characters that could turn them into HTML in a page.
install(MinerClient) replaces those three methods; the dashboard and the widget server both call it.

It also paces the web API per miner: one request at a time to each address, with a short gap between
them (longer for models whose web backend falls over under a burst, such as the SC BOX), however many
parts of the app are asking. And it refuses to write a clock / fan setting to a miner whose power plan
isn't in the SC Lite form, instead of failing halfway or writing another algorithm's plan.
"""
from __future__ import annotations

import json
import re
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Any

MAX_API_BYTES = 8 * 1024 * 1024        # the miner's own log is a few MB at most
MAX_BFG_BYTES = 1024 * 1024            # port-4028 replies are a few KB
BFG_DEADLINE_S = 10.0
BFG_PORT = 4028

# Web-API pacing per miner address: one request at a time, at least this many seconds apart.
FAST_GAP_S = float(os.environ.get("SCLITE_MINER_GAP_S", "0.2"))   # an SC Lite (probed)
BOX_GAP_S = 1.0                         # SC BOX / HS BOX: their web backend crashes under bursts
DEFAULT_GAP_S = BOX_GAP_S               # a miner not identified yet: the careful pace
_gaps: dict[str, float] = {}
_ip_locks: dict[str, threading.RLock] = {}
_last_req: dict[str, float] = {}
_locks_guard = threading.Lock()


def set_gap(ip: str, seconds: float) -> None:
    """Pace this miner's web API at `seconds` between requests (the probe sets it per model)."""
    _gaps[str(ip)] = max(0.0, min(10.0, float(seconds)))


def _ip_lock(ip: str) -> threading.RLock:
    with _locks_guard:
        return _ip_locks.setdefault(ip, threading.RLock())


class _Paced:
    """Hold the miner's address for one request, waiting out the gap since its last one."""

    def __init__(self, ip: str):
        self.ip, self.lock = ip, _ip_lock(ip)

    def __enter__(self) -> None:
        self.lock.acquire()
        wait = _last_req.get(self.ip, 0.0) + _gaps.get(self.ip, DEFAULT_GAP_S) - time.time()
        if wait > 0:
            time.sleep(wait)

    def __exit__(self, *_a: Any) -> None:
        _last_req[self.ip] = time.time()
        self.lock.release()


SC_LITE_PLAN = re.compile(r"^\s*\d+\s*MHz\s+\d+\s*V\s+\d+\s*RPM\s+\d+\s*RPM\s+PV\s+\d+\s*$", re.I)
FORMAT_REFUSAL = ("this miner's power plan isn't in the SC Lite form (the SC BOX and HS BOX write volts as a "
                  "decimal with no PV), so the app doesn't change its clock, voltage or fans yet")


def _plan_writable(self: Any) -> None:
    s = self.api("GET", "/mcb/setting")
    if not isinstance(s, dict):
        raise RuntimeError("couldn't read the miner's setting")
    if not SC_LITE_PLAN.match(str(s.get("manualPowerplan") or "")[:200]):
        raise RuntimeError(FORMAT_REFUSAL)


def api_bounded(self: Any, method: str, path: str, body: Any = None, retries: int = 3) -> Any:
    with self._lock:
        last_err: Exception | None = None
        for _ in range(retries):
            if self._token is None:
                self.login()
            data = None
            headers = {"Authorization": self._token, "Accept": "*/*"}
            if body is not None:
                data = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(self.host() + path, data=data, headers=headers, method=method)
            try:
                with _Paced(self.ip), urllib.request.urlopen(req, timeout=20) as r:
                    raw = r.read(MAX_API_BYTES + 1)
                if len(raw) > MAX_API_BYTES:
                    raise RuntimeError(f"reply to {path} is too large")
                if not raw:
                    return None
                try:
                    return json.loads(raw)
                except Exception:
                    return raw.decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code == 401:
                    self._token = None
                    continue
                raise
            except Exception as e:
                last_err = e
                self._token = None
        raise RuntimeError(f"API {method} {path} failed: {last_err}")


def bfg_bounded(self: Any, cmd: str, parameter: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"command": cmd}
    if parameter is not None:
        payload["parameter"] = parameter
    last_err: Exception | None = None
    for _attempt in range(3):
        try:
            sock = socket.create_connection((self.ip, BFG_PORT), timeout=5)
            try:
                sock.sendall((json.dumps(payload) + "\n").encode())
                sock.settimeout(2.5)
                buf = bytearray()
                end = time.time() + BFG_DEADLINE_S
                try:
                    while time.time() < end and len(buf) <= MAX_BFG_BYTES:
                        b = sock.recv(65536)
                        if not b:
                            break
                        buf += b
                        if b"\x00" in b or (b.rstrip().endswith(b"}") and self._bfg_json_complete(bytes(buf))):
                            break
                except (socket.timeout, OSError):
                    pass
            finally:
                sock.close()
            if len(buf) > MAX_BFG_BYTES:
                raise RuntimeError("port-4028 reply too large")
            return self._parse_bfg_json(bytes(buf))
        except Exception as e:
            last_err = e
            time.sleep(0.2)
    raise RuntimeError(f"bfg {cmd} failed: {last_err}")


_UNSAFE = re.compile(r"[<>\"'`]")


def clean(v: Any, depth: int = 0) -> Any:
    """Strip characters that could make a reported value act as HTML. Numbers and booleans pass as they
    are; pool URLs, worker names, models and messages never legitimately contain these."""
    if depth > 8:
        return None
    if isinstance(v, str):
        return _UNSAFE.sub("", v)[:2000]
    if isinstance(v, dict):
        return {(_UNSAFE.sub("", k) if isinstance(k, str) else k): clean(x, depth + 1) for k, x in v.items()}
    if isinstance(v, list):
        return [clean(x, depth + 1) for x in v[:500]]
    return v


def install(cls: Any) -> None:
    if getattr(cls, "_b2ac_safe", False):
        return
    orig = cls.snapshot

    def snapshot(self: Any, *a: Any, **k: Any) -> Any:
        return clean(orig(self, *a, **k))

    orig_login = cls.login

    def login(self: Any, *a: Any, **k: Any) -> Any:
        with _Paced(self.ip):
            return orig_login(self, *a, **k)

    def guarded(name: str) -> Any:
        orig_w = getattr(cls, name)

        def write(self: Any, *a: Any, **k: Any) -> Any:
            _plan_writable(self)
            return orig_w(self, *a, **k)
        write.__name__ = name
        return write

    cls.api, cls.bfg, cls.snapshot, cls.login = api_bounded, bfg_bounded, snapshot, login
    for name in ("set_fan_bias", "set_plan"):
        if hasattr(cls, name):
            setattr(cls, name, guarded(name))
    cls._b2ac_safe = True
