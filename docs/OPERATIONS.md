# Operations on 10.101.1.183

Owner decisions 2026-10-28: the management DB lives on this server (Docker MariaDB, user-level,
no sudo); nightly backups go to the NAS under `/mnt/ai-pipeline/backups` (CONTRACT §65 layout);
data goes to the **local SCP only** — no Central publication for now; a locally hosted HTML viewer
shows runs, extracted metadata, DB rows, delivery results and backups.
Later decisions: Central publication is enabled for owner-approved runs since 2026-11-02 (Browser flow step 6
below and docs/HANDOVER.md for the current state).

## Components

| Component | Where | Notes |
|---|---|---|
| Python env | `~/.venvs/ai-platform-dataops` | `uv venv` (python3-venv/ensurepip unavailable; `/drive` is NTFS so the venv is on ext4) |
| Management DB | container `ai-platform-dataops-db` (`mariadb:11.4`), `127.0.0.1:13306`, volume `ai-platform-dataops-db-data` | schema `public_dataops`, app account `dataops_app` (rights on its schema and `dataops_test_%` only) |
| Pseudonym map DB | same container, schema `dataops_deid_map`, account `dataops_deid` (rights on that schema only) | original ↔ pseudonym of pseudonymised runs; written when pseudonymised files are made; see docs/DATABASE.md |
| Secrets | `~/.config/ai-platform-dataops/env` (mode 600) | DB app/root/map passwords; never in Git, never under `/drive`, never in backups |
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
http://127.0.0.1:8096/. Since 2026-10-29 the owner has it bound to the LAN address
(`DATAOPS_BIND_HOST=10.101.1.183` in the env file): http://10.101.1.183:8096/ — no authentication, so
anyone on the LAN can use the pilot and approval forms.

## DB browser (read-only)

`/db`: every table of the management DB with a Korean description, row count, primary key and links
(the chain datasets → subjects → studies → series → instances → transfer_results, plus dataset_series,
ingest_runs, external_mappings, schema_migrations). `/db/<table>`: search over text columns, exact filters
as `?column=value` (`NULL` for IS NULL), sorting, 25–200 rows per page, CSV of the current filter
(`/db/<table>.csv`, up to 100,000 rows); foreign-key values link to the referenced row.
`/db/<table>/row?<pk>=…`: one row with JSON pretty-printed, links to its parents and the number of child
rows in each referencing table. SELECT only; table/column names are whitelisted from information_schema
and values are bound parameters (`dataops/web/dbview.py`).

The pages are live: every 5 s they poll `/db/state.json?t=<tables>&exact=1` (the live `CHECKSUM TABLE` of
the tables the page shows rows of; the overview polls row counts only) and re-render in place when a mark
differs from the one the page was drawn with, so a registration, update or delete shows up without a
reload. Polling pauses while the tab is hidden or after "일시 정지".

## Catalogue pages (read-only, 2026-11-03)

`dataops/web/catalog.py` shows the management DB as a catalogue of every dataset, whatever its origin
(이 시스템 = own runs, 기존 DB = `import-legacy`, 배포 묶음 = `import-deploy`).
- `/datasets`: totals, origin filter and name filter. Per dataset: patients (its own plus those of its linked
  series, by PatientID), series, RTSTRUCT, train/valid, DICOM files. Counts are cached for 60 s.
- `/datasets/<uuid>`: filters by modality / split / text (PatientID, description, UID, ROI name), the source
  of each series, ROI names under each RTSTRUCT.
- `/series/<uid>`: patient, study, every dataset holding the series (with its split), ROI names, the image an
  RTSTRUCT points at and the RTSTRUCT pointing at an image, the other series of the study, transfers.
- `/search?q=`: datasets, ROI names (structure-set and dataset counts), RTSTRUCT, patients (one row per
  PatientID with every dataset that has them) and series (UID prefix or description).
- `/browse` (nav "표", `dataops/web/browse.py`): the DB as one flat table. 시리즈 단위 = a row per series of a
  dataset (dataset, origin, split, patient, study, modality, description, files, ROI names, the ROI names of the
  RTSTRUCT pointing at an image, source, UIDs …); 환자 단위 = a row per patient of a dataset (own or via linked
  series, series count, modalities, splits). Search panel:
  - a word search over every text column (every word must match somewhere);
  - dataset name and ROI name with suggestions (all dataset names; the 2,000 most used ROI names). ROI: several
    names separated by commas must all be there; "이름 전체가 같은 것만" matches the whole name. A series row
    matches when it is the RTSTRUCT holding the ROI or the image such an RTSTRUCT points at; a patient row when
    the patient has such an RTSTRUCT in that dataset;
  - ticks for origin / split / modality / sex / source (several = any of them, "(없음)" = empty);
  - a field for every other column (text: contains, `a|b` either, `=exact`, `!exclude`, `-` empty; numbers:
    `>10`, `<=3`, `5`) and a study date range (`2019` … `202106`: whole year / month);
  - every condition shows as a chip that drops it.
  Header sort, a column picker, 50–500 rows a page, CSV of the filtered rows (up to 200,000). Column expressions
  are fixed in code; values are bound parameters. Row counts are cached for 60 s, suggestions for 5 min.

Source values are shown as they are. Migration 0007 indexes `series.referenced_series_uid` and
`subjects.patient_id` for these lookups.

## Browser flow (owner decision 2026-10-28)

`deploy/web.sh start` runs the web app and one **worker** (jobs run one at a time; logs in
`runtime/jobs/<job>.log`, page `/jobs`).

1. **새 분석** (`/new`): an absolute folder under `DATAOPS_INPUT_ROOTS` (default
   `/mnt2/data/Public_dataset`; on this host also `/mnt2/data/Onco_dataset` and its read-only copy
   `/mnt2-v3-test/data/Onco_dataset`; backup/hidden folders refused). Pseudonymisation is chosen by location
   ("자동", the default): on for a folder under `DATAOPS_INTERNAL_ROOTS` (default the two Onco_dataset roots:
   hospital and in-house data). Public data are never pseudonymised (owner, 2026-11-01): they keep their published
   case IDs, the run page has no switch for them and flags nothing in them as identifying. Job `analyze`:
   `scan` (directory overview + NIfTI headers, ~1 min for AMOS 62 GB) → analysis agent → `spec`
   check → plan. Nothing is converted or sent.
   NFS inputs are read in parallel: each directory is listed once and stat'ed with 64 threads, header probes
   run in `DATAOPS_PROCS` processes (default min(10, CPUs)) × 8 threads, and the freeze handles DICOM series
   in `DATAOPS_FREEZE_PROCS` processes (`DATAOPS_IO_WORKERS`, 16, is the per-process thread pool). Measured
   on CIRT (40k DICOM, 21 GB, 138 NFS): analysis 2 min 41 s end to end (scan 41 s, agent 83 s, plan 33 s);
   pseudonymised freeze of all 349 series 135 s (295 files/s). One at a time, the plan step alone was
   ~45 min and the freeze ~20 min.
2. **Analysis agent**: `claude -p` with tools `Read,Glob,Grep,Skill` only, `--permission-mode dontAsk`, no MCP,
   `--setting-sources project` (the work folder's own `.claude/`, which holds only a fresh copy of the
   **dataset-intake skill**: naming rule, vocabulary, folder layout recipes, `codes.json`; the code's prefix
   check reads the same `codes.json`), `--add-dir <dataset>`, work folder `~/.cache/ai-platform-dataops/agent/<run>/`,
   `--json-schema` (proposal in `dataops/agent.py`), cap `DATAOPS_AGENT_BUDGET_USD` (5) and
   `DATAOPS_AGENT_TIMEOUT_S` (1800), model `DATAOPS_AGENT_MODEL` (claude-opus-5-5). Dataset text is
   data. Raw output: `runs/<run>/agent_proposal.json`. The proposal echoes `rules_version` from
   `codes.json`; without it, table 4 says the rules were not read. Rules change in the skill (Notion is the
   master copy), not in `agent.py`. `python -m dataops prefix-check PREFIX --commercial Y|N|U` checks a
   prefix by hand.
3. **Report**: every dataset gets the same standard report (`dataops/report.py`, schema_version 2) —
   the same numbered tables with the same columns, whatever the dataset; agent text only appears as
   cell values. Tables 6 and 7 are what leaves this machine: the DICOM values the local SCP receives, and
   the rows of every management-DB table. The run page `/runs/<run>` keeps only what the owner uses: the
   sample send to the Local SCP, the final approval → Central, and the **sample values**, one sample at a
   time (3 sample subjects = the pilot subjects, `spec.SAMPLES`, extracted by the analysis job itself; job
   `sample` re-extracts after a change). For the chosen sample the page shows two separate tables, each with
   the real source value on the left and the value that really goes out on the right: **DICOM 메타데이터**
   (every top-level element of the first converted file of each series) and **DB** (the rows registration
   writes: dataset, subject, study, each series and its link, the first instance of each series), with what
   was done (생성, 새 UID 생성, 계산, 그대로, 버림, 빈 값) — `dataops/compare.py`. DB columns read from the outgoing
   DICOM (series header columns, patient sex/age, slice location) show the same attribute of the source file (or the
   NIfTI header field it is computed from) on the left; a pseudonymised run also shows its `deid_*` map rows next
   to the row they map, whose `original_*` columns keep the source value on purpose (this server's DB only).
   Identifying DICOM attributes or DB columns that leave unchanged are flagged "식별 정보 그대로".
   **Naming** follows the company rule (Notion "04-1. Data prefix 규칙", "Track P"): the prefix is
   `<source>_<region>[_<modality>][_<sub>]_<PUB|NC|IO>` (A-Z/0-9/_, 3–20 characters; the tier agrees with
   commercial use: PUB = Y, NC = N, IO = internal only), and PatientID = PatientName = `<prefix>_<source ID>`
   for NIfTI and original DICOM alike (only those two tags change in a DICOM copy). A prefix that breaks the
   rule is a plan error, and a run whose commercial use is still U cannot be approved.
   **Pseudonymisation** (`dataops/deid.py`) applies to internal data only — public data are de-identified
   already and cannot be switched on — and is switched per internal run by the button under the title (a plan
   field, so part of what is approved). On, the
   subject key becomes a pseudonym number (PatientID = `<prefix>_0001`, …; numbers are kept in the plan's
   `deid.subjects`, given in keyed-hash order and never reused) in PatientID / PatientName /
   SeriesDescription and in the DB, ImageComments leaves out the source path, and the DB stores a keyed
   hash instead of source paths. Original DICOM exports are rewritten instead of copied: PatientID /
   PatientName become `<prefix>_<pseudonym>`, person names (patient, physicians, operators), institution
   name/address/department, address/telephone/other patient IDs, accession/study/order numbers and comments
   (`deid.REMOVE`) are emptied, private tags are dropped, every instance UID is replaced by a keyed `2.25.` UID
   (consistently, so an RTSTRUCT still references its CT). Kept as they are (owner, 2026-11-01, profile
   `pseudonym-v2`): dates and times, birth date, age, sex, weight, device and station names. A new profile
   changes the plan, so artifacts frozen under the old one are made again. The new-analysis form decides it before the first sample extraction (자동 / 켬 / 끔, see 1). The
   pages are for the owner: the left (source) column always shows the real values; only the right
   (outgoing) column follows the switch. The
   whole-dataset tables (8, 7, 6) are folded under "전체 목록"; 오류 rows of table 4 and plan errors appear
   above; dataset fields, splits, labels and history are under "설정 · 기록". The folder overview
   (`scan.py`) stops after 120 s and stats at most 200 files per directory (sizes beyond are estimated). `/runs/<run>/report` has all 10, `report.json`, `dicom.json` / `dicom.csv` (6), `records.json` / `records/<table>.csv` (7, with
   the live DB state), `series.json` / `series.csv` (8), and `/reports` (+ `.csv`): one row per dataset
   (latest run of each folder) with fixed columns for comparison.

   | # | Table | Columns |
   |---|---|---|
   | 1 | 데이터셋 | 24 fixed fields (name, UUID, path, size, licence, counts, rules, agent summary, status…) |
   | 2 | 분할 구성 | train / validation / test / other / none + total × 대상자, CT, MR, 라벨, 인스턴스, 포함 |
   | 3 | 폴더 판정 | 경로, 판정 (포함/무시/제외), 역할, 분할, 파일 수, 크기, 근거 |
   | 4 | 확인 사항 | #, 구분 (오류/미해결/경고/질문/사실), 내용, 출처 |
   | 5 | 전송·등록 | 파일럿→Local SCP, 최종 승인, Central, DB × 대상, 대상자, 시리즈, 인스턴스, 결과, 일시 |
   | 6 | Local SCP 메타데이터 (DICOM) | one row per included series: 대상자, 분할, 종류, 파일럿, Local SCP, 값 출처 + 20 series-level tags (`metadata.TAGS`: PatientID … ROIName) + 실제 파일 확인 |
   | 7 | DB 등록 예정 | summary per table (예정 행, 신규/같음/변경/충돌 in the live DB), then every table with its DB column names, one row per row registration would write, plus DB 상태 and 차이 컬럼 |
   | 8 | 원본 메타데이터 | 21 source fields (`metadata.FIELDS`), one row per series |
   | 9 | 열 정의 | every table-6 tag, the remaining written tags (fixed / per slice), every DB column (+ the ones the DB fills), every source field with where it goes; with the checks |
   | 10 | DICOM 헤더 | every tag of the pilot files per modality with its origin |

   Table 6 values come from the converted files once a series is frozen (pilot first; cached in
   `runs/<run>/dicom_tags.json` per manifest hash) and are compared with the values predicted from the
   plan; a difference is an 오류 in table 4. Series not yet converted show the plan values. Table 7 is
   `records.build_records(preview=True)`: before the full conversion the pilot's files stand in for
   `instances` and `transfer_results` (same deterministic SOP UIDs) and the note says how many rows the
   full conversion makes. Each row's DB state is what registration would do (`db.row_status`, SELECT
   only); the report re-checks it every 15 s.

   Source metadata that has no field of its own (the rest of the NIfTI header, or the DICOM series tags
   of a pass-through series) is kept whole in the 8 field "원본 기타 메타데이터" and goes nowhere. NIfTI
   headers are read once after the analysis into `runs/<run>/nifti_headers.json`
   (`/runs/<run>/report?refresh=1` re-reads changed or missing files; prepare never uses this cache).
   Editable until approval (forms on `/runs/<run>`): dataset fields (name, licence, commercial use, URL,
   PatientID prefix), included splits, labels on/off, modality of unresolved images, the two pilot
   subjects (typed beside the send button; saved before sending). Every edit is re-validated and bumps
   the plan revision.
4. **Pilot**: freezes only the pilot subjects (`manifest-pilot.json`: CT/MR DICOM + label RTSTRUCT) and
   sends them to the local SCP. Check them in OncoStudio on 107. Viewer: `/runs/<run>/series/<uid>`
   with the RTSTRUCT contours.
5. **Approval**: needs status `PILOT_SENT` (every file of at least one sample accepted — the run page sends
   the one sample chosen in its dropdown; `approval.pilot_subjects` lists the received ones) for the current plan, the
   approver's name and the dataset name typed as confirmation. Stored in `run.json` `approval`
   (plan sha256, pilot manifest sha256, target) and later in `ingest_runs.approval_json`.
6. **Central + DB** (job `publish`): full freeze of every series of the plan (pilot artifacts reused after
   their sources are re-hashed) → C-STORE of every frozen file to Central with a journal (one record per SOP;
   pressing again sends only what was not accepted) → registration in the management DB. Before the first
   file leaves, `publish.coverage` accounts for **every file of the input folder** — sent (a series), excluded
   series, skipped by the plan (with the reason), duplicate / conflicting copy, or left out (outside every
   source, with the agent's reason) — and publication is refused while a file the plan read is unclassified or
   an active series is missing from the full manifest. The run page shows this table under section 2
   ("폴더 전체 파일 처리 내역"). Refused unless `DATAOPS_CENTRAL_HOST/PORT/CALLED_AE` are set **and**
   `DATAOPS_CENTRAL_ENABLED=true`. Since 2026-11-02 (owner: "보내도 돼") the env file sets Central to
   `ONCOPACS@10.101.1.4:4002`, calling AE `DATAOPS` (C-ECHO and CT/MR/RTSTRUCT storage contexts checked first);
   an approval in the web then starts the publish job. Tests still use a loopback SCP (`tests/test_agent_flow.py`).
   A send journals its results every 15 s, so the run page moves during a long send; from a shell,
   `python3 ops/transfer_progress.py RUN [--watch 30]` shows conversion and sent bytes without touching the job.
   The **Central** page (`/central`) lists every run that has sent to Central — from each run's journal (receiver
   answers, send times, approver) plus what the management DB holds (dataset, prefix, licence, subjects,
   series by modality, bytes, registration). Only what this system sent; Central also holds data from elsewhere.
   The run page's derived views (report, sample comparison, coverage) are kept in the web process and re-derived
   only when a file of the run changes; a background loop refreshes them within ~30 s of a job ending, so even
   a full run (AMOS, ~10^5 files, ~12 s to derive) opens at once. Just after `restart-web` the first open of a
   big run may still wait for that derivation.

CLI equivalents: `analyze DIR [--wait]`, `pilot RUN`, `approve RUN --by NAME --confirm NAME`,
`publish RUN [--dry-run]`, `worker [--once]`.

Space: a full AMOS freeze is roughly 40–60 GB of DICOM under `runtime/runs/<run>/artifacts` on
`/drive`, and the nightly backup hard-links it into `ai-platform-dataops-files/<date>`.

## Flaky NFS input (/mnt2)

The 254 server behind `/mnt2` sometimes answers "No such file" for files that exist, for a few minutes, then
recovers by itself; the 138 server exports the same data read-only at `/mnt2-v3-test`. Every read of an input
file (directory listing, stat, DICOM/NIfTI header, hash, copy, conversion) goes through `dataops/nfs.py`: when the
primary fails that way, the same path on the mirror is read (`DATAOPS_INPUT_MIRRORS`, default
`/mnt2/data=/mnt2-v3-test/data`; empty turns it off); when neither answers, both are retried for about five minutes
before the step fails. Plans, manifests and the DB keep the primary path; bytes read from the mirror are checked
against the hashes taken at planning time like any other read. A directory that cannot be listed on either mount
now stops the analysis instead of being skipped silently. Each fallback is logged as `[nfs] …` in the job log.
The analysis agent (Claude CLI) reads the folder itself and is not redirected; the code's checks of its proposal
(paths, headers) are.

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
