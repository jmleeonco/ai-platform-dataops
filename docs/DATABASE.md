# New management DB and future integration

Schema = `sql/migrations/0001_initial.sql` (the 2026-10-27 draft) + `0002_registration_fields.sql` +
`0003_source_paths.sql` (full source path: `series.source_path` = the converted file or the folder of a DICOM
series, `instances.source_path` = the file; = `ingest_runs.input_root` + `source_relative_path`; descriptive,
a keyed hash when pseudonymised),
applied in order by `python -m dataops db-migrate` (table `schema_migrations` stores version and file
sha256; an edited applied migration is refused — add a new file instead). No CREATE DATABASE/USE,
credentials or cross-DB FK in migrations. The connector refuses the schema names scpdb/oncolabdb.

Location (owner decision 2026-10-28): MariaDB 11.4 in Docker on 10.101.1.183
(`ai-platform-dataops-db`, 127.0.0.1:13306, schema `public_dataops`, app account `dataops_app`).
Production runs MariaDB 10.5.8; the exporter must be tested against that version before use.
Integration tests run against disposable `dataops_test_<random>` schemas on the same server.

Registration (`dataops.db.register`) is one transaction: rows are inserted or descriptive columns
updated; an identity mismatch (same SOP UID with another sha256, same Study UID under another
subject, same subject key with another PatientID, ...) rolls back everything. `transfer_results`
currently holds local-SCP outcomes (`target_key` = `local:<AE>@<host>:<port>`); `accepted` means a
C-STORE success response, and `verification_json` stays NULL until a target can be queried.
Known limit: a study belongs to one subject of one dataset, so registering the same study under a
second dataset's subject is a conflict (series membership in several datasets is supported).

## Identity
- UUID for dataset, subject, ingest run. PatientID is not globally unique: scope local
  identity by dataset and source subject key; assign the approved public prefix before
  final local review. Use <=64 characters for legacy LAB patient_id compatibility.
- Study/Series/SOP UID stored as up to 64 ASCII characters. Validate DICOM syntax.
  Preserve existing DICOM identities for unmodified copies. New derived objects need
  appropriate new identities and reference mapping. Persist generated UIDs; a rerun
  must not depend on a moved absolute source path or accidentally reuse a UID for changed bytes.
- Separate datasets-to-series membership allows one series in several datasets.
- File hashes bind review to bytes; same UID with different bytes is a conflict to
  inspect, not an instruction to overwrite. Transfer-syntax-only differences can need
  semantic comparison; do not silently assume equivalence.
- No Central absolute path guess. Local relative artifacts and remote identity/location
  are different fields. Central SCP owns its final file placement.

## Proposed tables
datasets: UUID/name/provenance/license/commercial permission.
subjects: dataset-scoped source key and approved DICOM PatientID.
studies/series/instances: DICOM identities and review metadata, artifact references.
dataset_series: dataset membership, separate from image identity.
ingest_runs: revision/manifest hash/status, conversion and interpretation evidence.
transfer_results: per target/run/instance delivery and verification evidence.
external_mappings: new dataset UUID -> legacy DB numeric dataset ID, scoped by system.

Migration 0004 (2026-10-31) makes a later merge column-to-column:
- `series`, `subjects`, `instances` gained a column for every value the existing DB keeps per series, subject
  and file, named like `oncolabdb.lab_dataset_items` (slice_thickness, pixel_spacing, image_orientation,
  image_type, rescale_*, echo/repetition/inversion time, flip_angle, magnetic_field_strength, sequence_*,
  diffusion_b_value, contrast_bolus_*, body_part_examined, series_date/time, manufacturer,
  frame_of_reference_uid, referenced_series_uid, roi_names, patient_name/sex/age/weight/ethnicity,
  slice_location). The values are read from the first frozen (outgoing) file of each series
  (`records.outgoing_headers`); before a series is frozen the preview falls back to the source header with
  UIDs as they will leave.
- `deid_runs`, `deid_subject_mappings`, `deid_series_mappings`, `deid_instance_mappings` correspond to
  `oncolabdb.enc_pipeline_runs` / `enc_patient_mappings` / `enc_series_mappings` (+ one row per file). They are
  filled only for pseudonymised runs and hold the originals in clear (original PatientID, Study/Series/SOP UID,
  full source path, institution) next to the pseudonym, so the source is found from the DB alone. Person
  names, birth dates and addresses are never read, so those columns stay empty. Public tables keep only what
  leaves (`instances.source_path` stays a keyed hash).

**Pseudonym map DB (2026-11-01, owner).** Migration 0005 moved the four `deid_*` tables (empty then) out of
`public_dataops` into their own schema **`dataops_deid_map`** with its own account **`dataops_deid`**
(`sql/deid_map_migrations/`, `deploy/db.sh init-deid-map`, `DATAOPS_DEIDMAP_DB_*` in the env file; the password
is only there). The app account has no rights on it and the map account none on the management schema, so
the management DB holds pseudonyms only. Keys are the management DB's (`subject_uuid`, `series_uid`, `sop_uid`,
`run_uuid`), with no cross-schema foreign keys. The map is written as soon as pseudonymised files exist (sample
extraction, pilot, full freeze: `jobs.save_deid_map`, before any send), covers every subject and series of the
plan and every file frozen so far, and is written again (idempotently, map first) at registration; registering a
pseudonymised run without a map DB is refused. `python -m dataops deid-map-save RUN|all` writes it by hand;
`db-migrate` / `db-status` cover both DBs. `/db` shows the map tables in their own section, read from the map DB.
The nightly backup dumps it as `db/dataops_deid_map.sql.gz` (+ row counts) and `restore_check.sh` loads it.

`/legacy` lists every column of the six tables a merge maps value by value (lab_datasets, lab_dataset_items,
enc_pipeline_runs, enc_patient_mappings, enc_series_mappings, scpdb.dicom_instances; 170 columns on
2026-10-31): 101 are filled from a new-DB column (`legacy_db.MAPPING`), 69 are LAB workflow, AI predictions,
auto keys or values the outgoing DICOM empties, each with its reason (`legacy_db.NOT_MAPPED`).
`tests/test_db.py` fails if a column of those tables (`tests/data/legacy_compared_columns.json`, shape only)
has neither, or if a mapped new-DB column does not exist after the migrations.

No PACS configuration is copied into this DB.
Only fields necessary for the dataset should be collected. DB preview must use the same
DTO/projection as registration (not a separately coded approximation).

## Legacy constraints observed 2026-10-27
`lab_datasets`: only name has no default (id auto increment); status defaults COLLECTING.
`lab_dataset_items`: dataset_id, series_instance_uid, study_instance_uid, patient_id
required; source_type defaults local; `(dataset_id,series_instance_uid)` unique.
`scpdb.dicom_instances`: many NOT NULL columns default empty strings, PatientID defaults
0, SOPInstanceUID index is NOT unique. These permissive DB defaults do not validate DICOM.

## Export/import to oncolabdb
1. Detect target schema version/columns and produce a dry-run mapping report.
2. Match an explicit external mapping/UUID, NOT name or numeric ID equality.
3. Allocate target lab_datasets.id and save external_mappings transactionally.
4. dataset_series + series + studies + subjects -> lab_dataset_items. Use source_type=pacs
   only after Central registration is confirmed; preserve UIDs, map dataset_id.
5. Fill counts from committed memberships. Preview updates to existing target rows and
   resolve conflicts; do not overwrite another department's newer annotations.
6. Re-run same export without duplicate datasets/items. Export auxiliary history separately.

The page `/legacy` (`dataops/legacy_db.py`) shows the existing DB's shape and this mapping: with
`DATAOPS_LEGACY_DB_*` set (a SELECT-only account) "구조 다시 읽기" reads information_schema (tables, columns,
indexes, FKs) and VERSION() in a read-only session into `runtime/legacy_schema.json` — never a row of a
production table — and marks each mapped column 있음 / 열 없음 / 테이블 없음. Without it the page shows the
2026-10-27 observations above.

This future adapter is NOT implemented. Direct writes to production scpdb are never part
of export. Existing Central receiver registers incoming DICOM normally.
