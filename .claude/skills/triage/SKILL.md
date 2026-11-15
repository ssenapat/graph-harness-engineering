---
name: triage
description: Architect procedure after failed tests. Classify each defect as design/impl/test-bug/env, route fixes and amend the design if needed.
allowed-tools: Read, Write, Glob, Grep
---
# triage — classify defects, route fixes (spec §7.7)

Input: `defects/round-N.json` (with runner-added fingerprints), `test-results.json`, `CONTRACT.md`, designs, `test-cases.json`.
For each defect decide:
- `design` — the design/contract is wrong or incomplete → do NOT edit `hld.md`/`lld.md`/`CONTRACT.md`. Propose: `T design-propose --id DR-N --summary "<exact change>" --files hld.md ... --stories <impacted ids|none> --tests <impacted test ids> --reason <id>=<why it is affected> ...` (one `--reason` per listed story/test id; the runner refuses a proposal that names an affected consumer without a reason). Only after the human/owner approves (`T design-decide --id DR-N --decision approve --by "<approver>"`) is the design edited, and only the named stories/tests are invalidated and re-run. One batch per run (`max_design_batches`).
- `impl` — code does not match the contract → Builder fix story.
- `test-bug` — the test case is wrong → QA fixes the case.
- `env` — deployment/config → DevOps.
Write `defects/triage-round-N.json`: `{"round":N,"items":[{"defect":"D-001","class":"impl","fixer":"builder","reason":"","fix_story":{"id":"FIX-N-01","title":"","reqs":[],"owns":[],"dod":[]}}]}`.
Only items that appear in this round's defects; no new scope. Exit criteria: `every_defect_classified`, `fixes_routed`, `design_changes_proposed_and_approved_before_edit`.
