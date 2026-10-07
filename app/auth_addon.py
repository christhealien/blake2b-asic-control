"""Username + password login for the web dashboard.

Every page and API call needs a signed-in session, except the login page itself.
The first visit, with no account yet, asks you to create one.

Account file: auth.json next to miners.json (on Umbrel: the app's data folder).
On Umbrel the account is created automatically from the username and password that
Umbrel shows on the app's page (SCLITE_WEBUI_DEFAULT_USER / _PASSWORD). You can change
it afterwards under Settings → Account.
Forgot the password? Delete auth.json and restart the app; it goes back to the
Umbrel default (or, outside Umbrel, asks you to create a new account).

One sign-in covers every page. Sessions are kept in sessions.json, so restarting or
updating the app does not sign you out.

Wired into server.py by install_addons.py: each do_GET / do_POST / do_DELETE starts
with `if auth_addon.gate(self): return`.
Set SCLITE_WEBUI_AUTH=off to switch the login off.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

HERE = Path(__file__).resolve().parent
_registry = Path(os.environ.get("SCLITE_WEBUI_MINERS", str(HERE / "miners.json")))
AUTH_FILE = Path(os.environ.get("SCLITE_WEBUI_AUTH_FILE", str(_registry.parent / "auth.json")))
SESSIONS_FILE = AUTH_FILE.with_name("sessions.json")
ENABLED = os.environ.get("SCLITE_WEBUI_AUTH", "on").lower() not in ("off", "0", "false", "no")

COOKIE = "b2ac_session"
SESSION_DAYS = 30
ITERATIONS = 600_000          # PBKDF2-SHA256 (OWASP 2023); older hashes keep their own count until next sign-in
CSRF_HEADER = "X-B2AC"         # every page's fetch adds it (theme.js); another site can't without a CORS preflight
OPEN_PATHS = {"/login.html", "/style.css", "/theme.css", "/theme.js", "/favicon.ico"}
# Signing in (and creating the login) needs the disclaimer on the sign-in page acknowledged.
# Change this when the disclaimer text changes. Each acknowledgement is logged in auth.json.
DISCLAIMER_VERSION = "2026-09-29"

_lock = threading.Lock()
_sessions: dict[str, tuple[str, float]] = {}   # sha256(token) -> (username, expires)
_fails: dict[str, tuple[int, float]] = {}       # client -> (misses, blocked until): back-off after bad logins
_login_lock = threading.Lock()                   # one password check at a time (no parallel guessing)
_recent_misses: list[float] = []                 # all clients: at most 30 wrong guesses a minute overall


# ------------------------------------------------------------------ storage ---

def _load() -> dict[str, Any]:
    try:
        return json.loads(AUTH_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _save(doc: dict[str, Any]) -> None:
    tmp = AUTH_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc))
    os.chmod(tmp, 0o600)
    os.replace(tmp, AUTH_FILE)


def _hash(password: str, salt: bytes, iterations: int = ITERATIONS) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations).hex()


def has_account() -> bool:
    return bool(_load().get("username"))


def _set_account(username: str, password: str) -> None:
    salt = secrets.token_bytes(16)
    old = _load()
    _save({"username": username, "salt": salt.hex(), "hash": _hash(password, salt),
           "iterations": ITERATIONS, "changed": int(time.time()), "acks": old.get("acks") or []})
    _drop_all_sessions()          # a new or changed login signs everyone out (the caller signs the user back in)


def _record_ack(username: str) -> None:
    """Log that this user acknowledged the disclaimer (last 50 kept)."""
    doc = _load()
    acks = list(doc.get("acks") or [])
    acks.append({"user": username, "version": DISCLAIMER_VERSION,
                 "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    doc["acks"] = acks[-50:]
    _save(doc)
    _acked.add((username, DISCLAIMER_VERSION))


_acked: set[tuple[str, str]] = set()   # (user, version) known to be acknowledged


def _ack_ok(username: str) -> bool:
    if (username, DISCLAIMER_VERSION) in _acked:
        return True
    a = _last_ack(username)
    if a and a.get("version") == DISCLAIMER_VERSION:
        _acked.add((username, DISCLAIMER_VERSION))
        return True
    return False


def _last_ack(username: str) -> dict[str, Any] | None:
    acks = [a for a in _load().get("acks") or [] if a.get("user") == username]
    return acks[-1] if acks else None


def _check(username: str, password: str) -> bool:
    doc = _load()
    if not doc.get("username"):
        return False
    n = int(doc.get("iterations") or 240_000)
    good = _hash(password, bytes.fromhex(doc["salt"]), n)
    ok = (hmac.compare_digest(doc["username"].encode(), username.encode())
          and hmac.compare_digest(doc["hash"].encode(), good.encode()))
    if ok and n != ITERATIONS:            # upgrade an older, weaker hash now that we know the password
        salt = secrets.token_bytes(16)
        doc.update(salt=salt.hex(), hash=_hash(password, salt), iterations=ITERATIONS)
        _save(doc)
    return ok


# ----------------------------------------------------------------- sessions ---

def _key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()   # only hashes are stored on disk


def _persist() -> None:
    """Save sessions (caller holds _lock) so an app restart keeps everyone signed in."""
    now = time.time()
    live = {k: [u, e] for k, (u, e) in _sessions.items() if e > now}
    try:
        tmp = SESSIONS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(live))
        os.chmod(tmp, 0o600)
        os.replace(tmp, SESSIONS_FILE)
    except OSError:
        pass   # read-only or missing folder: sessions just last until restart


def _load_sessions() -> None:
    try:
        doc = json.loads(SESSIONS_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return
    now = time.time()
    with _lock:
        for k, v in doc.items():
            if isinstance(v, list) and len(v) == 2 and float(v[1]) > now:
                _sessions[k] = (str(v[0]), float(v[1]))


def _new_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    with _lock:
        _sessions[_key(token)] = (username, time.time() + SESSION_DAYS * 86400)
        _persist()
    return token


def _user(handler: Any) -> str | None:
    raw = handler.headers.get("Cookie")
    if not raw:
        return None
    c = SimpleCookie()
    try:
        c.load(raw)
    except Exception:
        return None
    m = c.get(COOKIE)
    if not m:
        return None
    with _lock:
        k = _key(m.value)
        s = _sessions.get(k)
        if not s:
            return None
        if s[1] < time.time():
            _sessions.pop(k, None)
            _persist()
            return None
        user = s[0]
    account = _load().get("username")
    if not account or user != account:      # the login was reset or changed since: this session is void
        with _lock:
            _sessions.pop(k, None)
            _persist()
        return None
    return user


def _drop_all_sessions() -> None:
    with _lock:
        _sessions.clear()
        _persist()


BEHIND_PROXY = os.environ.get("SCLITE_BEHIND_PROXY", "").strip().lower() in ("1", "true", "yes", "on")


def _client(handler: Any) -> str:
    """Who's asking, for the login back-off. Behind a proxy that says so (SCLITE_BEHIND_PROXY=1, set
    for Umbrel's app_proxy) it's the address the proxy forwards; otherwise the header is whatever the
    client sent, so only the socket's address counts."""
    if BEHIND_PROXY:
        # the last entry is the one the proxy added (earlier ones are whatever the client sent)
        fwd = (handler.headers.get("X-Forwarded-For") or "").split(",")[-1].strip()
        if fwd:
            return fwd[:64]
    return str(handler.client_address[0])


# ------------------------------------------------------------------ helpers ---

def _json(handler: Any, code: int, obj: Any, cookie: str | None = None) -> None:
    raw = json.dumps(obj).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "no-store")
    if cookie is not None:
        handler.send_header("Set-Cookie", cookie)
    handler.end_headers()
    handler.wfile.write(raw)


def _cookie(token: str, max_age: int) -> str:
    return f"{COOKIE}={token}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict"


def _body(handler: Any) -> dict[str, Any]:
    n = int(handler.headers.get("Content-Length") or 0)
    if n <= 0 or n > 10_000:
        return {}
    try:
        d = json.loads(handler.rfile.read(n))
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def _valid(username: str, password: str) -> str | None:
    if not (1 <= len(username) <= 64) or any(ch.isspace() for ch in username):
        return "username must be 1-64 characters with no spaces"
    if len(password) < 8:
        return "password must be at least 8 characters"
    return None


def _bootstrap() -> None:
    """Create the account from Umbrel's default credentials if none exists yet."""
    user = os.environ.get("SCLITE_WEBUI_DEFAULT_USER", "").strip()
    pw = os.environ.get("SCLITE_WEBUI_DEFAULT_PASSWORD", "")
    if ENABLED and user and pw and not has_account():
        try:
            _set_account(user, pw)
            print(f"[auth] created the login '{user}' from the app's default credentials")
        except OSError as e:
            print(f"[auth] could not create the default login: {e}")


_load_sessions()
_bootstrap()


# --------------------------------------------------------------------- gate ---

def gate(handler: Any) -> bool:
    """Return True if the request was answered here (login API, redirect, or 401)."""
    if not ENABLED:
        return False
    path = urlparse(handler.path).path
    method = handler.command
    if method in ("POST", "PUT", "PATCH", "DELETE") and handler.headers.get(CSRF_HEADER) != "1":
        # only this app's own pages send this header; a page from another site or another app on the same
        # Umbrel can't add it without a CORS preflight, which this server never allows
        _json(handler, 403, {"ok": False, "error": "request refused (missing the app's request header); reload the page"})
        return True

    if path.startswith("/api/auth/"):
        _auth_api(handler, path, method)
        return True
    if path in OPEN_PATHS:
        return False
    user = _user(handler)
    if user and _ack_ok(user):
        return False
    if path.startswith("/api/"):   # signed out, or signed in before the disclaimer existed / changed
        _json(handler, 401, {"ok": False, "error": "not signed in" if not user else "acknowledge the disclaimer first",
                             "login": "/login.html"})
        return True
    nxt = quote(handler.path if handler.path != "/login.html" else "/", safe="")
    handler.send_response(302)
    handler.send_header("Location", f"/login.html?next={nxt}")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", "0")
    handler.end_headers()
    return True


def _auth_api(handler: Any, path: str, method: str) -> None:
    if path == "/api/auth/status" and method == "GET":
        user = _user(handler)
        return _json(handler, 200, {"ok": True, "has_account": has_account(), "user": user,
                                    "umbrel_default": bool(os.environ.get("SCLITE_WEBUI_DEFAULT_USER")),
                                    "platform": (os.environ.get("B2AC_PLATFORM") or "")[:20],
                                    "disclaimer_version": DISCLAIMER_VERSION,
                                    "last_ack": _last_ack(user) if user else None,
                                    "ack_ok": bool(user) and _ack_ok(user)})
    if method != "POST":
        return _json(handler, 405, {"ok": False, "error": "method not allowed"})
    body = _body(handler)
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    if path in ("/api/auth/setup", "/api/auth/login") and body.get("disclaimer") != DISCLAIMER_VERSION:
        return _json(handler, 400, {"ok": False, "need_disclaimer": True,
                                    "error": "read and acknowledge the disclaimer first"})

    if path == "/api/auth/setup":
        err = _valid(username, password)
        if err:
            return _json(handler, 400, {"ok": False, "error": err})
        with _login_lock:                     # check and create in one step (two setups can't both win)
            if has_account():
                return _json(handler, 403, {"ok": False, "error": "an account already exists"})
            _set_account(username, password)
        _record_ack(username)
        token = _new_session(username)
        return _json(handler, 200, {"ok": True}, _cookie(token, SESSION_DAYS * 86400))

    if path == "/api/auth/login":
        who = _client(handler)
        with _login_lock:                     # one check at a time, so parallel guesses can't slip past
            n, until = _fails.get(who, (0, 0.0))
            now = time.time()
            _recent_misses[:] = [t for t in _recent_misses if now - t < 60]
            if len(_recent_misses) >= 30:
                until = max(until, _recent_misses[0] + 60)
            if now < until:
                return _json(handler, 429, {"ok": False, "error": f"too many attempts; wait {int(until - now) + 1} s"})
            if _check(username, password):
                _fails.pop(who, None)
                ok = True
            else:
                _recent_misses.append(now)
                n += 1                        # 1, 2, 4 ... 300 s from the 3rd miss, per client
                _fails[who] = (n, now + min(300, 2 ** (n - 3)) if n >= 3 else 0.0)
                if len(_fails) > 1000:
                    _fails.clear()
                ok = False
        if not ok:
            time.sleep(0.5)                   # outside the lock: slows this client, not everyone's sign-in
            return _json(handler, 401, {"ok": False, "error": "wrong username or password"})
        _record_ack(username)
        token = _new_session(username)
        return _json(handler, 200, {"ok": True}, _cookie(token, SESSION_DAYS * 86400))

    if path == "/api/auth/ack":
        user = _user(handler)
        if not user:
            return _json(handler, 401, {"ok": False, "error": "not signed in"})
        if body.get("disclaimer") != DISCLAIMER_VERSION:
            return _json(handler, 400, {"ok": False, "error": "read and acknowledge the disclaimer first"})
        _record_ack(user)
        return _json(handler, 200, {"ok": True})

    if path == "/api/auth/logout":
        raw = handler.headers.get("Cookie") or ""
        c = SimpleCookie()
        try:
            c.load(raw)
            if c.get(COOKIE):
                with _lock:
                    _sessions.pop(_key(c[COOKIE].value), None)
                    _persist()
        except Exception:
            pass
        return _json(handler, 200, {"ok": True}, _cookie("", 0))

    if path == "/api/auth/change":
        user = _user(handler)
        if not user:
            return _json(handler, 401, {"ok": False, "error": "not signed in"})
        with _login_lock:
            ok = _check(user, str(body.get("current") or ""))
        if not ok:
            time.sleep(1.0)                   # a wrong guess costs time (outside the lock)
            return _json(handler, 401, {"ok": False, "error": "current password is wrong"})
        new_user = username or user
        err = _valid(new_user, password)
        if err:
            return _json(handler, 400, {"ok": False, "error": err})
        _set_account(new_user, password)          # also signs out everywhere else
        token = _new_session(new_user)
        return _json(handler, 200, {"ok": True}, _cookie(token, SESSION_DAYS * 86400))

    return _json(handler, 404, {"ok": False, "error": "unknown"})


# ---------------------------------------------------------------- command line ---
# Set (or replace) the login from the command line, for a platform that makes the password itself (StartOS's
# "Set login password" action runs this with the service stopped). The password comes on stdin, never as an
# argument (arguments show up in process lists); only its salted hash is written. Signs everyone out.
#   echo -n 'the password' | python auth_addon.py set-login admin
if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "set-login":
        name, secret = sys.argv[2].strip(), sys.stdin.read().rstrip("\r\n")
        problem = _valid(name, secret)
        if problem:
            print(f"not set: {problem}", file=sys.stderr)
            sys.exit(2)
        _set_account(name, secret)
        print(f"login set for {name}; everyone is signed out")
        sys.exit(0)
    print("usage: python auth_addon.py set-login USERNAME   (the password on stdin)", file=sys.stderr)
    sys.exit(2)
