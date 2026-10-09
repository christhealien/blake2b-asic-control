#!/bin/sh
# Blake2b ASIC Control: start the dashboard and the Umbrel widget server. When the app is stopped (Umbrel update,
# restart, uninstall), stop every running tuning run first so each puts its best setting back.
# Everything this app writes is for this app only: miners.json holds the miners' admin passwords.
umask 077
DATA="${SCLITE_TUNER_DATA:-/data/tuner}"
mkdir -p "$DATA"
REG="${SCLITE_WEBUI_MINERS:-/data/miners.json}"
# files written by older versions were readable by everyone: tighten them once
for F in "$REG" "$(dirname "$REG")"/*.json; do
  [ -f "$F" ] && chmod 600 "$F" 2>/dev/null
done
chmod -R go-rwx "$DATA" 2>/dev/null

python -u widget_server.py &
WIDGETS=$!

python -u server.py &
SERVER=$!

# Stop every running tuning run and wait (up to STOP_WAIT_S) while each puts its setting back. A restore can
# take a while on a slow miner (sign-in retries), so the compose file's stop grace period is longer still.
STOP_WAIT_S=110
stop_tuners() {
  PIDS=""
  for PIDFILE in "$DATA"/*/asic_tuner.pid.json; do
    [ -f "$PIDFILE" ] || continue
    PID=$(python -c "import json,sys; print(json.load(open(sys.argv[1]))['pid'])" "$PIDFILE" 2>/dev/null)
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null && grep -q asic_tuner "/proc/$PID/cmdline" 2>/dev/null; then
      echo "stopping the tuning run in $(dirname "$PIDFILE") so it restores the miner's setting..."
      kill -INT "$PID"
      PIDS="$PIDS $PID"
    fi
  done
  # wait for all of them together (an exited run can linger as a zombie until the
  # dashboard reaps it, so check its cmdline rather than the pid)
  i=0
  while [ -n "$PIDS" ] && [ $i -lt $STOP_WAIT_S ]; do
    LEFT=""
    for PID in $PIDS; do
      grep -q asic_tuner "/proc/$PID/cmdline" 2>/dev/null && LEFT="$LEFT $PID"
    done
    PIDS="$LEFT"
    [ -n "$PIDS" ] && sleep 1
    i=$((i + 1))
  done
}

stop() {
  stop_tuners
  kill -TERM "$WIDGETS" "$SERVER" 2>/dev/null
  wait "$SERVER" 2>/dev/null
  exit 0
}
trap stop TERM INT

wait "$SERVER"
RC=$?
# the dashboard stopped on its own (a crash): the tuning runs it started would be killed with the container
# before they could put the miner's setting back, so stop them properly first
stop_tuners
kill -TERM "$WIDGETS" 2>/dev/null
# pass its exit code on, so Docker restarts the app after a crash
[ "$RC" -eq 0 ] && RC=1
exit "$RC"
