---
name: test-design
description: QA procedure. Write test-cases.md and test-cases.json from requirement-spec and CONTRACT only, tagging parallel_safe cases.
allowed-tools: Read, Write, Glob, Grep
---
# test-design — QA node procedure

1. Read `requirement-spec.md`, `hld.md`, `lld.md`, `CONTRACT.md`. Do NOT open source code.
2. For every `REQ-###`: ≥1 positive and ≥1 negative or boundary case. Case ids `TC-001`...
3. Each case: `reqs`, `title`, `preconditions`, `steps`, exact `expected` (from the CONTRACT), `type`, `parallel_safe`, `data`.
4. `parallel_safe: true` only when no shared mutable state is touched (unique data per case).
5. Write both `test-cases.md` (readable) and `test-cases.json` (schema in `${CLAUDE_PLUGIN_ROOT}/schemas/test-cases.schema.json`).
6. Verify: every `REQ-###` covered; every case cites existing ids. The runner re-checks with `ghe_tool.py trace`.

Exit criteria: `every_req_has_positive_and_negative`, `all_cases_reference_valid_reqs`, `expected_results_concrete`, `parallel_safe_tagged`.
Fix rounds (`MODE=fix`): amend only cases classified `test-bug` in the triage report; keep ids stable.
