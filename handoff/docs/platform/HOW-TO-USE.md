# How to use the platform's repositories

From a fresh clone to a merged change. Nothing here needs access to the Oncosoft hosts; everything that does
(deploying, reading production logs, driving agent terminals) is in `RUNBOOK.md` and is done by the system's owner.

## 1. Clone

Start with this repository to understand the whole platform, then the repository of the system you will change,
on its registered branch:

```sh
git clone git@github.com:oncosoft-io/onco-mlops.git                   # coordination: main (this repo)
git clone git@github.com:oncosoft-io/Oncostudio-Labeling-Server.git   # hub: Labeling-Server-Main (backend), labeling-web (web UI)
git clone git@github.com:oncosoft-io/ai-training-web.git              # executor: ai-training-web-main
git clone git@github.com:oncosoft-io/onco-plan.git                    # OncoStudio: feature/labeling-provenance
git clone git@github.com:oncosoft-io/ai-pipeline.git                  # ai-pipeline: mlops-phase1
git clone git@github.com:jmleeonco/ai-platform-dataops.git            # data management DB: main (the user's account, D336)
```

Access: ask the organisation owner to add you to `oncosoft-io` (all repositories are private).
Patient data and secrets are never committed; if you see either in a diff, stop and remove it before pushing.

## 2. Read, in this order

1. `README.md` of this repo (10 minutes): what the platform is, the diagrams, the repositories, the rules.
2. `docs/platform/REPOSITORIES.md`: which repository holds what, and its layout.
3. `ARCHITECTURE.md`: hosts, components, the numbered flows.
4. The `README.md` and `CLAUDE.md` of the repository you will change, including its section "Working from a GitHub
   clone" (what runs locally, what is host-only).
5. `CONTRACT.md`, the sections your change touches (cited in code comments, commit messages and STATUS lines as `§n`).
6. The last 30 lines of `STATUS.md` here and of your system's `handoff/STATUS.md`: what is in flight.

## 3. Run the tests of one system

Each repository's "Working from a GitHub clone" section is authoritative and was verified on a fresh clone; the
summary:

### Hub backend (`Oncostudio-Labeling-Server`, branch `Labeling-Server-Main`)

Needs Docker. The suite runs inside a throw-away container with its own PostgreSQL.

```sh
docker build -t oncolab-ingest:wip .
bash deploy/run_tests.sh oncolab-ingest:wip
```

### Hub web UI (`Oncostudio-Labeling-Server`, branch `labeling-web`, directory `web/`)

Needs Node 20. Unit tests with `node --test`; end-to-end tests with Playwright need a running hub.

```sh
cd web && npm ci && node --test tests/ && npx vite build
```

### Executor (`ai-training-web`)

Needs Python 3.11 or newer and, for the adapter goldens, a read-only clone of `Oncostudio-Research` at the pinned
commit (its `CLAUDE.md` says how); no GPU, Docker or Ray.

```sh
cd backend && python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt && python -m pytest -q
```

### ai-pipeline (`ai-pipeline`)

Needs Python 3. The hermetic suites run without the host; the smoke test reads the model store and is host-only.

```sh
pip install -r requirements.txt && ONCO_MLOPS_DIR=<clone of onco-mlops> scripts/run_tests.sh --no-smoke
```

### Data management DB (`ai-platform-dataops`)

Needs Python 3 and, for the DB tests, a MariaDB 11.4 reachable as `.env.example` describes (`deploy/db.sh up`
starts one in docker). Read `docs/HANDOVER.md` first; no Central, no production DB, no real patient data in tests.

```sh
pip install -r requirements-dev.txt && PYTHONPATH=. python -m pytest -q -p no:warnings
```

### OncoStudio (`onco-plan`)

Windows, Visual Studio 2022 and Qt as its `README.md` lists; the headless test executables run without the hub.

### Coordination (`onco-mlops`)

No code to test. The documentation site is static: open `docs/index.html` in a browser. The documentation gate
that the owners run is `scripts/docs-gate.sh <rev-range>`: a commit that changes a path listed in `docs/DOCMAP.txt`
must also change the page named there.

## 4. Make a change

1. In the system's own repository, branch from its registered branch: `git switch -c <short-topic>`.
2. Change one system. If the change needs another system to change too, it is a contract change: first a pull
   request to `onco-mlops` that bumps the version in `CONTRACT.md`, adds the decision to `DECISIONS.md` and updates
   the documentation page; then one pull request per system, producer before consumer.
3. Run that system's tests (section 3) and the documentation gate when a mapped path changed.
4. Append a `MILESTONE` line to the system's `handoff/STATUS.md` (or `handoff/STATUS-web.md` for the web UI,
   `mlops_platform/STATUS.md` for OncoStudio) saying what changed, the commit, and the evidence (test counts, the
   check you ran). This log is how the next person or agent picks up where you stopped.
5. Open a pull request to the registered branch of that repository (`CONTRIBUTING.md` has the checklist). One topic.

## 5. What happens after the merge

The platform runs on the Oncosoft hosts from checkouts of each repository. After a merge, the owning role pulls the
branch on the host (`git pull --ff-only`) and deploys once its proof passes (deploy commands: `WORKSPACES.md`,
`RUNBOOK.md`). The host-side commits (deploy records, STATUS lines) are pushed back to the same repository by the
role and by the platform's sync script.

## 6. Reading the state of the platform

| Question | Where the answer is |
|---|---|
| What was decided and why | `DECISIONS.md`, newest at the end |
| What the apps promise each other | `CONTRACT.md`; the version header lists the latest sections |
| What each role is working on | the last lines of its STATUS file; `FOR <role>:` lines are open orders, `MILESTONE` lines closed work, `QUESTION:` / `BLOCKED:` need an answer |
| The plan behind a batch of changes | `plans/<date>-<name>/PLAN.md` and `TASKS_<role>.md` |
| How a page or flow looks to users | `docs/` (`guides/` per page, `flows.html` per numbered flow) |
| How a training job runs end to end | the executor's `README.md`, `CONTRACT.md` §9 and §43 to §61, `docs/guides/training.html` |

## 7. Working as an AI agent

Read `AGENTS.md`. In short: pick the system, read its `CLAUDE.md`, build against the contract, run the tests,
write the MILESTONE line, push your registered branch on the host or open a pull request from a clone, and never
touch `Oncostudio-Research`, patient data or secrets.
