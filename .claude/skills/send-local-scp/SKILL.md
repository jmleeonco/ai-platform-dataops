---
name: send-local-scp
description: Send a dataset directory to the local DICOM SCP for inspection, using the existing CT/MR NIfTI converter when appropriate. Does not publish to Central.
argument-hint: "<directory> [--dry-run]"
---

From repository root run:
`python .claude/skills/send-local-scp/scripts/send_local_scp.py send "<directory>" [options]`.
Read the adjacent README for converter limitations. Copy config/local-scp.example.json
to this folder's local_scp.json if needed and confirm the target is the intended local SCP.
OS_LJM is the expected local AE. Keep the local-host guard. Do not reinterpret this command
as Central publication. Unresolved modality, grouping or mask ambiguity must be reported.
Use --dry-run for inspection without sending; it still creates conversion artifacts.
For HTML metadata preview use tools/review.py with the generated summary.json and a dataset
JSON as in examples/dataset.json. This static preview does not authorize publication.
