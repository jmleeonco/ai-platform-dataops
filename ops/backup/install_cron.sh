#!/usr/bin/env bash
# Add (or replace) this system's block in the user crontab; other lines are left exactly as they are.
# The block: the nightly backup (00:30, CONTRACT §65) and the dataset catalogue refresh (02:30, §78.7).
#   ops/backup/install_cron.sh          install / update
#   ops/backup/install_cron.sh --remove remove the block
set -euo pipefail
SYSTEM=ai-platform-dataops
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BEGIN="# >>> $SYSTEM nightly backup (CONTRACT §65, 00:30) + catalogue refresh (§78.7, 02:30) >>>"
END="# <<< $SYSTEM <<<"

current="$(crontab -l 2>/dev/null || true)"
others="$(printf '%s\n' "$current" | sed "/^# >>> $SYSTEM/,/^# <<< $SYSTEM/d")"
if [[ "${1:-}" == "--remove" ]]; then
  printf '%s\n' "$others" | sed '/./,$!d' | crontab -
  echo "removed $SYSTEM block"; exit 0
fi
block="$BEGIN
30 0 * * * /bin/bash $REPO/ops/backup/nightly_backup.sh >/dev/null 2>>/tmp/$SYSTEM-backup.err
30 2 * * * /bin/bash $REPO/ops/catalogue_nightly.sh 2>>/tmp/$SYSTEM-catalogue.err
$END"
printf '%s\n%s\n' "$others" "$block" | sed '/./,$!d' | crontab -
crontab -l | sed -n "/^# >>> $SYSTEM/,/^# <<< $SYSTEM/p"
