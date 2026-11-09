# Claude Code

Read and follow `AGENTS.md`. It is the shared source of implementation instructions
for Claude Code and Codex. Read `docs/HANDOVER.md` first for the current state (the
milestones in `docs/IMPLEMENTATION.md` are implemented); do not stop at a proposal.

Project skills are under `.claude/skills/`. Existing `/send-local-scp` is local-only;
do not remove its local-host guard to add Central publication. Implement a distinct
publication adapter consuming reviewed immutable artifacts.
