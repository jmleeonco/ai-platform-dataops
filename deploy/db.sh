#!/usr/bin/env bash
# Management DB (MariaDB in Docker, user-level; no sudo) for ai-platform-dataops.
#
#   deploy/db.sh init     create secrets (once), start the container, create the app account
#   deploy/db.sh up       start (or create) the container
#   deploy/db.sh down     stop the container (data volume kept)
#   deploy/db.sh status   container state and a ping
#   deploy/db.sh shell    mariadb client as the app account
#
# Secrets live in ~/.config/ai-platform-dataops/env (mode 600, ext4 home) - never in the
# repository or under /drive. Data lives in the named Docker volume $DB_VOLUME.
set -euo pipefail

CONF_DIR="${DATAOPS_CONFIG_DIR:-$HOME/.config/ai-platform-dataops}"
ENV_FILE="$CONF_DIR/env"
CONTAINER="${DATAOPS_DB_CONTAINER:-ai-platform-dataops-db}"
IMAGE="${DATAOPS_DB_IMAGE:-mariadb:11.4}"
DB_VOLUME="${DATAOPS_DB_VOLUME:-ai-platform-dataops-db-data}"
HOST_PORT="${DATAOPS_DB_PORT:-13306}"

rand() { python3 -c 'import secrets; print(secrets.token_urlsafe(24))'; }

ensure_env() {
  if [[ -f "$ENV_FILE" ]]; then return; fi
  mkdir -p "$CONF_DIR"; chmod 700 "$CONF_DIR"
  umask 077
  cat > "$ENV_FILE" <<EOF
# ai-platform-dataops secrets (generated $(date -Is)). Not backed up; see docs/OPERATIONS.md.
DATAOPS_DB_HOST=127.0.0.1
DATAOPS_DB_PORT=$HOST_PORT
DATAOPS_DB_NAME=public_dataops
DATAOPS_DB_USER=dataops_app
DATAOPS_DB_PASSWORD=$(rand)
DATAOPS_DB_ROOT_PASSWORD=$(rand)
EOF
  echo "created $ENV_FILE"
}

load_env() {
  # shellcheck disable=SC1090
  set -a; source "$ENV_FILE"; set +a
}

up() {
  load_env
  if docker container inspect "$CONTAINER" >/dev/null 2>&1; then
    docker start "$CONTAINER" >/dev/null
  else
    # Root password only via the container environment; host binds to loopback only.
    docker run -d --name "$CONTAINER" --restart unless-stopped \
      -p "127.0.0.1:$HOST_PORT:3306" \
      -v "$DB_VOLUME:/var/lib/mysql" \
      -e MARIADB_ROOT_PASSWORD="$DATAOPS_DB_ROOT_PASSWORD" \
      -e MARIADB_AUTO_UPGRADE=1 \
      "$IMAGE" --character-set-server=utf8mb4 --collation-server=utf8mb4_unicode_ci >/dev/null
  fi
  for _ in $(seq 60); do
    if docker exec "$CONTAINER" sh -c 'mariadb-admin ping --protocol=tcp -h127.0.0.1 -uroot -p"$MARIADB_ROOT_PASSWORD" --silent' >/dev/null 2>&1; then
      echo "$CONTAINER up on 127.0.0.1:$HOST_PORT"; return
    fi
    sleep 1
  done
  echo "DB did not become ready" >&2; exit 1
}

root_sql() { docker exec -i "$CONTAINER" sh -c 'mariadb -uroot -p"$MARIADB_ROOT_PASSWORD"'; }

init() {
  ensure_env; up; load_env
  # App account: full rights on its own schema and on disposable test schemas only.
  root_sql <<EOF
CREATE DATABASE IF NOT EXISTS \`$DATAOPS_DB_NAME\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '$DATAOPS_DB_USER'@'%' IDENTIFIED BY '$DATAOPS_DB_PASSWORD';
ALTER USER '$DATAOPS_DB_USER'@'%' IDENTIFIED BY '$DATAOPS_DB_PASSWORD';
GRANT ALL PRIVILEGES ON \`$DATAOPS_DB_NAME\`.* TO '$DATAOPS_DB_USER'@'%';
GRANT ALL PRIVILEGES ON \`dataops\_test\_%\`.* TO '$DATAOPS_DB_USER'@'%';
FLUSH PRIVILEGES;
EOF
  echo "app account $DATAOPS_DB_USER ready on schema $DATAOPS_DB_NAME"
}

case "${1:-}" in
  init) init ;;
  up) ensure_env; up ;;
  down) docker stop "$CONTAINER" ;;
  status)
    docker ps -a --filter "name=^${CONTAINER}$" --format '{{.Names}} {{.Status}} {{.Ports}}'
    docker exec "$CONTAINER" sh -c 'mariadb-admin ping -uroot -p"$MARIADB_ROOT_PASSWORD"' ;;
  shell) load_env; docker exec -it "$CONTAINER" mariadb -u"$DATAOPS_DB_USER" -p"$DATAOPS_DB_PASSWORD" "$DATAOPS_DB_NAME" ;;
  *) sed -n '2,10p' "$0"; exit 2 ;;
esac
