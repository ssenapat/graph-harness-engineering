---
name: test-run
description: Tester procedure. Execute test cases in parallel waves with evidence; write test-results and fingerprintable defect reports.
allowed-tools: Read, Write, Glob, Grep, Bash
---
# test-run — Tester node procedure (spec §7.7)

1. Read `test-cases.json`, `deploy-report.md` (base URL), `CONTRACT.md`. Round number N from the prompt.
2. Run `parallel_safe` cases concurrently (≤ `test_concurrency`), the rest serially. Capture evidence (response bodies, logs, screenshots for UI via Playwright).
3. A case is `pass` only if the observed result equals the expected. Unrunnable → `blocked`. No retrying to hide flakiness.
4. Write `test-results.json`, `test-results.md`, and `defects/round-N.json` (see `agents/tester.md`). The runner then runs `ghe_tool.py defects --round N` to add fingerprints and judge progress.
Exit criteria: `all_cases_attempted`, `evidence_captured`, `defects_have_expected_actual`.

Evidence binding (F12/T16): `defects/round-N.json` must carry `plan_sha` (sha256 of `plans/stories.json`) and may carry `source_hash`. The runner rejects evidence whose binding is stale, and refuses to treat an empty defect list as a pass when it is not bound to the current plan.
