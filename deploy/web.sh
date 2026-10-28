#!/usr/bin/env bash
# Local HTML app + job worker (user processes, no sudo).
#   deploy/web.sh start|stop|restart|status|logs
# Binds DATAOPS_BIND_HOST:DATAOPS_PORT (default 127.0.0.1:8096). The worker runs browser jobs (analysis,
# pilot, publication) one at a time. Not restarted automatically after a reboot: run `deploy/web.sh start`
# again (and `deploy/db.sh up` if the container is not running).
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${DATAOPS_PYTHON:-$HOME/.venvs/ai-platform-dataops/bin/python}"
STATE="$HOME/.cache/ai-platform-dataops"
mkdir -p "$STATE"
export PATH="$HOME/.local/bin:$PATH"           # the analysis agent (claude) for the worker

pidf() { echo "$STATE/$1.pid"; }
running() { [[ -f "$(pidf "$1")" ]] && kill -0 "$(cat "$(pidf "$1")")" 2>/dev/null; }
start_one() {   # name, command...
  local name=$1; shift
  if running "$name"; then echo "$name already running (pid $(cat "$(pidf "$name")"))"; return 0; fi
  cd "$REPO"
  nohup "$@" >>"$STATE/$name.log" 2>&1 &
  echo $! > "$(pidf "$name")"
  sleep 2
  running "$name" && echo "$name started pid $(cat "$(pidf "$name")"); log $STATE/$name.log" \
    || { tail -20 "$STATE/$name.log"; return 1; }
}
stop_one() {
  local name=$1
  if running "$name"; then kill "$(cat "$(pidf "$name")")"; rm -f "$(pidf "$name")"; echo "$name stopped"
  else echo "$name not running"; fi
}

case "${1:-}" in
  start) start_one web "$PY" -m dataops serve; start_one worker "$PY" -m dataops worker ;;
  stop) stop_one web; stop_one worker ;;
  restart) "$0" stop; "$0" start ;;
  status) rc=0; for n in web worker; do if running $n; then echo "$n running pid $(cat "$(pidf $n)")"; else echo "$n not running"; rc=1; fi; done; exit $rc ;;
  logs) for n in web worker; do echo "== $n"; tail -30 "$STATE/$n.log" 2>/dev/null || true; done ;;
  *) sed -n '2,6p' "$0"; exit 2 ;;
esac
