# Initial validation

2026-10-27, Windows / local Python environment:

- `python -m pytest -q`: 24 passed (local converter/sender + offline preview tests).
- Example HTML and candidate-records.json generated with tools/review.py.
- Source metadata HTML-escaped; conflicting study ownership and duplicate series rejected.
- Runtime outputs are gitignored. No patient files, credentials or DB dumps committed.

The inherited tests use synthetic images and loopback test receivers. No live OS_LJM,
Central transmission or production DB write was performed. MariaDB DDL is a draft and
has not been executed. GitHub Actions is configured but its result must be checked after
push; the local result does not prove that CI or a different dependency version passes.
