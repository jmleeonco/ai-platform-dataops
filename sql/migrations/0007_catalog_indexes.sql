-- Lookups the web catalogue makes: the RTSTRUCT that point at an image series, and a patient across datasets.
ALTER TABLE series
  ADD INDEX ix_series_referenced (referenced_series_uid);

ALTER TABLE subjects
  ADD INDEX ix_subjects_patient_id (patient_id);
