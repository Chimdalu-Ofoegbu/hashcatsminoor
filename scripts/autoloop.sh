#!/usr/bin/env bash
# Unattended loop: run the autopilot, wait, run it again. Each pass costs nothing
# unless the live numbers pass the gate, so leaving this running is safe.
#
#   set -a; . ./.env; set +a
#   nohup bash scripts/autoloop.sh > state/autoloop.log 2>&1 &
#
# Stop it: touch state/STOP   (the loop exits before the next pass; a pass in
# progress still finishes and destroys its rental). Ctrl-C works too.
set -u
cd "$(dirname "$0")/.."
INTERVAL_MINUTES="${LOOP_INTERVAL_MINUTES:-30}"
MAX_PASSES="${LOOP_MAX_PASSES:-48}"
FAILS="${LOOP_MAX_CONSECUTIVE_FAILS:-3}"
mkdir -p state
fails=0
for ((pass = 1; pass <= MAX_PASSES; pass++)); do
  if [ -e state/STOP ]; then echo "stop file found, exiting"; exit 0; fi
  echo "== pass ${pass}/${MAX_PASSES} $(date -u +%FT%TZ) =="
  if python autopilot.py --yes "$@"; then
    fails=0
  else
    fails=$((fails + 1))
    echo "pass ${pass} did not mint (see state/autopilot.log); consecutive: ${fails}"
    if [ "${fails}" -ge "${FAILS}" ]; then echo "giving up after ${fails} failed passes"; exit 1; fi
  fi
  echo "sleeping ${INTERVAL_MINUTES} minutes"
  sleep $((INTERVAL_MINUTES * 60))
done
