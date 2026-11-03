-- Which part of a dataset a series belongs to (train / valid / test …), when the dataset's own list says so
-- (e.g. a deploy bundle's train_valid_dict.json). NULL when the dataset does not split its series.
ALTER TABLE dataset_series
  ADD COLUMN split VARCHAR(32) NULL;
