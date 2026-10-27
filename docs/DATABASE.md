# New management DB and future integration

`sql/001_public_dataops.sql` is a draft for a NEW database, never for scpdb/oncolabdb.
It deliberately has no CREATE DATABASE/USE, credentials or server-side cross-DB FK.
Apply only to an explicitly configured disposable/new schema. SQL syntax and migrations
must be integration-tested with MariaDB before production; no migration was run here.

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

No need to copy production patient mapping tables or PACS configuration into this DB.
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

This future adapter is NOT implemented. Direct writes to production scpdb are never part
of export. Existing Central receiver registers incoming DICOM normally.
