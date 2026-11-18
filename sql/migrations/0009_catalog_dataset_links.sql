-- One dataset list (CONTRACT §78.8, U210): a person's decision that a catalogue entry and a registered dataset are
-- the same dataset (confirmed) or are not (rejected). Links from an intake run's folder and shared PatientID
-- prefixes are derived when the list is read; a name match is only ever a proposal until a person decides here.

CREATE TABLE catalog_dataset_links (
  entry_id INT UNSIGNED NOT NULL,
  dataset_uuid CHAR(36) CHARACTER SET ascii NOT NULL,
  state VARCHAR(16) CHARACTER SET ascii NOT NULL,            -- confirmed | rejected
  note VARCHAR(2000) NULL,
  decided_by VARCHAR(128) NOT NULL,
  decided_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (entry_id, dataset_uuid),
  KEY ix_link_dataset (dataset_uuid),
  CONSTRAINT fk_link_entry FOREIGN KEY (entry_id) REFERENCES catalog_entries (entry_id),
  CONSTRAINT fk_link_dataset FOREIGN KEY (dataset_uuid) REFERENCES datasets (dataset_uuid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
