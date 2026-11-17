#!/usr/bin/env bash
# Nightly refresh of the dataset catalogue (CONTRACT §78.7; user crontab 02:30, after the 00:30 backup).
#   1. catalog-scan          list the NAS roots (one directory listing per root and per folder) and recompute
#                            every entry's availability
#   2. catalog-scan --sizes  walk the matched folders for file count and size: metadata only (no file is
#                            opened), at most MAX_RATE directory entries per second, oldest first, until BUDGET
#                            seconds are spent; the results are cached in the DB and re-walked after 30 days
# Read only: nothing on the NAS is written, moved or deleted. Off-hours and throttled so training jobs that read
# the NAS are not slowed. Errors go to /tmp/ai-platform-dataops-catalogue.err.
set -Eeuo pipefail
SYSTEM=ai-platform-dataops
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${DATAOPS_PYTHON:-$HOME/.venvs/$SYSTEM/bin/python}"
BUDGET="${DATAOPS_CATALOGUE_SIZE_BUDGET:-10800}"
MAX_RATE="${DATAOPS_CATALOGUE_MAX_RATE:-500}"
exec 9>"/tmp/$SYSTEM-catalogue.lock"
if ! flock -n 9; then echo "$(date -Is) catalogue refresh already running" >&2; exit 75; fi
cd "$REPO"
export PYTHONPATH="$REPO"
nice -n 19 "$PY" -m dataops catalog-scan --by nightly >/dev/null
nice -n 19 "$PY" -m dataops catalog-scan --sizes --budget "$BUDGET" --max-rate "$MAX_RATE" --by nightly >/dev/null
