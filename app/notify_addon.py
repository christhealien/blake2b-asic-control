"""Notifications for Blake2b ASIC Control: Telegram (a bot) and Discord (a webhook).

What can be sent (each one switched on or off in Settings; every miner switched on or off on its own
Miner page or in Settings):
  offline       a miner stops answering for N minutes (port 4028), and online when it answers again
  hot           the hottest board reaches N °C (once, until it's 5 °C cooler again)
  hashrate      the hashrate stays under N % of what that miner usually does at that clock for 10 minutes
  self_restart  the miner's software restarted without the app asking (uptime went back)
  app_restart   a restart the app started (Restart button, scheduled restart, pool switch) finished or failed
  schedule      a scheduled preset change or restart didn't go through
  tuner         a tuning run finished (or stopped)
  best          a new best-share record
  chip          a chip's health turned weak

The same thing for the same miner is sent at most once per cooldown (30 min by default); "back online"
and "cooled down" always go through. Nothing is sent until a channel is set up and switched on, and the
only places this talks to are api.telegram.org and discord.com (webhooks).

Stored in miners.json (readable by the app only) under "notify"; a miner's own switch is "notify" on
its row. The bot token and webhook URL are never sent back to the page (only their last 4 characters).

Routes (behind the login, wired through fan_addon):
  GET  /api/notify/state
  POST /api/notify/save            {telegram: {enabled, bot_token, chat_id}, discord: {enabled, webhook_url},
                                    events: {name: bool}, offline_min, hot_c, low_pct, cooldown_min}
  POST /api/notify/test            {channel: "telegram" | "discord"}
  POST /api/notify/miner           {miner_id, enabled}
  POST /api/notify/telegram_chats  {bot_token?}  the chats that have messaged the bot (to find the chat ID)
"""
from __future__ import annotations

import json
import queue
import re
import statistics
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable

EVENTS: list[tuple[str, str, bool]] = [
    ("offline", "A miner stops answering, and when it's back", True),
    ("hot", "Too hot", True),
    ("hashrate", "Hashrate low", True),
    ("self_restart", "Restarted on its own", True),
    ("app_restart", "Restarts the app started (button, schedule, pool switch): done or failed", False),
    ("schedule", "A scheduled change or restart didn't go through", True),
    ("tuner", "A tuning run finished", True),
    ("best", "New best-share record", True),
    ("chip", "A chip turned weak", True),
    ("reject", "A share rejected for a reason other than stale", True),
]
NUMS = {"offline_min": (1, 120, 5), "hot_c": (50, 95, 80), "low_pct": (10, 95, 70), "cooldown_min": (0, 1440, 30)}
LOW_FOR_S = 600            # hashrate low for this long before it's sent
WARMUP_S = 900             # ... and never in the first 15 minutes after a restart
APP_RESTART_QUIET_S = 900  # an uptime drop this soon after the app restarted it isn't "on its own"
TICK_S = 30
TG_TOKEN = re.compile(r"^\d{5,12}:[A-Za-z0-9_-]{30,60}$")
TG_CHAT = re.compile(r"^(-?\d{1,20}|@[A-Za-z0-9_]{5,32})$")
DISCORD_HOOK = re.compile(r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/\d{5,25}/[A-Za-z0-9_-]{20,100}$")

_srv: Callable[[], Any] = lambda: None                 # set by fan_addon
_tuner_running: Callable[[str], bool] = lambda mid: False
_tuner_finished: Callable[[str], str] = lambda mid: ""
_health: Callable[[str], dict | None] = lambda mid: None
_restarting: Callable[[str], bool] = lambda mid: False

_q: "queue.Queue[tuple[str, str]]" = queue.Queue(maxsize=200)
_log: list[dict[str, Any]] = []            # the last sends, newest last
_last_sent: dict[tuple[str, str], float] = {}
_state: dict[str, dict[str, Any]] = {}     # per miner: what the watcher saw last
_app_restart_at: dict[str, float] = {}     # miner ip -> when the app last restarted it
_tick = [0.0]
_busy = [False]
_lock = threading.Lock()


# ---------------------------------------------------------------- config

def _cfg(doc: dict[str, Any] | None = None) -> dict[str, Any]:
    doc = doc if doc is not None else _srv().load_registry_doc()
    c = dict(doc.get("notify") or {})
    c.setdefault("telegram", {})
    c.setdefault("discord", {})
    ev = dict(c.get("events") or {})
    for k, _l, on in EVENTS:
        ev.setdefault(k, on)
    c["events"] = ev
    for k, (_lo, _hi, d) in NUMS.items():
        c.setdefault(k, d)
    return c


def _mask(s: Any) -> str:
    s = str(s or "")
    return ("••••" + s[-4:]) if len(s) > 8 else ("••••" if s else "")


def _channels(c: dict[str, Any]) -> list[str]:
    out = []
    t, d = c.get("telegram") or {}, c.get("discord") or {}
    if t.get("enabled") and t.get("bot_token") and t.get("chat_id"):
        out.append("telegram")
    if d.get("enabled") and d.get("webhook_url"):
        out.append("discord")
    return out


def _row_id(m: dict[str, Any]) -> str:
    return str(m.get("id") or m.get("ip") or "")


def state() -> dict[str, Any]:
    doc = _srv().load_registry_doc()
    c = _cfg(doc)
    t, d = c["telegram"], c["discord"]
    return {"ok": True,
            "telegram": {"enabled": bool(t.get("enabled")), "chat_id": t.get("chat_id") or "",
                         "token_set": bool(t.get("bot_token")), "token_hint": _mask(t.get("bot_token"))},
            "discord": {"enabled": bool(d.get("enabled")), "webhook_set": bool(d.get("webhook_url")),
                        "webhook_hint": _mask(d.get("webhook_url"))},
            "events": c["events"], "event_list": [{"key": k, "label": l} for k, l, _on in EVENTS],
            **{k: c[k] for k in NUMS},
            "active": _channels(c),
            "miners": [{"id": _row_id(m), "name": m.get("name") or _row_id(m), "enabled": bool(m.get("notify"))}
                       for m in doc.get("miners") or [] if not m.get("demo") and m.get("ip")],
            "log": list(reversed(_log[-20:]))}


def save(body: dict[str, Any]) -> dict[str, Any]:
    srv = _srv()
    doc = srv.load_registry_doc()
    c = _cfg(doc)
    tb = body.get("telegram") or {}
    if isinstance(tb, dict):
        t = dict(c["telegram"])
        tok = str(tb.get("bot_token") or "").strip()
        if tok and not tok.startswith("••••"):
            if not TG_TOKEN.match(tok):
                raise ValueError("that doesn't look like a Telegram bot token (it's like 123456789:AAE…, from @BotFather)")
            t["bot_token"] = tok
        if tb.get("clear_token"):
            t.pop("bot_token", None)
        if "chat_id" in tb:
            chat = str(tb.get("chat_id") or "").strip()
            if chat and not TG_CHAT.match(chat):
                raise ValueError("the Telegram chat ID is a number (like 123456789, or -100… for a group) or @channelname")
            t["chat_id"] = chat
        t["enabled"] = bool(tb.get("enabled", t.get("enabled")))
        if t["enabled"] and not (t.get("bot_token") and t.get("chat_id")):
            raise ValueError("Telegram needs a bot token and a chat ID before it can be switched on")
        c["telegram"] = t
    db = body.get("discord") or {}
    if isinstance(db, dict):
        d = dict(c["discord"])
        url = str(db.get("webhook_url") or "").strip()
        if url and not url.startswith("••••"):
            if not DISCORD_HOOK.match(url):
                raise ValueError("that isn't a Discord webhook URL (it starts https://discord.com/api/webhooks/…)")
            d["webhook_url"] = url
        if db.get("clear_webhook"):
            d.pop("webhook_url", None)
        d["enabled"] = bool(db.get("enabled", d.get("enabled")))
        if d["enabled"] and not d.get("webhook_url"):
            raise ValueError("Discord needs a webhook URL before it can be switched on")
        c["discord"] = d
    ev = body.get("events")
    if isinstance(ev, dict):
        c["events"] = {k: bool(ev.get(k, c["events"].get(k))) for k, _l, _on in EVENTS}
    for k, (lo, hi, _d) in NUMS.items():
        if k in body:
            try:
                v = float(body[k])
            except (TypeError, ValueError):
                raise ValueError(f"{k} must be a number")
            if not lo <= v <= hi:
                raise ValueError(f"{k} must be {lo}-{hi}")
            c[k] = int(v) if float(v).is_integer() else v
    doc["notify"] = c
    srv.save_registry_doc(doc)
    return state()


def set_miner(body: dict[str, Any]) -> dict[str, Any]:
    srv = _srv()
    mid = str(body.get("miner_id") or "")
    doc = srv.load_registry_doc()
    row = next((m for m in doc.get("miners") or [] if _row_id(m) == mid), None)
    if not row:
        raise ValueError(f"unknown miner {mid}")
    row["notify"] = bool(body.get("enabled"))
    srv.save_registry_doc(doc)
    _state.pop(mid, None)
    return {"ok": True, "enabled": row["notify"], "active": _channels(_cfg(doc))}


def miner_enabled(mid: str) -> bool:
    doc = _srv().load_registry_doc()
    return bool(next((m.get("notify") for m in doc.get("miners") or [] if _row_id(m) == mid), False))


# ---------------------------------------------------------------- sending

def _post(url: str, payload: dict[str, Any], timeout: float = 12) -> dict[str, Any]:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "Blake2b-ASIC-Control"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(65536)
        try:
            return json.loads(raw) if raw else {"ok": True}
        except ValueError:
            return {"ok": True}
    except urllib.error.HTTPError as e:
        try:
            j = json.loads(e.read(4096))
            msg = j.get("description") or j.get("message") or str(e)
        except Exception:
            msg = str(e)
        raise RuntimeError(f"HTTP {e.code}: {msg}"[:200])


def _send_now(channel: str, text: str, c: dict[str, Any]) -> None:
    text = text[:1900]
    if channel == "telegram":
        t = c["telegram"]
        r = _post(f"https://api.telegram.org/bot{t['bot_token']}/sendMessage",
                  {"chat_id": t["chat_id"], "text": text, "disable_web_page_preview": True})
        if r.get("ok") is False:
            raise RuntimeError(str(r.get("description") or "Telegram refused it")[:200])
    elif channel == "discord":
        _post(c["discord"]["webhook_url"] + "?wait=true",
              {"content": text, "username": "Blake2b ASIC Control", "allowed_mentions": {"parse": []}})


def _worker() -> None:
    while True:
        channel, text = _q.get()
        ok, err = True, None
        try:
            _send_now(channel, text, _cfg())
        except Exception as e:
            ok, err = False, f"{type(e).__name__}: {e}"[:200]
        with _lock:
            _log.append({"at": int(time.time()), "channel": channel, "text": text[:300], "ok": ok, "error": err})
            del _log[:-50]
        if not ok:
            print(f"[notify] {channel}: {err}", flush=True)
        time.sleep(1.1)        # well under both services' rate limits


threading.Thread(target=_worker, daemon=True, name="notify").start()


def _clean(s: Any) -> str:
    return re.sub(r"[\x00-\x1f]", " ", str(s or ""))[:120]


def emit(mid: str | None, kind: str, text: str, key: str | None = None, recovery: bool = False) -> bool:
    """Queue a notification. Checks the channel, the kind's switch, the miner's switch and the cooldown."""
    try:
        doc = _srv().load_registry_doc()
    except Exception:
        return False
    c = _cfg(doc)
    chans = _channels(c)
    if not chans or not c["events"].get(kind):
        return False
    name = ""
    if mid is not None:
        row = next((m for m in doc.get("miners") or [] if _row_id(m) == mid), None)
        if not row or not row.get("notify"):
            return False
        name = _clean(row.get("name") or mid)
    k = (str(mid), key or kind)
    now = time.time()
    if not recovery and now - _last_sent.get(k, 0) < float(c.get("cooldown_min") or 0) * 60:
        return False
    _last_sent[k] = now
    msg = (f"{name}: " if name else "") + text
    for ch in chans:
        try:
            _q.put_nowait((ch, msg))
        except queue.Full:
            pass
    return True


def test(body: dict[str, Any]) -> dict[str, Any]:
    ch = str(body.get("channel") or "")
    c = _cfg()
    if ch == "telegram" and not (c["telegram"].get("bot_token") and c["telegram"].get("chat_id")):
        raise ValueError("save a bot token and a chat ID first")
    if ch == "discord" and not c["discord"].get("webhook_url"):
        raise ValueError("save a webhook URL first")
    if ch not in ("telegram", "discord"):
        raise ValueError("channel must be telegram or discord")
    try:
        _send_now(ch, "✅ Test from Blake2b ASIC Control: notifications reach you here.", c)
    except Exception as e:
        raise ValueError(f"{ch} didn't take it: {e}")
    with _lock:
        _log.append({"at": int(time.time()), "channel": ch, "text": "test", "ok": True, "error": None})
    return {"ok": True}


def telegram_chats(body: dict[str, Any]) -> dict[str, Any]:
    """The chats that have written to the bot lately: send the bot a message first, then look here."""
    tok = str(body.get("bot_token") or "").strip()
    if not tok or tok.startswith("••••"):
        tok = _cfg()["telegram"].get("bot_token") or ""
    if not TG_TOKEN.match(tok):
        raise ValueError("enter the bot token first (from @BotFather)")
    req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/getUpdates?limit=50",
                                 headers={"User-Agent": "Blake2b-ASIC-Control"})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            j = json.loads(r.read(1_000_000))
    except urllib.error.HTTPError as e:
        raise ValueError(f"Telegram refused the token (HTTP {e.code})")
    except Exception as e:
        raise ValueError(f"couldn't reach Telegram: {type(e).__name__}")
    chats: dict[str, dict[str, Any]] = {}
    for u in j.get("result") or []:
        msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        ch = msg.get("chat") or {}
        if ch.get("id") is not None:
            chats[str(ch["id"])] = {"id": str(ch["id"]), "type": ch.get("type"),
                                    "name": _clean(ch.get("title") or " ".join(x for x in (ch.get("first_name"), ch.get("last_name")) if x) or ch.get("username") or "")}
    return {"ok": True, "chats": list(chats.values())}


# ---------------------------------------------------------------- hooks from the rest of the app

def note_app_restart(ip: str) -> None:
    _app_restart_at[str(ip)] = time.time()


def app_restart_done(mid: str, st: dict[str, Any]) -> None:
    state_ = st.get("state")
    if state_ == "done":
        emit(mid, "app_restart", f"🔄 restarted, hashing again after {st.get('took_s')} s", key=f"app_restart:{st.get('since')}")
    else:
        emit(mid, "app_restart", f"⚠️ restart: {st.get('msg')}", key=f"app_restart:{st.get('since')}")


def schedule_problem(mid: str, text: str) -> None:
    emit(mid, "schedule", f"⚠️ schedule: {text}", key="schedule:" + text[:40])


_idle_changed: dict[str, float] = {}


def idle_changed(mid: str) -> None:
    """The app just put a miner into Idle or out of it: a restart of its mining software right after
    isn't 'on its own'."""
    _idle_changed[mid] = time.time()


def reject_problem(mid: str, text: str) -> None:
    emit(mid, "reject", text, key="reject:" + text[:60])


def best_share(mid: str, best_text: str, prev_text: str, on: str) -> None:
    emit(mid, "best", f"🏆 new best-share record {best_text} (was {prev_text})" + (f" on {on}" if on else ""),
         key=f"best:{best_text}", recovery=True)


# ---------------------------------------------------------------- the watcher

def _read(row: dict[str, Any]) -> dict[str, Any]:
    from miner_client import MinerClient
    c = MinerClient(ip=str(row["ip"]), password="", name=_row_id(row), id=_row_id(row))
    s = (c.bfg("summary").get("SUMMARY") or [{}])[0]
    devs = list(c.bfg("devs").get("DEVS") or [])
    temps, mhs, clocks = [], 0.0, []
    for d in devs:
        for k, v in d.items():
            if (k == "Temperature" or str(k).startswith("tstemp-")) and v is not None:
                try:
                    temps.append(float(v))
                except (TypeError, ValueError):
                    pass
        for k in ("MHS 20s", "MHS 5s", "MHS av"):
            if d.get(k) is not None:
                mhs += float(d[k])
                break
        if d.get("clock") is not None:
            try:
                clocks.append(float(d["clock"]))
            except (TypeError, ValueError):
                pass
    if not mhs:
        mhs = float(s.get("MHS 20s") or s.get("MHS av") or 0)
    return {"elapsed": float(s.get("Elapsed") or 0), "mhs": mhs, "temp": max(temps) if temps else None,
            "clock": max(clocks) if clocks else None}


def _fmt_ths(mhs: float) -> str:
    return f"{mhs / 1e6:.2f} TH/s" if mhs >= 1e5 else f"{mhs / 1e3:.1f} GH/s"


def _check(row: dict[str, Any], c: dict[str, Any], now: float) -> None:
    mid = _row_id(row)
    st = _state.setdefault(mid, {"hist": [], "since": now})
    tuning = _tuner_running(mid)
    # tuner finished
    if st.get("tuning") and not tuning:
        line = _tuner_finished(mid) or "the run ended"
        emit(mid, "tuner", f"🔧 tuning run finished: {line}", key=f"tuner:{now:.0f}", recovery=True)
    st["tuning"] = tuning
    # weak chips
    h = _health(mid) or {}
    weak = set(h.get("weak") or [])
    if "weak" in st:
        new = sorted(weak - st["weak"])
        if new:
            emit(mid, "chip", f"🧩 chip health: {', '.join(new)} turned weak (see the Miner page)", key="chip:" + ",".join(new))
    if h:
        st["weak"] = weak
    try:
        r = _read(row)
    except Exception:
        r = None
    off_after = float(c["offline_min"]) * 60
    idle = row.get("active_preset") == "idle"      # put to sleep on purpose: no hashing is the point
    if idle:
        st["hist"], st["low_since"] = [], None
    if r is None and idle:
        st.pop("down_since", None)                 # the mining software may stop answering while idle
        st["off_sent"] = False                     # (and no "back online" for it later)
        return
    if r is None:
        st.setdefault("down_since", now)
        if (not st.get("off_sent") and now - st["down_since"] >= off_after and not _restarting(mid)):
            emit(mid, "offline", f"🔴 not answering for {int((now - st['down_since']) / 60)} min", key="offline")
            st["off_sent"] = True
        return
    if st.get("off_sent") and st.get("down_since"):
        emit(mid, "offline", f"🟢 back online after {int((now - st['down_since']) / 60)} min", key="online", recovery=True)
    st.pop("down_since", None)
    st["off_sent"] = False
    # restarted on its own
    last_el = st.get("elapsed")
    if last_el is not None and r["elapsed"] + 60 < last_el:
        app = now - _app_restart_at.get(str(row.get("ip")), 0) < APP_RESTART_QUIET_S
        if not app and not tuning and not _restarting(mid) and not idle and now - _idle_changed.get(mid, 0) > APP_RESTART_QUIET_S:
            emit(mid, "self_restart", f"♻️ its mining software restarted on its own (it had been up {int(last_el / 3600)} h {int(last_el % 3600 / 60)} min)",
                 key="self_restart")
        st["hist"] = []
    st["elapsed"] = r["elapsed"]
    # hot
    hot = float(c["hot_c"])
    if r["temp"] is not None:
        if r["temp"] >= hot and not st.get("hot_sent"):
            emit(mid, "hot", f"🔥 hottest board {r['temp']:.0f} °C (alert at {hot:g} °C)", key="hot")
            st["hot_sent"] = True
        elif st.get("hot_sent") and r["temp"] <= hot - 5:
            emit(mid, "hot", f"❄️ cooled down to {r['temp']:.0f} °C", key="cool", recovery=True)
            st["hot_sent"] = False
    # hashrate against its own usual at this clock (a preset change starts the usual again)
    if idle:
        st["elapsed"] = r["elapsed"]
        return
    sig = (row.get("active_preset"), r["clock"])
    if st.get("sig") != sig:
        st["sig"], st["hist"], st["low_since"] = sig, [], None
        if st.get("low_sent"):
            st["low_sent"] = False
    hist = st["hist"]
    if tuning or r["elapsed"] < WARMUP_S or r["mhs"] <= 0:
        return
    usual = statistics.median(x for _t, x in hist) if len(hist) >= 20 else None
    low = usual is not None and r["mhs"] < usual * float(c["low_pct"]) / 100
    if not low:
        hist.append((now, r["mhs"]))
        del hist[:-120]                       # about an hour at a reading every 30 s
    if low:
        st["low_since"] = st.get("low_since") or now
        if not st.get("low_sent") and now - st["low_since"] >= LOW_FOR_S:
            emit(mid, "hashrate", f"📉 hashrate {_fmt_ths(r['mhs'])} for {int((now - st['low_since']) / 60)} min, "
                                  f"under {c['low_pct']:g}% of its usual {_fmt_ths(usual)}", key="hashrate")
            st["low_sent"] = True
    else:
        if st.get("low_sent") and usual is not None and r["mhs"] >= usual * 0.9:
            emit(mid, "hashrate", f"📈 hashrate back to {_fmt_ths(r['mhs'])}", key="hashrate_ok", recovery=True)
            st["low_sent"] = False
        st["low_since"] = None


def tick() -> None:
    """Called from the fan loop; checks every notified miner every TICK_S, in the background."""
    now = time.time()
    if _busy[0] or now - _tick[0] < TICK_S:
        return
    _tick[0] = now

    def run() -> None:
        try:
            doc = _srv().load_registry_doc()
            c = _cfg(doc)
            if not _channels(c):
                _state.clear()      # nothing is watched while no channel is on: start fresh when one is, so
                return              # an old uptime or outage isn't reported as news
            for m in doc.get("miners") or []:
                if m.get("demo") or not m.get("ip") or not m.get("notify"):
                    continue
                try:
                    _check(m, c, time.time())
                except Exception as e:
                    print(f"[notify] {_row_id(m)}: {e}", flush=True)
        finally:
            _busy[0] = False
    _busy[0] = True
    threading.Thread(target=run, daemon=True, name="notify-watch").start()


ROUTES_GET = {"/api/notify/state": state}
ROUTES_POST = {"/api/notify/save": save, "/api/notify/test": test, "/api/notify/miner": set_miner,
               "/api/notify/telegram_chats": telegram_chats}
