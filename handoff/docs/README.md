# docs/ — the platform's human-readable documentation (CONTRACT §10, D77)

Static HTML, served by the hub at `/docs/` without login; snapshots in every workspace's `handoff/docs/`.
**Every behaviour, contract or UI change updates its page in the same commit** — `DOCMAP.txt` says which page, and
`scripts/docs-gate.sh` refuses a commit that forgets.

## Pages and owners

| Page | Owner role | What it answers |
|---|---|---|
| `index.html` | orchestrator | what the platform is, who uses it for what, where to start |
| `architecture.html` | orchestrator (kept current with every change of roles, components, flows or ports) | hosts, components, ports, integration flows, repos |
| `flows.html` | orchestrator | the data's path: label → review → cohort → split → training → model → evaluation → release; label states; provenance |
| `roles.html` | orchestrator (matrix) + labeling-web (screens) | user roles, what each sees (D76), accounts and OncoStudio login binding |
| `components/hub.html` | labeling-server (+ labeling-web) | pages, APIs, tables, jobs, tests, deploy |
| `components/executor.html` | ai-training-web (+ adapter section) | jobs, stages, guards, events, adapter pin/verify |
| `components/ai-pipeline.html` | ai-pipeline | /spec metrics, queue, packaging, ROI registry, registrations |
| `components/oncostudio.html` | oncostudio | labeling build: façade, recorder/uploader, assignments, pre-labels, installer |
| `components/dataops.html` | ai-platform-dataops | the data management DB: schema and the three origins, the intake flow (analyse → pilot → approve → publish → register), web pages, CLI, hard rules, backup and restore (§78) |
| `guides/labeling.html` | oncostudio (OncoStudio side) + labeling-server / labeling-web (hub side) | how a labeler works, claims, saves, sees suggestions |
| `guides/rounds.html` | labeling-server / labeling-web (page) + ai-training-web + ai-pipeline + oncostudio (sections) | labeling rounds: pool, sampler, validation set, pre-label, train, round metrics |
| `guides/review.html` | labeling-server / labeling-web | review queue, verdicts, banners, legacy reviews |
| `guides/datasets.html` | labeling-server / labeling-web | datasets/splits, label states, demographics, profile xlsx |
| `guides/training.html` | labeling-web / labeling-server (wizard, jobs, policies, pipeline page) + ai-training-web (execution) + ai-pipeline (evaluation) | new job → stages → curve → evaluation |
| `guides/deploy.html` | ai-training-web | deploy stage, memo, versions, dry run, rollback |
| `guides/releases.html` | ai-pipeline (+ hub part by labeling-web) | packaging, validation, registration, withdrawal, hub pages |

## Page contract

- Copy `_template.html`; keep `<link rel="stylesheet" href="…/assets/docs.css">`, `<script src="…/assets/site.js">`
  and `data-root` on `<body>` (`./` for top-level pages, `../` inside `components/` and `guides/`).
- `<title>` = two to four words; `header.page` with an eyebrow (section · owner role), an `h1`, a `p.lede`
  (what the reader will be able to do after reading) and `p.meta` (last verified date + the contract version).
- Sections with stable `id`s (other pages and the hub's help links point at them); `h2` per section, tables for
  facts, `.note` for gotchas, `figure.shot` for screenshots (`assets/img/<role>-<name>.png`, ≤ 300 KB, **never a
  patient identifier or a real name** — use the dev instance with simulated data).
- Write for the person who uses the function, not for the agent who wrote it: what it is for, how to do it, what
  can go wrong, where the record ends up. Link the contract section by number instead of restating the API.
- Keep everything self-contained: no CDN, no external images; fonts come from Google Fonts with system fallbacks.
- Korean UI labels appear as they are on screen (e.g. 새 학습), followed by an English gloss once.

## Workflow (any workspace on 183, or the Windows PC)

1. Edit the page in the master: `/drive/project/onco-mlops/docs/…` (PC: `C:\onco-mlops\docs\…`).
2. `git -C /drive/project/onco-mlops add docs/<your file> && git -C /drive/project/onco-mlops commit -m "docs: …"`
   (add **only your files**: the working tree is shared by three agents).
3. From your workspace: `bash /drive/project/onco-mlops/scripts/docs-sync.sh` (copies `docs/` → `handoff/docs/`),
   then commit the code **and** `handoff/docs/` together. The pre-commit hook (`install-docs-gate.sh`) runs
   `docs-gate.sh --staged`; it fails when a `DOCMAP.txt` path changed without its page, or when the snapshot page
   differs from the master.
4. `scripts/sync-snapshots.sh` (orchestrator, or anyone on 183) refreshes the other workspaces; the hub's next
   `dev-deploy`/`prod-only` ships the new pages at `/docs/`.
