#!/usr/bin/env bash
# Local read-only HTML viewer (user process, no sudo).
#   deploy/web.sh start|stop|restart|status|logs
# Binds DATAOPS_BIND_HOST:DATAOPS_PORT (default 127.0.0.1:8096). Not restarted automatically after a
# reboot: run `deploy/web.sh start` again (and `deploy/db.sh up` if the container is not running).
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${DATAOPS_PYTHON:-$HOME/.venvs/ai-platform-dataops/bin/python}"
STATE="$HOME/.cache/ai-platform-dataops"
PID="$STATE/web.pid"
LOG="$STATE/web.log"
mkdir -p "$STATE"

running() { [[ -f "$PID" ]] && kill -0 "$(cat "$PID")" 2>/dev/null; }

case "${1:-}" in
  start)
    if running; then echo "already running (pid $(cat "$PID"))"; exit 0; fi
    cd "$REPO"
    nohup "$PY" -m dataops serve >>"$LOG" 2>&1 &
    echo $! > "$PID"
    sleep 2
    running && echo "started pid $(cat "$PID"); log $LOG" || { tail -20 "$LOG"; exit 1; } ;;
  stop) if running; then kill "$(cat "$PID")"; rm -f "$PID"; echo stopped; else echo "not running"; fi ;;
  restart) "$0" stop; "$0" start ;;
  status) if running; then echo "running pid $(cat "$PID")"; else echo "not running"; exit 1; fi ;;
  logs) tail -50 "$LOG" ;;
  *) sed -n '2,5p' "$0"; exit 2 ;;
esac
