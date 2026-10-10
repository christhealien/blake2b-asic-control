# Testing Blake2b ASIC Control on your own computer

You don't need an Umbrel. Pick **one** of the two ways below. Both download the dashboard this
app builds on (Maveth's, pinned to the tested version) and add this app on top, so you
need an internet connection the first time.

> ⚠ Read the disclaimer on the sign-in page. The Tuner, presets, schedules and the Miner page
> change clock, voltage and fans on real miners. To just look around, use **demo miners**
> (Settings → Demo miners): nothing is ever sent to them.

## Option 1: Docker (any OS with Docker)

```sh
cd blake2b-asic-control/app
docker build -t blake2b-asic-control .
mkdir -p data
docker run -d --name blake2b -p 127.0.0.1:8787:8787 -v "$PWD/data:/data" --user "$(id -u):$(id -g)" \
  -e SCLITE_WEBUI_DEFAULT_USER=admin -e SCLITE_WEBUI_DEFAULT_PASSWORD='pick-a-long-password' \
  blake2b-asic-control
```

Add `-e TZ=Europe/Berlin` (your zone) to start in your time zone, or pick it later under Settings → Time zone.
Open http://localhost:8787 and sign in with that login (change it under Settings → Account). Stop it with
`docker stop blake2b`, start again with `docker start blake2b`. Your miners, login and results stay in the
`data` folder, readable by you only (it holds the miners' passwords).

Don't set `SCLITE_BEHIND_PROXY` here (Umbrel sets it for its own proxy): without a proxy in front,
the login back-off has to go by the real connection's address.

`127.0.0.1:` keeps it on this computer. To open it from other devices use `-p 8787:8787` instead, but only
with the default login set as above: without one, the first person to open the page creates the login.

## Option 2: Python (no Docker)

Needs **git** and **Python 3.10 or newer**. The scripts are in the `app` folder.

- **Linux / macOS:** `sh app/run-local.sh`
- **Windows:** right-click `app\run-local.ps1` → **Run with PowerShell**
  (or `powershell -ExecutionPolicy Bypass -File run-local.ps1`)

The first run sets everything up in an `app/local` folder (a minute or two); later runs start straight away.
Open http://127.0.0.1:8787 and press Ctrl+C in the window to stop.

To reach it from a phone or another PC on your network:
`HOST=0.0.0.0 sh run-local.sh` (Linux / macOS) or `run-local.ps1 -Listen 0.0.0.0` (Windows),
then open `http://<this computer's IP>:8787`. Create the login straight away when you do: until there is one, the first person to open the page creates it. Delete the `app/local` folder to start completely fresh.

## First steps

1. Acknowledge the disclaimer and **create a login** (first visit only).
2. **Settings → Add miner** (IP + the miner's web password). It's probed automatically within a minute
   (model, boards, chips, fans); or tick "add if login ok" in Setup / Probe.
3. Or **Settings → Demo miners** to fill Fleet with fake miners.

## What to report

What you clicked, what you expected, what happened, a screenshot, and your miner model + firmware
(shown on its Fleet card). The SC Lite tuner is tested on the SC Lite; other models of that form need the
"allow untested models" box and should be watched closely.

**SC Box / HS Box:** the app sets the clock only (voltage and fans stay as the miner has them).
- **Clock** (Miner page, clock panel): pick a clock and press Set clock. Check port 4028 or the miner's own
  page reports the new clock, and that the voltage and the fan target didn't change.
- **Presets** (Profiles): *Make four from its clock*, then Apply one; the Fleet card shows it as on the miner.
- **Tuner** (Tuner page, the SC Box / HS Box form): the first runs on these models are new, so watch the
  first steps. A plug-in power meter is useful: note the watts at each preset. Stop it once mid-run and check
  the miner goes back to exactly what it ran before (on an HS Box on its stock plan: 850 MHz on port 4028, and
  "manual" off on its own page).
- **Fan target** (Miner page, fan panel): the firmware's own fan loop holds the control board at that
  temperature. After changing it, check the miner's own page shows the new target and that its clock is
  unchanged.
- On the SC Box expect the fans to run near full speed for about 15 minutes after any settings change.
