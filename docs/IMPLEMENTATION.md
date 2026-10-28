# Implementation handoff / ordered backlog

Owner asks agents to implement, not merely redesign. Start by running the existing tests.
Use the small existing sender as a reusable baseline, not a production-ready publisher.
Update this checklist when actual acceptance criteria pass; retain limitations in README.

## Baseline delivered
- [x] Local-only DICOM sender / NIfTI CT-MR converter and synthetic tests imported.
- [x] Offline summary -> candidate DB rows -> escaped HTML preview, with tests.
- [x] New schema draft and migration compatibility design.
- [x] Claude/Codex entrypoints and shared implementation brief.
- [ ] Interactive application and end-to-end production flow (below).

## Status 2026-10-28, later (owner: folder -> agent analysis -> HTML report -> 2-case local pilot ->
approval -> Central, all in the browser; labels as RTSTRUCT; Central target not decided yet)
- [x] Agent analysis job (`dataops/agent.py`, `scan.py`): headless Claude Code with Read/Glob/Grep only,
  JSON-schema proposal; `spec.py` re-checks every field and builds the plan from NIfTI headers (no
  voxels, no hashes: NIfTI UIDs use the dataset-relative path as identity, sources are hashed and pinned
  at freeze time, `sources.json`).
- [x] Label maps -> RTSTRUCT (`rtstruct.py`) on the converted image grid; per-ROI round-trip Dice in the
  report; contours reference their slice; grid mismatch is not converted.
- [x] Pilot scope (`manifest-pilot.json`), approval (`publish.approve`) bound to plan sha256 and a fully
  received pilot; full freeze reuses pilot artifacts after re-hashing their sources.
- [x] M4 Central publication adapter with journal/retry; refused unless configured + enabled + approved.
  Not exercised against a real Central SCP (target not decided).
- [x] Browser actions with CSRF/Origin checks; worker process for jobs; viewer with RTSTRUCT overlay.

## Status 2026-10-28 (owner: DB on 183, nightly NAS backup, local SCP only, local HTML viewer)
- [x] M1 inventory, plan schema 1 + validator, frozen artifacts/manifest, UID policy audit
  (NIfTI Series UID = f(PatientID prefix+group, relative path, file sha256, modality, layout,
  converter version); deterministic bytes; no LOCALREVIEW placeholder — prefix required), tests.
- [x] M2 migrations (`sql/migrations`), repository, DTO shared by preview and registration,
  idempotent registration with conflict rollback; tested on disposable `dataops_test_*` schemas.
  Legacy-export test is still open (exporter not written).
- [~] M3 read-only local viewer (runs, metadata, DB compare, delivery, slice viewer with orientation
  and window, DB tables, backups); tests for escaping, traversal, manifest-bound image serving.
  Not done: metadata editing / revisions from the browser, optimistic revision conflicts, approval.
- [ ] M4 Central publication — deferred by the owner (local SCP only for now). The local send already
  has the per-instance journal, retry and tamper refusal M4 will reuse.
- [~] M5 synthetic end-to-end (`dataops.synthetic`), configurable paths, backup/restore with proof
  (docs/OPERATIONS.md, docs/VALIDATION.md). Not done: legacy exporter dry-run, screenshots in docs.

## M1: Structured plan and immutable preparation
- Inventory mixed inputs (DICOM with/without extension, NIfTI, sidecar JSON, masks,
  unsupported files); report coverage including skips/errors. No recursive backup scans.
- Versioned typed plan schema + deterministic validators. LLM supplied via agent skill
  initially; don't require an API key just to execute an already reviewed plan.
- Per-case grouping adapters. Existing modality heuristics are proposals, not silent
  production facts; explicit dataset evidence overrides only with recorded justification.
- Freeze all selected artifacts (including source DICOM) in runtime/<run>/artifacts;
  persist UUIDs, exact hash, UID, input lineage, revision, conversion settings.
- Audit legacy UID generation and LOCALREVIEW PatientID handling. Never publish placeholder
  identities without the user seeing and accepting the final mapping before local review.
- Tests: mixed folder grouping, path relocation, changed pixels/config -> correct UID
  policy, ambiguous masks, geometry/intensity, no input mutation, artifact tamper detection.

## M2: Database and preview DTO
- Build migrations and repository layer for the draft schema; exercise on a disposable
  MariaDB database, never the production DB. No migrations at import/startup without config.
- Validate foreign keys and idempotency. Introduce required schema changes with migrations.
- Replace the current limited offline projection with full exact persistence DTOs.
  The offline preview currently has no instance-level manifest, no artifact hash and no approval.
- Tests: create/rollback/retry, dataset-series membership, duplicate UID conflict,
  external numeric ID remapping, future export produces expected legacy fields.

## M3: Local HTML application
- Implement local backend/worker and review UI described in WORKFLOW.md.
- Use same staged artifacts for OS_LJM and viewer; show where each preview came from.
- Show metadata changes, evidence, unresolved choices, skips and exact proposed DB records.
- Synthetic DICOM tests plus browser tests: escaping, traversal prevention, optimistic
  revision conflicts, status updates, slice navigation/orientation, stale review invalidation.
- No unsupported SEG/RTSTRUCT claim: initially block unsupported masks with a reason.
  Add conversion later only with geometry/reference tests and a working viewer.

## M4: Publication (separate adapter)
- Central config explicitly supplied; disabled by default. Keep local-only sender guard.
- Publish requires current user-reviewed revision/hash/target; no conversion in publish.
- Durable per-instance delivery journal, timeout reconciliation, duplicate request locking.
- New DB registration follows accepted/verified outcomes; preserve partial state and retry.
- Tests with loopback test SCP and disposable DB: rejected instance, warning, timeout,
  disconnect, DB unavailable after accepted transfer, process restart, double-click publish,
  no duplicate IDs, changed artifact/target rejected. No real Central test by default.

## M5: Operational handoff
- End-to-end sample with generated synthetic input, screenshots and setup instructions.
- Configurable runtime/DB paths; backup/restore docs for new DB + artifacts + UID mappings.
- Dry-run legacy exporter and merge report; schema version compatibility tests.
- User-provided real Central details and explicit sample-publication action are needed
  for live acceptance. Report what wasn't verified instead of inventing success.

## Missing configuration (do independent work first)
Central host/port/AE and confirmation method; new DB host/schema/app account; chosen
dataset source and license/prefix; Local SCP availability on the target workstation.
OS_LJM local default is in config/local-scp.example.json. Do not search for credentials
in unrelated folders or use the production root account from prior conversations.

## Suggested agent prompt
Read AGENTS.md and this backlog. Implement the first unfinished milestone, then continue
through the workflow while synthetic checks are possible. Keep scope on directory ->
OS_LJM + HTML review -> explicit Central + NEW DB publication. Commit coherent changes
with test evidence. Ask only for facts blocking real integration; never claim that
the initial static HTML renderer implements approvals, image viewing or publication.
