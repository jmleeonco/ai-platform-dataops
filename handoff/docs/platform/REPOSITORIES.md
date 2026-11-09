# Repositories and their layouts

One private GitHub repository per system under `oncosoft-io` (ai-platform-dataops under the user's own account `jmleeonco` for now, D336). The registered branch is what runs on the hosts. Each
repository carries a `handoff/` folder (OncoStudio: `mlops_platform/`) with its append-only STATUS log and read-only
snapshots of this repo's CONTRACT, DECISIONS, WORKSPACES, COLLABORATION, ARCHITECTURE and `docs/`, refreshed by the
orchestrator's sync; they are context, never the place to edit the contract.

| Repository | System | Registered branch | Owner role | Stack | Entry points |
|---|---|---|---|---|---|
| `onco-mlops` | coordination (this repo) | `main` | orchestrator | Markdown, static HTML, bash | `README.md`, `ARCHITECTURE.md`, `CONTRACT.md`, `DECISIONS.md`, `WORKSPACES.md`, `RUNBOOK.md`, `docs/index.html` |
| `Oncostudio-Labeling-Server` | hub backend (and the web UI as released) | `Labeling-Server-Main` | labeling-server | Python 3 / FastAPI, PostgreSQL 16, Docker | `README.md`, `app/`, `deploy/setup.sh`, `deploy/run_tests.sh` |
| `Oncostudio-Labeling-Server` | hub web UI | `labeling-web` | labeling-web | Svelte 5, Vite, ECharts, Node 20 | `web/package.json`, `web/src/`, `web/tests/`, `web/e2e/` |
| `ai-training-web` | training executor and pipeline adapter | `ai-training-web-main` | ai-training-web (adapter: ai-pipeline) | Python 3 / FastAPI, SQLite, Docker, Ray | `README.md`, `backend/app/`, `backend/tests/`, `pipeline_adapter/`, `scripts/dev_up.sh` |
| `ai-pipeline` | regulatory metrics, releases, ROI registry, model flags | `mlops-phase1` (also the production tree on 183) | ai-pipeline | Python 3 / Flask | `README.md`, `AGENTS.md`, `scripts/run_tests.sh`, `scripts/run_dev.sh` |
| `onco-plan` | OncoStudio C++ desktop app (labeling build + product) | `feature/labeling-provenance` | oncostudio | C++17, Qt, CMake/Ninja, Visual Studio 2022 | `README.md`, `CLAUDE.md`, `mlops_platform/`, `build_batch/` |
| `ai-platform-dataops` | the data management DB and the dataset intake flow (web :8096, MariaDB in docker) | `main` | ai-platform-dataops | Python 3, MariaDB 11.4 (docker), pydicom | `docs/HANDOVER.md` (read first), `README.md`, `AGENTS.md`, `dataops/`, `deploy/web.sh`, `deploy/db.sh` |

## onco-mlops (coordination)

| Path | What it is |
|---|---|
| `README.md` | the platform entry page: what, diagrams, repositories, how work flows, what is in this repo, rules |
| `AGENTS.md`, `CONTRIBUTING.md`, `docs/platform/` | the entry pages for people and agents (this page, `HOW-TO-USE.md`, `AGENTS-AND-ORCA.md`) |
| `ARCHITECTURE.md` | hosts, components, numbered integration flows, catalogue of repositories on the host |
| `CONTRACT.md` | the versioned cross-app API contract (§1 identities … §9 executor … §61 training data selection) |
| `DECISIONS.md` | append-only numbered decisions with reasons (U# user, D# agent) |
| `COLLABORATION.md` | how the orchestrator and the role agents work together; production rules (§5), hard limits, GitHub (§11) |
| `WORKSPACES.md`, `workspaces.env` | the role registry (paths, branches, ports, GitHub remotes, test/health/agent commands) and its machine-readable copy |
| `RUNBOOK.md` | operating the platform: services, deploy, tests, backups, acceptance recipes |
| `STATUS.md` | the orchestrator's append-only log: GOAL, MILESTONE, DECISION, FOR lines |
| `plans/<date>-<name>/` | programme plans: `PLAN.md` + `TASKS_<role>.md`; closed plans are history |
| `docs/` | the documentation site the hub serves at `/docs/`: `index.html`, `architecture.html`, `flows.html`, `roles.html`, `components/`, `guides/`; `DOCMAP.txt` maps code paths to pages; `docs/README.md` is the page contract |
| `scripts/` | the orchestrator's harness (needs ssh to the hosts): `status.sh`, `health.sh`, `jobs.sh`, `post.sh` / `report.sh`, `orca.sh`, `renew.sh`, `branches.sh`, `ws.sh`, `inbox.sh`, `sync-snapshots.sh`, `docs-gate.sh` / `docs-sync.sh` / `install-docs-gate.sh`, `install-skills.sh` |
| `skills/` | Claude Code skills: `mlops` (the orchestrator's loop) and `platform-role` (role agents) |
| `templates/` | the `CLAUDE.md` skeleton every system follows |
| `.github/pull_request_template.md` | the pull-request checklist (copy into each repository) |

## Oncostudio-Labeling-Server (the hub; two registered branches)

| Path | What it is |
|---|---|
| `app/` | FastAPI application: `/api/v1` (OncoStudio ingest and client), `/ui/api` (web UI JSON API), `/svc/v1` (service API for the executor and ai-pipeline), auth, assignments, workflow, training, label files |
| `migrations/` | PostgreSQL schema migrations, applied by `deploy/setup.sh` |
| `jobs/` | background and command-line jobs (web users, reconcile, nightly checks) |
| `tests/` | pytest suite, run inside the test container by `deploy/run_tests.sh`; `tests/mock_exec.py` mocks the executor |
| `deploy/` | `setup.sh` (build, migrate, prod and dev deploy), `run_tests.sh`, `sync_deploy.sh`, nightly scripts, a MariaDB fixture |
| `import/` | importers for legacy data |
| `web/` | the Svelte single-page app: `src/`, `tests/` (node --test), `e2e/` (Playwright), `vendor/`, `build.sh`; developed on `labeling-web`, merged into `Labeling-Server-Main` for release |
| `Dockerfile`, `requirements.txt`, `pytest.ini` | the container image (`oncolab-ingest`) and Python dependencies |
| `handoff/` | `STATUS.md` (labeling-server) or `STATUS-web.md` (labeling-web) logs and snapshots |

## ai-training-web (the executor)

| Path | What it is |
|---|---|
| `backend/app/` | the executor: `execapi.py` (/exec/v1), `jobs.py`, `execspec.py` (templates), `graph.py`, `scheduler.py`, `runner.py`, `stages.py`, `durable.py`, `execevents.py` (events to the hub), `hubclient.py`, `pipelinepin.py`, `rtss.py` (structure set selection and repair), `legacy.py` |
| `backend/tests/` | pytest suite (also runs the adapter tests); `hubmock.py` mocks the hub |
| `pipeline_adapter/` | the only code that knows the research pipeline: PipelineSpec, `pipeline.lock` (commit + tree + image), `verify`, `PIPELINE_UPDATE.md` |
| `docker/` | the derived workspace image |
| `scripts/` | `dev_up.sh` (dev instance), `deploy_prod.sh` (production deploy with backup) |
| `AGENTS.md`, `CLAUDE.md`, `PLAN.md` | agent map, rules, the original plan |
| `handoff/` | `STATUS.md` log and snapshots |

## ai-pipeline

The Flask service for regulatory metrics (`/spec`), release packaging, the ROI registry file, model flags, bundle
validation and the hub-facing side of the contract; its `README.md` (operations), `AGENTS.md` (domain and data
contracts; read §5 before touching anything under the model store) and `HANDOFF.md` (current state) are the map.
The `pipeline_adapter/` code it owns lives in the executor repository.

## ai-platform-dataops (the data management DB)

| Path | What it is |
|---|---|
| `dataops/` | the Python package: `inventory.py` / `scan.py` / `nfs.py` (read a dataset folder), `agent.py` / `spec.py` / `plan.py` / `rules.py` (analysis proposal → validated, versioned plan), `prepare.py` / `rtstruct.py` / `deid.py` (frozen artifacts, RTSTRUCT, pseudonymisation), `local_send.py` (pilot to the PC's OncoPACS, host guard), `publish.py` (owner-approved Central publication), `records.py` / `db.py` / `metadata.py` (the management DB), `legacy_db.py` / `legacy_import.py` / `deploy_import.py` / `central_find.py` (catalogue imports), `jobs.py` / `runs.py` (worker, run folders), `web/` (the web app: runs, reports, Central page, `/datasets`, `/search`, `/browse`, `/db`, `/legacy`, `/backups`) |
| `sql/migrations/`, `sql/deid_map_migrations/` | `public_dataops` (0001–0007) and the pseudonym map DB |
| `deploy/` | `web.sh` (web + worker), `db.sh` (the MariaDB container) |
| `ops/backup/` | `nightly_backup.sh` (00:30, §65 shape), `restore_check.sh` |
| `tests/` | pytest (139; DB tests use `dataops_test_*` schemas; synthetic images, loopback receiver) |
| `docs/` | `HANDOVER.md` (current state, hard rules, code map — read first), `OPERATIONS.md`, `WORKFLOW.md`, `IMPLEMENTATION.md`, `DATABASE.md`, `VALIDATION.md`, `CONTEXT.md` |
| `.claude/skills/` | the system's own skills (`public-dataops`, `dataset-intake`, `send-local-scp`) |
| `AGENTS.md`, `CLAUDE.md` | the owner's constraints and the role section (platform, §78) |
| `handoff/` | `STATUS.md` log and snapshots |
| not in git | `runtime/` (runs, jobs, deploy scans), `~/.venvs/ai-platform-dataops`, `~/.config/ai-platform-dataops/env` |

## onco-plan (OncoStudio)

The C++ desktop application. The labeling build (`LABELING_MODE=1`) adds the Labeling tab and the hub client under
`src/Infrastructure/Service/Activity/` and `src/Applications/OncoStudio/View/Labeling/`; `mlops_platform/` holds the
role's STATUS log, screenshots and platform notes; `build_batch/` the build and installer scripts; headless test
executables (`OncoStudioHeadlessTest.exe`, suites) run without a display. Its `CLAUDE.md` is the map.
