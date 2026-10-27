#!/usr/bin/env bash
# Prove a backup restores: DB dump -> fresh throw-away MariaDB container and row counts,
# repo bundle -> clone at the recorded commit, artifact snapshot -> re-hash, run metadata -> untar.
# Touches nothing in the live DB, runtime folder or backup folders.
#
#   ops/backup/restore_check.sh [YYYY-MM-DD]      (default: latest backup)
set -Eeuo pipefail

SYSTEM=ai-platform-dataops
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ROOT="${DATAOPS_BACKUP_ROOT:-/mnt/ai-pipeline/backups}"
BASE="$ROOT/$SYSTEM"
DAY="${1:-$(ls "$BASE" | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' | sort | tail -1)}"
DIR="$BASE/$DAY"
IMAGE="${DATAOPS_DB_IMAGE:-mariadb:11.4}"
TOOL=(python3 "$REPO/ops/backup/backup_tool.py")
TMP="$(mktemp -d)"
NAME="$SYSTEM-restore-check-$$"
PW="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; rm -rf "$TMP"; }
trap cleanup EXIT

echo "== restore check of $DIR"
"${TOOL[@]}" verify-files "$DIR"
DB_NAME="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["db_name"])' "$DIR/manifest.json")"
COMMIT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["commit"])' "$DIR/manifest.json")"

echo "== DB: load dump into a fresh container ($IMAGE, no published port)"
docker run -d --name "$NAME" -e MARIADB_ROOT_PASSWORD="$PW" "$IMAGE" >/dev/null
for _ in $(seq 90); do
  docker exec "$NAME" mariadb-admin ping --protocol=tcp -h127.0.0.1 -uroot -p"$PW" --silent >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$NAME" mariadb -uroot -p"$PW" -e "CREATE DATABASE restore_check CHARACTER SET utf8mb4"
gunzip -c "$DIR/db/$DB_NAME.sql.gz" | docker exec -i "$NAME" mariadb -uroot -p"$PW" restore_check
MISMATCH=0
while IFS=$'\t' read -r t n; do
  got="$(docker exec "$NAME" mariadb -uroot -p"$PW" -N -B -e "SELECT COUNT(*) FROM restore_check.\`$t\`")"
  if [[ "$got" == "$n" ]]; then echo "  $t: $got rows ok"; else echo "  $t: expected $n got $got"; MISMATCH=1; fi
done < "$DIR/db/row_counts.tsv"

echo "== repo bundle"
git clone -q "$DIR/repo.bundle" "$TMP/repo" 2>/dev/null || git init -q "$TMP/repo"
if git -C "$TMP/repo" fetch -q "$DIR/repo.bundle" '+refs/*:refs/remotes/bundle/*' 2>/dev/null \
   && git -C "$TMP/repo" cat-file -e "$COMMIT^{commit}" 2>/dev/null; then
  echo "  commit $COMMIT present"
else
  echo "  commit $COMMIT MISSING"; MISMATCH=1
fi

echo "== run metadata"
if [[ -f "$DIR/runs/runs-metadata.tar.gz" ]]; then
  tar -xzf "$DIR/runs/runs-metadata.tar.gz" -C "$TMP"
  echo "  runs restored: $(find "$TMP/runs" -maxdepth 2 -name run.json | wc -l)"
fi

echo "== artifact snapshot"
"${TOOL[@]}" verify-artifacts "$ROOT/$SYSTEM-files/$DAY" || MISMATCH=1

if [[ $MISMATCH -eq 0 ]]; then echo "RESTORE CHECK OK ($DAY)"; else echo "RESTORE CHECK FAILED ($DAY)"; exit 1; fi
