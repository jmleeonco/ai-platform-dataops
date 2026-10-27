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

Not verified: sending to a real local SCP (OS_LJM is not on 183; no target chosen), any real dataset,
Central publication (not implemented), MariaDB 10.5 compatibility of the exporter (not written).
