# Initial validation

2026-10-27, Windows / local Python environment:

- `python -m pytest -q`: 24 passed (local converter/sender + offline preview tests).
- Example HTML and candidate-records.json generated with tools/review.py.
- Source metadata HTML-escaped; conflicting study ownership and duplicate series rejected.
- Runtime outputs are gitignored. No patient files, credentials or DB dumps committed.

The inherited tests use synthetic images and loopback test receivers. No live OS_LJM,
Central transmission or production DB write was performed. MariaDB DDL is a draft and
has not been executed. GitHub Actions is configured but its result must be checked after
push; the local result does not prove that CI or a different dependency version passes.

## 2026-10-28, 10.101.1.183 (Ubuntu, Python 3.10.12, venv ~/.venvs/ai-platform-dataops)

Dependencies: pydicom 3.0.2, pynetdicom 3.0.4, nibabel 5.4.2, numpy 2.2.6, fastapi 0.162, pymysql 1.2.3.
The system Python has pydicom 2.4.4, which made 15 inherited tests fail (`save_as(enforce_file_format=)`);
inside the venv the inherited 24 pass.

- `python -m pytest -q`: **61 passed** — inventory/plan/prepare/local send (17), DB on disposable
  `dataops_test_*` schemas of the dev MariaDB 11.4 (7), web viewer (10), backup tool (3), inherited (24).
  Without a configured DB the 8 DB-backed tests are skipped (GitHub Actions).
- Covered: mixed folder (DICOM with/without extension, NIfTI CT/MR, sidecar, mask, text, `backup/` not
  entered), no input mutation, byte-identical DICOM copies, tamper detection blocks sending, identical vs
  conflicting duplicate SOP UIDs, PatientID edits on pass-through DICOM refused, missing prefix blocks
  (no LOCALREVIEW), modality edit / changed pixels → new Series UID, relocation → same UIDs and
  bytes, input changed after plan refused, run lock, partial C-STORE failure + retry of the failed
  instance only, unreachable SCP, remote host refused, idempotent registration, conflict rollback,
  migration-edit detection, forbidden schema names, HTML escaping, path traversal / manifest-bound
  images, retention rule.
- End-to-end CLI on synthetic data (scratch schema `dataops_test_demo`, loopback SCP, scratch backup
  root): `ingest --send-local --register` → 28/28 accepted, 1 dataset / 4 subjects / 4 studies /
  4 series / 28 instances / 28 transfer rows; `nightly_backup.sh` then `restore_check.sh` →
  every table count equal in a fresh mariadb:11.4 container, bundle commit present, 1 run restored,
  28 artifacts re-hashed equal: `RESTORE CHECK OK`. Scratch schema dropped afterwards.
- Real backup to `/mnt/ai-pipeline/backups/ai-platform-dataops/2026-10-28/` (empty `public_dataops`,
  migrations only) and `restore_check.sh` → OK. User crontab block `30 0 * * *` installed; the
  previous 54 crontab lines are unchanged (diff shows only the 3 added lines).
- Web viewer started with `deploy/web.sh start` on 127.0.0.1:8096; all pages 200, headless Chromium
  screenshots without console errors (CSP: no inline script/style).

Not verified: sending to a real local SCP (target OS_LJM@10.101.1.107:4001 chosen later the same day; it was down), any real dataset,
Central publication (not implemented), MariaDB 10.5 compatibility of the exporter (not written).

## 2026-10-28 (later) — local SCP target OS_LJM@10.101.1.107:4001

- The owner named the target: OncoStudio's OS_LJM on the Windows test PC 10.101.1.107. The sender's
  host guard gained an explicit review-host list (`DATAOPS_LOCAL_SCP_REVIEW_HOSTS`, default
  10.101.1.107) and refuses the called AE `ONCOPACS` on any host. Tests: a listed PC passes, an
  unlisted PC and another LAN host are refused, `ONCOPACS` on loopback is refused, defaults resolve
  to OS_LJM@10.101.1.107:4001 (64 passed).
- `python -m dataops echo` from 183: the guard passes; 107 answers ping but refuses TCP 4001
  (OncoStudio SCP not running). Nothing was sent; the main session was asked to start it.
- After the owner started OS_LJM on 107: `python -m dataops echo` → `C-ECHO 성공`
  (calling AE DATAOPS from 183). No C-STORE yet (no dataset chosen).

## 2026-10-28 (evening) — browser flow: agent analysis, pilot with RTSTRUCT, approval gate

- Tests: 81 passed (`pytest -q`), including `tests/test_agent_flow.py` (17): synthetic nnU-Net style
  challenge with a duplicate copy, an archive and a README that tries to instruct the agent; agent test
  double (`tests/fake_agent.py`); proposal refusals (path outside root, missing path, no image source,
  regex without group / invalid, bad prefix); label on another grid not converted; pilot of 2 subjects
  to a loopback SCP with RTSTRUCT ROI names and contour references checked on the received objects;
  approval refused before/with a partial pilot, with a wrong confirmation, after a plan edit; source
  file changed after the pilot refused at the full freeze (found and fixed: reused pilot artifacts now
  re-hash their sources); Central refused when unset / disabled / not approved, then CENTRAL_SENT to a
  loopback SCP with a no-op retry; publish job registers 4 subjects / 7 series (3 RTSTRUCT) with the
  approval in `ingest_runs.approval_json` on a disposable schema; browser flow with CSRF and Origin
  refusals, job queue, overlay PNG, approval without a Central target.
- Real data, run `20261028T191410-e4d4c39c`, started through the web form:
  `/mnt2/data/Public_dataset/09 Multi-Modality Abdominal Multi-Organ Segmentation Challenge (AMOS)`
  (62 GB). Scan + agent (claude-opus-5-5, 10 turns, $0.47) + plan in about 1 minute: 600 subjects,
  CT 500 / MR 100 (agent derived from the split counts that id 0500 is CT), 360 label RTSTRUCTs,
  `amos22_2` (reorganised copy, 357 cases), `amos22.zip` and `data_info` left out with reasons;
  0 plan errors. The agent's only question: whether to send the unlabelled test split.
- Pilot (amos_0001 CT, amos_0507 MR) frozen in ~18 s and sent to OS_LJM@10.101.1.107:4001:
  164/164 accepted. RTSTRUCT: 15 and 13 ROIs, round-trip Dice 1.0 for every ROI; independent check of
  the frozen files: every contour lies on its referenced slice plane (0.0 mm), inside the image, same
  Study / FrameOfReference. Overlay screenshots show kidneys, liver, spleen and aorta on the right
  anatomy (patient left/right correct).
- Not done: approval (owner's decision), full freeze of AMOS (~40–60 GB), Central publication (no
  target), verification of the pilot in OncoStudio by the owner.

## 2026-10-29 — standard dataset report (same tables for every dataset)

- Owner asked for one consistent table format per dataset instead of free-form report text. Tests: 85
  passed (`pytest -q`); `tests/test_metadata.py` builds the report for an agent-analysed NIfTI challenge
  and for a legacy DICOM + NIfTI plan and asserts identical table numbers, titles, columns, row labels
  of tables 1/2/5 and series fields; field checks equal before the pilot (DB) and after it (real CT/MR/
  RTSTRUCT headers, every tag with a stated origin); unreadable source reported in table 4 and in the
  comparison table; CSV cells cannot become formulas.
- AMOS run `20261028T191410-e4d4c39c`: 960 series, DB checks 960/960 (600 image-only fields, 360 label
  fields), DICOM checks on the 4 pilot series all equal. NIfTI headers carry values that had no place
  before and now sit in field 15, e.g. `descrip=Time=144225.000`, `aux_file=Venous_Phase`.
- Found on 2026-10-29: on the NFS mount `/mnt2` (10.101.1.254) `amos22/imagesTs`, `amos22/labelsTr`,
  `amos22_2` and `data_info` are listed but cannot be opened ("No such file or directory"); 480 of the
  960 sources were unreadable when the header cache was written (table 4 shows it as 오류). They were
  readable on 2026-10-28 (analysis and pilot). A full freeze will fail until the storage is back;
  nothing was changed on /mnt2.

## 2026-10-29 — read-only DB browser

- Tests: 87 passed (`pytest -q`). `tests/test_web.py`: on a disposable schema with a registered run, the
  overview, row view (children with filtered links, XSS escaped), FK links from a filtered/sorted table, CSV
  export and refusals (unknown table 404 before any DB access, unknown row 404, injection text in a filter
  value and in `sort` finds nothing and changes nothing); without a DB the pages say so.
- Rendered on a temporary populated schema (dropped afterwards) and checked visually; the real management
  DB is empty until the first Central publication registers a run.

## 2026-10-29 — report tables 6/7 = what goes out; live DB pages

- Tests: 89 passed. Owner: the report must show what goes into the DB and the metadata that goes to the local SCP; the DB
  pages must be live. Report schema_version 2: table 6 is the DICOM values per series (20 series-level
  tags), table 7 the rows of every DB table with the DB's column names and each row's state in the live DB.
- AMOS run 20261028T191410-e4d4c39c: table 6 has 960 series, 4 read from the pilot files (CT, MR and both
  RTSTRUCTs; every checked tag 일치), 956 plan values. Table 7: datasets 1, subjects 600, studies 600,
  series 960, dataset_series 960, ingest_runs 1, instances 164 and transfer_results 164 (pilot files only;
  98,975 instances after the full conversion), all 신규 because the management DB is empty.
  `records.json` 0.6 s, `/runs/<run>/report` 0.3 s.
- Live pages: a change within the same second is detected (CHECKSUM TABLE, not timestamps), shown by
  `tests/test_web.py::test_db_pages_are_live`: an UPDATE after registration changes the mark, the table
  page shows the new value, and `records.json` turns that one series into 변경 with `_diff` description.
