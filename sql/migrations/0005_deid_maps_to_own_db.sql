-- Migration 0005: the pseudonymisation maps leave the management DB. They hold the originals in clear (patient IDs,
-- UIDs, source paths), so they live in their own schema with their own account (sql/deid_map_migrations,
-- default schema dataops_deid_map) and the management DB keeps pseudonyms only. The tables were empty when moved.
DROP TABLE deid_instance_mappings;
DROP TABLE deid_series_mappings;
DROP TABLE deid_subject_mappings;
DROP TABLE deid_runs;
