---
name: triage
description: Architect procedure after failed tests. Classify each defect as design/impl/test-bug/env, route fixes and amend the design if needed.
allowed-tools: Read, Write, Glob, Grep
---
# triage — classify defects, route fixes (spec §7.7)

Input: `defects/round-N.json` (with runner-added fingerprints), `test-results.json`, `CONTRACT.md`, designs, `test-cases.json`.
For each defect decide:
- `design` — the design/contract is wrong or incomplete → you amend `hld.md`/`lld.md`/`CONTRACT.md` minimally and state exactly what changed; builder+qa re-sync.
- `impl` — code does not match the contract → Builder fix story.
- `test-bug` — the test case is wrong → QA fixes the case.
- `env` — deployment/config → DevOps.
Write `defects/triage-round-N.json`: `{"round":N,"items":[{"defect":"D-001","class":"impl","fixer":"builder","reason":"","fix_story":{"id":"FIX-N-01","title":"","reqs":[],"owns":[],"dod":[]}}]}`.
Only items that appear in this round's defects; no new scope. Exit criteria: `every_defect_classified`, `fixes_routed`, `design_changes_minimal_and_listed`.
