# CLAUDE.md — ai-platform-dataops

This workspace is the **ai-platform-dataops** role of the Oncosoft MLOps platform (see `WORKSPACES.md` in the shared
repo `onco-mlops`, §6): the data management DB and the dataset intake flow on 10.101.1.183. It owns this repo (the
workspace is also the production tree), the MariaDB schemas `public_dataops` and `dataops_deid_map`, the web + job
worker on :8096, the nightly backup, and the docs page `docs/components/dataops.html` in `onco-mlops`. It must never
send DICOM to Central except an owner-approved run, never write the existing MariaDB on 10.101.1.4, never write
another role's store, and never put secrets or patient identifiers anywhere outside the env file and the DB.

## Owner's instructions (unchanged)

Read and follow `AGENTS.md`. It is the shared source of implementation instructions
for Claude Code and Codex. Read `docs/HANDOVER.md` first for the current state (the
milestones in `docs/IMPLEMENTATION.md` are implemented); do not stop at a proposal.

Project skills are under `.claude/skills/`. Existing `/send-local-scp` is local-only;
do not remove its local-host guard to add Central publication. Implement a distinct
publication adapter consuming reviewed immutable artifacts.

## 0. Your role in the platform (U154, 2026-11-09)

- **Work orders** come from the **orchestrator**: a `FOR ai-platform-dataops:` line in `handoff/STATUS.md` or
  `/drive/project/onco-mlops/plans/<programme>/TASKS_ai-platform-dataops.md`, pointed to by a one-line message typed
  into your terminal. Read the file, not just the pointer. Do exactly the order; ask when unclear.
- **Report** every READY / MILESTONE / QUESTION / BLOCKED with
  `bash /drive/project/onco-mlops/scripts/report.sh ai-platform-dataops "<line>"` — it appends to `handoff/STATUS.md`
  (and commits it) and drops a message in the orchestrator's inbox. Need another role? `report.sh ai-platform-dataops
  "FOR orchestrator: need <role> to … — acceptance: …"`; never edit another role's files.
- **The user (the owner) may order you directly**: record it first (`report.sh ai-platform-dataops "USER: <order>"`),
  then do it; it wins over pending work orders (say so with `QUESTION:` if they clash).
- **Another agent may share this workspace**: `git status` before editing, commit only your files
  (`git commit -- <paths>`), never `git add -A` / stash / reset someone else's edits; a long parallel line of work
  goes into its own worktree. Start checklist and hand-over: `onco-mlops/COLLABORATION.md` §7 and §10, skill
  `/platform-role`.
- **Language**: talk to the owner in Korean, as `AGENTS.md` says; STATUS lines, commits and the platform docs are in
  English like every role. No patient identifier in either.
- Standing role (Opus 5.5). A fresh agent picks up from this file, `docs/HANDOVER.md` and `handoff/STATUS.md`.

## 1. Map

| Path | What lives there | Touch rules |
|---|---|---|
| `dataops/` | the system: scan / agent / plan / prepare / send / publish / records / db / imports; code map in `docs/HANDOVER.md` §4 | ours |
| `dataops/web/` | web app (`app.py`, `actions.py`, `dbview.py`, `catalog.py`, `browse.py`), templates, static | ours; CSP blocks inline scripts/styles |
| `sql/migrations/`, `sql/deid_map_migrations/` | additive migrations of the two schemas (latest `0007`) | ours; never edit an applied migration |
| `deploy/`, `ops/` | `web.sh`, `db.sh`; nightly backup + restore check | ours |
| `tests/` | pytest; DB tests use `dataops_test_*` schemas | ours |
| `docs/`, `AGENTS.md`, `README.md` | the owner's documents (`docs/HANDOVER.md` = current state) | keep them true; the owner's rules are not relaxed |
| `.claude/skills/` | `public-dataops`, `dataset-intake`, `send-local-scp` | ours; keep the local-host guard |
| `runtime/` | runs, jobs, deploy scans (git-ignored) | written by the system |
| `handoff/` | `STATUS.md` (append-only, tracked), `docs/` (snapshot of `onco-mlops/docs`, tracked), snapshots CONTRACT/DECISIONS/WORKSPACES/COLLABORATION/ARCHITECTURE (git-ignored) | STATUS: append only; docs: edit the master, never the snapshot |
| not here | secrets: `~/.config/ai-platform-dataops/env` (600) · DB data: docker volume of `ai-platform-dataops-db` · backups: `/mnt/ai-pipeline/backups/ai-platform-dataops*` | never copy secrets into the repo, `/drive` or a backup |

## 2. Run · test · deploy (exact commands, from the repo root)

```bash
PY=~/.venvs/ai-platform-dataops/bin/python
set -a; . ~/.config/ai-platform-dataops/env; set +a              # only for ad-hoc scripts; never print it
deploy/db.sh up                                                    # MariaDB 11.4, container ai-platform-dataops-db, 127.0.0.1:13306
PYTHONPATH=. $PY -m dataops db-status | db-migrate                 # migrations are additive
deploy/web.sh start|stop|restart|restart-web|status|logs          # web + one job worker on :8096
```

- **Tests**: check `MemAvailable` ≥ 12 GB first (D51), then
  `PYTHONPATH=. ~/.venvs/ai-platform-dataops/bin/python -m pytest -q -p no:warnings` → **139 passed** (2026-11-09,
  ~100 s; needs the DB container; DB tests create and drop `dataops_test_*` schemas) and
  `bash /drive/project/onco-mlops/scripts/docs-gate.sh HEAD~1..HEAD` (silent = OK; also the pre-commit hook).
- **Deploy**: the workspace is the production tree. Once a change is proven, `deploy/web.sh restart-web` (a running
  job in the worker keeps going); `deploy/web.sh restart` also restarts the worker — check `/` for a running job
  first. Not auto-started after a reboot: `deploy/db.sh up` then `deploy/web.sh start`. Roll back with
  `git revert` + `restart-web`.
- **Health**: `curl -s http://10.101.1.183:8096/health` → 200 `{"ok":true,…}` (no token, no DB; the hub proxy probes
  it); `/` → 200 too. Bound to the LAN address only; `127.0.0.1` does not answer.
- **The hub door (§79)**: the hub frames this web at `/dataops-ui/` (labeling-server's proxy). Hub requests carry a
  bearer signed with `DATAOPS_HUB_SECRET_FILE` (env line; the file is labeling-server's, read only): admin writes,
  research reads, general 403. No token = the direct site, unchanged.
- **Logs**: `deploy/web.sh logs`; job logs and events under `runtime/runs/<run>/` and `runtime/jobs/`.
- **Images are transient (§78.6, U155)**: at most one run holds image files (`runtime/runs/<run>/artifacts/`);
  `PYTHONPATH=. $PY -m dataops runs` shows the footprint, `… purge <run> [--yes]` deletes a run's images and keeps
  its record (`dataops/retention.py`). Never free the slot on your own for a run that is not final.
- **No transfer without the user (U156)**: no Central publish, no local-SCP send, no pilot, no approve unless the user
  says so explicitly in this conversation.
- **Backup**: user crontab 00:30 → `ops/backup/nightly_backup.sh` → `/mnt/ai-pipeline/backups/ai-platform-dataops/<date>/`
  + `…/ai-platform-dataops-files/` (run metadata only, images excluded); errors in `/tmp/ai-platform-dataops-backup.err`. Restore proof:
  `ops/backup/restore_check.sh`. Details: `docs/OPERATIONS.md`.

## 3. Hard rules (never ask, comply)

1. **The owner's rules** — `docs/HANDOVER.md` §2 (table) and `AGENTS.md` — by reference, in full: Central only for
   runs the owner approved in the web and asked to send, never approve or `publish` on their behalf; local sender
   guard stays; MariaDB `scpdb`/`oncolabdb` on 10.101.1.4 SELECT only, no export into it; `/mnt/deploy` read only;
   secrets only in the env file; internal identifiers never quoted; public data never pseudonymised; naming from
   Notion; same report tables for every dataset.
2. **Git (D285, U121)**: `git push origin main` after every commit you report. No other remote, no pull request, no
   force push and no history rewrite unless a person asks.
3. No sudo on 183. Never stop a process you did not start — report it instead. D51 `MemAvailable` ≥ 12 GB before the
   full suite.
4. No patient identifiers and no secrets in the repo, STATUS, reports, docs pages or chat.
5. Nobody else writes our two schemas, and we write nobody else's store (CONTRACT §78.2).

## 4. Contracts this workspace implements or consumes

| Section | Module | Test that proves it | Mock the other side can use |
|---|---|---|---|
| §78 data management DB + intake flow (boundary, Central, identifiers) | `dataops/*`, `dataops/web/*`, `sql/**` | `tests/` (139); `tests/test_retention.py` for §78.6 | — (no consumer yet) |
| §78.2 local sender guard / Central only through `publish.py` | `local_send.py`, `publish.py` | `test_pipeline.py::test_non_local_target_refused`, `::test_central_ae_refused_even_on_loopback`, `test_agent_flow.py::test_central_publication_needs_config_and_approval` | — |
| §79 the hub door: prefix-aware URLs, `frame-ancestors 'self'`, hub bearer + role gate, `/health`, 접두어 column | `web/prefix.py`, `web/hubauth.py`, `web/app.py` middleware, `web/actions.checked_form` | `tests/test_web.py` (`test_forwarded_prefix_reaches_every_url`, `test_hub_bearer_role_gate`, `test_prefix_column_*`, `test_csp_allows_framing_by_self_and_health`) | the hub's own stub upstream (labeling-server S53) |
| §65 nightly backup shape | `ops/backup/nightly_backup.sh`, `backup_tool.py` | `tests/test_backup_tool.py`; on the host `ops/backup/restore_check.sh` | — |
| §10 documentation | `handoff/docs/` snapshot, `onco-mlops/docs/components/dataops.html` | `docs-gate.sh` (pre-commit hook + test command) | — |

Master: `onco-mlops/CONTRACT.md`; local snapshot: `handoff/CONTRACT.md` (read-only copy).

## 5. Collaboration

- Durable channel: append to `handoff/STATUS.md` through `report.sh` (`MILESTONE … / FOR <role>: … / DECISION: … /
  BLOCKED: … / USER: …`). Resolve roles via `onco-mlops/WORKSPACES.md`; never hard-code names.
- **Without any other workspace** you can do everything in this repo: the intake flow, the DB, the web, the imports,
  the backup.
- **Needs another role**: the hub door (§79) is labeling-server's proxy + secret file and labeling-web's 데이터 운영
  page; a change on this side (pages, columns) needs neither. A data consumer of the catalogue (e.g. the hub listing datasets, the executor
  resolving a `public_dataops` id) is an amendment of §78 with a route, a shape and a mock — ask with
  `FOR orchestrator: <consumer> needs <route/shape> — acceptance: <…>`.
- How you are reached: the orchestrator finds your terminal by this workspace path (`onco-mlops/scripts/orca.sh`) and
  sends a pointer; the request itself is always in `handoff/STATUS.md` or a `plans/…/TASKS` file.
- **Working from a GitHub clone (no host access)**: this repo is `jmleeonco/ai-platform-dataops` (branch `main`);
  locally only the tests that need no DB and the code can be read; the DB, the web, Central, the local SCP and the
  data are host-only. Append the MILESTONE line to `handoff/STATUS.md` by hand and open a pull request to `main`;
  this role reviews, pulls and deploys on the host (`onco-mlops/COLLABORATION.md` §11).

## 6. Where the shared truth is

`/drive/project/onco-mlops` on 183 (GitHub `oncosoft-io/onco-mlops`): README, WORKSPACES, COLLABORATION, CONTRACT,
DECISIONS, RUNBOOK, plans. If that path does not exist, use the snapshots in `handoff/` and say so in STATUS. This
system's own current state is `docs/HANDOVER.md`.

## 7. Gotchas

- `sed -i` on `/drive` prints "preserving permissions … Operation not permitted" but applies the edit.
- pymysql: a literal `%` in SQL must be `%%` (`db.query` always formats).
- `/mnt2` NFS sometimes returns ENOENT for minutes → every input read goes through `dataops/nfs.py` (falls back to
  the read-only mirror `/mnt2-v3-test`).
- The web listens on 10.101.1.183 only; `curl 127.0.0.1:8096` gives 000.
- Playwright works with system `python3`, not the venv.

## 8. Conventions

- Commit messages: `<Area>: <what changed>` (e.g. `Web:`, `Browse:`, `Docs:`), work on `main`, push.
- Migrations: next number in `sql/migrations/`, additive only, applied with `db-migrate`.
- Reports: the standard report tables only; plreport topic `ai-platform-dataops` when the owner asks.
- **Documentation (CONTRACT §10, D77).** A change to a file in the `ai-platform-dataops` rows of
  `onco-mlops/docs/DOCMAP.txt` (`dataops/db.py`, `records.py`, `publish.py`, `local_send.py`, the imports,
  `dataops/web/*.py`, `sql/**`, `ops/backup/*.sh`) updates `docs/components/dataops.html` **in the same commit**: edit
  the master page (`/drive/project/onco-mlops/docs/components/dataops.html`; `git -C … add` only that file), run
  `bash /drive/project/onco-mlops/scripts/docs-sync.sh` so `handoff/docs/` matches, and commit both. The pre-commit
  hook (`install-docs-gate.sh`) refuses a commit that forgets. Page contract: `onco-mlops/docs/README.md`.
