-- Full source path next to the relative one: where a series / file was read from, as an absolute path on
-- the ingest host (ingest_runs.input_root + source_relative_path). Descriptive, never an identity: the same
-- folder mounted elsewhere updates it. A keyed hash when the run is pseudonymised.
ALTER TABLE series
  ADD COLUMN source_path TEXT NULL;

ALTER TABLE instances
  ADD COLUMN source_path TEXT NULL AFTER source_relative_path;
