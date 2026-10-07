#!/bin/sh
# Run Blake2b ASIC Control on your own computer (Linux / macOS), no Umbrel or Docker needed.
#
#   sh run-local.sh              # first run sets everything up, then starts it
#   HOST=0.0.0.0 sh run-local.sh # also reachable from other devices on your network
#   PORT=8800 sh run-local.sh    # a different port
#
# Needs: git and Python 3.10 or newer. Everything goes into ./local next to this script
# (the dashboard it builds on, a Python venv, and your data). Delete ./local to start over.
set -e
cd "$(dirname "$0")"
HERE="$(pwd)"
L="$HERE/local"
REPO="${UPSTREAM_REPO:-https://github.com/Maveth/goldshell-config.git}"
REF="${UPSTREAM_REF:-92838b350f324bfca2d1b70eef48b0861927fbe5}"
PY="${PYTHON:-python3}"

command -v git >/dev/null || { echo "git is needed: install it first"; exit 1; }
command -v "$PY" >/dev/null || { echo "python3 is needed: install Python 3.10+ first"; exit 1; }

if [ ! -f "$L/app/webui/static/profiles.html" ] || [ "$HERE/install_addons.py" -nt "$L/app/.built" ]; then
  echo "== setting up (the dashboard + this add-on)"
  rm -rf "$L/app" "$L/src"
  mkdir -p "$L/app" "$L/data/tuner"
  git clone -q "$REPO" "$L/src"
  git -C "$L/src" checkout -q "$REF"
  cp -r "$L/src/sc-lite/webui" "$L/app/webui"
  cp -r "$L/src/sc-lite/python" "$L/app/python"
  rm -rf "$L/src"
  cp tuner_addon.py tuner.html fan_addon.py profiles.html schedule_addon.py shares_addon.py health_addon.py miner_safety.py notify_addon.py rejects_addon.py hashrate_addon.py report_addon.py schedule.html addon.js \
     auth_addon.py login.html theme.css theme.js widget_server.py install_addons.py "$L/app/webui/"
  cp asic_tuner.py "$L/app/python/"
  (cd "$L/app/webui" && "$PY" install_addons.py >/dev/null && echo "   add-on installed")
  touch "$L/app/.built"
fi

if ! "$L/venv/bin/python" -c "import Crypto" 2>/dev/null; then
  echo "== creating a Python environment (one time)"
  [ -x "$L/venv/bin/python" ] || "$PY" -m venv "$L/venv"
  "$L/venv/bin/python" -m pip install -q --upgrade pip || true
  "$L/venv/bin/python" -m pip install -q "pycryptodome>=3.18" || { echo "couldn't install pycryptodome (needs internet)"; exit 1; }
fi

# start with no miners (the dashboard would otherwise copy in its example miner)
mkdir -p "$L/data/tuner"
[ -f "$L/data/miners.json" ] || printf '{"fan_defaults": {"profile": "curve-60", "enabled_default": false}, "miners": []}\n' > "$L/data/miners.json"

umask 077   # miners.json holds the miners' passwords: readable by you only
export SCLITE_WEBUI_HOST="${HOST:-127.0.0.1}"
export SCLITE_WEBUI_PORT="${PORT:-8787}"
export SCLITE_WEBUI_MINERS="$L/data/miners.json"
export SCLITE_TUNER_DATA="$L/data/tuner"
export PYTHONUNBUFFERED=1
export B2AC_VERSION="$(sed -n 's/.*B2AC_VERSION=\([0-9.]*\).*/\1/p' "$HERE/Dockerfile" | head -n 1)"
echo "== open http://127.0.0.1:${SCLITE_WEBUI_PORT}  (Ctrl+C to stop)"
echo "   first visit: create a login; add miners under Settings, or show demo miners there"
cd "$L/app/webui"
exec "$L/venv/bin/python" -u server.py
