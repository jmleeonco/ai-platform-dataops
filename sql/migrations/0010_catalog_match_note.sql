-- Why a folder was confirmed or rejected for a catalogue entry (D370): the evidence a decision rests on, kept next to
-- the decision so a later reader can check or undo it. Additive: one nullable column.

ALTER TABLE catalog_matches ADD COLUMN note VARCHAR(2000) NULL AFTER decided_at;
