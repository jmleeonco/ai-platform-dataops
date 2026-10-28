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
| Web app + worker | `deploy/web.sh start`, `http://127.0.0.1:8096/` | loopback bind; CSRF + Origin checks on every POST; jobs in `runtime/jobs` |
| Analysis agent | `~/.local/bin/claude` (headless), work dirs `~/.cache/ai-platform-dataops/agent/` | read-only tools, JSON-schema output, budget/time caps |
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

## Browser flow (owner decision 2026-10-28)

`deploy/web.sh start` runs the web app and one **worker** (jobs run one at a time; logs in
`runtime/jobs/<job>.log`, page `/jobs`).

1. **새 분석** (`/new`): an absolute folder under `DATAOPS_INPUT_ROOTS` (default
   `/mnt2/data/Public_dataset`; backup/hidden folders refused). Job `analyze`:
   `scan` (directory overview + NIfTI headers, ~1 min for AMOS 62 GB) → analysis agent → `spec`
   check → plan. Nothing is converted or sent.
2. **Analysis agent**: `claude -p` with tools `Read,Glob,Grep` only, `--permission-mode dontAsk`, no MCP,
   no settings, `--add-dir <dataset>`, work folder `~/.cache/ai-platform-dataops/agent/<run>/`,
   `--json-schema` (proposal in `dataops/agent.py`), cap `DATAOPS_AGENT_BUDGET_USD` (5) and
   `DATAOPS_AGENT_TIMEOUT_S` (1800), model `DATAOPS_AGENT_MODEL` (claude-opus-5-5). Dataset text is
   data. Raw output: `runs/<run>/agent_proposal.json`.
3. **Report** (`/runs/<run>`): summary, findings, questions, split × modality counts, included folders
   and what was left out (with reasons), rules, label names. Editable until approval: dataset fields
   (name, licence, commercial use, URL, PatientID prefix), included splits, labels on/off, modality of
   unresolved images, the two pilot subjects. Every edit is re-validated and bumps the plan revision.
   **Metadata** (`/runs/<run>/metadata`, also `.json` / `.csv`): every series with its extracted values
   (analysis decisions, source file, the full NIfTI header, normalised geometry, DICOM identifiers) and,
   per value, where it goes: DICOM header (local SCP pilot, Central after approval), management DB
   (`table.column`), or nowhere. Each DB claim is checked against the rows registration would write
   (`records.build_records(preview=True)`), each DICOM claim against the real pilot headers once frozen;
   the page also lists every tag of the pilot DICOM files with its origin. NIfTI headers are read once
   after the analysis into `runs/<run>/nifti_headers.json`; "원본 헤더 다시 읽기" re-reads changed or
   missing files (prepare never uses this cache).
4. **Pilot**: freezes only the pilot subjects (`manifest-pilot.json`: CT/MR DICOM + label RTSTRUCT) and
   sends them to the local SCP. Check them in OncoStudio on 107. Viewer: `/runs/<run>/series/<uid>`
   with the RTSTRUCT contours.
5. **Approval**: needs status `PILOT_SENT` (every pilot instance accepted) for the current plan, the
   approver's name and the dataset name typed as confirmation. Stored in `run.json` `approval`
   (plan sha256, pilot manifest sha256, target) and later in `ingest_runs.approval_json`.
6. **Central + DB** (job `publish`): full freeze (pilot artifacts reused after their sources are
   re-hashed) → C-STORE to Central with a journal → registration in the management DB. Refused unless
   `DATAOPS_CENTRAL_HOST/PORT/CALLED_AE` are set **and** `DATAOPS_CENTRAL_ENABLED=true`; the target is
   not set (owner, 2026-10-28), so an approval is recorded and publication waits.

CLI equivalents: `analyze DIR [--wait]`, `pilot RUN`, `approve RUN --by NAME --confirm NAME`,
`publish RUN [--dry-run]`, `worker [--once]`.

Space: a full AMOS freeze is roughly 40–60 GB of DICOM under `runtime/runs/<run>/artifacts` on
`/drive`, and the nightly backup hard-links it into `ai-platform-dataops-files/<date>`.

## Local SCP target

Owner decision 2026-10-28: the local SCP is OncoStudio's local OncoPACS **`OS_LJM@10.101.1.107:4001`**
on the Windows test PC (calling AE `DATAOPS`; the same node CONTRACT lists as the dev destination).
Override in `~/.config/ai-platform-dataops/env`:
`DATAOPS_LOCAL_SCP_HOST/PORT/CALLED_AE/CALLING_AE`.

The inherited host guard still refuses every host that is not this machine, except exact IPs in
`DATAOPS_LOCAL_SCP_REVIEW_HOSTS` (default `10.101.1.107`; empty = this machine only). It also refuses
the called AE `ONCOPACS` (the central SCP) on any host. The standalone `/send-local-scp` passes no
review hosts and stays this-machine-only.

```bash
python -m dataops echo                 # C-ECHO the configured target
python -m dataops send-local RUN --dry-run
```

OncoStudio must be running on 107 with its SCP open (C-ECHO from 183 with calling AE `DATAOPS`
succeeded on 2026-10-28; if `echo` reports no open SCP, start OncoStudio on 107). An Orthanc (`ORTHANC@127.0.0.1:4242`) runs on 183 as a
system service and is not a target.

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
