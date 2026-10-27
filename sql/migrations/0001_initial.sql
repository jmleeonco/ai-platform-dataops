-- Migration 0001: initial schema (from the 2026-10-27 draft). Applied by `python -m dataops db-migrate`
-- to the NEW management schema only (never scpdb/oncolabdb). No CREATE DATABASE/USE, no credentials.
CREATE TABLE datasets (
 dataset_uuid CHAR(36) CHARACTER SET ascii PRIMARY KEY,
 name VARCHAR(256) NOT NULL,
 source_url TEXT NULL,
 license TEXT NULL,
 commercial_use ENUM('Y','N','U') NOT NULL DEFAULT 'U',
 description TEXT NULL,
 created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE subjects (
 subject_uuid CHAR(36) CHARACTER SET ascii PRIMARY KEY,
 dataset_uuid CHAR(36) CHARACTER SET ascii NOT NULL,
 source_subject_key VARCHAR(255) NOT NULL,
 patient_id VARCHAR(64) NOT NULL,
 UNIQUE KEY uq_source_subject(dataset_uuid, source_subject_key),
 FOREIGN KEY(dataset_uuid) REFERENCES datasets(dataset_uuid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE studies (
 study_uid VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
 subject_uuid CHAR(36) CHARACTER SET ascii NOT NULL,
 FOREIGN KEY(subject_uuid) REFERENCES subjects(subject_uuid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE series (
 series_uid VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
 study_uid VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 modality VARCHAR(16) NOT NULL,
 description TEXT NULL,
 metadata_json JSON NULL,
 FOREIGN KEY(study_uid) REFERENCES studies(study_uid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE dataset_series (
 dataset_uuid CHAR(36) CHARACTER SET ascii NOT NULL,
 series_uid VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 PRIMARY KEY(dataset_uuid, series_uid),
 FOREIGN KEY(dataset_uuid) REFERENCES datasets(dataset_uuid),
 FOREIGN KEY(series_uid) REFERENCES series(series_uid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE ingest_runs (
 run_uuid CHAR(36) CHARACTER SET ascii PRIMARY KEY,
 dataset_uuid CHAR(36) CHARACTER SET ascii NOT NULL,
 revision INT UNSIGNED NOT NULL,
 manifest_sha256 CHAR(64) CHARACTER SET ascii NULL,
 status VARCHAR(32) NOT NULL,
 plan_json JSON NOT NULL,
 approval_json JSON NULL,
 created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
 FOREIGN KEY(dataset_uuid) REFERENCES datasets(dataset_uuid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE instances (
 sop_uid VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
 series_uid VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 sop_class_uid VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 artifact_sha256 CHAR(64) CHARACTER SET ascii NOT NULL,
 artifact_relative_path TEXT NOT NULL,
 prepared_run_uuid CHAR(36) CHARACTER SET ascii NOT NULL,
 FOREIGN KEY(series_uid) REFERENCES series(series_uid),
 FOREIGN KEY(prepared_run_uuid) REFERENCES ingest_runs(run_uuid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE transfer_results (
 run_uuid CHAR(36) CHARACTER SET ascii NOT NULL,
 target_key VARCHAR(64) CHARACTER SET ascii NOT NULL,
 sop_uid VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 target_snapshot JSON NOT NULL,
 status VARCHAR(32) NOT NULL,
 attempts INT UNSIGNED NOT NULL DEFAULT 0,
 response_code VARCHAR(16) NULL,
 verification_json JSON NULL,
 error_message TEXT NULL,
 updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
 PRIMARY KEY(run_uuid, target_key, sop_uid),
 FOREIGN KEY(run_uuid) REFERENCES ingest_runs(run_uuid),
 FOREIGN KEY(sop_uid) REFERENCES instances(sop_uid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE external_mappings (
 dataset_uuid CHAR(36) CHARACTER SET ascii NOT NULL,
 target_system VARCHAR(128) CHARACTER SET ascii NOT NULL,
 target_dataset_id BIGINT NOT NULL,
 PRIMARY KEY(dataset_uuid, target_system),
 UNIQUE KEY uq_target_dataset(target_system,target_dataset_id),
 FOREIGN KEY(dataset_uuid) REFERENCES datasets(dataset_uuid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
