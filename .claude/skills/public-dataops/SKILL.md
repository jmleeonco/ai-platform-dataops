---
name: public-dataops
description: Interpret heterogeneous public medical dataset folders and prepare local SCP and HTML review, then publish only an explicitly reviewed revision when the backend is implemented.
---

Read AGENTS.md and docs/WORKFLOW.md. Inspect actual implementation status in README.md
and docs/IMPLEMENTATION.md before using any command. If asked to implement, continue
the backlog. If asked to process data, propose a structured grouping/conversion plan
with evidence and unresolved choices. Validate before conversion. Use the shared local
sender only for supported inputs and tools/review.py for current offline preview.
Do not invent absent prepare/publish endpoints or treat static HTML as approval.
Publication must consume the exact reviewed artifacts through the future backend,
never ad hoc SQL or an unrestricted modification of the local sender.
