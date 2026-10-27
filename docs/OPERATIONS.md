# Operations on 10.101.1.183

Owner decisions 2026-10-28: the management DB lives on this server (Docker MariaDB, user-level,
no sudo); nightly backups go to the NAS under `/mnt/ai-pipeline/backups` (CONTRACT §65 layout);
data goes to the **local SCP only** — no Central publication for now; a locally hosted HTML viewer
shows runs, extracted metadata, DB rows, delivery results and backups.

## Components

| Component | Where | Notes |
|---|---|---|
| Python env | `~/.venvs/ai-platform-dataops` | `uv venv` (python3-venv/ensurepip unavailable; `/drive` is NTFS so the venv is on ext4) |
| Management DB | container `ai-platform-dataops-db` (`mariadb:11.4`), `127.0.0.1:13306`, volume `ai-platform-dataops-db-data` | schema `public_dataops`, app account `dataops_app` (rights on its schema and `dataops_test_%` only) |
| Secrets | `~/.config/ai-platform-dataops/env` (mode 600) | DB app/root passwords; never in Git, never under `/drive`, never in backups |
| Runtime | `runtime/runs/<run_id>/` in the repo (gitignored) | inventory, plan, frozen artifacts, manifest, transfer journals, registration |
| Web viewer | `deploy/web.sh start`, `http://127.0.0.1:8096/` | read-only; loopback bind |
| Nightly backup | `ops/backup/nightly_backup.sh`, user crontab `30 0 * * *` | block marked `# >>> ai-platform-dataops` |

Hub 0:00, executor 0:10 and ai-pipeline 0:20 back up before this system's 0:30 slot.

## Setup / after a reboot

```bash
cd /drive/project/ai-platform-dataops
~/.local/bin/uv venv -p python3.10 ~/.venvs/ai-platform-dataops        # once
VIRTUAL_ENV=~/.venvs/ai-platform-dataops ~/.local/bin/uv pip install -r requirements-dev.txt
deploy/db.sh init        # once: creates secrets + container + app account (idempotent); later: deploy/db.sh up
~/.venvs/ai-platform-dataops/bin/python -m dataops db-migrate
deploy/web.sh start      # not started automatically after a reboot
```

The DB container has `--restart unless-stopped`; the web viewer does not restart by itself.
To view it from a PC: `ssh -L 8096:127.0.0.1:8096 ubuntu@10.101.1.183`, then open
http://127.0.0.1:8096/. (Binding to `0.0.0.0` via `DATAOPS_BIND_HOST` would expose dataset
metadata to the LAN without authentication; decide that explicitly.)

## Local SCP target

Default from `config/local-scp.example.json`: `OS_LJM@127.0.0.1:4001` (calling AE `DATAOPS`).
Override in `~/.config/ai-platform-dataops/env`:
`DATAOPS_LOCAL_SCP_HOST/PORT/CALLED_AE/CALLING_AE`. The inherited host guard accepts only
addresses of this machine. Nothing listens on 4001 on 183 as of 2026-10-28 (OS_LJM is the
OncoStudio SCP on the workstation); an Orthanc (`ORTHANC@127.0.0.1:4242`) runs on 183 as a
system service but has not been chosen as a target. `send-local --dry-run` shows what would go.

## Nightly backup (CONTRACT §65)

`/mnt/ai-pipeline/backups/ai-platform-dataops/<YYYY-MM-DD>/`:
`db/public_dataops.sql.gz` (mariadb-dump, single transaction), `db/row_counts.tsv`,
`db/server_version.txt`, `runs/runs-metadata.tar.gz` (plans, manifests, UID derivations, journals,
registrations; no artifacts), `repo.bundle` (all refs), `crontab.txt` (this system's block),
`manifest.json` (files, sizes, sha256, commit, duration, `ok`, DB row counts, artifact check).
`/mnt/ai-pipeline/backups/ai-platform-dataops-files/<YYYY-MM-DD>/` is an `rsync --link-dest`
snapshot of `runtime/runs` (unchanged artifacts are hard links). One log line per run in
`ai-platform-dataops/backup.log`; cron stderr in `/tmp/ai-platform-dataops-backup.err`.
`flock` on `/tmp/ai-platform-dataops-backup.lock`. Retention: 14 daily folders plus the first
folder of each month for 12 months, never today's; only `YYYY-MM-DD` folders inside the two
folders above are ever deleted. Uncommitted working-tree changes are not in the bundle (the
manifest records their count).

Commands:
```bash
ops/backup/nightly_backup.sh              # run now (same-day re-run replaces today's folder)
ops/backup/restore_check.sh [YYYY-MM-DD]  # prove restore without touching live data
ops/backup/install_cron.sh [--remove]     # add/replace or remove the crontab block
```

## Restore

1. Secrets: recreate `~/.config/ai-platform-dataops/env` with `deploy/db.sh init` on an empty
   volume (new passwords), or restore the file from wherever the owner keeps it.
2. DB: `deploy/db.sh init`, then
   `gunzip -c <day>/db/public_dataops.sql.gz | docker exec -i ai-platform-dataops-db sh -c 'mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" public_dataops'`
   and compare with `db/row_counts.tsv`.
3. Artifacts and run folders: `rsync -a <root>/ai-platform-dataops-files/<day>/ runtime/runs/`
   then `python -m dataops verify RUN` for each run.
4. Code: `git clone <day>/repo.bundle` (or GitHub).

`restore_check.sh` does steps 2–4 into a throw-away container and temp folder and reports
`RESTORE CHECK OK` (see docs/VALIDATION.md for the recorded proof).
