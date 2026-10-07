#!/usr/bin/env python3
"""Blake2b ASIC Control: add its pages (Tuner, Profiles, Schedule), the login and the Fleet additions
to the web dashboard it builds on (Maveth's, sc-lite/webui).

Put these three files in the webui folder (next to server.py), then run:
    python install_addons.py              # install (safe to run again)
    python install_addons.py --uninstall  # put the original files back

It makes a backup of server.py and static/index.html before changing them,
moves the pages into static/, and checks that ../python/asic_tuner.py exists.
Restart the dashboard (python server.py) afterwards.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE / "server.py"
INDEX = HERE / "static" / "index.html"
PAGE_SRC = HERE / "tuner.html"
PAGE_DST = HERE / "static" / "tuner.html"
ADDON = HERE / "tuner_addon.py"
AUTH = HERE / "auth_addon.py"
FANS = HERE / "fan_addon.py"
PROF_SRC = HERE / "profiles.html"
PROF_DST = HERE / "static" / "profiles.html"
SCHED = HERE / "schedule_addon.py"
SHARES = HERE / "shares_addon.py"
HEALTH = HERE / "health_addon.py"
SAFETY = HERE / "miner_safety.py"
NOTIFY = HERE / "notify_addon.py"
REJECTS = HERE / "rejects_addon.py"
HASHRATE = HERE / "hashrate_addon.py"
REPORT = HERE / "report_addon.py"
EXTRA_PAGES = [(HERE / n, HERE / "static" / n) for n in ("schedule.html", "addon.js")]
FAN_MARK = "fan_addon"
APP_NAME = "Blake2b ASIC Control"
LOGIN_SRC = HERE / "login.html"
LOGIN_DST = HERE / "static" / "login.html"
THEME_SRC = HERE / "theme.css"
THEME_DST = HERE / "static" / "theme.css"
THEME_LINK = '  <link rel="stylesheet" href="/theme.css" />\n'
THEMEJS_SRC = HERE / "theme.js"
THEMEJS_DST = HERE / "static" / "theme.js"
THEMEJS_TAG = '  <script src="/theme.js"></script>\n'
TOGGLE = '<button type="button" class="theme-btn" data-theme-toggle>\u2600 Light</button>'

AUTH_MARK = "auth_addon.gate"
# sends the browser to the login page if a session expires while a page is open
FETCH_GUARD = ("<script>(()=>{const f=window.fetch;window.fetch=async(...a)=>{const r=await f(...a);"
               "if(r.status===401&&!String(a[0]).includes('/api/auth/'))"
               "location.href='/login.html?next='+encodeURIComponent(location.pathname);return r;}})();</script>\n")
TUNER = HERE.parent / "python" / "asic_tuner.py"
APPJS = HERE / "static" / "app.js"
LAYOUT_MARK = "hdr-right"

# Header: Fleet / Miner / Batch / Tuner on the left; Settings top right (the account is in Settings),
# with Refresh + auto underneath. Same element ids as before, so app.js keeps working.
HEADER = """<header class="app">
    <h1><span class="brand">Blake2b</span> ASIC Control</h1>
    <div class="tabs">
      <button type="button" id="tabFleet" class="active">Fleet</button>
      <button type="button" id="tabDetail" style="display:none">Miner</button>
      <button type="button" id="tabBatch">Pools</button>
      <button type="button" id="tabProfiles" onclick="location.href='/profiles.html'">Profiles</button>
      <button type="button" id="tabSchedule" onclick="location.href='/schedule.html'">Schedule</button>
      <button type="button" id="tabTuner" onclick="location.href='/tuner.html'">Tuner</button>
    </div>
    <div class="spacer"></div>
    <div class="hdr-right">
      <div class="row">
        <button type="button" id="tabSettings">Settings</button>
      </div>
      <div class="row">
        <span id="autoState" class="muted" style="font-size:.75rem"></span>
        <button type="button" class="theme-btn" data-theme-toggle>☀ Light</button>
        <button type="button" id="btnRefresh">Refresh</button>
        <label class="muted"><input type="checkbox" id="autoRefresh" checked /> auto</label>
      </div>
    </div>
  </header>"""

HEAD_EXTRA = """  <style>
    .hdr-right { display: flex; flex-direction: column; align-items: flex-end; gap: .4rem; }
    .hdr-right button.active { border-color: var(--accent); color: var(--accent); }
    @media (max-width: 640px) { .hdr-right { align-items: flex-start; } }
  </style>
"""

# Loaded before app.js: while you are typing in (or have just changed) a field, the
# auto-refresh skips its turn so it cannot overwrite what you entered. Clicking any
# button (Save, Apply, a tab...) ends the pause; so do 2 minutes without typing.
EDIT_GUARD = """  <script>(() => {
    let dirtyAt = 0;
    const field = (e) => e && e.matches && e.matches("main input:not([type=checkbox]):not([type=radio]):not([type=range]), main select, main textarea");
    document.addEventListener("input", (e) => {
      const t = e.target;
      if (t.closest && t.closest("main") && !t.closest("#fleetGrid")) dirtyAt = Date.now();
    }, true);
    document.addEventListener("click", (e) => {
      if (e.target.closest && e.target.closest("button")) setTimeout(() => { dirtyAt = 0; }, 300);
    }, true);
    window.sclEditing = () => field(document.activeElement) || (dirtyAt > 0 && Date.now() - dirtyAt < 120000);
    setInterval(() => {
      const l = document.getElementById("autoState"), a = document.getElementById("autoRefresh");
      if (l) l.textContent = a && a.checked && window.sclEditing() ? "auto paused while editing" : "";
    }, 1000);
  })();</script>
"""

# After app.js: open a tab from the address (/#settings, /#miner, /#batch, /#fleet)
HASH_NAV = """  <script>(() => {
    const id = { fleet: "tabFleet", batch: "tabBatch", pools: "tabBatch", settings: "tabSettings" }[location.hash.slice(1)];
    if (id) document.getElementById(id).click();
  })();</script>
"""
MARK = "tuner_addon"

# Before app.js: what each miner was detected as (for the Fleet cards), refreshed every minute
HW_SCRIPT = """  <script>(() => {
    window.sclHw = {};
    const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
    window.sclHwLine = (id) => {
      const h = window.sclHw[id]; if (!h) return "";
      const ok = h.hardware && h.hardware.ok;
      const bits = [`<span style="color:${ok ? "var(--muted)" : "var(--warn)"}">${h.probing ? "probing…" : esc(h.line)}</span>`];
      if (!h.tuning && h.schedule) bits.push(`<span style="color:var(--muted)">${esc(h.schedule)}</span>`);
      const sh = h.share;
      const rj = h.rejects;
      if (rj && rj.day && rj.day.other) bits.push(`<span style="color:var(--bad);font-weight:600" title="A share the pool refused for a reason other than stale (Miner page for the details)">${rj.day.other} rejected share${rj.day.other > 1 ? "s" : ""} NOT stale${rj.other_last ? ` (${esc(rj.other_last.slice(11, 16))})` : ""}</span>`);
      if (sh && (sh.now || sh.record)) bits.push(`<span style="color:var(--muted)" title="Best share since the miner last restarted, and the best ever seen (Miner page for the history)">Best share <b style="color:var(--text)">${esc(sh.now_text)}</b>${sh.record && sh.record > sh.now ? ` · record ${esc(sh.record_text)}` : ""}</span>`);
      return "<br>" + bits.join("<br>");
    };
    const load = () => fetch("/api/hardware/list").then(r => r.ok ? r.json() : null)
      .then(j => { if (j) window.sclHw = j.miners || {}; }).catch(() => {});
    load(); setInterval(load, 60000);
  })();</script>
"""


def backup(p: Path) -> Path:
    b = p.with_name(p.name + ".before-tuner")
    if not b.exists():
        shutil.copy2(p, b)
    return b


def insert_after_line(text: str, anchor: str, new: str) -> str | None:
    i = text.find(anchor)
    if i < 0:
        return None
    j = text.find("\n", i) + 1
    return text[:j] + new + text[j:]


def patch_server(text: str) -> str:
    # 1. import
    t = insert_after_line(text, "from soft_watchdog import", "import tuner_addon  # noqa: E402\n") \
        or insert_after_line(text, "sys.path.insert(0, str(HERE))", "import tuner_addon  # noqa: E402\n")
    if t is None:
        raise SystemExit("could not find where to add the import in server.py")
    text = t

    # 2. GET route: right at the start of the try block in do_GET
    g = text.find("def do_GET")
    if g < 0:
        raise SystemExit("could not find do_GET in server.py")
    k = text.find("try:\n", g)
    indent = " " * (len(text[:k]) - len(text[:k].rstrip(" ")) + 4)
    line = (f"{indent}if path.startswith(\"/api/tuner/\") and "
            f"tuner_addon.handle_get(self, path, json_response):\n{indent}    return\n")
    k = k + len("try:\n")
    text = text[:k] + line + text[k:]

    # 3. POST route: right after the body is read in do_POST
    p = text.find("def do_POST")
    if p < 0:
        raise SystemExit("could not find do_POST in server.py")
    b = text.find("body = read_json(self)", p)
    if b < 0:
        raise SystemExit("could not find 'body = read_json(self)' in do_POST")
    indent = " " * (len(text[:b]) - len(text[:b].rstrip(" ")))
    line = (f"{indent}if path.startswith(\"/api/tuner/\") and tuner_addon.handle_post("
            f"self, path, body, json_response, load_registry):\n{indent}    return\n")
    e = text.find("\n", b) + 1
    text = text[:e] + line + text[e:]
    return text


def patch_server_fans(text: str) -> str:
    """Route /api/fans/* and /api/presets/* to fan_addon, right after the tuner's routes."""
    t = insert_after_line(text, "import tuner_addon", "import fan_addon  # noqa: E402\n")
    if t is None:
        raise SystemExit("could not find the tuner import in server.py")
    text = t
    g = text.find('tuner_addon.handle_get(self, path, json_response)')
    if g < 0:
        raise SystemExit("could not find the tuner GET route in server.py")
    ls = text.rfind("\n", 0, g) + 1
    indent = " " * (len(text[ls:g]) - len(text[ls:g].lstrip(" ")))
    end = text.find("\n", text.find("\n", g) + 1) + 1   # after the "return" line
    line = (f"{indent}if path.startswith((\"/api/fans/\", \"/api/presets/\", \"/api/hardware/\", \"/api/schedule/\", \"/api/quick/\", \"/api/notify/\", \"/api/settings/\")) and "
            f"fan_addon.handle_get(self, path, json_response):\n{indent}    return\n")
    text = text[:end] + line + text[end:]
    p = text.find('tuner_addon.handle_post(')
    if p < 0:
        raise SystemExit("could not find the tuner POST route in server.py")
    ls = text.rfind("\n", 0, p) + 1
    indent = " " * (len(text[ls:p]) - len(text[ls:p].lstrip(" ")))
    end = text.find("\n", text.find("\n", p) + 1) + 1
    line = (f"{indent}if path.startswith((\"/api/fans/\", \"/api/presets/\", \"/api/hardware/\", \"/api/schedule/\", \"/api/quick/\", \"/api/notify/\", \"/api/settings/\")) and "
            f"fan_addon.handle_post(self, path, body, json_response):\n{indent}    return\n"
            f"{indent}if fan_addon.guard_post(self, path, body, json_response):\n{indent}    return\n")
    text = text[:end] + line + text[end:]
    # pages and scripts: always check for a newer copy, so an app update shows up on a normal refresh
    h = text.find("    def log_message(self, fmt: str, *args: Any) -> None:")
    if h < 0:
        raise SystemExit("could not find the request handler in server.py")
    nocache = ("    timeout = 60  # added: drop connections that send nothing for a minute\n\n"
               "    def end_headers(self) -> None:  # added: browsers re-check pages after an update\n"
               "        if not self.path.startswith(\"/api/\"):\n"
               "            self.send_header(\"Cache-Control\", \"no-cache\")\n"
               "        # added: no framing by other sites or apps (clickjacking), no MIME sniffing, no referrers\n"
               "        self.send_header(\"X-Frame-Options\", \"DENY\")\n"
               "        self.send_header(\"Content-Security-Policy\", \"frame-ancestors 'none'; object-src 'none'; base-uri 'self'; form-action 'self'\")\n"
               "        self.send_header(\"X-Content-Type-Options\", \"nosniff\")\n"
               "        self.send_header(\"Referrer-Policy\", \"same-origin\")\n"
               "        super().end_headers()\n\n")
    text = text[:h] + nocache + text[h:]
    # any number of demo miners (a later def replaces the dashboard's own two)
    g = text.find("\ndef get_client(")
    if g < 0:
        raise SystemExit("could not find get_client in server.py")
    demo = ("\ndef demo_miner_rows(doc: dict[str, Any]) -> list[dict[str, Any]]:  # replaced by fan_addon\n"
            "    return fan_addon.demo_rows(doc, _demo_snapshot)\n\n")
    return text[:g] + demo + text[g:]


SAVE_OLD = ('def save_registry_doc(doc: dict[str, Any]) -> None:\n'
            '    REGISTRY_PATH.write_text(json.dumps(doc, indent=2) + "\\n", encoding="utf-8")\n')
SAVE_NEW = ('_registry_write_lock = threading.Lock()\n\n\n'
            'def save_registry_doc(doc: dict[str, Any]) -> None:  # patched: atomic, one writer at a time, owner-only\n'
            '    # miners.json holds the miners\' passwords: write a temp file and rename it over the old one, so a\n'
            '    # crash or a second writer never leaves it half-written, and keep it readable by this app only\n'
            '    with _registry_write_lock:\n'
            '        tmp = REGISTRY_PATH.with_name(REGISTRY_PATH.name + ".tmp")\n'
            '        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)\n'
            '        with os.fdopen(fd, "w", encoding="utf-8") as fh:\n'
            '            fh.write(json.dumps(doc, indent=2) + "\\n")\n'
            '            fh.flush()\n'
            '            os.fsync(fh.fileno())\n'
            '        os.chmod(tmp, 0o600)\n'
            '        os.replace(tmp, REGISTRY_PATH)\n')
READ_OLD = ('    n = int(handler.headers.get("Content-Length") or 0)\n'
            '    if n <= 0:\n'
            '        return {}\n'
            '    return json.loads(handler.rfile.read(n))\n')
READ_NEW = ('    n = int(handler.headers.get("Content-Length") or 0)\n'
            '    if n <= 0:\n'
            '        return {}\n'
            '    if n > 1_000_000:  # patched: no request body is anywhere near this\n'
            '        raise ValueError("request too large")\n'
            '    return json.loads(handler.rfile.read(n))\n')
JSON_OLD = ('def json_response(handler: SimpleHTTPRequestHandler, code: int, obj: Any) -> None:\n'
            '    raw = json.dumps(obj, default=str).encode()\n')
JSON_NEW = ('def _no_passwords(obj: Any, depth: int = 0) -> Any:  # patched: miners\' passwords never leave the server\n'
            '    if depth > 32:\n'
            '        return obj\n'
            '    if isinstance(obj, dict):  # by key name, wherever it sits in the reply\n'
            '        return {k: _no_passwords(v, depth + 1) for k, v in obj.items()\n'
            '                if not (isinstance(k, str) and k.lower() in ("password", "passwd", "jwt", "token"))}\n'
            '    if isinstance(obj, (list, tuple)):\n'
            '        return [_no_passwords(v, depth + 1) for v in obj]\n'
            '    return obj\n\n\n'
            'def json_response(handler: SimpleHTTPRequestHandler, code: int, obj: Any) -> None:\n'
            '    raw = json.dumps(_no_passwords(obj), default=str).encode()\n')
MODEL_OLD = 'model = (ident.get("model") or "miner").replace(" ", "-").lower()'
MODEL_NEW = 'model = re.sub(r"[^a-z0-9-]+", "", (ident.get("model") or "miner").replace(" ", "-").lower())[:32] or "miner"  # patched: a miner names itself'


def patch_server_hardening(text: str) -> str:
    """Safer registry writes, a request size cap, no passwords in responses, a clean id from a probe."""
    for old, new, what in ((SAVE_OLD, SAVE_NEW, "save_registry_doc"), (READ_OLD, READ_NEW, "read_json"),
                           (JSON_OLD, JSON_NEW, "json_response"), (MODEL_OLD, MODEL_NEW, "probe id")):
        if old not in text:
            raise SystemExit(f"server.py: {what} changed upstream; hardening not applied")
        text = text.replace(old, new, 1)
    for mod in ("import os", "import re", "import threading"):
        if f"\n{mod}\n" not in text:
            text = text.replace("\nimport json\n", f"\nimport json\n{mod}\n", 1)
    return text


def patch_server_auth(text: str) -> str:
    t = insert_after_line(text, "import tuner_addon", "import auth_addon  # noqa: E402\n")
    if t is None:
        raise SystemExit("could not find where to add the auth import in server.py")
    text = t
    for method in ("def do_GET", "def do_POST", "def do_DELETE"):
        i = text.find(method)
        if i < 0:
            continue
        j = text.find("\n", i) + 1
        indent = " " * (len(text[:i]) - len(text[:i].rstrip(" ")) + 4)
        text = text[:j] + f"{indent}if auth_addon.gate(self):\n{indent}    return\n" + text[j:]
    # HEAD would otherwise answer for any page without signing in (headers only, but still)
    i = text.find("    def do_GET")
    head = ("    def do_HEAD(self) -> None:  # noqa: N802  added: same sign-in rule as GET\n"
            "        if auth_addon.gate(self):\n"
            "            return\n"
            "        super().do_HEAD()\n\n")
    return text[:i] + head + text[i:]


def patch_layout(text: str) -> str:
    import re
    t, n = re.subn(r"<header class=\"app\">.*?</header>", lambda m: HEADER, text, count=1, flags=re.S)
    if n != 1:
        raise SystemExit("could not find the header in static/index.html")
    t = re.sub(r"<title>.*?</title>", f"<title>{APP_NAME}</title>", t, count=1)
    t = t.replace("Local SC Lite control", "Local Blake2b ASIC control")
    t = t.replace("<h2>Batch add pool</h2>", "<h2>Add a pool to the ticked miners</h2>", 1)
    t = t.replace("<h2>Order / remove / promote</h2>", "<h2>Pool order, remove or promote (ticked miners)</h2>", 1)
    # point the stock fan panels at the curve editor
    t = t.replace("<h2>Auto fan defaults</h2>",
                  "<h2>Auto fan defaults</h2>\n        <p class=\"muted\" style=\"margin:0 0 .5rem\">Draw your own fan curves "
                  "and use presets on the <a href=\"/profiles.html\">Profiles</a> page.</p>", 1)
    t = t.replace("<h2>Fan</h2>", "<h2>Fan <a class=\"muted\" style=\"font-size:.8rem;font-weight:400\" "
                  "href=\"/profiles.html\">curves &amp; presets →</a></h2>", 1)
    t = t.replace("</head>", HEAD_EXTRA + "</head>", 1)
    i = t.find('<script src="/app.js"')
    if i < 0:
        raise SystemExit("could not find app.js in static/index.html")
    line = t.rfind("\n", 0, i) + 1
    end = t.find("\n", i) + 1
    if "window.fetch=async" not in t:
        t = t[:line] + "  " + FETCH_GUARD + t[line:]
        i = t.find('<script src="/app.js"'); line = t.rfind("\n", 0, i) + 1; end = t.find("\n", i) + 1
    return (t[:line] + EDIT_GUARD + HW_SCRIPT + t[line:end] + HASH_NAV
            + '  <script src="/addon.js"></script>\n' + t[end:])


def patch_appjs(text: str) -> str:
    old = "state.timer = setInterval(() => refreshAllSnapshots(), 5000);"
    if old not in text:
        raise SystemExit("could not find the auto-refresh timer in static/app.js")
    # every 15 s instead of 5 s (easier on the miner's small web server), and skip while editing
    text = text.replace(old, "state.timer = setInterval(() => { if (!(window.sclEditing && window.sclEditing())) "
                             "refreshAllSnapshots(); }, 15000);", 1)
    # Fleet cards: what each miner was detected as, under its IP (filled in by HW_SCRIPT)
    card = '<div class="meta">${escapeHtml(m.ip)}</div>'
    hooks = [("  function toast(msg, err = false) {", "  window.sclState = state;\n  function toast(msg, err = false) {"),
             ('  $("tabFleet").onclick = () => setView("fleet");',
              '  $("tabFleet").onclick = () => setView("fleet");\n  window.sclRefresh = refreshAllSnapshots;'
              '\n  window.sclDetail = () => (state.view === "detail" ? state.detailId : null);'
              '\n  window.sclRender = renderFleet;')]
    for a, b in hooks:
        if a not in text:
            raise SystemExit(f"could not find {a.strip()!r} in static/app.js")
        text = text.replace(a, b, 1)
    # Fleet cards: the power presets as one-click buttons instead of the auto-fan box and curve list
    fanrow = ('            <label class="muted"><input type="checkbox" class="fan-en" data-id="${m.id}" ${fanOn ? "checked" : ""}/> auto fan</label>\n'
              '            <select class="fan-prof" data-id="${m.id}">${profOpts}</select>\n')
    if fanrow in text:
        text = text.replace(fanrow, '            ${window.sclPresetRow ? window.sclPresetRow(m.id) : ""}\n', 1)
    else:
        print("app.js: fleet card fan row changed upstream; preset buttons not added")
    pill = '${fanOn ? `<span class="pill ok">auto-fan</span>` : `<span class="pill">fan off</span>`}'
    if pill in text:   # the pill next to online/down: the preset on the miner (or tuning) instead of fan on/off
        text = text.replace(pill, '${window.sclPill ? window.sclPill(m.id) : ""}', 1)
    else:
        print("app.js: fleet card pill changed upstream; preset pill not added")
    btnrow = ('            <button type="button" class="fan-minus" data-id="${m.id}">Fan −5</button>\n'
              '            <button type="button" class="fan-plus" data-id="${m.id}">Fan +5</button>\n'
              '            <button type="button" class="open primary" data-id="${m.id}">Open</button>\n'
              '            <button type="button" class="failback" data-id="${m.id}">Failback</button>\n')
    if btnrow in text:   # card buttons: pool switcher, restart, the miner's own page (click the name to open it here)
        text = text.replace(btnrow, '            ${window.sclCardActions ? window.sclCardActions(m) : ""}\n', 1)
    else:
        print("app.js: fleet card buttons changed upstream; card actions not replaced")
    autofan = ('            <div style="grid-column:1/-1">\n'
               '              <div class="label">Auto fan</div>\n'
               '              <div class="value" style="font-size:0.75rem">${escapeHtml(fr.last_status || "—")}\n'
               '                ${fr.last_applied_fan != null ? ` · plan ${fr.last_applied_fan}` : ""}\n'
               '                ${fc.fan_offset ? ` · offset ${fc.fan_offset}` : ""}\n'
               '              </div>\n'
               '            </div>\n')
    if autofan in text:   # the Auto fan tile: power estimate and chip health instead (fans are on the Profiles page)
        text = text.replace(autofan, '            ${window.sclCardInfo ? window.sclCardInfo(m) : ""}\n', 1)
    else:
        print("app.js: fleet card auto fan tile changed upstream; power / health tile not added")
    pool = ('            <div style="grid-column:1/-1">\n'
            '              <div class="label">Working pool</div>\n'
            '              <div class="value" style="font-size:0.8rem;word-break:break-all">\n'
            '                ${wp ? `P${wp.id} · ${escapeHtml(wp.url || "")}` : "—"}\n'
            '              </div>\n'
            '            </div>\n')
    if pool in text:   # the Working pool tile: a 24-hour hashrate graph instead (the pool is in the card's pool list)
        text = text.replace(pool, '            ${window.sclHashGraph ? window.sclHashGraph(m) : ""}\n', 1)
    else:
        print("app.js: fleet card working pool tile changed upstream; hashrate graph not added")
    hs = '<div><div class="label">Hashrate</div><div class="value">${fmtHs(s.mhs_av)}</div></div>'
    if hs in text:   # the Hashrate tile: the latest 20-second reading (MHS av is the average since the restart)
        text = text.replace(hs, '${window.sclHashTile ? window.sclHashTile(m, fmtHs(s.mhs_av)) : `' + hs.replace("`", "") + '`}', 1)
    else:
        print("app.js: fleet card hashrate tile changed upstream; 20-second hashrate not added")
    live = '      ["Hashrate", fmtHs(s.mhs_av)],'
    if live in text:   # the Miner page's Live panel: the same 20-second hashrate as the Fleet card
        text = text.replace(live, '      ["Hashrate", window.sclHashText ? window.sclHashText(state.detailId, fmtHs(s.mhs_av)) : fmtHs(s.mhs_av)],', 1)
    else:
        print("app.js: Live panel hashrate changed upstream; 20-second hashrate not added")
    if card in text:
        text = text.replace(card, '<div class="meta">${escapeHtml(m.ip)}${window.sclHwLine ? window.sclHwLine(m.id) : ""}</div>', 1)
    else:
        print("app.js: fleet card layout changed upstream; hardware line not added")
    # values a miner reports go into the page as text, never as HTML (a hostile miner could otherwise
    # inject script): escape the ones the dashboard writes raw
    xss = [('data-id="${m.id}"', 'data-id="${escapeHtml(m.id)}"'),
           ('sticky P${wp.id}', 'sticky P${escapeHtml(wp.id)}'),
           ('${wp ? `P${wp.id} · ', '${wp ? `P${escapeHtml(wp.id)} · '),
           ('`<div><div class="label">${label}</div><div class="value">${value}</div></div>`',
            '`<div><div class="label">${escapeHtml(label)}</div><div class="value">${escapeHtml(value)}</div></div>`'),
           ('<span class="muted">${s.error || s.jwt_error || "no board data"}</span>',
            '<span class="muted">${escapeHtml(s.error || s.jwt_error || "no board data")}</span>'),
           ('`<tr><td>${d.id}</td><td>${d.status}</td>', '`<tr><td>${escapeHtml(d.id)}</td><td>${escapeHtml(d.status)}</td>'),
           ('<td>P${p.id}</td>', '<td>P${escapeHtml(p.id)}</td>'),
           ('<td>${p.accepted ?? ""}</td>', '<td>${escapeHtml(p.accepted ?? "")}</td>'),
           ('data-idx="${p.id}"', 'data-idx="${escapeHtml(p.id)}"'),
           ('.replace(/"/g, "&quot;");\n  }', '.replace(/"/g, "&quot;")\n      .replace(/\'/g, "&#39;");\n  }')]
    for a, b in xss:
        if a in text:
            text = text.replace(a, b)
        else:
            print(f"app.js: couldn't escape {a[:40]!r} (changed upstream); miner values are also cleaned on the server")
    # "Failback" is network jargon (and easily read as "fallback", the opposite): say what it does
    for a, b in (('confirm("Failback to preferred + soft restart?")',
                  'confirm("Switch this miner to that pool (it becomes its first pool) and restart its miner software?")'),
                 ('confirm("Failback selected miners to P0 + soft restart?")',
                  'confirm("Switch the ticked miners back to pool 0 and restart their miner software?")')):
        text = text.replace(a, b, 1)
    return text


def patch_index(text: str) -> str:
    btn = ('      <button type="button" id="tabTuner" '
           'onclick="location.href=\'/tuner.html\'">Tuner</button>\n')
    t = insert_after_line(text, 'id="tabSettings"', btn)
    if t is None:  # no Settings tab: add after the first tab button we can find
        t = insert_after_line(text, 'class="tabs"', btn)
    if t is None:
        raise SystemExit("could not find the tab buttons in static/index.html")
    return t


def install() -> None:
    for p in (SERVER, INDEX, ADDON, AUTH, FANS, SCHED, SHARES, HEALTH, SAFETY, NOTIFY, REJECTS, HASHRATE, REPORT):
        if not p.exists():
            raise SystemExit(f"missing {p}. Run this from the webui folder with all three files in it.")
    if PAGE_SRC.exists():
        shutil.move(str(PAGE_SRC), str(PAGE_DST))
    if not PAGE_DST.exists():
        raise SystemExit("missing tuner.html; put it next to this script and run again")
    if LOGIN_SRC.exists():
        shutil.move(str(LOGIN_SRC), str(LOGIN_DST))
    if THEME_SRC.exists():
        shutil.move(str(THEME_SRC), str(THEME_DST))
    if THEMEJS_SRC.exists():
        shutil.move(str(THEMEJS_SRC), str(THEMEJS_DST))
    if PROF_SRC.exists():
        shutil.move(str(PROF_SRC), str(PROF_DST))
    if not PROF_DST.exists():
        raise SystemExit("missing profiles.html; put it next to this script and run again")
    for src, dst in EXTRA_PAGES:
        if src.exists():
            shutil.move(str(src), str(dst))
        if not dst.exists():
            raise SystemExit(f"missing {src.name}; put it next to this script and run again")
    if not LOGIN_DST.exists():
        raise SystemExit("missing login.html; put it next to this script and run again")

    s = SERVER.read_text(encoding="utf-8")
    if MARK in s:
        print("server.py: already set up")
    else:
        backup(SERVER)
        new = patch_server(s)
        compile(new, str(SERVER), "exec")  # refuse to write a broken file
        SERVER.write_text(new, encoding="utf-8")
        print("server.py: tuner routes added (backup: server.py.before-tuner)")

    h = INDEX.read_text(encoding="utf-8")
    if LAYOUT_MARK in h:
        print("index.html: layout already set up")
    else:
        backup(INDEX)
        INDEX.write_text(patch_layout(h), encoding="utf-8")
        print("index.html: new header (Tuner, Settings top right), edit guard (backup: index.html.before-tuner)")

    h = INDEX.read_text(encoding="utf-8")
    if "theme.css" in h:
        print("index.html: monochrome theme already linked")
    else:
        backup(INDEX)
        INDEX.write_text(h.replace("</head>", THEME_LINK + "</head>", 1), encoding="utf-8")
        print("index.html: monochrome theme linked")

    h = INDEX.read_text(encoding="utf-8")
    changed = False
    if "theme.js" not in h:
        h = h.replace("</head>", THEMEJS_TAG + "</head>", 1); changed = True
    if "data-theme-toggle" not in h:
        h = h.replace('<button type="button" id="btnRefresh">', TOGGLE + '\n        <button type="button" id="btnRefresh">', 1)
        changed = True
    if changed:
        backup(INDEX)
        INDEX.write_text(h, encoding="utf-8")
        print("index.html: light / dark switch added next to Refresh")
    else:
        print("index.html: light / dark switch already there")

    j = APPJS.read_text(encoding="utf-8")
    if "sclEditing" in j:
        print("app.js: auto-refresh already adjusted")
    else:
        backup(APPJS)
        APPJS.write_text(patch_appjs(j), encoding="utf-8")
        print("app.js: auto-refresh every 15 s, paused while editing")

    s = SERVER.read_text(encoding="utf-8")
    if FAN_MARK in s:
        print("server.py: fan curves and presets already set up")
    else:
        backup(SERVER)
        new = patch_server_fans(s)
        compile(new, str(SERVER), "exec")
        SERVER.write_text(new, encoding="utf-8")
        print("server.py: fan curve and preset routes added")

    s = SERVER.read_text(encoding="utf-8")
    if AUTH_MARK in s:
        print("server.py: login already set up")
    else:
        backup(SERVER)
        new = patch_server_auth(s)
        compile(new, str(SERVER), "exec")
        SERVER.write_text(new, encoding="utf-8")
        print("server.py: login added")

    s = SERVER.read_text(encoding="utf-8")
    if "_no_passwords" in s:
        print("server.py: hardening already applied")
    else:
        backup(SERVER)
        new = patch_server_hardening(s)
        compile(new, str(SERVER), "exec")
        SERVER.write_text(new, encoding="utf-8")
        print("server.py: atomic owner-only registry writes, request size cap, no passwords in responses")


    if TUNER.exists():
        print(f"tuner script found: {TUNER}")
    else:
        print(f"WARNING: {TUNER} not found. Put asic_tuner.py in the python folder.")
    print("\nDone. Restart the dashboard (Ctrl+C, then python server.py). Sign in with the default login")
    print("(Umbrel) or create one on the first visit; SCLITE_WEBUI_AUTH=off runs without a login.")


def uninstall() -> None:
    for p in (SERVER, INDEX, APPJS):
        b = p.with_name(p.name + ".before-tuner")
        if b.exists():
            shutil.copy2(b, p)
            b.unlink()
            print(f"restored {p.name}")
    for page in (PAGE_DST, LOGIN_DST, THEME_DST, THEMEJS_DST, PROF_DST, *[d for _s, d in EXTRA_PAGES]):
        if page.exists():
            page.unlink()
            print(f"removed static/{page.name}")
    print("tuner_addon.py left in place (harmless); delete it if you like. Restart the dashboard.")


if __name__ == "__main__":
    uninstall() if "--uninstall" in sys.argv else install()
