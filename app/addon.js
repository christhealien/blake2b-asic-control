/* Blake2b ASIC Control: additions to the dashboard's main page (loaded after app.js).
   - Fleet: a quick bar for the ticked miners (preset, auto fan, schedule, probe, back to pool 0, restart)
   - Miner page: detected hardware / preset / schedule line, every chip's accumulated hardware
     errors, the best share (since restart, record, top 10), a note on hand-set clocks, and locks on the fan and clock panels while tuning
   - Settings: any number of demo miners */
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  const S = () => window.sclState || { selected: new Set(), miners: [], profiles: {} };
  function toast(msg, err) {
    const t = $("toast"); if (!t) return;
    t.textContent = msg; t.className = "toast show" + (err ? " err" : "");
    clearTimeout(toast._t); toast._t = setTimeout(() => (t.className = "toast"), 6000);
  }
  async function api(method, path, body) {
    const r = await fetch(path, { method, headers: { "Content-Type": "application/json" },
                                  body: body ? JSON.stringify(body) : undefined });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || j.ok === false) throw new Error(j.error || `HTTP ${r.status}`);
    return j;
  }
  const nameOf = (id) => ((S().miners || []).find(m => m.id === id) || {}).name || id;
  const real = (ids) => ids.filter(id => !String(id).startsWith("demo-"));

  // ------------------------------------------------------------ styles
  const css = document.createElement("style");
  css.textContent = `
    #quickBar .qb-row { display: flex; flex-wrap: wrap; gap: .45rem .9rem; align-items: center; }
    #quickBar .qb-group { display: inline-flex; flex-wrap: wrap; gap: .35rem; align-items: center; padding: .25rem .45rem;
      border: 1px solid var(--line); border-radius: 10px; max-width: 100%; }
    @media (max-width: 640px) { #quickBar select { max-width: 100%; flex: 1 1 9rem; } }
    #quickBar .qb-group > span.lbl { font-size: .75rem; color: var(--muted); margin-right: .15rem; }
    #quickBar select { max-width: 13rem; }
    #quickBar .qb-hint { font-size: .78rem; color: var(--muted); margin: .45rem 0 0; }
    #quickBar button:disabled { opacity: .45; }
    .scl-prow { display: flex; align-items: center; gap: .5rem; width: 100%; min-width: 0; }
    .scl-prow > .muted { font-size: .78rem; }
    .scl-pbtns { display: flex; gap: 3px; flex: 1; min-width: 0; }
    .scl-pbtn { flex: 1 1 0; padding: .35rem .2rem; font-size: .82rem; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .scl-pbtn.active, .scl-pbtn.active:hover { background: var(--text) !important; color: var(--bg) !important; border-color: var(--text) !important; font-weight: 600; }
    .scl-crow { display: flex; gap: .35rem; width: 100%; min-width: 0; flex-wrap: wrap; }
    .scl-crow select { width: 100%; min-width: 0; font-size: .85rem; }
    .scl-cbtn { flex: 1 1 auto; font-size: .82rem; padding: .35rem .5rem; text-align: center; white-space: nowrap;
      display: inline-flex; align-items: center; justify-content: center; border: 1px solid var(--border); border-radius: 10px;
      background: var(--panel2); color: var(--text); text-decoration: none; }
    a.scl-cbtn:hover { border-color: var(--text); }
    .card h3.open { text-decoration: underline; text-decoration-color: var(--border); text-underline-offset: 3px; }
    .scl-lockable { position: relative; }
    .scl-lock { position: absolute; inset: 0; z-index: 5; border-radius: inherit; display: flex; align-items: center;
      justify-content: center; text-align: center; padding: 1rem; background: color-mix(in srgb, var(--panel) 88%, transparent);
      backdrop-filter: blur(1.5px); font-size: .9rem; }
    .scl-lock b { display: block; margin-bottom: .3rem; color: var(--warn); }
    #sclDetailInfo { font-size: .85rem; color: var(--muted); margin: .45rem 0 0; display: flex; flex-wrap: wrap; gap: .3rem .9rem; }
    #sclDetailInfo b { color: var(--text); font-weight: 600; }
    /* chip map: the same look as the Tuner page's */
    .scl-rbar { width: 100%; margin-top: .35rem; }
    .scl-rbar-track { height: 6px; border-radius: 3px; background: var(--panel2); box-shadow: inset 0 0 0 1px var(--border); overflow: hidden; }
    .scl-rbar-fill { height: 100%; width: 0; background: var(--warn); border-radius: 3px; transition: width .25s linear; }
    .scl-rbar.ok .scl-rbar-fill { background: var(--ok); }
    .scl-rbar.bad .scl-rbar-fill { background: var(--bad); }
    .scl-rbar-txt { font-size: .75rem; color: var(--muted); margin-top: .2rem; font-variant-numeric: tabular-nums; }
    .scl-flash { box-shadow: 0 0 0 2px var(--text); transition: box-shadow .4s; }
    #sclDetailGrid { display: flex; flex-direction: column; gap: .75rem; margin-top: .75rem; }
    #sclDetailGrid .panel { margin: 0; }
    #sclDetailGrid .scl-row2 { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: .75rem 1rem; }
    #sclDetailGrid .scl-col { display: flex; flex-direction: column; gap: .75rem; min-width: 0; }
    #sclDetailGrid .scl-col > .panel:last-child { flex: 1 1 auto; }
    #sclDetailGrid .scl-wide .kv { grid-template-columns: repeat(auto-fill, minmax(9.5rem, 1fr)); }
    @media (max-width: 960px) { #sclDetailGrid .scl-row2 { grid-template-columns: minmax(0, 1fr); } }
    #sclChips .board-row { display: grid; grid-template-columns: 6.2rem 1fr; gap: .7rem; align-items: center; margin: .45rem 0; }
    #sclChips .board-label { font-size: .85rem; }
    #sclChips .board-label .muted { display: block; font-size: .72rem; font-family: var(--mono); }
    #sclChips .cells { display: grid; grid-template-columns: repeat(var(--cols, 46), 1fr); gap: 3px; }
    @media (max-width: 1100px) { #sclChips .cells { grid-template-columns: repeat(var(--half, 23), 1fr); } }
    @media (max-width: 640px) { #sclChips .board-row { grid-template-columns: 1fr; gap: .25rem; } #sclChips .board-label .muted { display: inline; margin-left: .5rem; } }
    #sclChips .cell { aspect-ratio: 1; border-radius: 4px; background: var(--cell-empty); box-shadow: inset 0 0 0 1px var(--cell-edge);
      display: flex; align-items: center; justify-content: center; font: 600 .62rem/1 var(--mono); color: var(--text); cursor: default; user-select: none; }
    #sclChips .cell.strong { color: var(--bg); }
    #sclChips .cell.over { background: var(--over); color: #fff; font-size: .7rem; box-shadow: none; }
    #sclChips .cell.ring { box-shadow: 0 0 0 2px var(--text); }
    #sclChips .cell:hover { outline: 2px solid var(--text); outline-offset: 1px; }
    #sclChips .map-legend { display: flex; flex-wrap: wrap; gap: .35rem 1rem; align-items: center; font-size: .78rem; color: var(--muted); margin-top: .7rem; }
    #sclChips .map-legend .ramp { display: inline-flex; gap: 2px; vertical-align: middle; margin: 0 .35rem; }
    #sclChips .map-legend .sw { width: 14px; height: 14px; border-radius: 3px; display: inline-flex; align-items: center; justify-content: center;
      font: 700 .7rem/1 var(--mono); color: #fff; vertical-align: middle; }
    #sclChips .scl-bh { display: grid; grid-template-columns: repeat(auto-fill, minmax(11rem, 1fr)); gap: .4rem 1.2rem; line-height: 1.35; font-size: .8rem; color: var(--muted); margin: .2rem 0 .5rem; }
    #sclChips .scl-how { flex-basis: 100%; margin-top: .2rem; }
    #sclChips .scl-how summary { cursor: pointer; color: var(--text); }
    #sclChips .scl-how p { margin: .4rem 0; max-width: 62rem; line-height: 1.45; color: var(--muted); }
    #sclChips .scl-how b { color: var(--text); }
    #sclChips .scl-bh b { color: var(--text); font-weight: 600; }
    .scl-tip { position: fixed; z-index: 60; pointer-events: none; background: var(--tip-bg, var(--panel)); border: 1px solid var(--border); color: var(--text);
      border-radius: 10px; padding: .45rem .6rem; font-size: .8rem; display: none; box-shadow: 0 8px 28px rgba(0,0,0,.45); }
    .scl-tip b { font-family: var(--mono); }
    #sclChipTable { max-height: 360px; overflow: auto; }
    #sclChipTable th { position: sticky; top: 0; background: var(--panel); cursor: pointer; }
    #sclChipTable td.num, #sclChipTable th.num { text-align: right; font-family: var(--mono); }
    .scl-share-stats { display: flex; flex-wrap: wrap; gap: .6rem 2.2rem; margin-top: .5rem; }
    #sclNotify .nt-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(19rem, 1fr)); gap: .8rem; margin: .4rem 0 .6rem; }
    #sclNotify .nt-box { border: 1px solid var(--line); border-radius: 10px; padding: .6rem .7rem; display: flex; flex-direction: column; gap: .45rem; min-width: 0; }
    #sclNotify .nt-box h3 { margin: 0; font-size: .95rem; display: flex; justify-content: space-between; align-items: center; gap: .5rem; }
    #sclNotify label.nt-f { display: flex; flex-direction: column; gap: .2rem; font-size: .8rem; color: var(--muted); }
    #sclNotify label.nt-f input { width: 100%; font-family: var(--mono); }
    #sclNotify .nt-ev { display: flex; flex-wrap: wrap; align-items: center; gap: .3rem .5rem; font-size: .88rem; padding: .25rem 0; }
    #sclNotify .nt-ev input[type=number] { width: 4.6rem; }
    #sclNotify .nt-miners { display: flex; flex-wrap: wrap; gap: .3rem 1rem; }
    #sclNotify .nt-log { font: .78rem/1.5 var(--mono); color: var(--muted); max-height: 9rem; overflow: auto; }
    #sclNotify .nt-help { font-size: .78rem; color: var(--muted); margin: 0; line-height: 1.45; }
    .scl-bell { display: inline-flex; align-items: center; gap: .3rem; cursor: pointer; }
    .scl-share-stats > div { display: flex; flex-direction: column; gap: .1rem; }
    .scl-share-stats span.muted { font-size: .75rem; }
    .scl-share-stats b { font: 600 1.35rem/1.2 var(--mono); }
    .scl-share-stats small { font-size: .75rem; }
    #sclShareTable { width: 100%; font-size: .82rem; }
    #sclShareTable td.num, #sclShareTable th.num { text-align: right; font-family: var(--mono); }
    .scl-pbtn.scl-idle { flex: 0 0 2.2rem; }
    @media (max-width: 640px) { #viewSettings label:has(> select) { display: block; max-width: 100%; } #viewSettings label > select { max-width: 100%; }
      #sclTz input { min-width: 0 !important; width: 100%; } }
    #sclRejects { margin-top: .9rem; padding-top: .7rem; border-top: 1px solid var(--line); }
    #sclRejects h3 { margin: 0 0 .3rem; font-size: .95rem; }
    #sclRejects .rj-line { font-size: .85rem; line-height: 1.5; }
    #sclRejects .rj-line b.alert { color: var(--bad); }
    #sclRejects svg { display: block; width: 100%; height: auto; margin: .35rem 0 .1rem; }
    #sclRejects svg text { fill: var(--muted); font-size: 11px; font-family: var(--mono); }
    #sclRejects svg .grid { stroke: var(--line); stroke-width: 1; }
    #sclRejects svg .stale { stroke: var(--muted); stroke-width: 1.6; }
    #sclRejects svg .other { fill: var(--bad); }
    #sclRejects svg text.otherlbl { fill: var(--bad); font-weight: 700; }
    #sclRejects svg text.unseen { fill: var(--text); font-weight: 700; font-size: 13px; }
    #sclRejects .rj-key { display: flex; flex-wrap: wrap; gap: .2rem 1rem; font-size: .75rem; color: var(--muted); }
  `;
  document.head.appendChild(css);

  // ------------------------------------------------------------ shared data
  let HW = {};
  async function loadHw() {
    let changed = false;
    try {
      const j = (await api("GET", "/api/hardware/list")).miners || {};
      changed = JSON.stringify(j) !== JSON.stringify(HW);
      for (const [id, h] of Object.entries(j)) {      // a restart just finished: say how it went
        const was = ((HW[id] || {}).restart || {}).state, now = (h.restart || {}).state;
        if (was === "restarting" && now && now !== "restarting") toast(`${nameOf(id)}: ${h.restart.msg}`, now === "failed");
      }
      HW = j; window.sclHw = HW; hwAt = Date.now();
    } catch (e) { /* keep */ }
    if (Object.values(HW).some(h => h.restart && h.restart.state === "restarting")) {   // follow restarts closely
      clearTimeout(loadHw._t); loadHw._t = setTimeout(loadHw, 4000);
    }
    // redraw the cards when presets / tuning state changed (not while a list on a card is open)
    if (changed && window.sclRender && !(document.activeElement && document.activeElement.closest && document.activeElement.closest("#fleetGrid")))
      try { window.sclRender(); } catch (e) { /* ignore */ }
    updateDetailInfo(); applyLocks();
  }

  // ------------------------------------------------------------ Fleet: quick bar
  function buildQuickBar() {
    const view = $("viewFleet"); if (!view) return;
    const old = view.querySelector(".panel"); if (old) old.classList.add("hidden");   // the old fan-kick bar
    const bar = document.createElement("div");
    bar.className = "panel compact"; bar.id = "quickBar";
    bar.innerHTML = `
      <div class="qb-row">
        <span class="qb-group"><button type="button" id="qbAll">All</button><button type="button" id="qbNone">None</button>
          <b id="qbCount" style="font-size:.85rem;min-width:5.5rem;text-align:center">0 ticked</b></span>
        <button type="button" id="qbAddMiner" style="margin-left:auto" title="Add a miner (Settings → Add miner)">+ Add miner</button>
        <span class="qb-group" title="Put a preset (clock, voltage, PV and its fan curve) on the ticked miners">
          <span class="lbl">Preset</span><select id="qbPreset"></select><button type="button" class="primary" id="qbApply">Apply</button></span>
        <span class="qb-group" title="Turn the dashboard's automatic fan control on (with this curve) or off">
          <span class="lbl">Auto fan</span><select id="qbCurve"></select><button type="button" id="qbFanOn">On</button><button type="button" id="qbFanOff">Off</button></span>
        <span class="qb-group" title="Start or stop each miner's own schedule (made on the Schedule page)">
          <span class="lbl">Schedule</span><button type="button" id="qbSchOn">Resume</button><button type="button" id="qbSchOff">Pause</button></span>
        <span class="qb-group">
          <button type="button" id="qbProbe" title="Detect model, firmware, boards, chips and fans again">Probe</button>
          <button type="button" id="qbFailback" title="Switch back to pool 0 (your first pool) if the miner is stuck on a backup pool">Back to pool 0</button>
          <button type="button" class="danger" id="qbRestart" title="Soft-restart the miner software (hashing pauses about a minute)">Restart</button></span>
      </div>
      <p class="qb-hint">Tick miners with the box on their card, then use a button here. Miners being tuned are skipped for
        presets, fan and restart (the tuner is in control of them).</p>`;
    view.insertBefore(bar, view.firstChild);
    $("qbAddMiner").onclick = () => goAddMiner();
    $("qbAll").onclick = () => $("btnSelectAll") && $("btnSelectAll").click();
    $("qbNone").onclick = () => $("btnSelectNone") && $("btnSelectNone").click();
    $("qbApply").onclick = applyPresetTicked;
    $("qbFanOn").onclick = () => fanTicked(true);
    $("qbFanOff").onclick = () => fanTicked(false);
    $("qbSchOn").onclick = () => scheduleTicked(false);
    $("qbSchOff").onclick = () => scheduleTicked(true);
    $("qbProbe").onclick = probeTicked;
    $("qbFailback").onclick = () => fleetAction("failback", { preferred: 0, soft_restart: true },
      "Switch the ticked miners back to pool 0? Each one restarts its miner software briefly.");
    $("qbRestart").onclick = () => fleetAction("restart", {}, "Restart the miner software on the ticked miners? Hashing pauses for about a minute.");
  }
  const ticked = () => [...(S().selected || [])];
  function refreshBar() {
    if (!$("quickBar")) return;
    const ids = ticked(), n = ids.length;
    $("qbCount").textContent = n ? `${n} ticked` : "0 ticked";
    $("quickBar").querySelectorAll(".qb-group:not(:first-child) button").forEach(b => (b.disabled = !n));
    // presets: the usual four plus any others the ticked miners have
    const labels = { high: "High", middle: "Middle", low: "Low", lowest: "Lowest power" };
    ids.forEach(id => Object.entries((HW[id] || {}).presets || {}).forEach(([k, p]) => { labels[k] = labels[k] || p.label; }));
    const ps = $("qbPreset"), cur = ps.value;
    const html = Object.entries(labels).map(([k, l]) => `<option value="${esc(k)}">${esc(l)}</option>`).join("");
    if (ps._h !== html) { ps.innerHTML = html; ps._h = html; if (cur) ps.value = cur; }
    const cs = $("qbCurve"), curC = cs.value, profs = S().profiles || {};
    const chtml = Object.keys(profs).map(k => `<option value="${esc(k)}">${esc(profs[k].label || k)}</option>`).join("");
    if (cs._h !== chtml) { cs.innerHTML = chtml; cs._h = chtml; cs.value = curC || (profs["curve-60"] ? "curve-60" : Object.keys(profs)[0]); }
  }
  async function eachTicked(fn, what) {
    const ids = real(ticked());
    if (!ids.length) { toast("Tick at least one (real) miner first", true); return; }
    const ok = [], bad = [];
    for (const id of ids) {
      try { await fn(id); ok.push(nameOf(id)); }
      catch (e) { bad.push(`${nameOf(id)}: ${e.message}`); }
    }
    toast((ok.length ? `${what}: ${ok.join(", ")}` : "") + (bad.length ? `${ok.length ? " · " : ""}skipped ${bad.join(" · ")}` : ""), bad.length && !ok.length);
    loadHw(); if (window.sclRefresh) window.sclRefresh();
  }
  function applyPresetTicked() {
    const key = $("qbPreset").value, label = $("qbPreset").selectedOptions[0].textContent;
    const ids = real(ticked());
    if (!ids.length) return toast("Tick at least one miner first", true);
    if (!confirm(`Put the ${label} preset on ${ids.map(nameOf).join(", ")}?\n\nEach miner uses its own ${label} (its own clock, voltage and fan curve). Miners without a ${label} preset are skipped.`)) return;
    eachTicked(id => api("POST", "/api/presets/apply", { miner_id: id, key }), `${label} on`);
  }
  function fanTicked(on) {
    const curve = $("qbCurve").value;
    eachTicked(id => {
      if ((HW[id] || {}).tuning) throw new Error("being tuned");
      return api("POST", `/api/miners/${encodeURIComponent(id)}/fan_control`, on ? { enabled: true, profile: curve } : { enabled: false });
    }, on ? "Auto fan on" : "Auto fan off");
  }
  function scheduleTicked(pause) {
    eachTicked(id => api("POST", "/api/schedule/pause", { miner_id: id, paused: pause }), pause ? "Schedule paused" : "Schedule resumed");
  }
  function probeTicked() {
    toast("Probing… (up to about 30 s per miner)");
    eachTicked(id => api("POST", "/api/hardware/probe", { miner_id: id }).then(j => { if (!j.hardware.ok) throw new Error(j.line); }), "Probed");
  }
  // Restart runs in the background on the server; progress shows on the card ("restarting…")
  // and a message pops up when each miner is hashing again.
  async function restartIds(ids) {
    ids = real(ids);
    if (!ids.length) return;
    const j = await api("POST", "/api/quick/restart", { ids });
    const sk = Object.entries(j.skipped || {}).map(([k, v]) => `${nameOf(k)} (${v})`);
    toast(`Restarting ${j.started.map(nameOf).join(", ")}: hashing pauses for about a minute` + (sk.length ? ` · skipped ${sk.join(", ")}` : ""));
    loadHw();
  }
  window.sclRestart = restartIds;
  async function fleetAction(action, extra, question) {
    const ids = real(ticked());
    if (!ids.length) return toast("Tick at least one miner first", true);
    if (question && !confirm(question)) return;
    try {
      if (action === "restart") await restartIds(ids);
      else {   // failback: reorder the pools straight away, then restart in the background
        await api("POST", "/api/fleet/action", { action, ids, ...extra, soft_restart: false });
        await restartIds(ids);
      }
    } catch (e) { toast(e.message, true); }
    if (window.sclRefresh) window.sclRefresh();
  }

  // ------------------------------------------------------------ Fleet cards: preset buttons
  const ORDER = ["high", "middle", "low", "lowest"];
  const SHORT = { high: "High", middle: "Mid", low: "Low", lowest: "Lowest" };
  window.sclPresetRow = (id) => {
    if (String(id).startsWith("demo-")) return `<span class="muted" style="font-size:.8rem">Presets: (demo miner)</span>`;
    const h = HW[id] || {}, ps = h.presets || {};
    const keys = [...ORDER.filter(k => ps[k]), ...Object.keys(ps).filter(k => !ORDER.includes(k) && k !== "idle"), ...(ps.idle ? ["idle"] : [])];
    if (!keys.filter(k => k !== "idle").length && !ps.idle) return `<span class="muted" style="font-size:.8rem">No presets yet: <a href="/tuner.html?miner=${encodeURIComponent(id)}">run the tuner</a></span>`;
    const btns = keys.map(k => {
      const p = ps[k], on = h.active === k;
      return `<button type="button" class="scl-pbtn ${on ? "active" : ""}${p.idle ? " scl-idle" : ""}" data-preset="${esc(k)}" data-id="${esc(id)}" ${h.tuning ? "disabled" : ""}
        title="${esc(p.idle ? `Idle: the firmware's own Idle mode, the hash boards stop (no hashing). Any other preset wakes it.${on ? " — on the miner now" : ""}` : `${p.label}: ${p.mhz} MHz · ${p.mv} mV · PV ${p.pv}${p.tuned ? " (tuned)" : " (hand-set)"}${on ? " — on the miner now" : ""}`)}">${p.idle ? '<span aria-label="Idle">☾</span>' : esc(SHORT[k] || p.label)}</button>`;
    }).join("");
    return `<div class="scl-prow"><span class="muted">Preset</span><div class="scl-pbtns">${btns}</div></div>`;
  };
  // restart progress: counts up against how long this miner's restarts usually take (the server keeps its last few
  // restart times; 75 s until it has one)
  let hwAt = Date.now();
  const RPHASE = { send: "sending the restart", wait: "restarting", down: "restarting (not answering yet)",
                   up: "back up, waiting for hashing to start", hashing: "hashing again" };
  const rbarState = (id) => {
    const r = (HW[id] || {}).restart, since = (Date.now() - hwAt) / 1000;
    const show = !!r && (r.state === "restarting" || (r.ago_s != null && r.ago_s + since < 12));
    if (!show) return { show: false, busy: false };
    if (r.state !== "restarting") return { show: true, busy: false, pct: 100, cls: r.state === "done" ? "ok" : "bad", text: r.msg || r.state };
    const exp = r.expect_s || 75, t = (r.elapsed_s || 0) + since;
    // linear up to 95% at the usual time, then creeps on so it never sits still or reaches the end early
    const pct = t <= exp ? 95 * t / exp : 95 + 4 * (1 - Math.exp(-(t - exp) / exp));
    return { show: true, busy: true, pct, cls: "",
             text: `${RPHASE[r.phase] || "restarting"}… ${Math.round(t)} s / ~${Math.round(exp)} s` + (t > exp * 1.3 ? " (slower than usual)" : "") };
  };
  function paintRbar(el) {
    const st = rbarState(el.dataset.id);
    const btn = el.parentNode && el.parentNode.querySelector(`.scl-cbtn[data-act="restart"]`);
    if (el.hidden !== !st.show) el.hidden = !st.show;
    if (!st.show) return;
    if (btn && btn.disabled !== st.busy && !btn.dataset.locked) btn.disabled = st.busy;
    const fill = el.querySelector(".scl-rbar-fill"), txt = el.querySelector(".scl-rbar-txt");
    const cls = "scl-rbar" + (st.cls ? " " + st.cls : "");
    if (el.className !== cls) el.className = cls;
    fill.style.width = st.pct.toFixed(1) + "%";
    if (txt.textContent !== st.text) txt.textContent = st.text;
  }
  // card buttons: switch pool, restart, schedule on/off, the miner's own web page
  const hostOf = (url) => String(url || "").replace(/^stratum\+(tcp|ssl):\/\//, "").replace(/\/.*$/, "");
  window.sclCardActions = (m) => {
    const id = m.id, sn = m.snapshot || {}, pools = sn.pools || [], wp = sn.working_pool || {};
    if (String(id).startsWith("demo-")) return `<span class="muted" style="font-size:.8rem">(demo miner: no actions)</span>`;
    const h = HW[id] || {}, tuning = !!h.tuning, rb = rbarState(id);
    const opts = pools.length ? pools.map((p, i) => {
      const on = String(p.id) === String(wp.id);
      return `<option value="${i}" ${on ? "selected" : ""} ${p.status !== "Alive" ? "" : ""}>P${esc(p.id)} · ${esc(hostOf(p.url))}${on ? " (mining)" : p.status !== "Alive" ? " (down)" : ""}</option>`;
    }).join("") : `<option>no pools reported</option>`;
    const sch = h.schedule ? (h.schedule_paused
        ? `<button type="button" class="scl-cbtn" data-act="sch-on" data-id="${esc(id)}" title="Resume this miner's schedule">▶ Schedule</button>`
        : `<button type="button" class="scl-cbtn" data-act="sch-off" data-id="${esc(id)}" title="Pause this miner's schedule">⏸ Schedule</button>`) : "";
    return `<div class="scl-crow">
        <select class="scl-pool" data-id="${esc(id)}" title="Switch pool: the one you pick becomes the first pool and the miner restarts" ${pools.length ? "" : "disabled"}>${opts}</select>
      </div>
      <div class="scl-crow">
        <button type="button" class="scl-cbtn danger" data-act="restart" data-id="${esc(id)}" ${tuning ? "disabled data-locked='1' title='Locked while tuning'" : `${rb.busy ? "disabled " : ""}title='Soft-restart the miner software (hashing pauses about a minute)'`}>Restart</button>
        ${sch}
        <a class="scl-cbtn" href="http://${esc(m.ip)}/" target="_blank" rel="noopener" title="The miner's own web page">Miner page ↗</a>
      </div>
      <div class="${rb.cls ? "scl-rbar " + rb.cls : "scl-rbar"}" data-id="${esc(id)}" ${rb.show ? "" : "hidden"}><div class="scl-rbar-track"><div class="scl-rbar-fill" style="width:${rb.show ? rb.pct.toFixed(1) : 0}%"></div></div><div class="scl-rbar-txt">${rb.show ? esc(rb.text) : ""}</div></div>`;
  };
  // restart progress bar on the cards: kept moving 4x a second by paintRbar. The card itself is drawn with the
  // bar's current state (sclCardActions), so a Fleet refresh, which redraws every card, never shows a card without
  // its bar for a frame (it used to blink).
  setInterval(() => document.querySelectorAll(".scl-rbar").forEach(paintRbar), 250);
  document.addEventListener("change", async (e) => {
    const sel = e.target.closest && e.target.closest(".scl-pool"); if (!sel) return;
    const id = sel.dataset.id, m = (S().miners || []).find(x => x.id === id) || {}, p = ((m.snapshot || {}).pools || [])[Number(sel.value)];
    if (!p) return;
    sel.blur();
    if (!confirm(`Switch ${nameOf(id)} to P${p.id} (${hostOf(p.url)})?\n\nIt becomes the miner's first pool and the miner restarts (hashing pauses for about a minute).`)) {
      if (window.sclRender) window.sclRender(); return;
    }
    try {
      await api("POST", `/api/miners/${encodeURIComponent(id)}/action/make_preferred`, { url: p.url, soft_restart: false });
      await restartIds([id]);
    } catch (err) { toast(err.message, true); }
    if (window.sclRefresh) window.sclRefresh();
  });
  document.addEventListener("click", async (e) => {
    const b = e.target.closest && e.target.closest(".scl-cbtn[data-act]"); if (!b) return;
    const id = b.dataset.id, act = b.dataset.act;
    try {
      if (act === "restart") {
        if (!confirm(`Restart the miner software on ${nameOf(id)}? Hashing pauses for about a minute.`)) return;
        b.disabled = true;
        await restartIds([id]);
      } else {
        await api("POST", "/api/schedule/pause", { miner_id: id, paused: act === "sch-off" });
        toast(act === "sch-off" ? `Schedule paused on ${nameOf(id)}` : `Schedule resumed on ${nameOf(id)}`);
      }
    } catch (err) { toast(err.message, true); }
    await loadHw(); if (window.sclRefresh) window.sclRefresh();
  });
  // power estimate (same model as the server: chips scale with clock x PV^2, ~7% fixed; 12.5 MHz clock steps)
  const estW = (pm, mhz, pv) => {
    if (!pm || !mhz || !pv) return null;
    const eff = Math.floor(mhz / 12.5) * 12.5;
    const w = pm.rated_w * (pm.cal || 1) * (0.07 + 0.93 * (eff / pm.mhz) * Math.pow(pv / pm.pv, 2));
    const ths = pm.rated_ths ? pm.rated_ths * eff / pm.mhz : null;
    return { w: Math.round(w), amps: (w / (pm.mains_v || 110)).toFixed(1), wth: ths ? Math.round(w / ths) : null, eff, mains: pm.mains_v || 110 };
  };
  window.sclEstW = estW;
  const planOf = (m) => { const p = ((m.snapshot || {}).plan) || {}; return { mhz: p.mhz, mv: p.mv, pv: p.pv }; };
  window.sclCardInfo = (m) => {
    if (String(m.id).startsWith("demo-")) return "";
    const h = HW[m.id] || {}, pl = planOf(m), e = h.active === "idle" ? null : estW(h.power_model, pl.mhz, pl.pv), hl = h.health;
    const pw = h.active === "idle" ? "idle: not hashing" : e ? `≈ ${e.w} W · ${e.amps} A` + (e.wth ? ` · ${e.wth} J/TH` : "") : "—";
    const tipP = e ? `Estimate for ${pl.mhz} MHz${e.eff !== pl.mhz ? ` (runs ${e.eff})` : ""} · PV ${pl.pv}, at ${e.mains} V${h.power_model && h.power_model.cal_info ? " · calibrated to your wall reading" : " · not calibrated (Miner page)"}` : "Needs the miner's rated power and stock setting (probe it)";
    const ch = !hl ? `<span class="muted">checking…</span>` : hl.weak.length ? `<span style="color:var(--bad);font-weight:600">${hl.weak.length} weak</span> <span class="muted">${esc(hl.weak.slice(0, 2).join(", "))}</span>`
      : hl.watch.length ? `${hl.watch.length} to watch` : "all healthy";
    return `<div title="${esc(tipP)}"><div class="label">Power (estimate)</div><div class="value" style="font-size:.85rem">${pw}</div></div>
      <div title="Chip health from the miner's log and chip data (Miner page for details)"><div class="label">Chips</div><div class="value" style="font-size:.85rem">${ch}${hl && hl.max_chip_c != null ? ` <span class="muted">· max ${Math.round(hl.max_chip_c)} °C</span>` : ""}</div></div>`;
  };
  // Fleet card: the last 24 hours of hashrate (hashrate_addon.py), where the Working pool tile was
  const fmtTH = (v) => v >= 1 ? `${v.toFixed(2)} TH/s` : `${(v * 1000).toFixed(v * 1000 >= 100 ? 0 : 1)} GH/s`;
  window.sclHashGraph = (m, opt = {}) => {
    if (String(m.id).startsWith("demo-")) return "";
    const g = (HW[m.id] || {}).hashrate;
    const head = (extra) => opt.big
      ? `<div class="row" style="justify-content:space-between"><h2 style="margin:0">Hashrate, 24 h</h2><span class="muted" style="font-size:.85rem">${extra}</span></div>`
      : `<div class="label" style="display:flex;justify-content:space-between;gap:.5rem"><span>Hashrate, 24 h</span><span style="text-transform:none;letter-spacing:0">${extra}</span></div>`;
    if (!g || !g.points || g.points.length < 2)
      return `<div style="grid-column:1/-1">${head("")}<div class="value muted" style="font-size:.8rem">collecting… (a point every 5 min; the graph fills in over 24 h)</div></div>`;
    const W = opt.W || 360, H = opt.H || 76, T = 17, B = 4, now = Date.now() / 1000, span = 86400, step = g.step || 300;
    const vals = g.points.map(p => p[1]);
    let lo = Math.min(...vals), hi = Math.max(...vals);
    const pad = Math.max((hi - lo) * 0.15, hi * 0.02, 0.01); lo = Math.max(0, lo - pad); hi = hi + pad;
    // the scale on both sides: round levels (1, 2, 2.5 or 5 x 10^n apart), in TH/s, or GH/s for a slower miner
    const gh = Math.max(...vals) < 1, k = gh ? 1000 : 1, FS = opt.big ? 12 : 13, MAXT = opt.big ? 5 : 3;
    const nice = (r) => { const e = Math.pow(10, Math.floor(Math.log10(r)));      // the round step nearest r
      return [1, 2, 2.5, 5, 10].map(f => f * e).reduce((a, c) => Math.abs(Math.log(c / r)) < Math.abs(Math.log(a / r)) ? c : a); };
    let tstep = nice(((hi - lo) * k) / (MAXT - 1)) / k, ticks = [];
    for (let i = 0; i < 6; i++) {                   // no more levels than fit (3 on a card, 5 on the Miner page)
      ticks = []; for (let v = Math.ceil(lo / tstep - 1e-9) * tstep; v <= hi + 1e-9; v += tstep) ticks.push(v);
      if (ticks.length <= MAXT) break;
      tstep = nice(tstep * k * 2) / k;
    }
    const sk = tstep * k; let dec = 0;              // as few decimals as the step needs: 0.5 -> "4.5", 0.25 -> "4.25", 25 -> "525"
    while (dec < 3 && Math.abs(Math.round(sk * 10 ** dec) - sk * 10 ** dec) > 1e-6) dec++;
    const tl = (v) => (v * k).toFixed(dec);
    const LW = Math.max(...ticks.map(v => tl(v).length), 2) * FS * 0.62 + 6;     // room for the longest label
    const L = LW, R = LW, PW = W - L - R;
    const xOf = (t) => (L + PW * (1 - (now - t) / span));
    const yOf = (v) => T + (H - T - B) * (1 - (v - lo) / (hi - lo));
    const scale = ticks.map(v => { const y = yOf(v).toFixed(1);
      return `<line x1="${L}" x2="${W - R}" y1="${y}" y2="${y}" stroke="var(--border)" stroke-width="1" stroke-dasharray="1 3" opacity=".8"/>`
        + `<text x="${L - 4}" y="${y}" dy=".35em" text-anchor="end" font-size="${FS}" fill="var(--muted)" font-family="var(--mono)">${tl(v)}</text>`
        + `<text x="${W - R + 4}" y="${y}" dy=".35em" text-anchor="start" font-size="${FS}" fill="var(--muted)" font-family="var(--mono)">${tl(v)}</text>`; }).join("");
    // a gap of more than 3 points (no readings: miner off or the app not running) breaks the line
    let d = "", area = "", seg = [];
    const flush = () => {
      if (seg.length > 1) {
        const pl = seg.map((p, i) => `${i ? "L" : "M"}${xOf(p[0]).toFixed(1)} ${yOf(p[1]).toFixed(1)}`).join("");
        d += pl; area += pl + `L${xOf(seg[seg.length - 1][0]).toFixed(1)} ${H - B}L${xOf(seg[0][0]).toFixed(1)} ${H - B}Z`;
      } else if (seg.length === 1) d += `M${(xOf(seg[0][0]) - 1).toFixed(1)} ${yOf(seg[0][1]).toFixed(1)}h2`;
      seg = [];
    };
    g.points.forEach((p, i) => { if (i && p[0] - g.points[i - 1][0] > 3 * step) flush(); seg.push(p); }); flush();
    let grid = "";
    for (let h = 6; h < 24; h += 6) { const x = xOf(now - h * 3600).toFixed(1); grid += `<line x1="${x}" x2="${x}" y1="${T}" y2="${H - B}" stroke="var(--border)" stroke-width="1"/>`; }
    const MK = { R: "var(--muted)", P: "var(--muted)", T: "var(--muted)", X: "var(--bad)" };
    const when = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    // marks closer than a letter's width share one label (letters side by side, every event in its hover)
    const groups = [];
    (g.markers || []).filter(k => k[0] > now - span).sort((a, b) => a[0] - b[0]).forEach(([t, k, label]) => {
      const x = Math.max(L + 4, Math.min(W - R - 4, xOf(t))), last = groups[groups.length - 1];
      if (last && x - last.x < 12) { last.items.push([t, k, label]); if (!last.ks.includes(k)) last.ks.push(k); }
      else groups.push({ x, items: [[t, k, label]], ks: [k] });
    });
    const marks = groups.map(gr => {
      const x = gr.x.toFixed(1), col = gr.ks.includes("X") ? MK.X : "var(--muted)";
      const tipM = gr.items.map(([t, k, label]) => `${when(t)} · ${k}: ${label}`).join("\n");
      const lbl = gr.ks.join("") + (gr.items.length > gr.ks.length ? "+" : "");
      const anchor = "middle";
      return `<g><title>${esc(tipM)}</title><line x1="${x}" x2="${x}" y1="${T - 2}" y2="${H - B}" stroke="${col}" stroke-width="1" stroke-dasharray="2 3"/>
        <text x="${x}" y="${T - 4}" text-anchor="${anchor}" font-size="13" font-weight="700" fill="${col}" font-family="var(--mono)">${esc(lbl)}</text>
        <rect x="${(gr.x - 7).toFixed(1)}" y="0" width="14" height="${H}" fill="transparent"/></g>`;
    }).join("");
    const last = g.points[g.points.length - 1];
    const span0 = (now - g.from) / 3600;
    const tip = `Every 5 minutes from the miner's 20-second hashrate. Low ${fmtTH(Math.min(...vals))} · high ${fmtTH(Math.max(...vals))}${span0 < 23 ? ` · ${span0 < 1 ? "under an hour" : Math.floor(span0) + " h"} so far` : ""}. Marks: R restarted · P setting changed · T tuning started · X a share rejected, not stale. Hover a mark for when and what.`;
    const avgSpan = span0 >= 23 ? "24 h" : span0 >= 1 ? `${Math.floor(span0)} h` : `${Math.max(1, Math.round(span0 * 60))} min`;
    return `<div style="grid-column:1/-1" title="${esc(tip)}">${head(`avg ${avgSpan} <b style="color:var(--text)">${fmtTH(g.avg)}</b>`)}
      <svg viewBox="0 0 ${W} ${H}" style="display:block;width:100%;height:auto;margin-top:.2rem" role="img" aria-label="hashrate over the last 24 hours">
        ${scale}${grid}<path d="${area}" fill="var(--text)" opacity=".07"/><path d="${d}" fill="none" stroke="var(--text)" stroke-width="1.5" stroke-linejoin="round"/>
        <circle cx="${xOf(last[0]).toFixed(1)}" cy="${yOf(last[1]).toFixed(1)}" r="2.2" fill="var(--text)"/>${marks}</svg>
      <div style="display:flex;justify-content:space-between;font-size:.68rem;color:var(--muted);font-family:var(--mono);padding:0 ${(100 * R / W).toFixed(2)}% 0 ${(100 * L / W).toFixed(2)}%">${opt.big
        ? "<span>24 h ago</span><span>18 h</span><span>12 h</span><span>6 h</span><span>now</span>" : "<span>24 h ago</span><span>12 h</span><span>now</span>"}</div></div>`;
  };
  // Miner page, Live panel: the value is text (the dashboard escapes it); the "avg since restart" line under it
  // is added to the tile after each render
  window.sclHashText = (id, avgText) => {
    const g = (HW[id] || {}).hashrate;
    window._sclLiveAvg = avgText;
    return g && g.now != null ? fmtTH(g.now) : avgText;
  };
  const liveAvgLine = () => {
    const kv = $("detailKv"); if (!kv) return;
    const tile = kv.firstElementChild; if (!tile || !/hashrate/i.test((tile.querySelector(".label") || {}).textContent || "")) return;
    const id = detailId(), g = (HW[id] || {}).hashrate, fresh = g && g.now != null;
    let sub = tile.querySelector(".scl-live-avg");
    const txt = fresh ? `avg since restart ${window._sclLiveAvg || "—"}` : "average since restart";
    if (!sub) { sub = document.createElement("div"); sub.className = "muted scl-live-avg"; sub.style.cssText = "font-size:.7rem;margin-top:.15rem"; tile.appendChild(sub); }
    if (sub.textContent !== txt) sub.textContent = txt;
    tile.title = fresh ? "The miner's 20-second hashrate at the last reading (every 30 s); under it, its own average since its mining software last started" : "The miner's own average since its mining software last started";
  };
  setTimeout(function watchLive() {      // after this script has run (detailId is defined further down)
    const kv = $("detailKv");
    if (!kv) return setTimeout(watchLive, 500);
    new MutationObserver(liveAvgLine).observe(kv, { childList: true });
    liveAvgLine();
  }, 0);
  window.sclHashTile = (m, avgText) => {
    const g = (HW[m.id] || {}).hashrate;
    // one line, never wrapping: a number getting longer would otherwise make the card (and the page) jump
    const one = "font-size:.68rem;margin-top:.15rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis";
    const sub = `<div class="muted" style="${one}" title="avg since restart ${esc(avgText)}: the miner's own average since its mining software last started (it includes any tuning steps or other clocks since then)">avg since restart ${esc(avgText)}</div>`;
    if (!g || g.now == null) return `<div><div class="label">Hashrate</div><div class="value">${esc(avgText)}</div><div class="muted" style="${one}">average since restart</div></div>`;
    return `<div title="The miner's 20-second hashrate at the last reading (every 30 s)"><div class="label">Hashrate</div><div class="value">${fmtTH(g.now)}</div>${sub}</div>`;
  };
  window.sclPill = (id) => {
    if (String(id).startsWith("demo-")) return `<span class="pill">demo</span>`;
    const h = HW[id] || {};
    if (h.tuning) return `<span class="pill warn" title="A tuning run is going on this miner">tuning</span>`;
    if (h.restart && h.restart.state === "restarting") return `<span class="pill warn" title="${esc(h.restart.msg || "")}">restarting…</span>`;
    const p = (h.presets || {})[h.active];
    if (p && p.idle) return `<span class="pill" style="color:var(--text);border-color:var(--text);border-style:dashed" title="The firmware's own Idle mode: not hashing. Any other preset wakes it.">Idle</span>`;
    if (p) return `<span class="pill" style="color:var(--text);border-color:var(--text)" title="${esc(`${p.label}: ${p.mhz} MHz · ${p.mv} mV · PV ${p.pv}${p.tuned ? " (tuned)" : " (hand-set)"}`)}">${esc(p.label)}</span>`;
    return `<span class="pill" title="Not on one of its presets (its own or a hand-set clock)">no preset</span>`;
  };
  document.addEventListener("click", async (e) => {
    const b = e.target.closest && e.target.closest(".scl-pbtn"); if (!b) return;
    const id = b.dataset.id, key = b.dataset.preset, p = ((HW[id] || {}).presets || {})[key]; if (!p) return;
    if (!confirm(p.idle ? `Put ${nameOf(id)} in Idle?\n\nIt stops hashing (the firmware's own Idle mode) until you pick another preset, or its schedule does.`
                        : `Put ${p.label} on ${nameOf(id)}?\n${p.mhz} MHz · ${p.mv} mV · PV ${p.pv} and its fan curve`)) return;
    b.disabled = true; b.blur();
    try { await api("POST", "/api/presets/apply", { miner_id: id, key }); toast(p.idle ? `${nameOf(id)} is going idle` : `${p.label} is on ${nameOf(id)}`); }
    catch (err) { toast(err.message, true); }
    b.disabled = false;
    await loadHw(); if (window.sclRefresh) window.sclRefresh();
  });

  // ------------------------------------------------------------ Miner page
  const detailId = () => (window.sclDetail ? window.sclDetail() : null);
  const detailOpen = () => $("viewDetail") && !$("viewDetail").classList.contains("hidden");
  function panelOf(id) { const el = $(id); return el ? el.closest(".panel") : null; }

  // Miner page layout, top to bottom: Live (full width), Chips (full width, the Tuner's squares), then
  // Boards / Pools / Add pool beside Best share, then Fan beside Clock / voltage. Each row's two columns
  // end level (the last box in a column stretches), so there are no gaps. Remove sits in the title row.
  function layoutDetail(split, chips, shares, boards, clock) {
    const live = panelOf("detailKv"), fan = panelOf("detailFanAuto");
    const rm = $("btnRemoveMiner"), rmPanel = rm && rm.closest(".panel");
    const known = new Set([live, fan, clock, boards, shares, panelOf("btnFailback"), panelOf("poolUrl"), rmPanel]);
    const extra = [...split.querySelectorAll(".panel")].filter(x => !known.has(x));   // anything the dashboard adds later
    const row = (left, right) => {
      const r = document.createElement("div"); r.className = "scl-row2";
      for (const list of [left, right]) {
        const c = document.createElement("div"); c.className = "scl-col";
        list.filter(Boolean).forEach(x => c.appendChild(x)); r.appendChild(c);
      }
      return r;
    };
    const wrap = document.createElement("div"); wrap.id = "sclDetailGrid";
    if (live) { live.classList.add("scl-wide"); wrap.appendChild(live); }
    wrap.appendChild(chips);
    wrap.appendChild(row([boards, panelOf("btnFailback"), panelOf("poolUrl"), ...extra], [shares]));
    wrap.appendChild(row([fan], [clock]));
    split.replaceWith(wrap);
    if (rm) {
      const tr = $("detailTitle").closest(".row");
      rm.style.cssText = "margin-left:auto;font-size:.8rem;padding:.3rem .65rem";
      rm.title = "Remove this miner from the app (the miner itself is not changed)"; tr.appendChild(rm);
      if (rmPanel) rmPanel.remove();
    }
  }

  function buildDetail() {
    const title = $("detailTitle"); if (!title) return;
    const fb = $("btnFailback");      // the dashboard calls it "Failback": say what it does
    if (fb) { fb.textContent = "Switch to pool"; fb.title = "Make the pool number next to it this miner's first pool and restart the miner software (pool 0 = back to your main pool)"; }
    const row = title.closest(".row");
    const info = document.createElement("div"); info.id = "sclDetailInfo";
    row.parentNode.insertBefore(info, row.nextSibling);
    // clock note
    const clock = panelOf("planMhz");
    if (clock) {
      const note = document.createElement("p");
      note.className = "muted"; note.style.cssText = "margin:0 0 .5rem;font-size:.85rem";
      note.innerHTML = `While a preset or a running schedule is in charge of this miner, its clock and fans are
        <b>locked</b> here so nothing fights it. <i>Take manual control</i> unlocks them: it turns the preset off and pauses
        the schedule. To hand control back, apply a preset (Fleet card or <a href="/profiles.html">Profiles</a>) and resume
        the schedule (Fleet card or <a href="/schedule.html">Schedule</a>).
        <br><b>PV</b> is the voltage the hash boards really get (the miner reads back about PV − 20 mV) and drives power and
        heat; <b>mV</b> is passed alongside it, so keep the stock gap (PV = mV + 300 on an SC Lite). Clocks run in 12.5 MHz steps.`;
      const h = clock.querySelector("h2"); h.parentNode.insertBefore(note, h.nextSibling);
    }
    // chips and best share panels (placed by layoutDetail)
    const boards = panelOf("detailBoards");
    if (boards) {
      const p = document.createElement("div");
      p.className = "panel compact"; p.id = "sclChips";
      p.innerHTML = `<div class="row" style="justify-content:space-between"><h2 style="margin:0">Chips</h2>
          <div class="row" style="gap:.5rem"><label class="muted" style="font-size:.85rem">Shade by
            <select id="sclChipMetric"><option value="bad">Bad results (%)</option><option value="hw">HW errors</option><option value="perf">Perf</option></select></label>
            <button type="button" id="sclChipsRefresh">Refresh</button></div></div>
        <p class="muted" id="sclChipsCap" style="font-size:.85rem;margin:.5rem 0 .2rem">Loading…</p>
        <div id="sclBoardHealth" class="scl-bh"></div>
        <div id="sclChipMap"></div><div class="map-legend" id="sclChipLegend"></div>
        <details style="margin-top:.7rem"><summary class="muted" id="sclChipSum" style="font-size:.85rem">Full chip list</summary>
          <div id="sclChipTable"><table><thead><tr><th data-k="b">Board</th><th class="num" data-k="c">Chip</th>
            <th class="num" data-k="e">HW errors</th><th class="num" data-k="r">per hour</th><th class="num" data-k="bp">Bad results</th>
            <th class="num" data-k="pr">Perf</th><th data-k="h">Health</th></tr></thead>
            <tbody id="sclChipBody"></tbody></table></div></details>`;
      const split = boards.closest(".split");
      const bs = document.createElement("div");
      bs.className = "panel compact"; bs.id = "sclShares";
      bs.innerHTML = `<div class="row" style="justify-content:space-between"><h2 style="margin:0">Best share</h2>
          <button type="button" id="sclSharesReset" title="Clear the record and history for this miner">Reset record</button></div>
        <div class="scl-share-stats" id="sclShareStats"></div>
        <p class="muted" style="font-size:.78rem;margin:.5rem 0 .3rem">The miner's best share starts again from 0 every time it restarts.
          The app logs every new best the moment it's found, with the setting it was on, so the record survives restarts
          (it keeps the 20 biggest and shows the top 10). Mostly luck, so it says little about a setting.
          Shown the way Settings → Best share numbers says: by default on the same scale as DATUM and mempool (the miner
          counts in units of 2<sup>32</sup> hashes), so you can compare it with the network difficulty.</p>
        <table id="sclShareTable"><thead><tr><th class="num">#</th><th class="num">Best share</th><th>On</th><th style="text-align:right">Found</th></tr></thead>
          <tbody id="sclShareBody"></tbody></table>
        <div id="sclRejects"><h3>Rejected shares</h3><div id="sclRejBody" class="rj-line muted">—</div>
          <div id="sclRejStrip"></div>
          <div class="rj-key"><span><svg width="10" height="12" style="display:inline;margin:0 .25rem 0 0;width:10px"><line class="stale" x1="5" x2="5" y1="1" y2="11"/></svg>stale: a correct answer that reached the pool just after it moved to the next block (pool timing, not the miner; a few a day is normal)</span>
            <span><svg width="12" height="12" style="display:inline;margin:0 .25rem 0 0;width:12px"><path class="other" d="M6 1L11 11L1 11Z"/></svg><b style="color:var(--bad)">not stale</b>: refused for another reason or none: look at the miner's log</span>
            <span><b style="color:var(--text)">?</b> counted by the miner but not found in its log (the miner trims its log)</span></div></div>`;
      boards.parentNode.insertBefore(bs, boards.nextSibling);
      if (split) layoutDetail(split, p, bs, boards, clock);
      $("sclSharesReset").onclick = async () => {
        const id = detailId(); if (!id) return;
        if (!confirm(`Clear the best-share record and history for ${nameOf(id)}?\nThe best since its last restart stays (the miner keeps that one itself).`)) return;
        try { await api("POST", "/api/hardware/shares_reset", { miner_id: id }); toast("Best-share record cleared"); } catch (e) { toast(e.message, true); }
        loadHw();
      };
      $("sclChipsRefresh").onclick = () => loadChips(true, true);
      $("sclChipMetric").onchange = () => renderChips();
      p.querySelectorAll("th[data-k]").forEach(th => th.onclick = () => { chipSort = th.dataset.k; renderChipTable(); });
      const tip = document.createElement("div"); tip.className = "scl-tip"; tip.id = "sclTip"; document.body.appendChild(tip);
      $("sclChipMap").addEventListener("mousemove", e => {
        const c = e.target.closest(".cell"); const t = $("sclTip");
        if (!c) { t.style.display = "none"; return; }
        t.innerHTML = chipTipHtml(c.dataset.k); t.style.display = "block";
        t.style.left = Math.min(innerWidth - t.offsetWidth - 8, e.clientX + 14) + "px"; t.style.top = (e.clientY + 14) + "px";
      });
      $("sclChipMap").addEventListener("mouseleave", () => ($("sclTip").style.display = "none"));
    }
    ["planMhz", "detailFanAuto"].forEach(id => { const p = panelOf(id); if (p) p.classList.add("scl-lockable"); });
    const ren = (id, text, title) => { const b = $(id); if (b) { b.textContent = text; if (title) b.title = title; } };
    if ($("btnDetailRestart")) $("btnDetailRestart").onclick = async () => {
      const id = detailId(); if (!id) return;
      if (!confirm(`Restart the miner software on ${nameOf(id)}? Hashing pauses for about a minute.`)) return;
      try { await restartIds([id]); } catch (e) { toast(e.message, true); }
    };
    ren("btnDetailFanApply", "Apply curve", "Save the auto fan switch and the chosen fan curve for this miner");
    ren("btnDetailFan", "Set fan once", "Send this fan % to the miner once. The miner's own control slowly turns it back down; use auto fan to hold a speed.");
    ren("btnDetailTc", "Apply", "Turn the miner's own temperature control on or off (on is safer)");
  }

  function updateDetailInfo() {
    const el = $("sclDetailInfo"), id = detailId();
    if (!el || !id) return;
    const h = HW[id] || {};
    const bits = [];
    bits.push(`<span>Detected as <b>${esc(h.probing ? "probing…" : h.line || "not probed yet")}</b></span>`);
    const st = (h.hardware || {}).stock;
    if (st) bits.push(`<span title="The firmware's own stock power plan (the tuner's baseline). The miner runs clocks in 12.5 MHz steps.">Stock <b>${st.mhz}${Math.floor(st.mhz / 12.5) * 12.5 !== st.mhz ? ` (runs ${Math.floor(st.mhz / 12.5) * 12.5})` : ""} MHz · ${st.mv} mV · PV ${st.pv}</b></span>`);
    const hwd = h.hardware || {}, sr = hwd.stock_read, rr = hwd.running_read;
    if (!st && hwd.plan_format && hwd.plan_format !== "sc-lite") {
      const pl2 = (x) => `${x.mhz} MHz · ${esc(x.volts)} V${x.pv ? ` · PV ${esc(x.pv)}` : ""}`;
      if (sr) bits.push(`<span title="The firmware's own level-0 plan${hwd.algo ? " for the algorithm it runs" : ""}, as the miner writes it">Stock <b>${pl2(sr)}</b>${hwd.algo ? ` <span class="muted">(${esc(hwd.algo)})</span>` : ""}</span>`);
      if (rr) bits.push(`<span title="What the miner is running: its own setting (manual) or its stock plan">Runs <b>${pl2(rr)}</b> <span class="muted">(${hwd.manual ? "manual setting" : "stock plan"})</span></span>`);
      bits.push(`<span class="muted" title="This model writes volts as a decimal with no PV. The app reads it but doesn't change clock, voltage or fans on it yet; chip health, pools and restarts work.">Clock, voltage and fans: read only on this model</span>`);
    }
    // clock / voltage and fan panels: off on a model whose plan format the app can't write yet
    const ro = !!(hwd.plan_format && hwd.plan_format !== "sc-lite");
    // the dashboard prints manualPowerplan under the clock panel; on an HS BOX that runs its stock plan it's
    // the other algorithm's plan, so say what really runs (kept up as the dashboard rewrites the line)
    const raw = $("detailPlanRaw");
    if (raw) {
      raw._scl = ro && hwd.running_text ? `Runs ${hwd.running_text}` + (hwd.live_text && hwd.live_text !== hwd.running_text ? ` (its manualPowerplan, ${hwd.live_text}, isn't in use)` : "") : "";
      const fix = () => { if (raw._scl && raw.textContent !== raw._scl) raw.textContent = raw._scl; };
      if (!raw._sclObs) { raw._sclObs = new MutationObserver(fix); raw._sclObs.observe(raw, { childList: true, characterData: true, subtree: true }); }
      fix();
    }
    for (const pid of ["planMhz", "detailFanAuto"]) {
      const pan = panelOf(pid); if (!pan) continue;
      pan.querySelectorAll("input, select, button").forEach(x => { if (["detailTc", "btnDetailTc", "btnDetailRestart"].includes(x.id)) return; if (ro) { x.disabled = true; x.dataset.sclRo = "1"; } else if (x.dataset.sclRo) { x.disabled = false; delete x.dataset.sclRo; } });
      let n = pan.querySelector(".scl-ro-note");
      if (ro && !n) { n = document.createElement("p"); n.className = "muted scl-ro-note"; n.style.cssText = "margin:0 0 .5rem;font-size:.85rem";
        n.textContent = "Read only on this model: it writes its power plan with volts as a decimal and no PV, which the app can't change yet.";
        const h2 = pan.querySelector("h2"); if (h2) h2.after(n); else pan.prepend(n); }
      if (!ro && n) n.remove();
    }
    const m = (S().miners || []).find(x => x.id === id) || {}, pl = planOf(m), e = h.active === "idle" ? null : estW(h.power_model, pl.mhz, pl.pv);
    if (h.active === "idle") bits.push(`<span title="The firmware's own Idle mode: the hash boards are off. Any other preset wakes it.">Idle: <b>not hashing</b></span>`);
    if (e) bits.push(`<span title="PV is the voltage the boards really get; power follows clock × PV²">Power <b>≈ ${e.w} W</b> · ${e.amps} A at ${e.mains} V${e.wth ? ` · ${e.wth} J/TH` : ""}${e.eff !== pl.mhz ? ` · runs ${e.eff} MHz` : ""}
      <a href="#" class="scl-cal">${h.power_model && h.power_model.cal_info ? "calibrated" : "calibrate"}</a></span>`);
    bits.push(`<span>Preset <b>${esc(h.preset || "none")}</b></span>`);
    if (bellHtml(id)) bits.push(`<span>${bellHtml(id)}</span>`);
    if (h.schedule) bits.push(`<span><a href="/schedule.html?miner=${encodeURIComponent(id)}">${esc(h.schedule)}</a></span>`);
    if (h.tuning) bits.push(`<span class="pill warn">being tuned</span>`);
    el.innerHTML = bits.join("");
    const cal = el.querySelector(".scl-cal");
    if (cal) cal.onclick = async (ev) => {
      ev.preventDefault();
      const m2 = (S().miners || []).find(x => x.id === id) || {}, p2 = planOf(m2), pm = h.power_model || {};
      const v = prompt(`Power estimate calibration for ${nameOf(id)}\n\nRead the wall power with a plug-in meter while the miner runs ${p2.mhz} MHz · PV ${p2.pv}, and enter the watts here.\nEmpty clears the calibration. (Mains voltage now ${pm.mains_v || 110} V: type e.g. "v220" to change it.)`, "");
      if (v === null) return;
      try {
        if (/^v\s*\d+/i.test(v.trim())) { await api("POST", "/api/hardware/mains", { mains_v: Number(v.trim().slice(1)) }); toast("Mains voltage saved"); }
        else if (!v.trim()) { await api("POST", "/api/hardware/power_cal", { miner_id: id, clear: true }); toast("Calibration cleared"); }
        else { const r = await api("POST", "/api/hardware/power_cal", { miner_id: id, watts: Number(v), mhz: p2.mhz, pv: p2.pv }); toast(`Calibrated (×${r.factor})`); }
      } catch (e2) { toast(e2.message, true); }
      loadHw();
    };
    renderShares(h.share, id);
    renderRejects(h.rejects, id);
    renderHashPanel(id);
  }

  const when = (t) => {
    if (!t) return "–";
    const d = new Date(t * 1000), today = new Date().toDateString() === d.toDateString();
    return today ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
                 : d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  };
  const ago = (t) => { const s = Date.now() / 1000 - t; return s < 3600 ? `${Math.max(1, Math.round(s / 60))} min` : s < 172800 ? `${(s / 3600).toFixed(1)} h` : `${Math.round(s / 86400)} days`; };
  function renderShares(sh, id) {
    const st = $("sclShareStats"), body = $("sclShareBody"); if (!st || !body) return;
    if (String(id).startsWith("demo-")) { st.innerHTML = `<span class="muted">(demo miner)</span>`; body.innerHTML = ""; return; }
    if (!sh) { st.innerHTML = `<span class="muted">Waiting for the first reading (every 30 s)…</span>`; body.innerHTML = ""; return; }
    st.innerHTML = `<div><span class="muted">Since restart</span><b>${esc(sh.now_text)}</b>
        <small class="muted">${sh.now_at ? `found ${esc(when(sh.now_at))}${sh.now_on ? " · " + esc(sh.now_on) : ""}` : "none yet"}${sh.since ? ` · up ${esc(ago(sh.since))}` : ""}</small></div>
      <div><span class="muted">Record</span><b>${esc(sh.record_text)}</b>
        <small class="muted">${sh.record_at ? `found ${esc(when(sh.record_at))}${sh.record_on ? " · " + esc(sh.record_on) : ""}` : ""}${sh.reset_at ? ` · since reset ${esc(when(sh.reset_at))}` : ""}</small></div>`;
    const SHOW = 10, all = sh.history || [];
    let rows = all.slice(0, SHOW);
    const cur = all.slice(SHOW).find(h => h.current);   // the best since restart stays in view even below the top 10
    if (cur) rows = rows.concat([cur]);
    const more = all.length - rows.length;
    body.innerHTML = rows.map((h, i) => `<tr${h.current || h.record ? ' style="font-weight:600"' : ""}>
      <td class="num">${esc(h.rank ?? i + 1)}</td><td class="num">${esc(h.text)}${h.record ? " ★" : ""}</td>
      <td>${esc(h.on || "")}${h.current ? ' <span class="muted">(best since restart)</span>' : ""}</td>
      <td style="text-align:right"${h.after ? ` title="The app wasn't reading this miner for a while, so this share was found some time between ${esc(when(h.after))} and ${esc(when(h.at))}; the setting is the one at ${esc(when(h.at))}"` : ""}>${h.after ? `${esc(when(h.after))} – ` : ""}${esc(when(h.at))}</td></tr>`).join("")
      + (more > 0 ? `<tr><td colspan="4" class="muted" style="font-size:.78rem">${more} smaller one${more > 1 ? "s" : ""} kept but not shown (the record keeps the 20 biggest)</td></tr>` : "")
      || `<tr><td colspan="4" class="muted">Nothing yet</td></tr>`;
  }

  // Miner page: the same 24-hour hashrate graph as the Fleet card, wider, between Live and Chips
  function renderHashPanel(id) {
    const chips = $("sclChips"); if (!chips) return;
    let p = $("sclHash");
    if (!p) { p = document.createElement("div"); p.className = "panel compact"; p.id = "sclHash"; }
    if (p.nextElementSibling !== chips) chips.parentNode.insertBefore(p, chips);
    if (String(id).startsWith("demo-")) { p.hidden = true; return; }
    p.hidden = false;
    const m = { id }, g = (HW[id] || {}).hashrate;
    const html = window.sclHashGraph(m, { W: 1200, H: 150, big: true }).replace('style="grid-column:1/-1"', "")
      + `<p class="muted" style="font-size:.75rem;margin:.4rem 0 0">A point every 5 minutes from the miner's 20-second hashrate${g && g.now != null ? ` (now ${fmtTH(g.now)}${g.since_restart ? `, ${fmtTH(g.since_restart)} on average since its last restart` : ""})` : ""}.
        Marks: <b>R</b> restarted · <b>P</b> setting changed · <b>T</b> tuning started · <b style="color:var(--bad)">X</b> a share rejected, not stale. Hover a mark for when and what.</p>`;
    if (p._last !== html) { p.innerHTML = html; p._last = html; }
  }

  // Rejected shares: how many, and which kind (rejects_addon.py)
  function rejectWords(r) {
    const d = r.day, total = d.stale + d.other + d.unseen + d.pending, bits = [];
    if (!d.rejected && !total) return "none";
    if (d.other) bits.push(`<b class="alert">${d.other} NOT STALE${r.other_last ? ` (last at ${esc(r.other_last.slice(11, 16))} on the miner's clock)` : ""}</b>`);
    bits.push(d.other || d.unseen || d.pending ? `${d.stale} stale` : "all stale");
    if (d.unseen) bits.push(`${d.unseen} not found in the miner's log`);
    if (d.pending) bits.push(`${d.pending} being checked`);
    const pct = (x) => (+x).toFixed(x < 1 ? 2 : 1).replace(/\.?0+$/, "") || "0";
    bits.push(`${pct(d.pct)} % of accepted` + (r.high ? `, above this miner's usual ${pct(r.usual_pct)} %`
      : r.usual_pct != null && r.covered_h >= 48 ? ` (usual ${pct(r.usual_pct)} %)` : ""));
    return bits.join(" · ");
  }
  function rejectStrip(r) {
    const W = 600, H = 52, L = 6, R = 6, y = 20, now = Date.now() / 1000, span = 3 * 86400;
    const xOf = (t) => L + (W - L - R) * (1 - (now - t) / span);
    let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="rejected shares, last 3 days">`;
    for (let i = 0; i <= 3; i++) {
      const t = now - i * 86400, x = xOf(t);
      svg += `<line class="grid" x1="${x.toFixed(1)}" x2="${x.toFixed(1)}" y1="6" y2="${H - 16}"/>`;
      if (i > 0 && i < 3) svg += `<text x="${x.toFixed(1)}" y="${H - 3}" text-anchor="middle">${i === 1 ? "24 h ago" : "48 h ago"}</text>`;
    }
    svg += `<text x="${W - R}" y="${H - 3}" text-anchor="end">now</text><text x="${L}" y="${H - 3}">3 days ago</text>`;
    svg += `<line class="grid" x1="${L}" x2="${W - R}" y1="${H - 16}" y2="${H - 16}"/>`;
    for (const e of r.events || []) {
      const x = xOf(e.t), tip = `${when(e.t)}${e.at ? ` (miner's log ${e.at.slice(11, 16)})` : ""} · ${e.kind === "stale" ? "stale" : e.kind === "other" ? "rejected, NOT stale" + (e.reason ? ` (${e.reason})` : " (no reason given)") : "not found in the miner's log"}${e.n > 1 ? ` · ×${e.n}` : ""}`;
      svg += `<g><title>${esc(tip)}</title>`;
      if (e.kind === "stale") svg += `<line class="stale" x1="${x.toFixed(1)}" x2="${x.toFixed(1)}" y1="${y - 7}" y2="${y + 7}"/>`;
      else if (e.kind === "other") svg += `<path class="other" d="M${x.toFixed(1)} ${y - 8}L${(x + 8).toFixed(1)} ${y + 7}L${(x - 8).toFixed(1)} ${y + 7}Z"/><text class="otherlbl" x="${(x > W - 80 ? x - 10 : x + 10).toFixed(1)}" y="${y + 4}" text-anchor="${x > W - 80 ? "end" : "start"}">not stale</text>`;
      else svg += `<text class="unseen" x="${x.toFixed(1)}" y="${y + 5}" text-anchor="middle">?</text>`;
      if (e.n > 1 && e.kind !== "other") svg += `<text x="${(x + 4).toFixed(1)}" y="${y - 9}">×${e.n}</text>`;
      svg += `<rect x="${(x - 8).toFixed(1)}" y="${y - 12}" width="16" height="24" fill="transparent"/></g>`;
    }
    return svg + "</svg>";
  }
  function renderRejects(r, id) {
    const body = $("sclRejBody"), strip = $("sclRejStrip"); if (!body || !strip) return;
    if (String(id).startsWith("demo-")) { body.innerHTML = "(demo miner)"; strip.innerHTML = ""; return; }
    if (!r) { body.innerHTML = "Waiting for the first reading (every 30 s)…"; strip.innerHTML = ""; return; }
    const span = r.covered_h < 24 ? `since the app started counting (${r.covered_h < 1 ? "under an hour" : Math.floor(r.covered_h) + " h"})` : "last 24 h";
    body.className = "rj-line";
    body.innerHTML = `<span class="muted">Since restart:</span> ${(+r.accepted).toLocaleString()} accepted · ${(+r.rejected).toLocaleString()} rejected<br>
      <span class="muted">${esc(span)}:</span> ${rejectWords(r)}`;
    strip.innerHTML = (r.events || []).length ? rejectStrip(r) : "";
  }

  function applyLocks() {
    const id = detailId(), h = (id && HW[id]) || {};
    const tuning = !!h.tuning, held = (h.in_charge || []);
    [["detailFanAuto", "Fan control"], ["planMhz", "Clock / voltage"]].forEach(([anchor, what]) => {
      const p = panelOf(anchor); if (!p) return;
      let lock = p.querySelector(".scl-lock");
      const mode = tuning ? "tuning" : held.length ? "held:" + held.join("|") : "";
      if (!mode) { if (lock) lock.remove(); return; }
      if (lock && lock.dataset.mode === mode) return;
      if (!lock) { lock = document.createElement("div"); lock.className = "scl-lock"; p.appendChild(lock); }
      lock.dataset.mode = mode;
      lock.innerHTML = tuning
        ? `<div><b>🔒 ${what} is locked while this miner is being tuned</b>
            The tuner controls the clock and fans of a miner it is tuning, so changes here would spoil its tests.
            <br><a href="/tuner.html?miner=${encodeURIComponent(id)}">Open the Tuner</a> to watch the run or stop it.</div>`
        : `<div><b>🔒 ${what} is set by ${esc(held.join(" and "))}</b>
            So that nothing fights it, hand changes are locked. Taking manual control turns the preset off and pauses the
            schedule until you resume it.<br><button type="button" class="scl-manual" style="margin-top:.6rem">Take manual control</button></div>`;
      const btn = lock.querySelector(".scl-manual");
      if (btn) btn.onclick = async () => {
        if (!confirm(`Take manual control of ${nameOf(id)}?\n\nIts preset turns off and its schedule pauses, so the clock and fans stay where you set them. Apply a preset or resume the schedule to hand control back.`)) return;
        try { await api("POST", "/api/quick/manual", { miner_id: id }); toast(`${nameOf(id)}: manual control`); }
        catch (e) { toast(e.message, true); }
        await loadHw(); if (window.sclRefresh) window.sclRefresh();
      };
    });
  }

  // chips: the same map as the Tuner page (squares, shades, legend), shaded by the chip's health
  let chipData = null, chipFor = null, chipSort = "h";
  async function loadChips(force, fresh) {
    const id = detailId(); if (!id || !$("sclChips")) return;
    if (String(id).startsWith("demo-")) { $("sclChipsCap").textContent = "Demo miner: no chip data."; $("sclChipMap").innerHTML = ""; $("sclBoardHealth").innerHTML = ""; return; }
    if (!force && chipFor === id && chipData && Date.now() - chipData._at < 60000) return;
    $("sclChipsCap").textContent = "Reading the chips…";
    try {
      chipData = await api("GET", `/api/hardware/chips?miner=${encodeURIComponent(id)}${fresh ? "&force=1" : ""}`); chipData._at = Date.now();
      // a miner whose log has no bad-result lines (SC BOX, HS BOX): shade by HW errors, which is what it's judged on
      if (chipFor !== id && $("sclChipMetric")) $("sclChipMetric").value = (chipData.boards || []).length && chipData.boards.every(b => b.judged_on_hw) ? "hw" : "bad";
      chipFor = id;
      renderChips();
    } catch (e) { $("sclChipsCap").textContent = "Couldn't read the chips: " + e.message; $("sclChipMap").innerHTML = ""; }
  }
  const RAMP = [0, 1, 2, 3, 4, 5].map(i => `var(--ramp-${i})`);
  const binR = (r) => Math.min(RAMP.length - 1, Math.max(0, Math.ceil(r * RAMP.length) - 1));
  const HL = { ok: "OK", watch: "watch", weak: "weak" };
  const HOW_HTML = `<p>Each chip is checked these ways, and only flagged when the difference is far bigger than chance:</p>
    <p><b>Bad results</b> (the real sign of a failing chip). The miner logs every result a chip got wrong. A chip
      is <b>weak</b> when 10% or more of its results are bad, and <b>watch</b> from 3%, but only once it has at
      least 50 results and its share is clearly above the board's typical chip (4 and 3 standard deviations).
      A few bad results out of a handful can just be bad luck.</p>
    <p><b>Perf</b> (how much work the chip reports, against the board's typical chip). It's a count, so it
      scatters, a lot when the numbers are small: right after a restart a typical chip might show 55, and 43
      is only about 1.6 standard deviations below that, ordinary luck. A chip is only <b>watch</b> for being
      slow when it's 15% or more below <i>and</i> 4 standard deviations out, and only <b>weak</b> when it does
      under half the work. A slow chip with no bad results isn't failing, so speed alone rarely makes one weak.</p>
    <p>Hover a chip to see its numbers and, for a healthy chip, why a low number isn't a worry. Judgements get
      steadier the longer the boards have been running.</p>
    <p><b>HW errors</b>, only for a miner whose log has no bad-result lines (the SC BOX and HS BOX hardly write
      them): a chip is <b>watch</b> at 10 or more hardware errors that are 5 times the board's typical chip and
      well beyond chance. Never weak on this alone, since the count covers the whole time since the boards started.</p>`;
  function metricOf(c, m, ctx) {
    // a 0..1 share for the shade, and the number written in the square (only for the chips worth reading)
    if (m === "bad") { const v = c.bad_pct; return v == null ? [c.bad ? 0.05 : 0, ""] : [Math.min(1, v / Math.max(10, ctx.maxBad)), v >= 3 ? Math.round(v) : ""]; }
    // perf: shaded by how sure it is that the chip is slower than its board (scatter taken out), not by the raw %
    if (m === "perf") { const z = c.perf_z || 0; return c.perf_rel == null || z < 2 ? [0, ""] : [Math.min(1, (z - 1) / 5), z >= 3 ? Math.round(c.perf_rel * 100) : ""]; }
    const lg = Math.log(ctx.maxHw + 1); return [c.hwerr ? Math.log(c.hwerr + 1) / lg : 0, c.hwerr >= 0.6 * ctx.maxHw && c.hwerr >= 10 ? c.hwerr : ""];
  }
  function renderChips() {
    const d = chipData; if (!d) return;
    const m = $("sclChipMetric").value, log = d.log;
    const all = d.boards.flatMap(b => b.chips);
    const ctx = { maxHw: Math.max(1, ...all.map(c => c.hwerr)), maxBad: Math.max(0, ...all.map(c => c.bad_pct || 0)) };
    const weak = [], watch = [];
    let h = "";
    for (const b of d.boards) {
      const n = b.chips.length, cols = n % 2 ? n + 1 : n, tot = b.chips.reduce((a, c) => a + c.hwerr, 0);
      h += `<div class="board-row"><div class="board-label">Board ${b.board}<span class="muted">${tot} errors</span>${b.bad_pct != null ? `<span class="muted">${(+b.bad_pct).toFixed(1)}% bad</span>` : ""}${b.reboots ? `<span class="muted">${b.reboots} restarts</span>` : ""}</div><div class="cells" style="--cols:${cols};--half:${cols / 2}">`;
      for (const c of b.chips) {
        const [r, txt] = metricOf(c, m, ctx);
        let cls = "cell", style = "";
        if (c.health === "weak") { cls += " over"; weak.push(`board ${b.board} chip ${c.chip}`); }
        else if (r > 0) { const i = binR(r); style = `background:${RAMP[i]}`; if (i >= 3) cls += " strong"; }
        if (c.health === "watch") { cls += " ring"; watch.push(`board ${b.board} chip ${c.chip}`); }
        h += `<div class="${cls}" style="${style}" data-k="${b.board},${c.chip}">${c.health === "weak" ? (txt || "!") : txt}</div>`;
      }
      h += `</div></div>`;
    }
    $("sclChipMap").innerHTML = h;
    // per-board line: chip temperatures and board voltage (from the miner's log)
    $("sclBoardHealth").innerHTML = d.boards.some(b => b.max_c != null || b.volt_mv)
      ? d.boards.map(b => `<span><b>Board ${b.board}</b><br>${b.avg_c != null ? `chips avg ${b.avg_c.toFixed(0)} · max ${b.max_c.toFixed(0)} °C` : ""}${b.board_c != null || b.volt_mv ? "<br>" : ""}${b.board_c != null ? `sensor ${b.board_c.toFixed(0)} °C` : ""}${b.board_c != null && b.volt_mv ? " · " : ""}${b.volt_mv ? `${(b.volt_mv / 1000).toFixed(2)} V` : ""}</span>`).join("")
      : "";
    const up = d.boards.map(b => b.uptime_s).filter(Boolean);
    const what = m === "bad" ? "each chip's share of bad results (from the miner's log)" : m === "perf" ? "how sure it is that a chip does less work than the typical chip on its board (normal scatter left blank)" : "each chip's HW errors since the boards last started";
    $("sclChipsCap").innerHTML = esc(`Each square is a chip, shaded by ${what}.`) +
      (weak.length ? ` <b style="color:var(--bad)">Weak: ${esc(weak.join(", "))}.</b>` : "") +
      (watch.length ? ` Watch: ${esc(watch.slice(0, 5).join(", "))}${watch.length > 5 ? ` and ${watch.length - 5} more` : ""}.` : "") +
      (!weak.length && !watch.length ? " Every chip looks healthy." : "") +
      esc((up.length ? ` Boards up ${(Math.max(...up) / 3600).toFixed(1)} h.` : "") +
          (log ? ` Log ${String(log.from || "").slice(11, 16)}–${String(log.to || "").slice(11, 16)}.` : " (No miner log: bad results and chip temperatures unavailable.)") +
          " Hover a chip for its numbers.");
    const sw = RAMP.map(c => `<span class="sw" style="background:${c}"></span>`).join("");
    const lo = m === "bad" ? "0%" : m === "perf" ? "within normal scatter" : "1 error", hi = m === "bad" ? `${Math.max(10, Math.round(ctx.maxBad))}% bad` : m === "perf" ? "certainly slower (number = % of typical)" : `${ctx.maxHw} errors`;
    $("sclChipLegend").innerHTML =
      `<span><span class="sw" style="background:var(--cell-empty);box-shadow:inset 0 0 0 1px var(--cell-edge)"></span> ${m === "perf" ? "typical or better" : "none"}</span>` +
      `<span>${lo}<span class="ramp">${sw}</span>${hi}</span>` +
      `<span><span class="sw" style="background:var(--ramp-2);box-shadow:0 0 0 2px var(--text)"></span> watch: 3%+ bad results, or 15%+ slower, and well beyond chance${d.boards.some(b => b.judged_on_hw) ? " (with no bad results in the log: 5x the typical chip's HW errors)" : ""}</span>` +
      `<span><span class="sw" style="background:var(--over)">!</span> weak: 10%+ bad results (or under half the work), well beyond chance</span>` +
      `<details class="scl-how"><summary>How is a chip judged?</summary>${HOW_HTML}</details>`;
    renderChipTable();
  }
  function renderChipTable() {
    const d = chipData; if (!d) return;
    const rows = [], rank = { weak: 2, watch: 1, ok: 0 };
    for (const b of d.boards) {
      const hrs = b.uptime_s / 3600;
      for (const c of b.chips) rows.push({ b: b.board, c: c.chip, e: c.hwerr, r: hrs > 0.05 ? c.hwerr / hrs : null, bp: c.bad_pct, bad: c.bad, tot: c.total,
                                           pr: c.perf_rel, perf: c.perf, h: rank[c.health] ?? 0, hs: c.health, why: c.why });
    }
    rows.sort((x, y) => chipSort === "b" ? x.b - y.b || x.c - y.c : chipSort === "c" ? x.c - y.c || x.b - y.b
      : chipSort === "pr" ? (x.pr ?? 9) - (y.pr ?? 9) : (y[chipSort] ?? -1) - (x[chipSort] ?? -1) || (y.bp ?? 0) - (x.bp ?? 0));
    $("sclChipSum").textContent = `Full chip list (${rows.length} chips; click a heading to sort)`;
    $("sclChipBody").innerHTML = rows.map(r => `<tr><td>${r.b}</td><td class="num">${r.c}</td><td class="num">${r.e}</td>
      <td class="num">${r.r == null ? "—" : r.r.toFixed(1)}</td>
      <td class="num">${r.bp == null ? (r.e ? "—" : "0%") : `${r.bp.toFixed(1)}% <span class="muted">(${r.bad}/${r.tot})</span>`}</td>
      <td class="num">${r.pr == null ? "—" : `${Math.round(r.pr * 100)}%`}</td>
      <td title="${esc(r.why || "")}" style="${r.hs === "weak" ? "color:var(--bad);font-weight:600" : r.hs === "watch" ? "font-weight:600" : "color:var(--muted)"}">${HL[r.hs] || ""}</td></tr>`).join("");
  }
  function chipTipHtml(k) {
    const d = chipData; if (!d) return "";
    const [bn, cn] = k.split(",").map(Number);
    const b = d.boards.find(x => x.board === bn), c = b && b.chips.find(x => x.chip === cn); if (!c) return "";
    const hrs = b.uptime_s / 3600;
    return `Board ${bn} · chip ${cn}<br><b>${c.hwerr}</b> HW error${c.hwerr === 1 ? "" : "s"}${hrs > 0.05 ? ` (${(c.hwerr / hrs).toFixed(1)}/hour)` : ""}` +
      (c.bad_pct != null ? `<br><b>${c.bad_pct.toFixed(1)}%</b> of its results bad (${c.bad} of ${c.total}${c.cores ? `, from ${c.cores} cores` : ""})` : "") +
      (c.perf_rel != null ? `<br>perf <b>${c.perf}</b> · ${Math.round(c.perf_rel * 100)}% of the board's typical chip (${b.typical_perf})` : "") +
      (c.health !== "ok" ? `<br><span style="color:${c.health === "weak" ? "var(--bad)" : "var(--text)"}">${c.health === "weak" ? "✕ weak" : "▲ watch"}: ${esc(c.why)}</span>`
                         : `<br><span style="color:var(--ok)">✓ healthy</span>${c.note ? `<br><span class="muted">${esc(c.note)}</span>` : ""}`);
  }

  // ------------------------------------------------------------ Settings: demo miners
  function buildDemo() {
    const t = $("demoToggle"); if (!t) return;
    const panel = t.closest(".panel"), row = t.closest(".row");
    if (row) row.classList.add("hidden");
    const p = panel.querySelector("p"); if (p) p.textContent = "UI-only fake miners for trying out the layout (a big fleet, say). They aren't real miners: nothing is ever sent to them, and the Profiles, Schedule and Tuner pages ignore them.";
    const box = document.createElement("div"); box.className = "row";
    box.innerHTML = `<label class="muted">How many <input type="number" id="demoCount" min="0" max="60" value="4" style="width:5rem" /></label>
      <button type="button" class="primary" id="btnDemoCount">Show demo miners</button>
      <button type="button" id="btnDemoClear">Remove all demo miners</button>`;
    panel.appendChild(box);
    const set = async (n) => {
      try { await api("POST", "/api/quick/demo", { count: n }); toast(n ? `${n} demo miners on Fleet` : "Demo miners removed"); if (window.sclRefresh) window.sclRefresh(); }
      catch (e) { toast(e.message, true); }
    };
    $("btnDemoCount").onclick = () => set(Number($("demoCount").value) || 0);
    $("btnDemoClear").onclick = () => set(0);
  }

  // ------------------------------------------------------------ Settings: miner reference
  // What the miner itself understands and reports: from its firmware (the web page's own code and the
  // mining software), checked against a live SC Lite on firmware 2.2.0. ✓ = this app uses it, ✎ = changes the miner.
  const REF = [
    { t: "Power plan format", where: "How the miner writes a clock / voltage / fan setting, in /mcb/setting and on its own web page.", items: [
      ["625 MHz 9100 V 75 RPM 75 RPM PV 9400", "One setting: chip clock 625 MHz, chip voltage 9100 mV (the firmware writes \"V\" but means mV), the two fan groups at 75 (the \"RPM\" numbers are fan %, not RPM), and PV 9400 mV, the voltage the board's regulator is really set to."],
      ["725 MHz 0.41 V 70 RPM 70 RPM", "The same setting as the SC BOX and HS BOX write it: volts as a decimal and no PV. This app reads it but can't change clock or voltage in this form yet. The HS BOX also keeps one list of plans per algorithm."],
      ["MHz", "Moves in 12.5 MHz steps and rounds down: 615 runs at 612.5, 640 at 637.5. The miner shows the number you set, not the one it runs."],
      ["V (mV)", "The chip voltage passed to the boards with the clock. Keep the stock gap to PV (PV = mV + 300 on an SC Lite)."],
      ["PV", "The board voltage. It's what really drives power and heat; the miner reads back about PV − 20 mV."],
    ]},
    { t: "Settings the miner keeps (/mcb/setting)", where: "GET returns these, PUT with the same body changes them. Captured from an SC Lite.", items: [
      ["powerplans", "The firmware's own plans. level 0 is the stock setting (the probe reads it; the tuner works its hard limits out from it and uses it as the default baseline). level 3 (\"0 MHz 0 V 100 RPM 100 RPM\") is a built-in no-hashing, fans-full plan this app never picks."],
      ["select", "Which power plan is in use (0 = stock)."],
      ["manual · manualPowerplan", "manual: true means the miner runs manualPowerplan, your own setting in the format above, instead of a plan. Presets, schedules, the tuner and the clock panel all set these. ✎"],
      ["tempcontrol", "The firmware's own fan control. On, it raises the fans when it gets hot and walks them back as it cools; this app's fan curves work with it on."],
      ["ledcontrol", "The front LEDs blink red / green (\"Light indicator\")."],
      ["name", "The miner's name; its MAC address by default."],
      ["version", "The settings format version (v1.0)."],
    ]},
    { t: "Miner web API", where: "What the miner's own web page calls (port 80). Sign in first: GET /user/login with username admin and the password encrypted, which returns a JWT token that goes with every call. ✓ = this app uses it, ✎ = changes the miner.", items: [
      ["GET /user/login · /user/logout", "✓ Sign in (returns the JWT token) and out."],
      ["PUT /user/updatepd", "✎ Changes the miner's web password."],
      ["GET /mcb/status", "✓ model (as the miner names it), firmware (2.2.0), hardware (30.40.SA) and control board (MCB_V4_3). Often answers without a login."],
      ["GET · PUT /mcb/setting", "✓ ✎ The power plans, clock / voltage / PV, fans, tempcontrol, LEDs (see above)."],
      ["GET /mcb/pools", "✓ The pool list with each pool's status."],
      ["PUT /mcb/pools · /mcb/newpool · /mcb/delpool", "✓ ✎ Saves the whole pool list, adds one pool ({url, user, pass}), deletes one."],
      ["PUT /mcb/restart", "✓ ✎ Restarts the mining software; hashing stops for about a minute. A GET does nothing."],
      ["GET /mcb/cgminer?cgminercmd=devs", "✓ Port 4028's devs reply through the web API (the page's per-board table)."],
      ["GET · PUT /mcb/algosetting", "✓ The algorithm: blake2b(SC), id 0. It's the only one on an SC Lite."],
      ["GET · PUT /mcb/ip", "✎ Network settings: DHCP or a fixed IP."],
      ["GET · PUT /mcb/wifisetting", "✎ Wi-Fi, on models that have it. The page watches scan results over a websocket, /mcb/wifiresult."],
      ["GET · PUT /mcb/cmossetting", "✎ \"Cloud control\": linking the miner to the maker's cloud account with a key. Keep it off on a LAN-only miner."],
      ["GET · PUT /mcb/rgbsetting", "✎ ARGB fan lights (mode, brightness, speed) on models with them."],
      ["PUT /mcb/facrst", "✎ Factory reset: \"Miner will be set back to factory status\" (pools, password and settings)."],
      ["POST /mcb/uploadimage", "✎ Firmware update. The image is signature-checked, so only the maker's own firmware installs."],
      ["websocket /mcb/resultpool", "The pool connection test results the page shows while you edit pools."],
      ["GET /mcb/tutorial", "The mining tutorial text the page shows."],
      ["PUT /mcb/protect · /mcb/defend", "✎ In the firmware, purpose not confirmed. This app never calls them."],
      ["/mcb/state · /mcb/mode · /mcb/iptrigger · /mcb/initminerd · /mcb/newpools · /mcb/delpools · /cpb/control", "Also in the firmware, but its own page doesn't use them. This app never calls them."],
    ]},
    { t: "Miner debug pages (/dbg, /cpb)", where: "Read-only. The miner's Debug page shows them; all need the login.", items: [
      ["GET /dbg/icinfo", "✓ Every chip: chipindex (1–46 per board), hwerr (HW errors since its board started), perf (how much work it does), bist (self-test), plus each board's status, reboots and reason. The chip maps come from this."],
      ["GET /dbg/minersyslog", "✓ The mining software's log, a few MB. Chip health reads its bad-result, chip temperature and voltage lines (below)."],
      ["GET /dbg/minerinfo", "✓ Per-board details from the mining software ([PGAn] blocks)."],
      ["GET /dbg/fanctrllog", "✓ The fan controller's log, with its target temperature. Large (about 700 kB)."],
      ["GET /dbg/vsinfo", "On the Debug page; what it holds hasn't been checked yet."],
      ["GET /dbg/minerhistory · /dbg/monitorlog", "Two more logs from the Debug page: the mining software's history and the firmware monitor's."],
      ["GET /dbg/syslog · /dbg/kmsg", "The system log and the kernel's messages."],
      ["GET /dbg/psinfo · /dbg/meminfo", "Running processes and memory use."],
      ["GET /cpb/hshistory", "The hashrate history behind the Home page's graph."],
    ]},
    { t: "Port 4028 (mining software API)", where: "The mining software is intminer, a BFGMiner fork. Send one JSON line such as {\"command\":\"summary\"} to port 4028; no login. The miner's config only gives write access to the miner itself (127.0.0.1), so from your network the changing commands are refused (\"Access denied\").", items: [
      ["{\"command\":\"summary\"}", "✓ Whole miner: MHS av / MHS 20s (hashrate in MH/s), Accepted, Rejected, Hardware Errors, Best Share (since restart, in the miner's unit of 2^32 hashes; the app shows it x 2^32, as DATUM and mempool do), Elapsed (seconds since the software started), Device Hardware%, Pool Rejected%."],
      ["{\"command\":\"devs\"}", "✓ One entry per board (PGA 0–3): MHS av / 20s, Accepted, Rejected, Hardware Errors, clock, voltage (reads about 230 mV above the plan on an SC Lite), fan0–fan3 (RPM), tstemp-0/1/2 (temperatures), overheat, rebootcnt, estimate_hash_rate."],
      ["{\"command\":\"pools\"}", "✓ Each pool: URL, user, status, priority, accepted / rejected / stale, and the one in use."],
      ["{\"command\":\"stats\"}", "Per-board driver statistics."],
      ["{\"command\":\"version\"} · {\"command\":\"config\"}", "The software and API version (intminer 5.4.2); its configuration (pool strategy, devices, log interval)."],
      ["{\"command\":\"devdetails\"} · procs · procdetails · proccount · pgacount · coin · notify · check", "Device and processor details and counts, the coin / algorithm, the last device notices, and whether a command exists."],
      ["{\"command\":\"privileged\"}", "Says whether you have write access (from the network you don't)."],
      ["switchpool · enablepool · disablepool · addpool · removepool · poolpriority · poolquota", "✎ Pool changes. Refused from the network; use the web API (the Pools page)."],
      ["pgaenable · pgadisable · pgaidentify · pgaset · procenable · procdisable · procidentify · procset", "✎ Board on / off, blink a board, set a board value. Refused from the network."],
      ["restart · quit · save · setconfig · zero · debug · failover-only · devscan", "✎ Restart or stop the software, save its config, change settings, zero the counters. Refused from the network."],
    ]},
    { t: "Inside the firmware", where: "What the firmware does with a setting and what it logs. You can't send these yourself (the miner has no SSH); listed so the logs make sense.", items: [
      ["procset <board>,vfff,<PV>:<MHz>:<fan>:<fan>:<mV>", "What the firmware sends to the mining software for each board when a setting is applied."],
      ["intchains_qomo:vfff=0.43:750:100:100", "The start-up value in the mining software's own config. intchains_qomo is the driver for the boards' Intchains ICC590 chips: 46 per board, in 23 voltage levels of 2."],
      ["C3: nonce#… from 32.17  BAD(139/236)", "A bad result: board 3, chip 32, core 17, and 139 bad out of 236 results from that chip since the board started. The Miner page's \"bad results\" come from these."],
      ["C0: Chip Avgtemp 69'C, MaxTemp 76'C, CpbTemp 68.75'C", "Board 0's average and hottest chip, and its board sensor. MaxTemp − CpbTemp is what the \"hottest chip\" fan option adds."],
      ["watchdog dev0: read cpb voltage to 8980 success", "Board 0's measured voltage, about PV − 20 mV."],
      ["C0: DTFS chip(12) slowdown: …", "The firmware slowing one hot or failing chip down. Counted under chip health; not seen on a healthy SC Lite."],
      ["C0: chip(12) Individual: …", "A chip given its own clock. Also counted under chip health."],
      ["C0: DTFS chip(12) set clock to …Mhz Failed...", "A chip that didn't take its new clock."],
    ]},
  ];
  function copyText(text) {
    const old = () => {   // plain http on the LAN has no clipboard API: the classic way
      const ta = document.createElement("textarea"); ta.value = text; ta.style.cssText = "position:fixed;opacity:0;top:0;left:0";
      document.body.appendChild(ta); ta.select();
      let ok = false; try { ok = document.execCommand("copy"); } finally { ta.remove(); }
      if (!ok) throw new Error("copy refused");
    };
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text).catch(old);
    return new Promise(res => { old(); res(); });
  }
  function buildRef() {
    const view = $("viewSettings"); if (!view || $("sclRef")) return;
    const st = document.createElement("style");
    st.textContent = `
      #sclRef .ref-top { display: flex; flex-wrap: wrap; gap: .5rem .9rem; align-items: center; margin: 0 0 .6rem; }
      #sclRef .ref-top input[type=search] { flex: 1 1 14rem; min-width: 0; }
      #sclRef details { border-top: 1px solid var(--line, var(--border)); }
      #sclRef details:first-of-type { border-top: 0; }
      #sclRef summary { cursor: pointer; padding: .6rem 0; font-weight: 600; list-style-position: inside; }
      #sclRef summary .n { font-weight: 400; color: var(--muted); font-size: .8rem; margin-left: .4rem; }
      #sclRef .ref-where { font-size: .82rem; color: var(--muted); margin: 0 0 .5rem; }
      #sclRef .ref-item { display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr); gap: .3rem 1rem; padding: .45rem 0; border-top: 1px dashed var(--border); }
      #sclRef .ref-item:first-of-type { border-top: 0; }
      #sclRef .ref-cmd { position: relative; margin: 0; padding: .45rem 2.6rem .45rem .6rem; background: var(--panel2); border: 1px solid var(--border);
        border-radius: 8px; font: .76rem/1.45 var(--mono); white-space: pre-wrap; word-break: break-word; overflow-wrap: anywhere; }
      #sclRef .ref-cmd a { color: inherit; }
      #sclRef .ref-copy { position: absolute; top: .3rem; right: .3rem; padding: .15rem .4rem; font-size: .7rem; }
      #sclRef .ref-what { font-size: .84rem; line-height: 1.45; }
      #sclRef .ref-none { color: var(--muted); font-size: .85rem; }
      @media (max-width: 720px) { #sclRef .ref-item { grid-template-columns: 1fr; } }`;
    document.head.appendChild(st);
    const panel = document.createElement("div"); panel.className = "panel compact"; panel.id = "sclRef";
    panel.innerHTML = `<h2>Miner reference</h2>
      <p class="muted" style="margin:0 0 .5rem">What the miner itself understands and reports: its settings, web API, debug pages, port 4028 commands and log lines, with what each does. Taken from the SC Lite firmware (2.2.0). ✓ = this app uses it, ✎ = changes the miner, ⧉ copies.</p>
      <div class="ref-top">
        <input type="search" id="refFind" placeholder="Filter, e.g. restart, 4028, hwerr" />
        <button type="button" id="refAll">Open all</button></div>
      <div id="refBody"></div>`;
    view.appendChild(panel);
    const fill = (x) => x;
    const render = () => {
      const q = $("refFind").value.trim().toLowerCase();
      const open = new Set([...panel.querySelectorAll("details[open]")].map(d => d.dataset.g));
      const html = REF.map((g, gi) => {
        const items = g.items.filter(([c, w]) => !q || (c + " " + w + " " + g.t).toLowerCase().includes(q));
        if (!items.length) return "";
        return `<details data-g="${gi}"${q || open.has(String(gi)) ? " open" : ""}><summary>${esc(g.t)}<span class="n">${items.length}</span></summary>
          <p class="ref-where">${esc(fill(g.where))}</p>` + items.map(([c, w]) => {
            const cmd = fill(c);
            const body = esc(cmd);
            return `<div class="ref-item"><pre class="ref-cmd">${body}<button type="button" class="ref-copy" data-cmd="${esc(cmd)}" title="Copy">⧉</button></pre>
              <div class="ref-what">${esc(fill(w))}</div></div>`;
          }).join("") + `</details>`;
      }).join("");
      $("refBody").innerHTML = html || `<p class="ref-none">Nothing matches.</p>`;
    };
    render();
    $("refFind").oninput = render;
    $("refAll").onclick = () => {
      const ds = [...panel.querySelectorAll("#refBody details")], openAll = ds.some(d => !d.open);
      ds.forEach(d => (d.open = openAll)); $("refAll").textContent = openAll ? "Close all" : "Open all";
    };
    panel.addEventListener("click", (e) => {
      const b = e.target.closest(".ref-copy"); if (!b) return;
      copyText(b.dataset.cmd).then(() => toast("Copied")).catch(() => toast("Couldn't copy: select the text instead", true));
    });
  }

  // ------------------------------------------------------------ Settings: tidy the probe, Add miner from Fleet
  function tidySettings() {
    const issue = $("btnProbeCopyIssue");            // the GitHub issue draft pointed at the upstream project: gone
    if (issue) { const r = issue.closest(".row"); if (r) r.style.display = "none"; }
    const add = panelOf("addIp"), view = $("viewSettings");      // Add miner first: it's what Settings is mostly for
    if (add && view) view.insertBefore(add, view.firstChild);
    const probe = panelOf("btnProbe");
    if (probe) {
      const p = probe.querySelector("p");
      if (p) p.textContent = "Checks an address before you add it: which ports answer, the model and firmware, its settings format (SC Lite or HS Box), its pools and what it supports. Tick \"add if login ok\" to add it straight away.";
    }
  }
  function goAddMiner() {
    const t = $("tabSettings"); if (t) t.click();
    setTimeout(() => {
      const panel = panelOf("addIp"); if (!panel) return;
      panel.scrollIntoView({ behavior: "smooth", block: "center" });
      panel.classList.add("scl-flash"); setTimeout(() => panel.classList.remove("scl-flash"), 1800);
      const f = $("addId"); if (f) f.focus({ preventScroll: true });
    }, 60);
  }
  window.sclAddMiner = goAddMiner;

  // ------------------------------------------------------------ Settings: account (moved here from the header)
  function buildAccount() {
    const view = $("viewSettings"); if (!view || $("sclAccount")) return;
    const panel = document.createElement("div"); panel.className = "panel compact"; panel.id = "sclAccount";
    panel.innerHTML = `<h2>Account</h2>
      <p class="muted" style="margin:0 0 .5rem" id="accWho">Signed in.</p>
      <div class="row">
        <button type="button" onclick="location.href='/login.html'">Change login</button>
        <button type="button" onclick="location.href='/login.html#disclaimer'">Read the disclaimer</button>
        <button type="button" class="danger" id="accLogout">Log out</button>
      </div>`;
    view.appendChild(panel);
    fetch("/api/auth/status").then(r => r.json()).then(j => {
      if (!j.user) return;
      const ack = j.last_ack && j.last_ack.at ? String(j.last_ack.at).replace("T", " ").slice(0, 16) : "";
      $("accWho").innerHTML = `Signed in as <b>${esc(j.user)}</b>.` + (ack ? ` Disclaimer acknowledged ${esc(ack)}.` : "") +
        " One sign-in covers every page for 30 days.";
    }).catch(() => {});
    $("accLogout").onclick = async () => {
      if (!confirm("Log out of Blake2b ASIC Control on this browser?")) return;
      await fetch("/api/auth/logout", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }).catch(() => {});
      location.href = "/login.html";
    };
  }

  // ------------------------------------------------------------ Settings: notifications (Telegram bot, Discord webhook)
  let NT = null;
  async function loadNotify() { try { NT = await api("GET", "/api/notify/state"); } catch (e) { NT = null; } return NT; }
  // Settings: the app's time zone (schedules, restarts, logs and tuner files use it)
  function buildTz() {
    const view = $("viewSettings"); if (!view || $("sclTz")) return;
    const panel = document.createElement("div"); panel.className = "panel compact"; panel.id = "sclTz";
    const acc = $("sclAccount"); view.insertBefore(panel, acc || null);
    renderTz();
  }
  async function renderTz() {
    const panel = $("sclTz"); if (!panel) return;
    let t; try { t = await api("GET", "/api/settings/timezone"); } catch (e) { panel.innerHTML = `<h2>Time zone</h2><p class="muted">Couldn't load: ${esc(e.message)}</p>`; return; }
    let mine = ""; try { mine = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (e) { /* older browser */ }
    const off = mine && mine !== t.timezone && t.zones.includes(mine);
    panel.innerHTML = `<div class="row" style="justify-content:space-between"><h2 style="margin:0">Time zone</h2>
        <span class="pill">${esc(t.timezone)}</span></div>
      <p class="muted" style="margin:.3rem 0 .5rem;font-size:.85rem">Schedules, scheduled restarts, the logs and the tuner's files use the app's
        time zone. Now: <b style="color:var(--text)">${esc(t.now)}</b>.${t.can_set ? "" : " (Running on Windows: the computer's own time zone is used.)"}</p>
      ${off ? `<div class="note warn" style="margin:0 0 .5rem;font-size:.85rem">This browser is on <b>${esc(mine)}</b>, the app on <b>${esc(t.timezone)}</b>: a schedule runs on the app's clock.</div>` : ""}
      <div class="row" style="gap:.5rem;flex-wrap:wrap">
        <input id="tzIn" list="tzList" value="${esc(t.saved || "")}" placeholder="${esc(t.default)} (the default)" style="min-width:16rem" ${t.can_set ? "" : "disabled"} />
        <datalist id="tzList">${t.zones.map(z => `<option value="${esc(z)}">`).join("")}</datalist>
        ${mine && t.zones.includes(mine) ? `<button type="button" id="tzMine" ${t.can_set ? "" : "disabled"}>Use this browser's (${esc(mine)})</button>` : ""}
        <button type="button" class="primary" id="tzSave" ${t.can_set ? "" : "disabled"}>Save</button>
      </div>
      <p class="muted" style="margin:.4rem 0 0;font-size:.78rem">Empty goes back to ${esc(t.default)}. A schedule saved before a change keeps its times (09:00 stays 09:00, in the new zone).</p>`;
    const save = async (name) => {
      try { await api("POST", "/api/settings/timezone", { timezone: name }); toast(name ? `Time zone: ${name}` : "Time zone back to the default"); renderTz(); }
      catch (e) { toast(e.message, true); }
    };
    if ($("tzMine")) $("tzMine").onclick = () => save(mine);
    $("tzSave").onclick = () => save($("tzIn").value.trim());
  }

  // Settings: how best shares are shown (like DATUM and mempool, the miner's own number, or both)
  function buildScale() {
    const view = $("viewSettings"); if (!view || $("sclScale")) return;
    const panel = document.createElement("div"); panel.className = "panel compact"; panel.id = "sclScale";
    const acc = $("sclAccount"); view.insertBefore(panel, acc || null);
    renderScale();
  }
  async function renderScale() {
    const panel = $("sclScale"); if (!panel) return;
    let s; try { s = await api("GET", "/api/settings/share_scale"); } catch (e) { panel.innerHTML = `<h2>Best share numbers</h2><p class="muted">Couldn't load: ${esc(e.message)}</p>`; return; }
    const opt = (k, hint) => `<label class="scl-scale-opt" style="display:flex;gap:.5rem;align-items:baseline;margin:.25rem 0;cursor:pointer">
        <input type="radio" name="sclScale" value="${k}" ${s.share_scale === k ? "checked" : ""} style="width:auto;min-width:0" />
        <span><b>${esc(s.labels[k])}</b> <span class="mono" style="color:var(--text)">${esc(s.examples[k])}</span><br>
        <span class="muted" style="font-size:.8rem">${hint}</span></span></label>`;
    panel.innerHTML = `<div class="row" style="justify-content:space-between"><h2 style="margin:0">Best share numbers</h2>
        <span class="pill">${esc(s.labels[s.share_scale])}</span></div>
      <p class="muted" style="margin:.3rem 0 .5rem;font-size:.85rem">How best shares and records are shown on the Fleet cards, the Miner page,
        notifications and the widget. The miner counts share difficulty in units of 2<sup>32</sup> hashes; DATUM and mempool count in
        hashes. Only the display changes: the saved history stays as it is, so you can switch any time.</p>
      ${opt("hashes", "The same scale as DATUM's share difficulty and the network difficulty (for example 21.85E), so you can compare them.")}
      ${opt("miner", "The number the miner itself reports, as this app showed it before 1.15.5.")}
      ${opt("both", "DATUM's scale, with the miner's own number after it in brackets.")}`;
    panel.querySelectorAll('input[name="sclScale"]').forEach(r => r.onchange = async () => {
      try { await api("POST", "/api/settings/share_scale", { share_scale: r.value }); toast(`Best share numbers: ${s.labels[r.value]}`);
            await loadHw(); if (window.sclRefresh) window.sclRefresh(); renderScale(); }
      catch (e) { toast(e.message, true); renderScale(); }
    });
  }

  function buildNotify() {
    const view = $("viewSettings"); if (!view || $("sclNotify")) return;
    const panel = document.createElement("div"); panel.className = "panel compact"; panel.id = "sclNotify";
    const acc = $("sclAccount"); view.insertBefore(panel, acc || null);
    renderNotify();
  }
  async function renderNotify() {
    const panel = $("sclNotify"); if (!panel) return;
    const n = await loadNotify(); if (!n) { panel.innerHTML = `<h2>Notifications</h2><p class="muted">Couldn't load.</p>`; return; }
    const num = (k, v, unit) => `<input type="number" data-num="${k}" value="${esc(v)}" /> ${unit}`;
    const evExtra = { offline: () => `after ${num("offline_min", n.offline_min, "min")}`, hot: () => `at ${num("hot_c", n.hot_c, "°C")}`,
                      hashrate: () => `under ${num("low_pct", n.low_pct, "%")} of its usual for 10 min` };
    panel.innerHTML = `<div class="row" style="justify-content:space-between"><h2 style="margin:0">Notifications</h2>
        <span class="pill ${n.active.length ? "ok" : ""}">${n.active.length ? "sending to " + esc(n.active.join(" + ")) : "off"}</span></div>
      <p class="muted" style="margin:.3rem 0 .2rem;font-size:.85rem">Get a message on Telegram or Discord when something happens to a
        miner. Set up one or both, pick what to send, then switch notifications on for each miner (here or on its Miner page).
        Messages only go to Telegram's and Discord's own servers.</p>
      <div class="nt-grid">
        <div class="nt-box"><h3>Telegram <label class="switch"><input type="checkbox" id="ntTgOn" ${n.telegram.enabled ? "checked" : ""} /> on</label></h3>
          <label class="nt-f">Bot token <input type="password" id="ntTgTok" autocomplete="off" placeholder="${esc(n.telegram.token_hint || "123456789:AAE…")}" /></label>
          <label class="nt-f">Chat ID <input type="text" id="ntTgChat" value="${esc(n.telegram.chat_id)}" placeholder="123456789" /></label>
          <div class="row" style="gap:.4rem"><button type="button" id="ntTgFind">Find my chat ID</button><button type="button" id="ntTgTest">Send a test</button></div>
          <div id="ntTgChats" class="nt-help"></div>
          <p class="nt-help">1. In Telegram, message <b>@BotFather</b>, send <code>/newbot</code> and copy the token it gives you.
            2. Open your new bot and send it any message (or add it to a group and say hi there).
            3. Paste the token here, press <b>Find my chat ID</b> and pick your chat.</p></div>
        <div class="nt-box"><h3>Discord <label class="switch"><input type="checkbox" id="ntDcOn" ${n.discord.enabled ? "checked" : ""} /> on</label></h3>
          <label class="nt-f">Webhook URL <input type="password" id="ntDcUrl" autocomplete="off" placeholder="${esc(n.discord.webhook_hint || "https://discord.com/api/webhooks/…")}" /></label>
          <div class="row" style="gap:.4rem"><button type="button" id="ntDcTest">Send a test</button></div>
          <p class="nt-help">In Discord: the channel's <b>Edit Channel → Integrations → Webhooks → New Webhook → Copy Webhook URL</b>,
            then paste it here. Anyone with that URL can post to the channel, so keep it private.</p></div>
      </div>
      <h3 style="margin:.4rem 0 .2rem;font-size:.95rem">What to send</h3>
      <div>${n.event_list.map(e => `<div class="nt-ev"><label class="check"><input type="checkbox" data-ev="${esc(e.key)}" ${n.events[e.key] ? "checked" : ""} /> ${esc(e.label)}</label>
        ${evExtra[e.key] ? `<span class="muted">${evExtra[e.key]()}</span>` : ""}</div>`).join("")}</div>
      <div class="nt-ev"><span class="muted">The same alert for the same miner at most once every</span> ${num("cooldown_min", n.cooldown_min, "min")}
        <span class="muted">("back online" and "cooled down" always go through)</span></div>
      <h3 style="margin:.6rem 0 .2rem;font-size:.95rem">Miners</h3>
      <div class="nt-miners">${n.miners.map(m => `<label class="check"><input type="checkbox" data-nm="${esc(m.id)}" ${m.enabled ? "checked" : ""} /> ${esc(m.name)}</label>`).join("")
        || `<span class="muted">No miners yet.</span>`}</div>
      <div class="row" style="margin-top:.7rem"><button type="button" class="primary" id="ntSave">Save notifications</button></div>
      ${n.log.length ? `<details style="margin-top:.6rem"><summary class="muted">Last ${n.log.length} messages (sent or failed)</summary><div class="nt-log">${n.log.map(l =>
        `${esc(new Date(l.at * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }))} · ${esc(l.channel)} · ${l.ok ? "sent" : "FAILED " + esc(l.error)} · ${esc(l.text)}`).join("<br>")}</div></details>` : ""}`;
    const body = () => {
      const b = { telegram: { enabled: $("ntTgOn").checked, chat_id: $("ntTgChat").value.trim() }, discord: { enabled: $("ntDcOn").checked }, events: {} };
      if ($("ntTgTok").value.trim()) b.telegram.bot_token = $("ntTgTok").value.trim();
      if ($("ntDcUrl").value.trim()) b.discord.webhook_url = $("ntDcUrl").value.trim();
      panel.querySelectorAll("[data-ev]").forEach(x => b.events[x.dataset.ev] = x.checked);
      panel.querySelectorAll("[data-num]").forEach(x => b[x.dataset.num] = Number(x.value));
      return b;
    };
    const saveAll = async (quiet) => {
      await api("POST", "/api/notify/save", body());
      for (const x of panel.querySelectorAll("[data-nm]")) {
        const m = n.miners.find(y => y.id === x.dataset.nm);
        if (m && m.enabled !== x.checked) await api("POST", "/api/notify/miner", { miner_id: m.id, enabled: x.checked });
      }
      if (!quiet) toast("Notifications saved");
    };
    $("ntSave").onclick = async () => { try { await saveAll(); renderNotify(); loadHw(); } catch (e) { toast(e.message, true); } };
    const test = (ch) => async () => {
      try { await saveAll(true); await api("POST", "/api/notify/test", { channel: ch }); toast(`Test sent to ${ch}: check it arrived`); renderNotify(); }
      catch (e) { toast(e.message, true); }
    };
    $("ntTgTest").onclick = test("telegram"); $("ntDcTest").onclick = test("discord");
    $("ntTgFind").onclick = async () => {
      try {
        const j = await api("POST", "/api/notify/telegram_chats", { bot_token: $("ntTgTok").value.trim() });
        $("ntTgChats").innerHTML = j.chats.length ? "Pick your chat: " + j.chats.map(c => `<button type="button" data-chat="${esc(c.id)}">${esc(c.name || c.id)} (${esc(c.type)})</button>`).join(" ")
          : "No messages yet: send your bot a message in Telegram first, then press Find again.";
        $("ntTgChats").querySelectorAll("[data-chat]").forEach(b => b.onclick = () => { $("ntTgChat").value = b.dataset.chat; $("ntTgOn").checked = true; toast("Chat picked: press Save notifications (or Send a test)"); });
      } catch (e) { toast(e.message, true); }
    };
  }
  // Miner page: this miner's own notification switch
  function bellHtml(id) {
    const m = NT && NT.miners.find(x => x.id === id); if (!m) return "";
    const live = NT.active.length;
    return `<label class="scl-bell" title="${live ? "Send this miner's alerts to " + esc(NT.active.join(" + ")) : "Set up Telegram or Discord under Settings first"}">
      <input type="checkbox" class="scl-bell-in" ${m.enabled ? "checked" : ""} /> 🔔 Notifications${live ? "" : ' <span class="muted">(set up in Settings)</span>'}</label>`;
  }
  document.addEventListener("change", async (e) => {
    if (!e.target.classList || !e.target.classList.contains("scl-bell-in")) return;
    const id = detailId(); if (!id) return;
    try {
      await api("POST", "/api/notify/miner", { miner_id: id, enabled: e.target.checked });
      toast(e.target.checked ? `Notifications on for ${nameOf(id)}` : `Notifications off for ${nameOf(id)}`);
      await loadNotify(); if ($("sclNotify")) renderNotify();
    } catch (err) { toast(err.message, true); e.target.checked = !e.target.checked; }
  });

  // Fleet cards keep the tallest height they've had (until the window is resized): every refresh redraws the
  // cards, and a value that wraps onto one more or one fewer line would make the page jump and the browser flash
  // its scrollbar. Runs as the cards are put in, before the browser paints them.
  const cardMax = new Map();
  let cardW = window.innerWidth;
  function steadyCards() {
    const grid = $("fleetGrid"); if (!grid) return;
    if (window.innerWidth !== cardW) { cardW = window.innerWidth; cardMax.clear(); }
    [...grid.children].forEach((card, i) => {
      const idEl = card.querySelector("[data-id]"), key = (idEl && idEl.dataset.id) || "#" + i;
      card.style.minHeight = "";
      const h = card.offsetHeight, max = Math.max(h, cardMax.get(key) || 0);
      cardMax.set(key, max);
      if (max > h) card.style.minHeight = max + "px";
    });
  }
  setTimeout(function watchGrid() {
    const grid = $("fleetGrid"); if (!grid) return setTimeout(watchGrid, 500);
    new MutationObserver(steadyCards).observe(grid, { childList: true });
    steadyCards();
  }, 0);
  window.addEventListener("resize", () => { clearTimeout(steadyCards._r); steadyCards._r = setTimeout(steadyCards, 150); });

  // ------------------------------------------------------------ start
  buildQuickBar(); buildDetail(); buildDemo(); tidySettings(); buildAccount(); buildNotify(); buildTz(); buildScale(); buildRef();
  loadNotify().then(() => updateDetailInfo());
  loadHw(); setInterval(loadHw, 15000);
  let lastDetail = null;
  setInterval(() => {
    refreshBar();
    const id = detailId();
    if (detailOpen() && id && id !== lastDetail) { lastDetail = id; chipData = null; updateDetailInfo(); applyLocks(); loadChips(true); }
    if (!detailOpen()) lastDetail = null;
  }, 500);
  setInterval(() => { if (detailOpen()) loadChips(false); }, 60000);
})();
