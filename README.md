# Blake2b ASIC Control

A fleet dashboard, one-click presets, schedules, alerts and a per-chip auto-tuner for **Blake2b
miners** (SC Lite, SC Box, HS Box). It runs as an **Umbrel app** (this repo is also the Umbrel
community app store that serves it) or on any PC with Docker or Python. It talks to the miners' stock
web API only: no firmware changes, and nothing leaves your network unless you turn on notifications.

![Your whole Blake2b fleet on one dashboard](blake2b-asic-control/gallery/1.png)

## What it does

- **Fleet:** a card per miner with its 20-second hashrate, a **24-hour hashrate graph** (marks for
  restarts, setting changes, tuning runs and rejected shares that weren't stale), one-click presets,
  an estimated power draw, chip health, a pool switcher, Restart with a progress bar, and a quick bar
  for the miners you tick.
- **Miner page:** live stats, the same 24-hour graph, a **chip map** of every chip on the hash boards
  (shaded by bad results, HW errors or speed, with weak chips flagged), each board's temperatures and
  voltage, the **best share** (with a record that survives restarts, on the same scale as DATUM and
  mempool so you can compare it with the network difficulty; or the miner's own number, or both) and
  **rejected shares** sorted into stale (pool timing, harmless) and not stale (worth a look).
- **Presets:** High, Middle, Low and Lowest power per miner: a clock, voltage, PV and the fan curve to
  run them with. **Idle** puts an SC Lite in the firmware's own sleep mode. One click applies a preset.
  On the **SC Box and HS Box** a preset is a clock only (their voltage stays as the miner has it), and
  their Miner page has a clock control.
- **Fan curves:** drag the points of a curve (hottest board or chip → fan %); each miner runs its own,
  with a safety temperature that sends the fans to full.
- **SC Box / HS Box fan target:** these boxes ignore fan numbers and run their own fan loop, so their Miner
  page sets that loop's target temperature instead (lower is cooler and louder, higher quieter and warmer).
- **Schedule:** paint presets across a week of half-hour blocks (say Low at night and Idle during
  peak power rates), with scheduled restarts on the same grid, and copy one miner's week to others.
- **Tuner** (SC Lite): maps every clock from underclock to overclock and finds the lowest voltage each
  one runs cleanly, judging every chip against its own normal error rate while holding the boards at a
  temperature you choose. It confirms the winner and builds and tests the four presets, each with its
  own fan curve. **Rebuild the presets** tests them again from the last search's curve (for example at
  another temperature) in a few hours instead of a day. Several miners can tune at once.
  The **SC Box and HS Box** get a clock tuner: it learns every chip's normal error rate, steps the clock
  up 25 MHz at a time until a step isn't clean, confirms the fastest clean clock, then builds and tests
  the four clock presets.
- **Notifications** on Telegram or Discord: a miner offline, too hot, low hashrate, restarted on its
  own, a scheduled change that failed, a tuning run finished, a new best-share record, a weak chip, a
  share rejected for a reason other than stale. Each kind and each miner on or off.
- **Pools** on several miners at once, **Settings** (account, time zone, best share numbers, notifications, a Miner
  reference explaining the miner's settings, web API, debug pages and port 4028 commands), Light and
  Dark themes, a phone layout and three Umbrel home-screen widgets (overview with estimated fleet power, fleet health gauges, miner list).

![The tuner maps every clock and voltage](blake2b-asic-control/gallery/2.png)
![The Miner page: 24-hour hashrate and every chip](blake2b-asic-control/gallery/3.png)
![Schedule: paint the week](blake2b-asic-control/gallery/4.png)
![Presets, Idle and fan curves](blake2b-asic-control/gallery/5.png)
![Notifications on Telegram or Discord](blake2b-asic-control/gallery/6.png)

<sub>Screenshots use test miners (127.0.0.x addresses, example pools); the tuner results are from a
real SC Lite run.</sub>

## Models

What each model can do today, and what it was tested on. Monitoring means the Fleet card and Miner
page: hashrate and the 24-hour graph, temperatures, fans, chips, pools, restarts, best share,
rejected shares, alerts and the widgets.

| Model | Tested | Monitoring | Clock and voltage (presets, scheduled presets) | Fans | Tuner |
|---|---|---|---|---|---|
| **SC Lite** | ✓ fw 2.2.0 | ✓ | ✓ clock, voltage and PV, plus Idle | app fan curves and fan % | ✓ clock and voltage |
| **SC Box** | ✓ fw 2.2.5 | ✓ | **clock only** (voltage kept as the miner has it) | **fan target** 65–75 °C | **clock tuner** (new in 1.17) |
| **HS Box** | ✓ fw 2.2.6 | ✓ | **clock only** (voltage kept as the miner has it) | **fan target** 70–80 °C | **clock tuner** (new in 1.17) |
| **SC Box II** | not yet | expected ✓ | expected clock only | expected fan target | expected clock tuner |
| **SC5 Pro II** | probe, from a capture | ✓ | same format as the SC Lite, untested | app fan curves, untested | only with "allow untested models" |
| **SC5 Pro** | not yet | expected ✓ | expected like the SC5 Pro II | expected like the SC5 Pro II | only with "allow untested models" |
| **Other models** | – | probe only | – | – | – |

"Expected" means the model is the same family as a tested one, but nobody has run it yet: the probe
decides from what the miner reports, and a **Download report** (below) is the way to confirm it.

**SC Lite.** Everything is tested on it: presets (High, Middle, Low, Lowest power and your own), the
firmware's Idle mode, schedules, auto fan with curves, and the tuner.

**SC Box and HS Box.** Monitoring is complete. These models write their power plan differently
(`550 MHz 0.44 V 90 RPM 90 RPM`: decimal volts, no PV; the HS Box keeps one plan list per algorithm),
and since 1.17 the app sets their **clock**:

- **Clock only.** crProductGuy's clock and voltage test on both boxes, with a plug-in power meter,
  showed that a 0.01 V lower voltage made no measurable difference in wall power, while every 25 MHz
  moved power and hashrate together at the same J/TH (about 7 W per 25 MHz on the SC Box and 4–5 W on
  the HS Box; around 250–260 J/TH on the SC Box and 280–290 on the HS Box). So the app changes the clock
  and leaves the voltage and fan fields exactly as the miner has them. A clock is written as the
  miner's manual plan and applies at once, without a restart.
- **Never above stock** (SC Box 725 MHz, HS Box 850 MHz), on the 25 MHz grid, down to half of stock.
  Both test boxes made errors 25 MHz above what they ran (the SC Box at 575 MHz, the HS Box at
  875 MHz), so the clock control, the presets, schedules and the clock a tuning run ends on stay at
  stock or below. Only a tuning step may try stock + 25 MHz, to see if there's headroom.
- **Presets** are clocks: *Make four from its clock* (Profiles) makes High at the clock it runs now and
  Middle, Low and Lowest power 25, 50 and 75 MHz lower, not tested; the tuner tests them. There's no
  Idle preset on these boxes.
- **Clock control** on the Miner page, locked while a preset or schedule is in charge (like the SC
  Lite's) and while tuning.
- **Clock tuner:** a baseline at the clock it runs now (every chip's normal error rate), then 25 MHz up
  at a time until a step isn't clean, a confirm of the fastest clean clock, then the four presets,
  each tested. It ends on High when High passed; otherwise, or when stopped, it puts back exactly what
  the miner ran before. The first runs on these models are new: watch them.
- **Going back to a stock plan** writes the stock clock first: on the HS Box, turning the manual
  setting off alone kept the last clock written.

Their fans work differently too: the firmware ignores fan numbers and runs its own fan loop, which
holds the control board at a **fan target** (`temp_target`, inside the range it reports in
`temp_targets`). That target is the one fan setting these models take, so their Miner page sets it
instead of curves and fan %: lower is cooler and louder, higher is quieter and warmer. Only
`temp_target` is written; the settings are read fresh and sent back as the miner gave them. Tested by
crProductGuy on both: the target holds and the fan loop steers to it within seconds. The SC Box's fans
run near full speed for about 15 minutes after any settings change (a fan target or a clock); the HS
Box's kept their speed. Changes are at least a minute apart.

**SC5 Pro II.** Its power plan has the same format as the SC Lite's, so presets, auto fan and the
tuner can work, but they haven't been run on one: the tuner only starts with "allow untested
models", and should be watched closely.

**Have a model or firmware that isn't tested yet?** Open **Profiles**, pick the miner and press
**Download report**. It reads the miner (one request at a time, nothing is changed, about 30 s) and
saves a zip of what support takes: how it writes clock and voltage, its boards and chips, what its own
pages answer and, if ticked, its log. The MAC address, IP addresses, pool URLs, wallets, workers and
tokens are taken out, and the pool and WiFi settings aren't read at all. It's plain text, so look it
over, then attach it to a [new issue](https://github.com/christhealien/blake2b-asic-control/issues/new)
saying which unit it is. Support is only claimed for a model after a report like this and a test on a
real unit.

## Install

**On Umbrel:** App Store → ⋯ (top right) → **Community App Stores** → add
`https://github.com/christhealien/blake2b-asic-control` → open **christhealien Store** → install
**Blake2b ASIC Control**. Sign in with the username and password Umbrel shows for the app, then:

1. **Settings → Time zone:** pick yours (schedules and restarts run on it; it starts on UTC).
2. **Settings → Add miner:** its IP and admin password. It's probed by itself within a minute.
3. To tune an SC Lite: the **Tuner** tab. Read the disclaimer on the sign-in page first.

**Without Umbrel** (Docker, or Python on Linux / macOS / Windows): see [TESTING.md](TESTING.md).
**Demo miners** (Settings → Demo miners) let you look around without touching a real one.

> **⚠ Overclocking and voltage changes can damage hardware and void warranties.** The tuner and presets
> change clock, voltage and fans on real miners. Every value is checked against each miner's limits and
> a board reaching the abort temperature stops a run, but you use it at your own risk.

## Security

- **Keep miners and the Umbrel on a trusted network.** The miners' own interfaces have no real
  security: their login is sent over plain HTTP with a key every copy of the firmware shares, and port
  4028 needs no login at all. Anyone on the same network can reach them. A separate network (VLAN) for
  miners is best, and never forward their ports to the internet.
- **This app's login is the only gate** (Umbrel's own login is skipped so you don't sign in twice).
  Use a long password.
- **Other apps on the same Umbrel are trusted by your browser** (same host name), so only install
  apps you trust. Changes still need this app's own pages (a request header other pages can't add),
  but your session cookie is sent to any app on that host.
- **Outbound traffic:** the app talks to your miners, and to nothing else unless you set up
  notifications: then only to `api.telegram.org` and/or `discord.com` (the webhook you paste). The bot
  token and webhook URL are kept in `miners.json` and never sent back to the page.
- `miners.json` holds your miners' admin passwords (the miners need them in clear text); the app keeps
  it readable by itself only.
- Clock, voltage, PV and fan values are checked on the server against each miner's limits for every
  way of setting them.
- The sign-in back-off goes by the client's address. On Umbrel, `SCLITE_BEHIND_PROXY=1` (set in the
  compose file) makes it trust the address app_proxy forwards. Elsewhere leave it unset unless a
  reverse proxy you control sits in front and sets `X-Forwarded-For`; otherwise a client could make up
  a fresh address for every guess.

## Data

Everything that changes is in the app's data folder (on Umbrel
`~/umbrel/app-data/blake2b-asic-control/data`):

- `miners.json`: your miners and what each was detected as (including its stock setting), their
  auto-fan settings, fan curves (`profiles`), presets (`presets`), schedules (`schedules`), power
  calibrations (`power_cal`), mains voltage (`power`), notifications (`notify`), the time zone, how best shares are shown (`share_scale`) and, on an SC Box or HS Box, the last fan target the app set (`fan_target_set`).
- `auth.json`: the login (hashed) and a log of disclaimer acknowledgements. It's first created from
  Umbrel's default login for the app. Delete it and restart the app to go back to that default.
- `sessions.json`: who is signed in.
- `best_shares.json`: each miner's best share since restart, its record and its 20 biggest finds.
- `hashrate.json`: each miner's hashrate every 5 minutes for 24 hours, and the marks on its graph.
- `rejects.json`: each miner's accepted / rejected shares per hour (8 days) and its rejected-share
  events (7 days: when, stale or not, and the short reason word; no other log text).
- `restart_times.json`: how long each miner's last 5 restarts took (for the Fleet card's progress bar).
- `tuner/<miner id>/`: that miner's `asic_tuner_*` results, live readings, chip errors, presets, run
  plan, every run's options, what the last rebuild was built from, and the log, all downloadable from
  the Tuner page (one at a time or as a zip).

## Layout

```
umbrel-app-store.yml          the store: id "blake2b", name "christhealien Store"
blake2b-asic-control/         the Umbrel app (manifest, compose, icon, gallery)
app/                          the source and Dockerfile (the image's build context)
.github/workflows/image.yml   builds the image for amd64 and arm64 and pushes it to ghcr.io
TESTING.md                    running it without an Umbrel (Docker or Python)
CHANGELOG.md                  every version and what changed
THIRD_PARTY_NOTICES.md        the licenses of the projects it builds on
```

Umbrel only reads `*/umbrel-app.yml` in a store repo, so `app/` is ignored by it.

| File in `app/` | What it is |
|---|---|
| `Dockerfile` | Downloads the dashboard this builds on (pinned), adds the files below and patches them in |
| `entrypoint.sh` | Starts the dashboard and widgets. On shutdown it stops every tuning run so each one restores its best setting |
| `install_addons.py` | Wires every page, route, the login and the Fleet changes into the dashboard (runs during the build) |
| `asic_tuner.py` | The per-chip tuner, including the fan hold, presets and rebuilds |
| `asic_tuner_box.py` | The SC Box / HS Box clock tuner (it reuses the per-chip judging of `asic_tuner.py`) |
| `box_plan.py` | Writing and putting back an SC Box / HS Box clock, shared by the dashboard and the box tuner |
| `tuner_addon.py` / `tuner.html` | The Tuner API (one run and folder per miner) and the Tuner page |
| `fan_addon.py` / `profiles.html` | Fan curves, presets and Idle, probing, the chip list, locks, restart, time zone and demo miners, plus the Profiles page |
| `schedule_addon.py` / `schedule.html` | Per-miner schedules and the engine that applies them, plus the Schedule page |
| `addon.js` | Fleet, Miner page and Settings additions (quick bar, card buttons, graphs, chip map, locks) |
| `shares_addon.py` | Best share tracking: since restart, the record and every new best as it is found |
| `hashrate_addon.py` | The 24-hour hashrate history and marks behind the graphs |
| `report_addon.py` | Download report: a miner's read-only answers, private details taken out, as a zip |
| `rejects_addon.py` | Rejected shares: counts per hour, and stale vs not stale from the miner's own log |
| `notify_addon.py` | Notifications: the watcher, and sending to Telegram / Discord |
| `health_addon.py` | Chip health from the miner's own log: each chip's share of bad results, chip temperatures and voltage |
| `miner_safety.py` | Paced, bounded reads from miners, and miner-reported text cleaned before it reaches a page |
| `auth_addon.py` / `login.html` | The login and the sign-in page |
| `theme.css` / `theme.js` | The monochrome look with Light / Dark |
| `widget_server.py` | The Umbrel home-screen widgets (internal port 8788) |
| `run-local.sh` / `run-local.ps1` | Run it without Docker (see TESTING.md) |

## Releasing a new version

1. Put the new number in `blake2b-asic-control/docker-compose.yml` (`image:`),
   `blake2b-asic-control/umbrel-app.yml` (`version:`, the `?v=` on the icon and gallery URLs, and the
   release notes) and `app/Dockerfile` (the version label), and add it to `CHANGELOG.md`.
2. Commit and push, then tag it: `git tag v1.14.1 && git push origin v1.14.1`.
3. The **image** workflow builds the image and pushes `ghcr.io/christhealien/blake2b-asic-control:<version>`
   (GitHub → Actions shows it; about 10–15 minutes for both architectures).
4. Umbrel checks stores every few minutes and offers the update once the new manifest is there.

The store's "What's new" (`releaseNotes`) is markdown, and a community store app shows only one entry,
so it keeps a rolling list: the new version on top, earlier ones as one-line bullets, and a link to
CHANGELOG.md for everything.

## Credits

- **[Maveth's web dashboard](https://github.com/Maveth/goldshell-config)** (MaVeTh / BitcoinMechanic,
  MIT): the fleet dashboard, miner client, fan controller, probe and SC Lite helpers this app builds
  on. The build downloads it at a pinned commit and patches it; its MIT license files come along into
  the image. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
- **[crProductGuy's box tools](https://github.com/crProductGuy/goldshell-box-tools-productguy)**
  (MIT): its firmware API notes are where the restart method comes from (a `PUT` to `/mcb/restart`;
  the miner ignores a `GET`); its stale-share proposal is where the Rejected shares design comes from
  (stale means exactly `(stale-prevblk)`; anything else is flagged; rejects the log doesn't account for
  are said, never guessed); its sanitized SC5 Pro II capture was used to check a second model's stock
  plan and best-share fields; and **crProductGuy tested this app on an SC BOX and an HS BOX**, with the
  read-only captures and write tests that the box support is built from. Maveth's dashboard also adapts
  parts of that project (board parsing, the soft watchdog) and credits it in its own ATTRIBUTION.md.

This project is not affiliated with or endorsed by any miner manufacturer.

## License

[MIT](LICENSE) for this repository's own files. The projects it builds on keep their own MIT licenses
and notices: see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
