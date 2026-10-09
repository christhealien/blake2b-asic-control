# Changelog

Every version of Blake2b ASIC Control, newest first. The Umbrel store shows the same notes, shorter,
under "What's new" (`releaseNotes` in `blake2b-asic-control/umbrel-app.yml`).

To release a version: put the number in `app/Dockerfile` (`B2AC_VERSION` and the version label), `blake2b-asic-control/docker-compose.yml`
and `blake2b-asic-control/umbrel-app.yml` (version, the `?v=` on the image links, and a release note),
add it here, commit, push, then push a `v<version>` tag: GitHub Actions builds and publishes the image
(see the README).

## 1.16.0 (2026-10-09)

- **SC Box / HS Box fan target** (`fan_addon.py`, Miner page in `addon.js`). Their firmware ignores the
  fan fields of the power plan and runs its own fan loop (every 6 s, logged in `/dbg/fanctrllog`), steering
  the fans so the control board's sensor holds `temp_target`, clamped to `temp_targets` (SC Box 65–75 °C,
  HS Box 70–80 °C). That target is the one fan setting these models take, so:
  - The probe stores it as `hardware.fan_target` (`value`, `min`, `max`).
  - `GET /api/hardware/fan_target?miner=` reads it live; `POST /api/hardware/fan_target`
    (`miner_id`, `target`) writes it: whole degrees inside the miner's own range, at least 60 s between two
    writes to one miner, only for plan format `box` with a fan target range (every other model gets an
    error and keeps the app's own fan control). The settings are read fresh and sent back with only
    `temp_target` changed (the stock page's Save resets a manual clock to the stock plan; this doesn't), then
    read again to confirm the miner kept it. The last write is kept in `miners.json` as `fan_target_set`.
  - The Miner page's fan panel shows a **Fan target** box for these models instead of the curve and fan %
    controls (still read only there); the info line shows the target and the range.
  - The auto-fan status for these models now says the firmware's own fan loop is in charge.
  - Tested by crProductGuy with `fan_target_test.py` and his own box tools: the target holds and the fan loop
    switches to it within seconds on both models; the SC Box's fans run near full speed for about 15 minutes
    after a settings write, the HS Box's didn't change speed.

## 1.15.9 (2026-10-08)

- **Efficiency in J/TH** instead of W/TH (the same number: watts per TH/s is joules per TH): Fleet card,
  Miner page power line, preset cards on Profiles, and the tuner's Finished tests table. The API field names
  (`w_per_th`) are unchanged. Gallery images re-captured.

## 1.15.8 (2026-10-08)

- **Widgets redesigned** (`widget_server.py`), checked at Umbrel's phone size (160 x 110) and desktop size
  (270 x 150):
  - **Miners** (list): the top line is the icon, name, status and preset; the bright line is the numbers,
    kept short enough for one line on a phone (`4.80 TH · 56° · best 945P`; best shares to three figures).
    Offline miners say "not answering"; an empty fleet says how to add one.
  - **Fleet health** (new, `two-stats-with-guage`, `/widgets/health`): hashrate against the sum of each
    online miner's 24-hour average from `hashrate.json` (full gauge = mining as usual), and the hottest board
    against the heat alert from Settings → Notifications (`notify.hot_c`; full gauge = at the alert).
  - **Overview** (four-stats): Hashrate, Online (`3/4`, "1 down"; the tuner's progress while a run is on),
    **Est. power** (replaces Hottest, which the health widget's gauge now shows), Best share. Est. power is the
    sum over the hashing miners of the Fleet card's estimate (rated watts at the stock plan, scaled by clock x
    PV^2, with the wall-reading calibration), at each SC Lite's own setting (read over its web API every 2
    minutes), the tuning step, or the preset. A read-only box with no stock plan on record counts at its rated
    watts. `1.77+ kW` means a hashing miner of unknown rated power isn't counted. Best share follows Settings →
    Best share numbers (one number; DATUM's when both are picked).
- **Auto fan control skips read-only models** (SC Box, HS Box): `_tick_one` reports "read only on this model:
  the firmware's own fan control is in charge" instead of writing. Tester data showed their fan fields don't
  hold and every write sets off a 10 to 20 minute fan spike.

## 1.15.7 (2026-10-07)

- **Fleet card, Best share numbers → Both:** best share and record on a line each, the numbers in one column
  (an inline grid), instead of "Best share 8.98P (2.09 M) · record 5.99E (1.4 G)" wrapping with the record's
  numbers pushed onto a ragged second line. The other two choices keep the single line. The shares card in
  `/api/hardware/list` and `/api/hardware/shares` gains `scale`, so the card knows which one is picked.

## 1.15.6 (2026-10-07)

- **Settings → Best share numbers** (`/api/settings/share_scale`, GET and POST; `share_scale` in
  `miners.json`, left out for the default): `hashes` (the default: x 2^32, like DATUM and mempool, `5.42P`),
  `miner` (the miner's own number as before 1.15.5, `1.26 M`) or `both` (`5.42P (1.26 M)`). It applies to
  everything that shows a share (`shares_addon.fmt`: Fleet card, Best share panel and top 10, notifications;
  the widget shows one number, DATUM's when both are picked). Read once at start and when changed; the
  saved history is untouched, so switching back and forth loses nothing.
- **New gallery and README images**, with best shares in the new format and realistic values.
- **Note on 1.15.5:** its `v1.15.5` tag was made on the 1.15.4 commit, so the 1.15.5 image is 1.15.4's code.
  1.15.6 has everything 1.15.5 was meant to have.

## 1.15.5 (2026-10-07)

- **Best share on DATUM's and mempool's scale.** The miner counts share difficulty in the old unit where
  difficulty 1 = 2^32 hashes: on an SC Lite, MHS av x Elapsed / Difficulty Accepted came to 4.287e9 (2^32 is
  4.295e9), and its shares of 16384 are the 70T shares DATUM shows (7.3P over 104 shares). DATUM and mempool
  show difficulty in hashes (the network's 21.85E is about 39.6 PH/s x 600 s). `shares_addon.fmt` now shows a
  share x 2^32 and in their style: two decimals at most, trailing zeros dropped, the letter right after the
  number (1261948 -> `5.42P`; it was `1.26 M`). Everything that shows a share uses it (Fleet card, Best share
  panel and top 10, notifications, widget), and stored values stay in the miner's unit, so the record and
  history are converted when shown. The Best share panel says which scale it's on.
- **Fleet: the page no longer jumps on refresh** (the browser's overlay scrollbar flashed each time). The
  Hashrate tile's "avg since restart …" line wrapped onto a second line or not depending on the numbers, so a
  refresh could make a card, its row and the page one line taller or shorter. That line now stays on one line
  (cut with "…", the full text on hover), and each card keeps the tallest height it has had (`steadyCards`, a
  MutationObserver on `#fleetGrid` that sets `min-height` before the browser paints; reset when the window
  width changes), so a value elsewhere changing length can't shrink the page either. Measured over 46 s of
  refreshes at 1400, 760 and 390 px: one growth the first time a card reached its full height, then no change.

## 1.15.4 (2026-10-07)

- **Fleet: the restart bar no longer blinks.** Every Fleet refresh redraws all the cards, and the card was drawn
  with the bar hidden and the Restart button enabled; a timer put the bar back up to 250 ms later, so it flickered
  on each refresh during a restart. The card is now drawn with the bar's current state (shown, its width and
  text, the button disabled), from the same `rbarState()` the 4-a-second timer uses (`paintRbar`, which only
  touches what changed). Measured over 16 s of a restart with a redraw every 0.7 s: 83 frames without the bar
  before, none after.

## 1.15.3 (2026-10-07)

- **For the StartOS package** (no change on Umbrel):
  - `python auth_addon.py set-login USERNAME` sets or replaces the login with the password read from
    stdin (never an argument: arguments show in process lists). The same checks as the page (1-64
    characters, no spaces; a password of 8 or more), only the salted PBKDF2 hash is written, and
    everyone is signed out. StartOS's "Set login password" action runs it with the service stopped.
  - `B2AC_PLATFORM=startos` shows "First time? Sign in as admin with the password from StartOS:
    Actions → Set login password" on the sign-in page (`/api/auth/status` gains `platform`).

## 1.15.2 (2026-10-07)

- **Hashrate graphs: a scale on both sides** (Fleet card and Miner page). Round levels 1, 2, 2.5 or 5
  x 10^n apart, at most 3 on a card and 5 on the Miner page (the step doubles until they fit), with as
  few decimals as the step needs; in TH/s, or GH/s when the miner's highest point is under 1 TH/s.
  Faint dotted level lines across the plot. The plot is inset by the width of the longest label on
  each side, and the time labels under it and the marks follow the plot's edges.
- **Download report: "Nothing private goes in the report"** under the button on the Profiles page,
  opening to the list of what's never read (pool and WiFi settings, the 4028 pool list), never written
  (password, token), taken out (MAC, IPs, URLs, e-mail, wallet- or token-like strings, private-named
  settings, text mentioning a pool, user, wallet or the network) and dropped from the log, and what
  stays. The button's hover text, the untested-model note and the toast after a download say it too.

## 1.15.1 (2026-10-07)

- **Schedule: a live "now" line** in place of the outlined block: a thin mark at the current time on
  every day (bold on today, with the time in a tab above the grid), placed inside the current block by
  the minute and moved every 15 s, on a resize and when the page comes back into view; it redraws the
  week when the day changes. On a phone (two lines a day) it sits on the line for that half of the day.
- **The grid's "today" and "now" follow the app's clock** (its UTC offset from the schedule state),
  not the browser's: with the browser in another time zone they were off by the difference. The
  monthly restart marks use the app's date too.

## 1.15.0 (2026-10-07)

- **Download report** (Profiles, next to Probe; `POST /api/hardware/report`, then
  `GET /api/hardware/report?miner=` for progress and `/api/hardware/report/download?miner=` for the
  zip, kept 30 minutes): a fresh probe, then the reads from crProductGuy's capture guide (port 4028
  `version`, `summary`, `devs`; `/mcb/status`, `/mcb/setting`, `/mcb/cgminer?cgminercmd=devs`,
  `/mcb/algosetting`, `/dbg/minerinfo`, `/dbg/icinfo`, `/cpb/hshistory`) plus `/dbg/fanctrllog` and,
  if ticked, `/dbg/minersyslog`. One request at a time, 2 s apart, all reads; refused while the miner
  is being tuned, probed or restarted. Never read: `/mcb/pools`, `/mcb/wifisetting`, the 4028 `pools`
  command.
- **What's taken out** (`report_addon.scrub`): MAC addresses (as `00:11:22:33:44:55`), IP addresses,
  URLs, e-mail addresses, any unbroken run of 24+ letters and digits (wallets, tokens, hashes), string
  values under private-sounding keys (user, pass, wallet, worker, pool, url, ssid, host, ip, mac,
  serial, ...) and any string value mentioning a pool, a user, a wallet or the network. From the
  miner's log, such lines are dropped whole (the summary says how many). The token stays in the
  request header and the password is never written. Tested against a capture with a planted wallet,
  pool URL, worker, IPs, MACs, host name and e-mail address: none came through.
- The zip has a README (what's in it, what was taken out, where to send it), `summary.txt` (what the
  app found and which reads answered), the app's own probe and one file per read (`.error.txt` for a
  read that failed: an error is a finding too). A miner that answers nothing gives an error, not an
  empty zip.
- A model the tuner hasn't been tested on says so on the Profiles page and points to the report.
- The image carries its version (`B2AC_VERSION`), so a report says which app made it.

## 1.14.0 (2026-10-06): first public release

- **Public on GitHub** (github.com/christhealien/blake2b-asic-control): the repo is the Umbrel community
  store, and the image is built by GitHub Actions for amd64 and arm64 (`.github/workflows/image.yml`) and
  pulled from `ghcr.io/christhealien/blake2b-asic-control`. Icon, gallery and changelog links point at
  GitHub.
- **Licensed MIT** (`LICENSE`). `THIRD_PARTY_NOTICES.md` carries the MIT notices of Maveth's
  web dashboard (now MIT, pinned at `92838b3`, which only adds its license files to `8775f51`) and
  crProductGuy's box tools; the README credits both, with links.
- **Time zone setting** (Settings → Time zone, `/api/settings/timezone`): schedules, restarts, logs and
  the tuner's files use the app's time zone. The compose file starts it on UTC (it was Asia/Tokyo); the
  zone you pick is kept in `miners.json` and applied to the app and the tuner runs it starts. The panel
  offers the browser's zone and says when the browser and the app differ.
- **Miner page: the 24-hour hashrate graph** between Live and Chips (wider, with 6-hour ticks), and the
  Live panel's Hashrate shows the latest 20-second reading with the average since restart under it, like
  the Fleet card.
- **Graph marks that are closer than a letter's width share one label** (for example "XP"), and the
  hover lists every event in it.
- **New gallery and README images** of what the app has now (test miners; real tuner results).
- Store text: the tagline now says what the app is for, Blake2b miners, the
  description covers everything up to this version, and the widget example no longer shows a LAN address.

## 1.13.1 (2026-10-06)

- **Fleet card Hashrate tile:** the miner's latest 20-second hashrate (from the 30-second poll), with its own
  average since the mining software started under it. Before, the tile was only that average ("MHS av"),
  which after a tuning run or a preset change mixes in every clock since the restart (4.36 TH/s on a miner
  running 5.15).
- **Hashrate graph:** the average in its corner says what it covers ("avg 24 h", or "avg 40 min" while it
  fills in).
- **Idle preset for miners probed before 1.13:** they're probed again by themselves (not while tuning), so
  the Idle preset appears without pressing Probe.

## 1.13.0 (2026-10-06)

- **Tuner: Rebuild the presets from the last search** (a third option on the Tuner page). Builds and tests
  High, Middle, Low and Lowest power again from the voltage curve the last search measured, at the four
  temperatures you give, without mapping the curve again. The search is read back from the miner's tuner
  CSV files (`search_source` in `tuner_addon.py`): its baseline, every passing setting, the confirmed best
  (High) and every chip's normal error rate from its baseline (the chips CSV). Choices:
  - **Fresh baseline first** (default): runs the search's baseline setting again, held where the search
    held it, so every chip's normal rate is today's. **Reuse the search's baseline**: no baseline run (warns
    if it's over 3 days old).
  - High gets its own test (a search reuses its confirm test), with the same fallbacks as the others: one
    voltage step more, then one clock step less.
  - The miner ends on what it ran before. The rebuilt presets replace only the ones it tested; a preset it
    couldn't pass keeps its old version (Profiles says so). If a rebuilt preset was on the miner, it shows as
    no preset until it's applied again (the miner still runs the old numbers).
  - `asic_tuner.py --presets-only --source FILE --preset-temps 60,64,68,72` (and `--baseline-min 0` to reuse
    the saved baseline). Every run's options are now kept (`asic_tuner_runs.json`) so a later rebuild knows
    what a search used.
- **Rejected shares** (new `rejects_addon.py`, on the Miner page under Best share, and on the Fleet card when
  it matters). The best-share poll's summary reading counts accepted and rejected shares per hour (8 days
  kept). When the rejected count goes up, the miner's log is read (at most every 5 minutes) and each new
  `Rejected` line is sorted: **stale** only for exactly `(stale-prevblk)`, anything else (or no reason) is
  **not stale**. Rejects the log doesn't show within 15 minutes (the miner trims its log) are "not found in
  the miner's log", never guessed. Shown: since restart, the last 24 h ("all stale · 0.16 % of accepted",
  "1 NOT STALE (last at 11:03) · 3 stale", "above this miner's usual 0.07 %" at 1 % and 20+ rejects), and a
  3-day strip (tick = stale, red triangle = not stale, ? = not found). A not-stale reject shows on the Fleet
  card and sends a notification (new kind, on by default). Only the short reason word is kept from the log.
  Design from crProductGuy's stale-share proposal for gbox (MIT); this is a separate implementation.
- **Fleet cards: a 24-hour hashrate graph** where the Working pool tile was (the pool is in the card's pool
  list). A point every 5 minutes from the miner's 20-second hashrate (new `hashrate_addon.py`, kept in
  `hashrate.json` so it survives an app restart), gaps where there were no readings, and marks: **R** its
  mining software started again, **P** the setting changed (preset, schedule or by hand), **T** a tuning run
  started, **X** a share rejected that wasn't stale. Hover a mark for when and what.
- **Idle preset** (☾ on the Fleet card, a card on Profiles, a brush on Schedule): the firmware's own Idle mode
  (level 3, "0 MHz 0 V", the same as Idle on the miner's own Miner settings page): the hash boards stop.
  Added automatically to every SC Lite whose probe finds the Idle plan in `/mcb/setting`; it writes manual
  off and selects that plan, leaving the manual plan as it is, and any other preset wakes it (they write
  manual on, select 0 and their plan). It can't be edited or deleted. While a miner is idle, notifications
  don't send "hashrate low", "not answering" or "restarted on its own", and the power estimate says idle.
- **Best share:** the Miner page table shows the top 10 (the best since restart stays in view if it's
  smaller); the 20 biggest are still kept.
- **Fix:** the start-up permission tightening could follow a symbolic link, and with `SCLITE_TUNER_DATA`
  unset it worked on the current folder. It now only touches the tuner data folder when that's set, and
  never goes through a link (the Docker image and run-local always set it).

## 1.12.1 (2026-10-06)

- **Tuner: sign-in check before a run.** A run on SClite_1 stopped after 5 minutes because the miner
  answered every fresh sign-in (`/user/login`) with HTTP 500, while the dashboard kept showing it on its
  existing session. Start now signs in fresh first (the way the tuner will) and refuses to start if that
  fails, saying what the miner replied and what to try (sign in on the miner's own page; if that fails,
  power-cycle the miner; or fix its password in the app).
- **Tuner log:** an HTTP error from the miner now includes the miner's reply text (the first 160
  characters), so the log says why, not just "500 Internal Server Error".

## 1.12.0 (2026-10-05)

- **Notifications** (Settings → Notifications, new `notify_addon.py`):
  - **Telegram:** paste a bot token from @BotFather, send your bot a message, press **Find my chat ID** and
    pick the chat (it lists the chats that wrote to the bot). **Discord:** paste a channel webhook URL.
    Either or both, each with its own switch and a **Send a test** button.
  - **What to send**, each switched on or off: a miner stops answering for N min (default 5) and when it's
    back; too hot (hottest board at N °C, default 80; again when it's 5 °C cooler); hashrate low (under
    N %, default 70, of what that miner usually does at that clock and preset, for 10 minutes; never in
    the first 15 minutes after a restart or while tuning); restarted on its own (uptime went back without
    the app restarting it); restarts the app started, done or failed (off by default); a scheduled preset
    change or restart that didn't go through; a tuning run finished, with its result; a new best-share
    record; a chip turned weak.
  - The same alert for the same miner goes out at most once per cooldown (default 30 min); "back online",
    "cooled down" and "hashrate back" always go through.
  - **Each miner on or off:** in Settings, or with the new 🔔 Notifications switch on its Miner page.
  - The watcher reads each notified miner's port 4028 status every 30 s (read only; no extra load on the
    web page). The last 20 messages (sent or failed, with the reason) are listed under the section.
  - **Security:** the only places this talks to are api.telegram.org and discord.com, and only once you set
    them up. Bot tokens and webhook URLs are checked for their real form, kept in `miners.json` (readable by
    the app only) and never sent back to the page (just their last 4 characters). Discord messages can't
    ping anyone (mentions are off).

## 1.11.3 (2026-10-05)

- **The rest of the time: nothing.** "The rest of the time" (under the brushes) now has a first choice,
  **nothing: back to what it ran before**, and it's the default for a new schedule. Only the blocks you
  paint change anything. When a painted stretch starts, the schedule remembers what the miner was on (its
  preset, or else its hand-set clock / voltage / PV, and its fan control); when the stretch ends, it puts
  that back. Unpainted blocks stay empty on the week. In the Changes list this is the "back to what it ran
  before" line at the end of each stretch.
- In the unpainted time the schedule isn't in charge of the miner, so the clock and fan panels aren't
  locked by it then; a change you make there is what it goes back to after the next painted stretch.
- Schedules saved before this keep their most-used preset as the rest of the time.

## 1.11.2 (2026-10-05)

- **One click paints one block.** Before, the first block painted on an empty week filled the whole week
  with that preset (a schedule needs a preset at every moment). Now the blocks you don't paint run **the
  rest of the time** preset, picked under the brushes (it starts as High, or the next one if you're
  painting High) and shown lighter on the week. A click paints one block, a drag paints the rectangle
  from the first block (drag down for several days). Changing "the rest of the time" changes all the
  unpainted blocks. Schedules saved before this pick their most-used preset as the rest of the time.
- **↻ Restart works like the presets:** it's always in the Paint with row. Click a block to mark a
  restart at its start, or drag to mark several (they're kept at least an hour apart, so a drag across
  three hours marks three). Starting a drag (or a click) on a mark removes instead. Marks and a repeat
  (every hour ... every 3 months) now work together: the repeat is the **Repeat** dropdown in the
  Restarts section, and "At the blocks I mark" is gone from it (saved marks are kept).
- **⌫ Erase:** click or drag to clear blocks: painted presets go back to the rest of the time, and
  restart marks are removed.
- **Fill the whole week** fills with what's picked: a preset, Erase (clears everything, after asking), or
  ↻ Restart: a restart every hour of the week (168), only after a warning that says what it costs
  (about 3% less mining on your SC Lite, and the best share resets every hour).
- Presets whose two-letter marks would be the same (Low and Lowest) get different ones (LO and LT).
- Up to 168 marked restarts a week (one an hour).

## 1.11.1 (2026-10-05)

- **Restart brush first, then Fill:** the ↻ Restart brush now sits right after the presets, and **Fill the
  whole week** comes last and is hidden while the Restart brush is picked. (In 1.11.0, Fill with the Restart
  brush picked tried to save the brush as a preset, which the save refused.)
- **Marked restarts at least an hour apart**, on the page and on the server, so a week can't be marked
  with a restart every half hour.
- **What restarts cost:** the Restarts section says how many restarts a day the choice means, how long
  each stops hashing (this miner's measured restart time, plus about a minute to get back to full
  speed), the share of mining that costs, and that the best share since restart starts again each time.
  From about 4 a day (every 6 hours or more often) it's a warning: once a day or once a week is plenty.

## 1.11.0 (2026-10-05)

- **Scheduled restarts** (Schedule page, new **Restarts** section, per miner):
  - **Restart** every hour (at a minute past the hour), every 6 or 12 hours (from a time), every day,
    every week (a day and time), every month or every 3 months (a day 1–28 and time), or **at the blocks
    I mark**: pick the **↻ Restart** brush in the Paint with row and click half-hour blocks on the week
    (click again to remove; up to 48, each repeating weekly at the start of its block).
  - Every scheduled restart in the shown week appears as a ringed ↻ block, and its time is in the block's
    tooltip. The section shows the saved rule, the next restart (with the date) and how the last one went.
    The Fleet card's schedule line adds "restart Wed 04:00".
  - It's the same restart as the Restart button (with the progress bar on the Fleet card). It's separate
    from the preset changes: it runs whether those are on, off or paused, and works for a miner with no
    presets. A restart that comes due while the miner is being tuned is skipped, and one missed by more
    than 10 minutes (the app wasn't running) is skipped too, rather than restarting late. Saving never
    triggers a restart that was due before you saved.
  - **Copy to other miners** copies the restart too. **Clear preset changes** (was "Clear schedule")
    leaves the restart as it is; set Restart to Off to remove it.

## 1.10.4 (2026-10-05)

From the second full voltage-curve run on the real SC Lite (500–700 MHz, Oct 4–5, the first on 1.9.9+).

- **What 1.9.9 changed, on real hardware:** the curve came out the same as the first run (500, 525,
  550 at 8800 mV, then 575 / 9000, 600 / 9100, 625 / 9200, 650 / 9300, 675 / 9400; 700 not clean at
  9400), except 550 now at 8800 instead of 8900. Failures by bad luck (a chip other than the weak
  b3c32, passing its retest) fell from 6 to 1 in about 30 tests. The 675 / 9400 winner passed its
  90-minute confirm (it lost it to one extra error last time). Presets came out High 675 / 9400,
  Middle 625 / 9200, Low 550 / 8800, Lowest 500 / 8800. Four clear failures weren't retested, and
  the whole run took about 19.7 h against 23.8 h.
- **A clock starts at most one voltage step above the clock before.** 575 MHz needed +200 mV over
  550 (550 only just passed at 8800 on its retest), so 600 started at 9200, passed it, then found
  9100 clean on the way down: a 45-minute test that told it nothing. Starting too high always costs a
  full passing test; starting one step too low costs a failed test, which usually ends early.

## 1.10.3 (2026-10-04)

- **Best-share table order:** biggest first, down to the smallest (# 1 is the record, marked ★), then the
  setting it was found on, with when it was found in the last column on the right. (1.10.2 listed them
  newest first with the time first.)

## 1.10.2 (2026-10-04)

- **Best share: every new best is its own line.** The Miner page's best-share table kept one line per
  restart (the best of that restart), so a bigger share later in the same restart replaced the earlier
  one: an 800 M found yesterday disappeared when the same run found 1.4 G today. Now each new best since
  restart is logged when it's found, with the clock / preset it was on, and never replaces an earlier
  line. The table is newest first, so you can follow the progress; # is the size rank and ★ the record.
  The 20 biggest are kept (was 10). Shares already overwritten before this version can't be recovered.
- **"On" stays with the share.** Because a restart had one line, each bigger share rewrote that line's
  value, time and "on" too, so a share found on Low showed High once a later, bigger share came on High.
  Each line now keeps the setting from the reading that saw it, for good. The miner doesn't say when it
  found a share (the app reads it every 30 s), so: if the preset changed between two readings the line
  says both ("Low or High"), and if the app wasn't reading for a while (stopped, updating, miner
  offline) the Found column shows the window the share fell in instead of a single time.

## 1.10.1 (2026-10-04)

From crProductGuy's read-only captures of his SC BOX (40.40.HA, MCB_V5_4, fw 2.2.5) and HS BOX (10.10.SA,
MCB_V5_4, fw 2.2.6), taken with the read-only capture script and replayed here against a fake miner serving
the captured replies.

- **What a box really runs:** the probe now works out the running plan from `/mcb/setting` the way the
  firmware does: `manualPowerplan` when `manual` is true, otherwise the selected level of the stock plans,
  of the running algorithm (`algoname`) on the HS BOX, which keeps one plan list per algorithm. The HS BOX
  captured runs its Blake2b stock plan, 850 MHz / 0.44 V (port 4028 agrees), while its `manualPowerplan`
  holds the HNS-algorithm plan, 750 MHz / 0.41 V. 1.10.0 would have shown the HNS-algorithm plan; it was only
  saved from acting on it because box plans are read only. The stock plan is now also taken from the
  running algorithm's list (1.10.0 took the first level 0 it found: the HNS one).
- **Miner page on a box:** "Stock 725 MHz · 0.41 V" and "Runs 525 MHz · 0.41 V (manual setting)", with
  the algorithm on an HS BOX. The line under the clock panel says what really runs instead of printing
  `manualPowerplan`.
- **Chip health on boxes:** their logs carry almost no per-chip bad-result lines (one in ten hours on the
  HS BOX, none on the SC BOX), so on a board without them a chip is marked **watch** at 10+ hardware
  errors that are 5x the board's typical chip and at least 4 standard deviations out (never weak on this
  alone). The chip map then shades by HW errors. On the HS BOX that flags chip 16 (23 errors against a
  typical 2). The SC BOX's log writes chip temperatures without `CpbTemp`; those lines are read now too.
- **Pacing:** the SC BOX's web backend crashes under request bursts (crProductGuy's capture had to wait 3 s
  between requests). Every web-API request now goes one at a time per miner address, however many parts
  of the app ask: 0.2 s apart for a probed SC Lite, 1 s for boxes and for miners not identified yet.
  Replayed against the captures: no overlapping requests, none closer than 1 s, about 15–20 a minute with
  the Miner page open.
- **Read only on boxes, enforced:** clock, voltage and fan writes (the clock panel, set fan once, auto fan,
  fleet actions) are refused for a plan the app can't write, with that reason, and the Miner page's clock
  and fan controls are greyed out with a note. Underneath, `set_fan_bias` / `set_plan` now refuse any
  plan that isn't in the SC Lite form, so no path can write an SC Lite-style plan to a box (on an HS BOX
  that would have meant writing the other algorithm's clock). Restart, pools and temperature control
  still work.
- Not changed: tuning, presets and hand-set clocks on boxes. They need the write format tested on a real
  unit first (see the README).

## 1.10.0 (2026-10-04)

From a tester's review (crProductGuy, plain Docker on Ubuntu, with an SC BOX and an HS BOX). His SC BOX was
bought second-hand and can't run its stock 725 MHz (it restarts every few seconds there); it runs
steadily at 525. Before this version the tuner always ran the baseline at stock and put stock back on
every abort, stop or "nothing passed", so on a unit like that a failed run would have left it on the
one setting it can't run.

- **Back to what it ran before, not to stock:** the tuner reads the live setting before its first write.
  If nothing is confirmed, or the run stops early (Stop, a safety stop, an error before the confirm), the
  miner goes back to that. Only a setting that passed the confirm (or the curve, with confirm off)
  replaces it. The tuner's last line says `RESTORED:` instead of `BEST:` when nothing new was confirmed.
- **Choose the baseline** (Tuner, section 3): **Stock** (the default, as before), **What it runs now**,
  or **My own** clock and voltage. The hard limits are still worked out from the stock setting, and the
  baseline must be inside them. Lowest MHz defaults to 100 MHz below the baseline. `asic_tuner.py` has
  `--baseline-current` for the same from the command line.
- **A board reset ends the baseline at once.** Before, it was only acted on after the whole baseline
  (60 minutes by default) at a setting that kept resetting.
- **A warning when the miner runs 50 MHz or more below stock**, in the review and the tuner's log:
  it was probably turned down for a reason, and a stock baseline would run it at stock first.
- **SC BOX / HS BOX power-plan format:** these write `725 MHz 0.41 V 70 RPM 70 RPM` (decimal volts, no
  PV), and the HS BOX nests its plans per algorithm. The app still can't change clock or voltage in
  that format, but it now says so (on the Tuner page, the clock panel and presets) instead of "the stock
  setting isn't known yet, press Probe". The probe records the format, the stock plan as the miner
  writes it and the live setting. A stock setting outside the app's sanity band is now described as a
  limit of the app for that model, not a fault in the miner, and an unreadable one is told apart from it.
- **Security:**
  - `X-Forwarded-For` is only trusted with `SCLITE_BEHIND_PROXY=1`, which the Umbrel compose file sets
    for app_proxy. In plain Docker the login back-off goes by the connection's own address, so a client
    can't make up a fresh address for every guess.
  - The delay after a wrong password is now outside the sign-in lock, so one client's wrong guesses
    don't hold up everyone else's sign-in (the password check itself is still one at a time).
  - Passwords are stripped from every JSON reply by key name, at any depth, not only from miner lists.

## 1.9.9 (2026-10-04)

From the first full voltage-curve run on a real SC Lite (500–700 MHz, about 24 h). The curve itself
came out clean (500 and 525 MHz at 8800 mV, then +100 mV per 25 MHz up to 675 MHz at 9400 mV; 700 MHz
not clean at 9400), and every real failure was the same weak chip, board 3 chip 32. But about one test
in four failed by bad luck: a random chip a single error over its limit, which then passed its retest.
In the confirm and preset steps that cost real results, because they had no retest: the 675 MHz winner
lost its confirm to one extra error on another chip (10 against 9.8 over 90 minutes), and Low dropped
from 550 to 525 MHz.

- **Limits that account for all the chips:** each chip's limit is now the most errors a clean chip makes
  by chance (Poisson, from its own baseline rate), with the false-fail chance shared out between all
  184 chips (and between the boards for board totals). At the default sensitivity 2.5, a good setting
  fails by luck about 1 test in 20 (lower is stricter: 2.0 about 1 in 5; 3.0 about 1 in 100). Replayed
  on this run, the weak chip's failures are still far over the new limits (projected 14 to 200 errors
  against 13 to 15 allowed), while the chance failures land at or near them, so most would pass and the
  rest now get their retest. The limit is still the most errors allowed in the whole test, so a bad
  setting still fails the moment it crosses it.
- **Confirm and presets retest a close call** once, like the curve does. A clear failure (over the whole
  limit in under half the test) still isn't retested.
- **Presets step down to the measured voltage:** when Middle or Low fails and tries one clock lower, it
  uses the curve's lowest clean voltage at that clock. Before, it added 100 mV (525 at 9000 when the
  curve had 525 clean at 8800).
- **The curve skips tests that could only fail:**
  - It doesn't retest a voltage that just failed at the same clock on the way up. That was an hour on
    this run: 550 at 8800 twice more after 8900 passed.
  - It doesn't test a voltage a slower clock already failed at, since a faster clock needs at least as
    much. That was about 30 minutes on this run.
- On this run, these changes would have saved roughly 3 hours with the same curve. High would most
  likely have been 675 / 9400 and Low 550 / 8900.

## 1.9.8 (2026-10-03): security update

From a full security review of 1.9.7. Recommended for everyone.

- **Hardware limits on the server for every path:** the Miner page's clock / voltage panel (and the
  same action from Fleet) accepted any MHz, mV, PV or fan value once "unlock" was ticked, and saved
  presets were only checked when saved. Now every clock, voltage and PV is checked against the miner's
  hard limits (around its own stock setting) when it's set, when a preset is saved and again when it's
  applied, including by a schedule. Fans can't be set below 20 %. Nothing is allowed until the miner's
  stock setting has been read, and a miner reporting an implausible stock setting can't widen its own
  limits.
- **No script injection from miners:** a few values a miner reports (pool number, accepted / rejected /
  HW error counts, board id and status, error messages) were put into the page as HTML. They're now
  escaped in the page, and everything a miner reports is also cleaned on the server. A miner-reported
  model name can no longer end up in a miner id.
- **Other sites and apps can't act through your login (CSRF):** every change now needs a header only
  this app's own pages send. Before, a page from another app on the same Umbrel (browsers treat every
  port on umbrel.local as the same site) could make changes using your session.
- **A crafted miner log can't freeze the app:** the chip-health log parser could be made to take hours
  (blocking the fan control with it). Its patterns are now bounded and anchored, and only the newest
  4 MB of a log is read. Replies from miners are capped in size and time (web API 8 MB, port 4028
  1 MB / 10 s).
- **Sign-in:**
  - Wrong passwords back off per device (up to 5 minutes), password checks run one at a time so
    parallel guesses can't slip through, and there's an overall cap of 30 wrong guesses a minute.
  - Changing the password now has the same pace.
  - Passwords are hashed with 600,000 PBKDF2 rounds (older hashes upgrade at the next sign-in).
  - Resetting or changing the login ends every old session.
  - Two first-time setups can't race.
  - A non-ASCII username no longer drops the connection.
  - The sign-in page can no longer be used to redirect you to another site after you sign in
    (`next=/\evil` and similar).
  - `HEAD` requests need a sign-in like `GET`.
- **Response headers:** pages can't be framed by other sites or apps (clickjacking), and MIME sniffing
  and referrers are off.
- **miners.json** (it holds your miners' passwords) is written atomically (a crash or two writers at
  once can't leave it half-written) and readable by the app only, like the other data files. Existing
  files are tightened on start. Responses no longer include miners' passwords.
- **Probe and add miner** only accept a plain IPv4 address or host name, so they can't be pointed at
  other services (`host:port`, paths, this machine or link-local addresses).
- **Requests:** bodies are capped at 1 MB, and idle connections are dropped after a minute.
- **Tuning locks:** "Switch to pool", pool reordering and promoting a pool (they restart the miner) are
  also locked while a miner is being tuned.
- **Container:**
  - The image runs as user 1000.
  - `git` is removed after the build.
  - pycryptodome is held to 3.19.1 or newer within 3.x.
  - A crash of the dashboard now makes Docker restart the app, instead of leaving it stopped.
  - Data written by the app is owner-only (`umask 077`).
- **Repo:**
  - `.dockerignore` keeps local runs and data out of the build.
  - `.gitignore` covers zips and data files.
  - TESTING.md's Docker example listens on this computer only and sets a login.

## 1.9.7 (2026-10-02)

- **Light theme:** the reading tiles (Hashrate, Temp max, Fans, Uptime, Working pool, Power, Chips on
  the Fleet cards, and the Miner page's Live panel) were almost the same grey as the card behind them.
  They're now a clearly darker grey with darker labels, matching how the dark theme's lighter tiles stand
  out on black.

## 1.9.6 (2026-10-02)

- **The tuner maps every clock and voltage ("voltage curve"),** replacing the climb, the trim at the best
  clock and the clock map. Before, the climb only ever raised voltage and only the best clock was trimmed,
  so the clocks in between kept whatever voltage the climb used, and lower clocks were assumed fine at a
  faster clock's voltage. Now:
  - After the baseline (the stock setting, as before) it starts at **Lowest MHz** (default 100 MHz below
    stock, so 500 on an SC Lite) and works up one step at a time to **Highest MHz**, covering
    underclocking and overclocking in one run.
  - At each clock it finds the **lowest clean voltage**: the lowest clock starts at the stock voltage,
    every higher clock at the voltage that was lowest one clock slower (plus what the last step needed).
    If that isn't clean it adds voltage a step at a time (up to Highest mV), then lowers it a step at a
    time until it fails (down to Lowest mV; two failures in a row, or one at a voltage a slower clock
    failed at too).
  - **Every voltage is tested at its own clock;** nothing is assumed from another clock.
  - The curve ends at the first clock that isn't clean at any allowed voltage; the fastest clean clock is
    confirmed with the longer test.
  - **Presets come from the measured curve:** High is the best clock, Lowest power the Lowest MHz, and
    Middle and Low are spread evenly between them, each at the voltage measured at its own clock.
  - The Run plan shows one step, **Voltage curve**, with a line per clock (its tries and lowest clean
    voltage), and the log ends with the whole curve (`VOLTAGE CURVE: 500 MHz 8700 mV, …`).
- **Tuner page, section 4** is now "Clocks and voltages to map": Lowest / Highest MHz, MHz per step,
  Lowest / Highest mV and mV per step, plus "Find the lowest voltage at every clock" (off: voltage is only
  raised until clean, a quicker, rougher curve). Lowest MHz is always set now. The time estimate, review
  and Run plan describe the curve.
- A Highest MHz off the step grid (690 from 500 in 25s) is still tested as the last clock.

## 1.9.5 (2026-10-02)

From real SC Lite tuning runs (Oct 1): every failure was the same chip, board 3 chip 32, and it
always failed hard at one voltage and was clean 100 mV higher. 650 MHz needed 9200 mV, 675 needed 9300
and 700 needed 9400: about +100 mV per 25 MHz step. So:

- **No retest after a decisive failure:** a setting that goes over its whole test's limit in under half
  the test (at least twice the allowed error rate) is no longer retested, since a retest only repeats it.
  Close calls still get their retest. `--retest-all` brings back the old behaviour.
- **The climb predicts the voltage:** each next clock starts with the extra voltage the last clock
  needed (650 at 9200 → 675 starts at 9300), instead of first failing twice at the old voltage. The trim
  at the end still finds the lowest clean voltage. `--no-predict-mv` turns it off.
- Together, on the last full run (8.4 h) they'd have skipped 5 tests, about 1.4 hours.
- The Run plan notes a skipped retest, and the review explains both.
- **Pools:** "Failback" (network jargon, easy to read as "fallback", which means the opposite) is now
  **Switch to pool** on the Miner page (with the pool number next to it) and **Back to pool 0** on the
  Fleet quick bar, with plain confirmations.

## 1.9.4 (2026-10-02)

- **Chip health judged against chance:** a chip is only flagged when the difference is far bigger than
  random scatter, so healthy chips stop showing up as weak.
  - **Bad results** (the real sign of a failing chip): weak at 10%+ bad and watch at 3%+, but only with
    50+ results and when its share is clearly above the board's typical chip (4 and 3 standard
    deviations). Fewer results say "too few to judge".
  - **Perf** is a count, so it scatters, a lot after a restart: 43 against a typical 55 is only about
    1.6 standard deviations, ordinary luck. A chip is watch for being slow only when it's 15%+ below its
    board *and* 4 standard deviations out, and weak only when it does under half the work. Before, any
    chip 15% below the board's typical perf was weak, however small the numbers.
  - "Shade by Perf" now shades by how sure it is that a chip is slower (normal scatter is left blank).
  - Hovering a healthy chip says so, and why a low number isn't a worry. "How is a chip judged?" under
    the map explains all of it.
- **Fleet:** a "+ Add miner" button in the quick bar opens Settings at Add miner, with the cursor in the
  first field.
- **Settings:** Add miner is now the first box. The probe no longer offers a GitHub issue draft or an
  "Open new issue" link (they pointed at the upstream project, not yours). The Account box moved down,
  above the Miner reference.

## 1.9.3 (2026-10-02)

- **Restart progress bar on the Fleet cards:** after Restart (or a pool switch / failback, which also
  restart) a bar under the card's buttons counts up against how long this miner's restarts usually take,
  with what's happening and the seconds: sending the restart, restarting (not answering yet), back up and
  waiting for hashing, then a green "hashing again after N s" that fades after a few seconds. The app keeps
  each miner's last 5 restart times (`restart_times.json`) and uses the typical one (75 s until it has
  one), so the bar matches your miner. If a restart runs long the bar slows near the end instead of
  stopping, and says it's slower than usual. Restart is greyed out while it runs.
- **Miner page layout**, top to bottom:
  - **Live** across the full width (all eight readings in one row on a wide screen).
  - **Chips** full width, with the same squares as the Tuner's map (in 1.9.0 it was squeezed into tiny
    dots in the left column). Each board's chip temperatures, sensor and voltage sit in columns above
    it, and each board's errors and share of bad results under its name.
  - **Boards, Pools and Add pool** beside **Best share**.
  - **Fan** beside **Clock / voltage** at the bottom.
  - The two boxes in each row end level, so there are no gaps. On a phone everything stacks in that
    order.
  - **Remove from registry** is a small button at the top right, next to the miner's name, instead of a
    box of its own.

## 1.9.0 (2026-10-02)

- **Tuner baseline is always the firmware's stock setting.** The probe reads it from the miner, the
  Baseline section shows it read-only, and a run can't start until the miner has been probed. Its PV is
  the stock PV, so every result is compared with how the miner runs out of the box.
- **Tuner accuracy:**
  - **Setting check:** after every change the tuner reads the setting back from the miner and the
    boards' clock from port 4028. It re-applies once if it didn't take; if the miner still reports a
    different setting the run stops (a board clock that differs is only a warning in the log).
  - **Hashrate check:** a test whose chips are clean still fails if its hashrate is more than 4% below
    what the clock should give (the baseline's TH/s × the clock ratio).
  - **Longer final confirm:** twice the test length by default, or set it (Confirm minutes).
  - **12.5 MHz grid:** clocks run in 12.5 MHz steps (615 runs at 612.5). The climb and map start from
    the first step above / below stock, the review warns about clocks off the grid, and presets show the
    clock they really run.
  - **Noisy chips:** a chip already well above the others in the baseline (8× the median and 20+
    errors an hour) fails a step only above 1.5× its own rate, and board totals leave it out, so one
    damaged chip can't push the voltage up for all of them. It can be turned off in section 6.
- **Full voltage trim:** it now tests every voltage step down to "down to" (including ones that
  failed during the climb), and stops after two fails in a row.
- **Lowest MHz:** new in section 4. Set it below the baseline (say 500) and, after the trim, the tuner
  maps every clock step down to it with the lowest clean voltage at each. Lowest power then uses it and
  the four presets make an even ladder. (Replaces the Presets section's Lowest preset MHz / mV.)
- **Finished tests** show the estimated watts and W/TH for each test.
- **Downloads:** the tuner log (.txt), full log, results, chip errors, live readings, run plan,
  presets, or all of it as a zip, from the Tuner page.
- **Miner page, chip health:**
  - The chip map uses the same squares and legend as the Tuner's, shaded by bad results, HW errors
    or perf.
  - Each chip's share of bad results from the miner's own log, its perf against the board's typical
    chip, and weak (red) / watch (ring) chips flagged with why.
  - Each board's average and hottest chip temperature, board sensor and measured voltage.
  - Checked in the background every 10 minutes.
- **Power estimates** (the SC Lite has no power sensor): on the Fleet cards, the Miner page and every
  preset, in W, amps and W/TH. Calibrate from a wall-meter reading on the Miner page; the mains voltage
  (110 V by default) sets the amps.
- **Fleet cards:** the Auto fan tile is replaced by Power (estimate) and Chips (weak chips, hottest chip).
- **Locks:** while a preset or schedule is in charge of a miner, its fan and clock panels are locked
  (on the page and on the server) until you press **Take manual control**, which clears the preset and
  pauses the schedule.
- **Fan curves can follow the hottest chip** (Profiles → Follows): the curve then uses the board
  temperature plus how much hotter the hottest chip runs.
- **Settings → Miner reference:** what the miner itself understands and reports, taken from its
  firmware: the power plan format, every `/mcb/setting` field, every web API call and debug page (which
  ones this app uses and which change the miner), the port 4028 commands (and which are refused from the
  network), and the firmware's log lines. With a filter and copy buttons.
- **Account moved into Settings:** the header no longer has an Account button on every page. Settings
  has an Account panel (who's signed in, change login, the disclaimer, log out).
- **Phones:** the header is two short rows (name, theme, Settings, Account; then the page tabs), pages
  no longer run off the side, and fields no longer make iPhones zoom in.

## 1.8.9 (2026-10-01)

- **Tuner, current-step graph is now a bar chart:**
  - One bar per chip with new errors, the ones closest to failing first (up to 16; fewer on narrow
    screens).
  - Each bar is that chip's new errors as a share of its own limit, labelled errors / limit (say
    12/14).
  - The red **limit** line runs across the top. A bar above it fails the step, and that bar turns red.
  - Hover a bar for the chip, its errors, limit and normal rate.
- During the baseline, or before any chip has an error, the graph says so instead.

## 1.8.8 (2026-10-01)

- **Schedule:** the highlight around the block under the pointer (and the green "now" block) is no
  longer cut off on the bottom row and the outer edges of the week grid.

## 1.8.7 (2026-10-01)

- **Tuner, Lowest preset MHz:** new field in the Presets section. Leave it empty for the automatic
  Lowest power clock (one step below the baseline), or set one, say 500 MHz:
  - Lowest power runs at exactly that clock at the lowest preset voltage, and only adds voltage if it
    fails there.
  - Middle and Low are spread evenly between High and Lowest power (clock and voltage), so the four
    presets make an even ladder of power levels.

## 1.8.6 (2026-10-01)

- **Tuner:** a setting outside the usual range for a miner is now a ▲ warning you can start past, not
  an error. The usual range is worked out from the miner's stock setting (+75 MHz, +300 mV).
- Only clearly unsafe values are refused: more than 150 MHz or 500 mV above stock (775 MHz / 9600 mV
  on a 625 / 9100 SC Lite).

## 1.8.5 (2026-10-01)

- **Tuner, Lowest preset mV:** new field in the Presets section, for how low the Low and Lowest power
  presets may go. The default is 400 mV below the miner's stock voltage (8700 mV on an SC Lite).
  It's separate from the voltage trim's "down to".
- Lowest power is tested right at that voltage. If it fails, it tries one clock step lower at the same
  voltage before adding voltage, so it stays a real low-power preset.

## 1.8.4 (2026-10-01)

- **Tuner, Run plan panel** under Current step:
  - The run's settings: option, baseline, PV, climb, voltage limits, test length and sensitivity,
    fans, presets, safety and start time.
  - Every step (Baseline, Climb, Trim voltage, Confirm, Presets, Finish), ticked off as it goes:
    ✓ done, ✗ failed, ● running now, – not done yet, ⤼ skipped, ■ cut short.
  - Every clock, voltage and preset with each try (retests and voltage boosts included) and why a try
    failed. Skipped ones say why.
  - It stays after the run ends, so you can see how far it got. A stop or safety stop marks the
    running step as cut short.
- The tuner writes this to `asic_tuner_plan.json` in the miner's tuner folder.

## 1.8.3 (2026-10-01)

- **Stock settings per miner:** the probe reads each miner's stock power plan from its own firmware
  (625 MHz / 9100 mV / PV 9400 on an SC Lite, 700 / 11750 / 11900 on an SC5 Pro II, and so on).
- **Tuner defaults follow it:**
  - The baseline is the stock setting.
  - The climb goes 75 MHz above it, boosting stops 200 mV above stock and trim goes 300 mV below.
  - PV on auto keeps the stock mV-to-PV gap.
  - The Tuner page shows the stock setting with a one-click "use it" link.
- Allowed ranges also follow each miner's stock setting, so other models are no longer held to SC Lite
  numbers.
- Miners probed before are probed again automatically to read their stock setting.
- The stock setting shows on the Miner page, and new presets on Profiles start from it.

## 1.8.2 (2026-10-01)

- **Fairer tuning:** a faster clock gets a proportionally bigger error allowance (test MHz ÷ baseline
  MHz), because it does more work and so makes more errors at the same error rate per hash. Before,
  every clock was judged by the baseline's errors per hour, which made higher clocks fail too easily.
- **New Tuner defaults:**
  - The baseline is the SC Lite's stock 625 MHz / 9100 mV, not 525.
  - The climb goes to 700 MHz.
  - 60-minute baseline and 30-minute tests.
- The review warns when the baseline isn't the stock setting.

## 1.8.1 (2026-09-30)

- **Best share tracking**, checked every 30 s:
  - **Fleet cards** show the best share since the miner last restarted, and its record.
  - **Miner page** has a Best share panel: since restart, the all-time record, the 10 best restarts
    (when and at what clock / preset each was found) and a Reset record button.
  - **Widgets:** a Best share tile in the overview ("miners online" moved under the hashrate), and
    "best …" on each miner's line.
- Values are shown in k / M / G / T / P. The since-restart value starts from 0 whenever the miner
  restarts, and the record is kept in `best_shares.json`.

## 1.8.0 (2026-09-30)

- **One repo:** the app, its source and its Umbrel store all live in `blake2b-asic-control`. The
  store is called christhealien Store.
- **New app id** `blake2b-asic-control`, so it installs as a new app. Copy the old data folder across
  and your miners, curves, presets, schedules and tuner results carry over.
- **Renames:**
  - The tuner is now `asic_tuner`. Old `sclite_autotune*` result files are renamed automatically.
  - The build script is now `install_addons.py`.
- You sign in once more (same login, new session cookie).
- Run-local scripts, a tester package layout, and a GitHub Actions image workflow in `docs/` for a
  later move to GitHub.

## 1.7.7

- **Restart works:** it sends the restart the way the firmware expects (`PUT /mcb/restart`; the old
  `GET` was ignored), runs in the background so nothing hangs, shows "restarting…" on the card, and
  says when the miner is hashing again (checked by its uptime restarting).
- Pool switch and Failback use it too.

## 1.7.6

- **Fleet card buttons:**
  - A pool switcher that shows the pool the miner is mining on.
  - Restart.
  - Schedule pause / resume.
  - A link to the miner's own web page.
- Click a miner's name to open its page.

## 1.7.5

- **Fleet cards:** one-click preset buttons (High / Mid / Low / Lowest) replace the auto-fan box and
  curve list.
- The badge next to "online" shows the preset on the miner (or "tuning") instead of fan on / off.

## 1.7.4

- **Miner page chip map** in greys only, on a log scale, so 1 error and 300 both show.
- A ring marks the chips with the most errors, and numbers appear on hover instead of in the squares.

## 1.7.3

- **Tuner graph:** the current-step graph shows how many more errors the closest chip can make before
  the step fails. It's a step line against a fixed line at 0, with minutes along the bottom.

## 1.7.2

- After an update, a normal refresh shows the new pages: browsers no longer keep old copies.

## 1.7.1

- **Schedule:** the week is a grid of 30-minute blocks. Pick any of the miner's presets and paint
  when it runs (drag across times and days), or fill the whole week in one click.

## 1.7.0

- **New Schedule tab:** a weekly timetable per miner that switches between its presets (tuned or your
  own). It has a week view, examples (quiet nights, cheaper night power, quieter weekends) and copy to
  other miners.
- **Fleet quick bar** for the ticked miners: apply a preset, auto fan on / off with a curve, pause or
  resume schedules, probe, failback to pool 0, restart.
- **Tabs:** the Miner tab is gone (click a miner to open it), and Batch is now Pools.
- **Miner page:**
  - Every chip's hardware errors: a map and a full sortable list, with stand-out chips marked.
  - What the miner was detected as, its preset and its schedule.
- **Locks:** while a miner is being tuned, its fan and clock controls are locked, on the page and on
  the server. Setting a clock by hand turns off its preset and pauses its schedule.
- Settings can show any number of demo miners.
- The miner-list widget is shorter: status, hashrate and temperature.

## 1.6.0

- **Renamed Blake2b ASIC Control.**
- **New Profiles page:** a fan curve board (drag points, built-in curves, your own curves, live
  temperature on the graph), and per-miner presets (High / Middle / Low / Lowest power) you can apply
  in one click.
- **Tuner:**
  - Holds the boards at a target temperature you set.
  - Builds and tests the four presets with their own fan curves at the end.
  - Runs separately for each miner, so several miners can be tuned at once. Earlier results are moved
    into that miner's folder.
- **Disclaimer** to acknowledge before signing in.
- **Widgets:** Hottest ASIC (and which miner), and a miner list showing what each is doing (hashing,
  tuning, hot, offline), its preset and its MHz, mV and PV.
- **Probing:** every miner is probed automatically when it's added (model, firmware, hash boards,
  chips, fans). A miner must be probed before it can be tuned or have a preset applied.

## 1.5.1

- Home-screen widgets show live data (they were stuck on dashes).

## 1.5.0

- **Look:** monochrome with a Light / Dark switch next to Refresh, clearer outlines and section lines
  in light mode, and a chip map where every chip square has its own outline.
- New icon and store screenshots.
- Two home-screen widgets: overview and miner list.

## 1.4.0

- **Look:** monochrome (black and white, colour only for good / warning / failed) with a Light / Dark
  switch.
- New icon and store screenshots.
- Two home-screen widgets: an overview (miners online, total hashrate, hottest board, tuner) and a
  per-miner list.

## 1.3.0

- **Tuner form:** split into numbered, explained sections marked "both options", "search only" or
  "confirm only".
- **Review screen:** spells out exactly what the run will do (steps, voltages, PV, time, safety) and
  catches mistakes before anything starts.

## 1.2.1

- **Tuner:** shows the PV each setting will use, with an optional PV offset. Auto keeps each miner's
  own mV-to-PV gap.

## 1.2.0

- **Renamed** for the SC and HS lines.
- **Login:** sign in once with the default login Umbrel shows for this app, and stay signed in across
  pages and app updates.
- Settings and Account moved to the top right.
- Auto-refresh no longer overwrites what you are typing, and refreshes every 15 s.
- **Tuner:**
  - Keeps going through short miner outages.
  - New "confirm one setting" mode.
  - Logs in local time.

## Before 1.2.0

The first versions were the SC Lite per-chip auto-tuner added to Maveth's web dashboard for
Umbrel. Notes weren't kept for them.
