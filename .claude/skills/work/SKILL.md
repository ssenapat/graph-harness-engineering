---
name: work
description: Story worker procedure. Implement one story inside its owns-globs, self-check the DoD, commit.
allowed-tools: Read, Write, Edit, Glob, Grep, Bash
---
# work — story worker procedure

1. Read your story (`plans/stories.json`, id in prompt), its `contract_refs` in `CONTRACT.md`, and only the LLD/requirements it cites.
2. Implement the smallest change satisfying every DoD bullet. Stay inside `owns`; if blocked, report `blocked`.
3. Run the relevant validation commands; fix failures, never weaken checks.
4. Self-check: go through each DoD bullet; mark true only if verified.
5. Commit: `feat(<story-id>): <summary>` citing `REQ-###` IDs (Conventional Commits). No secrets, no debug leftovers.
6. Write `nodes/builder/stories/<story-id>.json` (see `agents/story-worker.md`).
