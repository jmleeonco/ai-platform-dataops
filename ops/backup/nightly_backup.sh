#!/usr/bin/env bash
# Nightly backup of ai-platform-dataops to the NAS (CONTRACT §65 layout; user crontab 30 0 * * *).
#
#   <root>/ai-platform-dataops/<YYYY-MM-DD>/        db/public_dataops.sql.gz, db/row_counts.tsv,
#                                                    runs/runs-metadata.tar.gz, repo.bundle, crontab.txt,
#                                                    manifest.json (sizes, sha256, commit, duration, ok)
#   <root>/ai-platform-dataops-files/<YYYY-MM-DD>/  rsync --link-dest snapshot of runtime/runs (artifacts)
#   <root>/ai-platform-dataops/backup.log           one line per run
#
# Secrets (~/.config/ai-platform-dataops/env) are never copied. Only folders under
# <root>/ai-platform-dataops* are created or deleted. Exit status is non-zero on failure.
set -Eeuo pipefail

SYSTEM=ai-platform-dataops
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ROOT="${DATAOPS_BACKUP_ROOT:-/mnt/ai-pipeline/backups}"
BASE="$ROOT/$SYSTEM"
FILES_BASE="$ROOT/$SYSTEM-files"
RUNTIME="${DATAOPS_RUNTIME_DIR:-$REPO/runtime}"
CONTAINER="${DATAOPS_DB_CONTAINER:-ai-platform-dataops-db}"
DB_NAME="${DATAOPS_DB_NAME:-$(sed -n 's/^DATAOPS_DB_NAME=//p' "${DATAOPS_CONFIG_DIR:-$HOME/.config/ai-platform-dataops}/env" 2>/dev/null || true)}"
DB_NAME="${DB_NAME:-public_dataops}"
TOOL=(python3 "$REPO/ops/backup/backup_tool.py")
DAY="$(date +%F)"
STARTED="$(date +%s)"
LOCK="/tmp/$SYSTEM-backup.lock"     # local lock file: the NAS is NFS v3 with local_lock=none

exec 9>"$LOCK"
if ! flock -n 9; then echo "$(date -Is) another $SYSTEM backup is running" >&2; exit 75; fi

[[ -d "$ROOT" ]] || { echo "backup root $ROOT not mounted" >&2; exit 1; }
mkdir -p "$BASE" "$FILES_BASE"
chmod 700 "$BASE" "$FILES_BASE"
WORK="$BASE/.partial-$DAY"
SNAP_WORK="$FILES_BASE/.partial-$DAY"
COMMIT="$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo unknown)"

log() { echo "$(date -Is) $*" >> "$BASE/backup.log"; echo "$*"; }

fail() {
  local rc=$? line=${1:-?}
  trap - ERR
  mkdir -p "$WORK"
  "${TOOL[@]}" manifest "$WORK" --started "$STARTED" --commit "$COMMIT" --ok 0 \
    --error "failed at line $line (exit $rc)" >/dev/null 2>&1 || true
  log "FAIL $DAY line $line exit $rc (partial kept in $WORK)"
  exit 1
}
trap 'fail $LINENO' ERR

# Leftovers of earlier interrupted runs, inside our own folders only.
find "$BASE" "$FILES_BASE" -maxdepth 1 -name '.partial-*' -type d -exec rm -rf {} +
mkdir -p "$WORK/db" "$WORK/runs"

# 1. Database: consistent logical dump (credentials stay inside the container environment).
docker exec "$CONTAINER" sh -c \
  "mariadb-dump -uroot -p\"\$MARIADB_ROOT_PASSWORD\" --single-transaction --routines --triggers --events --hex-blob '$DB_NAME'" \
  | gzip -6 > "$WORK/db/$DB_NAME.sql.gz"
gzip -t "$WORK/db/$DB_NAME.sql.gz"
TABLES="$(docker exec "$CONTAINER" sh -c "mariadb -uroot -p\"\$MARIADB_ROOT_PASSWORD\" -N -B -e \"SELECT table_name FROM information_schema.tables WHERE table_schema='$DB_NAME' AND table_type='BASE TABLE' ORDER BY table_name\"")"
: > "$WORK/db/row_counts.tsv"
for t in $TABLES; do
  n="$(docker exec "$CONTAINER" sh -c "mariadb -uroot -p\"\$MARIADB_ROOT_PASSWORD\" -N -B -e 'SELECT COUNT(*) FROM \`$DB_NAME\`.\`$t\`'")"
  printf '%s\t%s\n' "$t" "$n" >> "$WORK/db/row_counts.tsv"
done
docker exec "$CONTAINER" mariadb --version > "$WORK/db/server_version.txt"

# 2. Run metadata (plans, inventories, manifests, UID maps, transfer journals, registrations).
if [[ -d "$RUNTIME/runs" ]]; then
  tar -czf "$WORK/runs/runs-metadata.tar.gz" -C "$RUNTIME" --exclude='runs/*/artifacts' --exclude='*.lock' \
    --exclude='*.tmp' runs
fi

# 3. Frozen artifacts: hard-linked daily snapshot (unchanged files cost no space).
PREV="$(find "$FILES_BASE" -maxdepth 1 -type d -regextype posix-extended -regex '.*/[0-9]{4}-[0-9]{2}-[0-9]{2}' \
        ! -name "$DAY" | sort | tail -1)"
mkdir -p "$SNAP_WORK"
if [[ -d "$RUNTIME/runs" ]]; then
  rsync -a --delete --exclude='*.lock' --exclude='*.tmp' ${PREV:+--link-dest="$PREV"} "$RUNTIME/runs/" "$SNAP_WORK/"
fi
ART_CHECK="$("${TOOL[@]}" verify-artifacts "$SNAP_WORK")"

# 4. Repository (all refs) and this system's crontab block.
git -C "$REPO" bundle create "$WORK/repo.bundle" --all 2>/dev/null
git -C "$REPO" bundle verify "$WORK/repo.bundle" >/dev/null 2>&1
DIRTY="$(git -C "$REPO" status --porcelain | wc -l)"
crontab -l 2>/dev/null | sed -n "/# >>> $SYSTEM/,/# <<< $SYSTEM/p" > "$WORK/crontab.txt" || true

# 5. Manifest, then publish atomically by rename.
EXTRA="$(printf '{"artifacts_snapshot": "%s", "artifacts_check": "%s", "uncommitted_files": %s, "db_name": "%s"}' \
  "$FILES_BASE/$DAY" "$ART_CHECK" "$DIRTY" "$DB_NAME")"
"${TOOL[@]}" manifest "$WORK" --started "$STARTED" --commit "$COMMIT" --ok 1 --extra "$EXTRA" >/dev/null
rm -rf "$BASE/$DAY" "$FILES_BASE/$DAY"      # a same-day re-run replaces today's folders
mv "$SNAP_WORK" "$FILES_BASE/$DAY"
mv "$WORK" "$BASE/$DAY"

# 6. Retention inside our own folders only.
"${TOOL[@]}" prune "$BASE" --today "$DAY" >/dev/null
"${TOOL[@]}" prune "$FILES_BASE" --today "$DAY" >/dev/null

SIZE="$(du -sh "$BASE/$DAY" | cut -f1)"
log "OK $DAY $SIZE in $(( $(date +%s) - STARTED ))s commit ${COMMIT:0:10} ($ART_CHECK)"
