-- The dataset catalogue (CONTRACT §78.7, U208): every known dataset, downloaded or not, with its licence, commercial
-- use and availability. Imported from the audited dataset list (every column kept in `fields`) and corrected by a
-- read-only scan of the NAS roots. A catalogue entry is not the intake: nothing here downloads or moves a file.

CREATE TABLE catalog_imports (
  import_id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  kind VARCHAR(16) CHARACTER SET ascii NOT NULL,             -- list | scan | sizes
  file_name VARCHAR(255) NULL,
  file_sha256 CHAR(64) CHARACTER SET ascii NULL,
  sheet VARCHAR(64) NULL,
  rows_read INT NULL,
  added INT NOT NULL DEFAULT 0,
  changed INT NOT NULL DEFAULT 0,
  gone INT NOT NULL DEFAULT 0,
  unchanged INT NOT NULL DEFAULT 0,
  report LONGTEXT NULL CHECK (report IS NULL OR JSON_VALID(report)),
  run_by VARCHAR(128) NULL,
  run_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE catalog_entries (
  entry_id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  entry_key CHAR(40) CHARACTER SET ascii NOT NULL,           -- sha1 of kind|name|link|ordinal (list) or path (folder)
  kind VARCHAR(16) CHARACTER SET ascii NOT NULL,             -- list (a row of the audited list) | folder (internal folder)
  name VARCHAR(1024) NOT NULL,
  link TEXT NULL,
  ordinal SMALLINT UNSIGNED NOT NULL DEFAULT 1,              -- n-th list row with the same name + link
  list_row INT UNSIGNED NULL,                                -- spreadsheet row number in the last import
  list_source VARCHAR(255) NULL,                             -- the list's 출처 column
  is_marker TINYINT(1) NOT NULL DEFAULT 0,                   -- team marker row (no link, e.g. "신규 없어서 패스")
  fields LONGTEXT NULL CHECK (fields IS NULL OR JSON_VALID(fields)),   -- every imported column, header -> value
  tags LONGTEXT NULL CHECK (tags IS NULL OR JSON_VALID(tags)),         -- normalised filter values
  origin VARCHAR(16) CHARACTER SET ascii NOT NULL,           -- public | internal
  licence VARCHAR(64) NULL,                                  -- normalised (CC BY 4.0, CC BY-NC-SA 4.0, custom / DUA, …)
  licence_text TEXT NULL,                                    -- the original text, kept beside it
  commercial_use VARCHAR(8) CHARACTER SET ascii NOT NULL,    -- yes | no | unknown
  commercial_reason VARCHAR(512) NULL,
  list_download VARCHAR(255) NULL,                           -- the list's 다운로드 여부, as written
  list_availability VARCHAR(16) CHARACTER SET ascii NULL,    -- what the list says, normalised
  availability VARCHAR(16) CHARACTER SET ascii NOT NULL,     -- not_downloaded|downloading|downloaded|blocked|failed|unknown
  availability_note VARCHAR(1024) NULL,                      -- why, and where the list and the scan disagree
  conversion VARCHAR(255) NULL,
  origin_override VARCHAR(16) CHARACTER SET ascii NULL,      -- hand edits in the web: never overwritten by an import
  licence_override VARCHAR(64) NULL,
  commercial_use_override VARCHAR(8) CHARACTER SET ascii NULL,
  availability_override VARCHAR(16) CHARACTER SET ascii NULL,
  override_note TEXT NULL,
  override_by VARCHAR(128) NULL,
  override_at TIMESTAMP NULL,
  import_id INT UNSIGNED NULL,
  first_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  gone_at TIMESTAMP NULL,                                    -- absent from a newer list (kept, flagged)
  UNIQUE KEY uq_catalog_entry_key (entry_key),
  KEY ix_catalog_entries_kind (kind)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE catalog_folders (
  folder_id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  path_sha CHAR(40) CHARACTER SET ascii NOT NULL,
  path VARCHAR(1024) NOT NULL,
  root VARCHAR(255) NOT NULL,
  role VARCHAR(16) CHARACTER SET ascii NOT NULL,             -- source | converted
  name VARCHAR(512) NOT NULL,
  present TINYINT(1) NOT NULL DEFAULT 1,
  top_entries INT NULL,                                      -- entries directly inside (0 = empty folder)
  file_count BIGINT NULL,                                    -- from the throttled size walk (off-hours, cached)
  size_bytes BIGINT NULL,
  size_complete TINYINT(1) NOT NULL DEFAULT 0,
  size_scanned_at TIMESTAMP NULL,
  size_seconds INT NULL,
  error VARCHAR(512) NULL,
  seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_catalog_folder_path (path_sha)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE catalog_matches (
  entry_id INT UNSIGNED NOT NULL,
  folder_id INT UNSIGNED NOT NULL,
  method VARCHAR(16) CHARACTER SET ascii NOT NULL,           -- exact | list_path | name | person | internal
  state VARCHAR(16) CHARACTER SET ascii NOT NULL,            -- confirmed | proposed | rejected
  score DECIMAL(4,3) NULL,
  decided_by VARCHAR(128) NULL,
  decided_at TIMESTAMP NULL,
  PRIMARY KEY (entry_id, folder_id),
  KEY ix_catalog_matches_folder (folder_id),
  CONSTRAINT fk_catalog_match_entry FOREIGN KEY (entry_id) REFERENCES catalog_entries (entry_id),
  CONSTRAINT fk_catalog_match_folder FOREIGN KEY (folder_id) REFERENCES catalog_folders (folder_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE catalog_edits (
  edit_id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  entry_id INT UNSIGNED NOT NULL,
  field VARCHAR(64) CHARACTER SET ascii NOT NULL,
  old_value VARCHAR(1024) NULL,
  new_value VARCHAR(1024) NULL,
  note TEXT NULL,
  edited_by VARCHAR(128) NOT NULL,
  edited_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY ix_catalog_edits_entry (entry_id),
  CONSTRAINT fk_catalog_edit_entry FOREIGN KEY (entry_id) REFERENCES catalog_entries (entry_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
