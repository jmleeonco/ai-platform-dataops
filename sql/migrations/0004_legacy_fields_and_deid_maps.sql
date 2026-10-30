-- Migration 0004: a column for every value the existing DB (oncolabdb / scpdb on 10.101.1.4) keeps per series,
-- subject and file, named like oncolabdb.lab_dataset_items so a later merge is a column-to-column copy, and the
-- pseudonymisation maps that correspond to oncolabdb.enc_pipeline_runs / enc_patient_mappings /
-- enc_series_mappings. Values are what leaves this machine (the outgoing DICOM); the deid_* tables hold the
-- originals and are filled only for pseudonymised runs.
ALTER TABLE series
  ADD COLUMN sop_class_uid VARCHAR(64) CHARACTER SET ascii NULL,
  ADD COLUMN series_date VARCHAR(16) CHARACTER SET ascii NULL,
  ADD COLUMN series_time VARCHAR(16) CHARACTER SET ascii NULL,
  ADD COLUMN body_part_examined VARCHAR(64) NULL,
  ADD COLUMN manufacturer VARCHAR(128) NULL,
  ADD COLUMN frame_of_reference_uid VARCHAR(64) CHARACTER SET ascii NULL,
  ADD COLUMN referenced_series_uid VARCHAR(64) CHARACTER SET ascii NULL,
  ADD COLUMN roi_names TEXT NULL,
  ADD COLUMN slice_thickness DECIMAL(10,4) NULL,
  ADD COLUMN spacing_between_slices DECIMAL(10,4) NULL,
  ADD COLUMN pixel_spacing VARCHAR(64) NULL,
  ADD COLUMN image_orientation VARCHAR(128) NULL,
  ADD COLUMN patient_position VARCHAR(32) NULL,
  ADD COLUMN image_type VARCHAR(256) NULL,
  ADD COLUMN rescale_slope DECIMAL(12,6) NULL,
  ADD COLUMN rescale_intercept DECIMAL(12,6) NULL,
  ADD COLUMN bits_allocated SMALLINT NULL,
  ADD COLUMN repetition_time DECIMAL(10,3) NULL,
  ADD COLUMN echo_time DECIMAL(10,3) NULL,
  ADD COLUMN inversion_time DECIMAL(10,3) NULL,
  ADD COLUMN flip_angle DECIMAL(6,2) NULL,
  ADD COLUMN magnetic_field_strength DECIMAL(6,3) NULL,
  ADD COLUMN scanning_sequence VARCHAR(64) NULL,
  ADD COLUMN sequence_variant VARCHAR(64) NULL,
  ADD COLUMN sequence_name VARCHAR(128) NULL,
  ADD COLUMN diffusion_b_value DECIMAL(10,3) NULL,
  ADD COLUMN number_of_temporal_positions SMALLINT NULL,
  ADD COLUMN temporal_position_identifier SMALLINT NULL,
  ADD COLUMN contrast_bolus_agent VARCHAR(128) NULL,
  ADD COLUMN contrast_bolus_ingredient VARCHAR(128) NULL,
  ADD COLUMN contrast_bolus_volume DECIMAL(10,3) NULL,
  ADD COLUMN contrast_bolus_total_dose DECIMAL(10,3) NULL;

ALTER TABLE subjects
  ADD COLUMN patient_name VARCHAR(256) NULL AFTER patient_id,
  ADD COLUMN patient_sex VARCHAR(32) NULL AFTER patient_name,
  ADD COLUMN patient_age VARCHAR(16) NULL AFTER patient_sex,
  ADD COLUMN patient_weight VARCHAR(16) NULL AFTER patient_age,
  ADD COLUMN patient_ethnicity VARCHAR(64) NULL AFTER patient_weight;

ALTER TABLE instances
  ADD COLUMN slice_location VARCHAR(32) CHARACTER SET ascii NULL AFTER instance_number;

-- One row per pseudonymised run (oncolabdb.enc_pipeline_runs).
CREATE TABLE deid_runs (
 run_uuid CHAR(36) CHARACTER SET ascii PRIMARY KEY,
 dataset_uuid CHAR(36) CHARACTER SET ascii NOT NULL,
 profile VARCHAR(32) CHARACTER SET ascii NOT NULL,
 patient_id_prefix VARCHAR(64) NULL,
 key_label VARCHAR(128) CHARACTER SET ascii NOT NULL,
 pipeline_version VARCHAR(128) NULL,
 summary_json JSON NULL,
 created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
 FOREIGN KEY(run_uuid) REFERENCES ingest_runs(run_uuid),
 FOREIGN KEY(dataset_uuid) REFERENCES datasets(dataset_uuid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Original subject <-> pseudonym (oncolabdb.enc_patient_mappings). Re-identification key: originals in clear.
CREATE TABLE deid_subject_mappings (
 subject_uuid CHAR(36) CHARACTER SET ascii PRIMARY KEY,
 dataset_uuid CHAR(36) CHARACTER SET ascii NOT NULL,
 run_uuid CHAR(36) CHARACTER SET ascii NOT NULL,
 internal_hash VARCHAR(32) CHARACTER SET ascii NOT NULL,
 original_patient_id VARCHAR(256) NOT NULL,
 original_patient_name TEXT NULL,
 original_patient_birth_date VARCHAR(32) NULL,
 original_patient_address TEXT NULL,
 original_institution TEXT NULL,
 pseudonym_patient_id VARCHAR(64) NOT NULL,
 created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE KEY uq_deid_original(dataset_uuid, original_patient_id),
 KEY ix_deid_pseudonym(pseudonym_patient_id),
 FOREIGN KEY(subject_uuid) REFERENCES subjects(subject_uuid),
 FOREIGN KEY(run_uuid) REFERENCES ingest_runs(run_uuid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Original series <-> outgoing series (oncolabdb.enc_series_mappings).
CREATE TABLE deid_series_mappings (
 series_uid VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
 run_uuid CHAR(36) CHARACTER SET ascii NOT NULL,
 internal_hash VARCHAR(32) CHARACTER SET ascii NOT NULL,
 modality VARCHAR(32) NOT NULL,
 original_series_uid VARCHAR(128) CHARACTER SET ascii NULL,
 original_study_uid VARCHAR(128) CHARACTER SET ascii NULL,
 original_source_path TEXT NULL,
 original_acq_date VARCHAR(32) NULL,
 original_acq_time VARCHAR(32) NULL,
 out_acq_date VARCHAR(32) NULL,
 out_acq_time VARCHAR(32) NULL,
 created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
 KEY ix_deid_original_series(original_series_uid),
 FOREIGN KEY(series_uid) REFERENCES series(series_uid),
 FOREIGN KEY(run_uuid) REFERENCES ingest_runs(run_uuid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Original file <-> outgoing file: instances.source_path is a keyed hash when pseudonymised, this is the clear one.
CREATE TABLE deid_instance_mappings (
 sop_uid VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
 run_uuid CHAR(36) CHARACTER SET ascii NOT NULL,
 original_sop_uid VARCHAR(128) CHARACTER SET ascii NULL,
 original_source_path TEXT NOT NULL,
 FOREIGN KEY(sop_uid) REFERENCES instances(sop_uid),
 FOREIGN KEY(run_uuid) REFERENCES ingest_runs(run_uuid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
