# Handover — state of the project (2026-11-09)

Read this first. It is the current picture; the other docs hold the details and some of their older status
sections (dated 2026-10-28/29) describe states that have since changed. Where they disagree, this file and
the dated owner decisions below win. Talk to the owner in **Korean**.

## 1. What this is

A system on **10.101.1.183** (repo `/drive/project/ai-platform-dataops`, GitHub
`jmleeonco/ai-platform-dataops`, branch **main**) that takes a medical imaging dataset folder and, in a
browser:

1. **Analyse**: fast scan + a headless Claude Code "analysis agent" (read-only tools, JSON-schema output)
   proposes how to read the folder; deterministic code (`spec.py`, `plan.py`) checks every field and builds a
   versioned plan. A standard HTML report (fixed tables 1–10, same shape for every dataset) shows it.
2. **Pilot**: 2 sample subjects are converted/frozen (NIfTI → DICOM, label maps → RTSTRUCT, DICOM copied
   byte-for-byte) and sent to the **local SCP** `OS_LJM@10.101.1.107:4001` (OncoStudio on the owner's test PC)
   for visual review.
3. **Approve**: the owner presses approve in the web; the approval is bound to the plan's sha256.
4. **Publish**: the full dataset is frozen and sent to **Central** `ONCOPACS@10.101.1.4:4002` (shared
   production PACS) by a separate adapter, then registered in the **new management DB**.

Since 2026-11-03 the management DB is also the company-wide catalogue of datasets already in use: it holds
copies of the existing LAB DB and of the model-training bundles in `/mnt/deploy/data`, browsable on the web.

## 2. Hard rules (owner decisions — do not relax without asking)

| Rule | Detail |
|---|---|
| Central is production | Send only runs the owner approved in the web and asked to send. Never press approve or run `publish` on the owner's behalf. Central enabled 2026-11-02 (env file only); only AMOS has gone there. |
| Local sender guard | `/send-local-scp` and `local_send.py` refuse any host except this machine and `DATAOPS_LOCAL_SCP_REVIEW_HOSTS` (107), and refuse AE `ONCOPACS`. Keep it; Central goes through `publish.py` only. |
| Existing DB 10.101.1.4 (`scpdb`, `oncolabdb`) | **SELECT only**, never write, no export into it (owner 10-29). The account is root — only `legacy_db.py` (schema) and `legacy_import.py` (5 tables: LAB datasets/items + enc maps, READ ONLY session) read it. Ask before reading any other table's rows. |
| Central queries | Read-only C-FIND and C-GET of RTSTRUCT (in memory) were approved 2026-11-03 (`central_find.py`). |
| `/mnt/deploy/data` | Read only. `/mnt` writes only for the nightly backup. |
| Secrets | Only in `~/.config/ai-platform-dataops/env` (mode 600). Never in git, docs, chat, reports, backups. Do not look for credentials in other folders. |
| Identifiers | Never quote patient names/IDs/dates from internal data (CIRT, YCC 4D Liver …) in chat, commits or reports. **Web pages** show real source values on the source side (owner 10-30/31); only the outgoing side is pseudonymised. |
| Pseudonymisation | Public data: never. Internal roots (`DATAOPS_INTERNAL_ROOTS`): profile `pseudonym-v2` (names/institution/IDs/order numbers out, UIDs replaced, dates and birth date kept). Original↔pseudonym map lives in a separate schema/account (`dataops_deid_map`). |
| Naming | PatientID prefix `<source>_<region>[_<modality>][_<sub>]_<PUB\|NC\|IO>` from Notion "04-1 Data prefix 규칙"; Notion is the source, the code (`plan.prefix_problems`, `.claude/skills/dataset-intake`) is a copy. |
| Reports | Same numbered tables for every dataset — no free-form per-dataset write-ups. |
| Images are transient (2026-11-09, CONTRACT §78.6) | "the db agent should not store image permanently. The images should be in central scp … It may store one project's image (if necessary) at a time." At most one run holds image files; prepare/pilot/publish refuse while another does. Purge (`purge <run>` or the run page) keeps the record. A complete publish + registration, a failed analysis and a superseded pilot-stage run are purged automatically. Backups hold no images. |
| No transfer without the user (U156) | No Central publish, local-SCP send, pilot or approve unless the user says so explicitly. |
| Other | No sudo on 183. Do not touch `/drive/project/backups` or other projects. No crontab changes beyond the backup block. Work on main, commit and push; ask before PRs or deleting branches. |

## 3. Running it

```bash
cd /drive/project/ai-platform-dataops
PY=~/.venvs/ai-platform-dataops/bin/python
set -a; . ~/.config/ai-platform-dataops/env; set +a          # for ad-hoc scripts
PYTHONPATH=. $PY -m pytest -q -p no:warnings                  # 134 tests, ~125 s (DB tests use dataops_test_* schemas)
deploy/web.sh start|stop|restart|restart-web|status|logs      # web (:8096) + one job worker; not auto-started after reboot
deploy/db.sh up                                                # Docker MariaDB 11.4, container ai-platform-dataops-db, 127.0.0.1:13306
$PY -m dataops db-status | db-migrate
```

Web: **http://10.101.1.183:8096/** (bound to the LAN by the owner's choice; **no authentication**; CSRF +
Origin checks on POSTs). `restart-web` after template/code changes; a running job keeps going.

CLI (`python -m dataops …`): `plan`, `ingest`, `validate`, `prepare`, `verify`, `register`, `show`,
`send-local`, `echo`, `runs`, `db-migrate`, `db-status`, `deid-map-save`, `import-legacy`, `import-deploy`
(`--dry-run`, `--rescan`), `worker`, `analyze`, `pilot`, `approve`, `publish`, `serve`, `prefix-check`,
`purge` (delete a run's image files on this host; `runs` shows each run's footprint).

Nightly backup: user crontab 00:30 → `ops/backup/nightly_backup.sh` → `/mnt/ai-pipeline/backups/ai-platform-dataops/`
(DB dumps incl. map DB, run metadata, repo bundle) and `…-files/` (rsync snapshot of `runtime/runs`).
`ops/backup/restore_check.sh` proves a restore. Details: docs/OPERATIONS.md.

Gotchas: `sed -i` on `/drive` (NTFS) prints "preserving permissions … Operation not permitted" but applies the
edit. pymysql: literal `%` in SQL must be `%%` (`db.query` always formats). `/mnt2` NFS (254) sometimes
returns ENOENT for minutes — every input read goes through `dataops/nfs.py`, which falls back to the
read-only mirror `/mnt2-v3-test` (138). Playwright is available to system `python3` (not the venv) for
screenshots.

## 4. Code map

| Area | Modules |
|---|---|
| Read the input | `inventory.py` (file kinds, DICOM headers), `scan.py` (fast overview for agent/report), `nfs.py` (flaky NFS) |
| Decide | `agent.py` (headless Claude proposal), `spec.py` (check proposal → plan), `plan.py` (plan schema 1 + validator, prefix rule), `rules.py` + `.claude/skills/dataset-intake` (naming/layout rules shared with the agent) |
| Make the bytes | `prepare.py` (freeze + sha256 manifest), `legacy.py` (inherited NIfTI→DICOM converter / sender), `rtstruct.py` (label map → RTSTRUCT, round-trip Dice), `deid.py` (pseudonym-v2) |
| Send | `local_send.py` (local SCP, per-instance journal, guard), `publish.py` (approval + Central adapter), `progress.py` |
| Record | `records.py` (the one run → DB-rows projection, used by preview and registration), `db.py` (migrations, idempotent registration, conflict rollback), `metadata.py` (per-series metadata schema), `report.py` (standard report) |
| Jobs / runs | `jobs.py` (analyze / pilot / publish jobs for the worker), `runs.py` (`runtime/runs/<run_id>/`) |
| Existing DB & catalogue imports | `legacy_db.py` (schema read, `/legacy` page), `legacy_import.py` (`import-legacy`), `deploy_import.py` (`import-deploy`), `central_find.py` (C-FIND / C-GET RTSTRUCT) |
| Web | `web/app.py` (runs, reports, sample views, Central page, backups), `web/actions.py` (POST actions), `web/dbview.py` (`/db` raw tables, live), `web/catalog.py` (`/datasets`, `/series/<uid>`, `/search`), `web/browse.py` (`/browse` flat table + search panel) |
| Ops | `deploy/web.sh`, `deploy/db.sh`, `ops/backup/*`, `ops/transfer_progress.py` |
| SQL | `sql/migrations/0001–0007` (management DB), `sql/deid_map_migrations/` (map DB) |

Runtime state (gitignored): `runtime/runs/<run>/` (run.json status + events, plan, manifests, journals,
artifacts), `runtime/jobs/`, `runtime/deploy_scan/` (cached header scans of the deploy bundles, ~65 MB),
`runtime/legacy_schema.json`.

## 5. What the management DB holds now (`public_dataops`, migration 0007)

datasets 128 · subjects 23,788 · studies 13,041 · series 43,560 · dataset_series 90,368 · instances 98,975 ·
transfer_results 99,066 · ingest_runs 1 · external_mappings 68. Three origins (`series.source_kind` tells
series apart):

| Origin | How it got there | Notes |
|---|---|---|
| This system's runs (1) | AMOS 2022, run `20261101T204942-01d59b23`, prefix `AMOS_ABD_MRCT_PUB`, 600 subjects, 98,975 instances sent to Central 2026-11-02 (status `CENTRAL_SENT`) | the only Central publication so far |
| Existing LAB DB (68 datasets) | `import-legacy` (owner 11-03): `oncolabdb.lab_datasets` / `lab_dataset_items` values, series level; enc maps → map DB | `external_mappings` row `oncolabdb.lab_datasets` per dataset; `source_kind=oncolabdb` |
| Deploy bundles (59 datasets, named `deploy/<model>/<version>`) | `import-deploy` (owner 11-03): each bundle's `train_valid_dict.json` / `dataset.json` lists; DICOM in the bundle read header-only (`deploy`), else linked to existing series, else read-only C-FIND on Central (`central-find`); Central-only RTSTRUCT got ROI names by C-GET | `dataset_series.split` = train / valid / valid,valid_gt from the lists; 1,057 list UIDs found nowhere; some bundles are list-only (patients, no series); MR_Totalseg skipped (meta.csv only) |

Both imports are idempotent and cheap to re-run (deploy scans cached). Details: docs/DATABASE.md.

Map DB `dataops_deid_map` (own account): original↔pseudonym rows from pseudonymised pilot runs and the
copied enc maps. Never shown outside the admin pages; backed up nightly.

## 6. Runs on disk (`runtime/runs`)

| Run | Data | Status |
|---|---|---|
| 20261101T204942-01d59b23 | AMOS (public) | CENTRAL_SENT, registered |
| 20261028T191410-e4d4c39c | AMOS, first try with the old prefix `AMOS` | PILOT_SENT (two cases sit in OncoStudio under the old ID) |
| 20261030T203648-c8800318 (PILOT_SENT); 20261030T102514-a36f8f81, 20261030T141153-ab218947, 20261030T201545-daac20aa (PILOT_READY) | CIRT (internal prostate CT + RTSTRUCT, pseudonymised) | local review only — duplicates of the same data; **not for Central** |
| 20261031T061554-cac8965b, 20261031T055249-d76925c6 | HNN_GTV (`INT_HNN_CT_GTV_IO`, pseudonymised), MM_PLV_GTV | PILOT_SENT — local review only, not approved |
| 20261030T115544-679ee22e, 20261101T004152-228df0e1 | Task04_Hippocampus, Task02_Heart (public) | PILOT_SENT — not approved |
| 20261031T055249-1532fe83, 20261031T061554-6ee801e2 | Prostate-3T, CT-ORG | PLANNED |
| 20261030T195103-6f2ec0d5, 20261031T055249-83b5e739, 20261031T055249-99421f76 | CIRT, CT-ORG, HNN_GTV early attempts | ANALYSIS_FAILED (superseded) |

`python -m dataops runs` lists them; each has a page at `/runs/<run_id>`. Since 2026-11-09 (DO2) only
20261101T004152-228df0e1 holds image files; every other run's images were purged (records kept).

## 7. Web pages

`/` dashboard · `/new` start an analysis · `/runs/<run>` report, plan edits, pilot, approve, publish, sample
views (source vs outgoing values), slice viewer with RTSTRUCT contours · `/reports` compare datasets ·
`/central` everything sent to Central · `/datasets` catalogue (origin filter, totals) · `/datasets/<uuid>`
(split / modality / ROI filters) · `/series/<uid>` (datasets holding it, ROI, referenced image, siblings,
transfers) · `/search` (datasets, ROI names, patients, series) · **`/browse`** ("표": one flat table per
series or per patient; search panel with dataset and ROI suggestions, ROI matching the RTSTRUCT and the image
it points at, ticks for modality/split/origin, date range, a field per column, chips, column picker, CSV) ·
`/db` raw tables (live) · `/legacy` existing DB's schema and compatibility · `/backups`.

## 8. Open threads (not done / waiting on the owner)

- **AMOS tier**: registered and sent as `AMOS_ABD_MRCT_PUB`, but the Notion example lists `AMOS_ABD_MRCT_NC`;
  the licence/tier question was raised, not settled.
- **CIRT**: several pilot runs of the same data exist (duplicates); clean-up not decided. CIRT and YCC 4D Liver
  contain identifiers in folder names and tags — never ingest as-is; YCC needs a de-id profile + path
  scrubbing and an owner decision first.
- **Security**: the web has no login while bound to the LAN; the legacy DB account is a shared root —
  a SELECT-only account and a password rotation were recommended to the owner; not done.
- **Not implemented**: verification of received objects on Central (scpdb read-back); metadata editing with
  revisions beyond the plan's reviewable fields. An export into oncolabdb is **not wanted** (owner 10-29).
- **Naming rule questions** with the owner: Track P keeps NIfTI on 138 instead of converting; duplicate-prefix
  check against the Notion Data List; TotalSegmentator source code TSG vs TSEG.
- **Performance**: dashboard ~1.3 s; `/browse` patient view ~1–2 s (aggregates over all memberships).
- **Report**: plreport topic `ai-platform-dataops` — 실험 2 (system summary,
  https://plreport.oncosoft.io/topics/ai-platform-dataops/02/index.html) is as of 11-01 and lacks Central
  send, progress UI, NFS fallback, Central page, legacy/deploy imports and the catalogue/browse pages. Update
  only when asked (`aireport pull ai-platform-dataops/02`).

## 9. Where to read more

README.md (Korean overview) · AGENTS.md (constraints) · docs/WORKFLOW.md (flow contract) ·
docs/IMPLEMENTATION.md (milestones M1–M5) · docs/DATABASE.md (schema, legacy and deploy imports) ·
docs/OPERATIONS.md (components, web pages, Central, local SCP, NFS, backup, restore) ·
docs/VALIDATION.md (recorded proofs) · docs/CONTEXT.md (early facts) · `.claude/skills/public-dataops`
(how to drive the pipeline) · `.claude/skills/dataset-intake` (naming + folder layouts).
