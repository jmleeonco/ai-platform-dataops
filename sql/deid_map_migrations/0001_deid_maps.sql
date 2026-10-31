-- Pseudonymisation map DB (schema dataops_deid_map, account dataops_deid; apart from the management DB
-- public_dataops). Original <-> pseudonym for every pseudonymised run, written as soon as the pseudonymised files
-- are made (before any send), so what left can be traced back without the run folders. Corresponds to
-- oncolabdb.enc_pipeline_runs / enc_patient_mappings / enc_series_mappings. The *_uuid / *_uid keys are the
-- management DB's (pseudonymised) keys; there are no foreign keys across the two schemas.

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
 KEY ix_deid_runs_dataset(dataset_uuid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Original subject <-> pseudonym (oncolabdb.enc_patient_mappings).
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
 KEY ix_deid_pseudonym(pseudonym_patient_id)
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
 KEY ix_deid_original_series(original_series_uid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Original file <-> outgoing file.
CREATE TABLE deid_instance_mappings (
 sop_uid VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
 run_uuid CHAR(36) CHARACTER SET ascii NOT NULL,
 original_sop_uid VARCHAR(128) CHARACTER SET ascii NULL,
 original_source_path TEXT NOT NULL,
 KEY ix_deid_original_sop(original_sop_uid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
