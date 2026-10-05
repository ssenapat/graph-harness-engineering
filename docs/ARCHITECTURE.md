# Architecture
Source (ticket/file/prompt) → `discover` → `ba` → `architect` → [`review-gate`] → `qa` ∥ `builder` (story waves, worktrees) → `deploy` → `test` loop (triage → fix → redeploy, bounded) → `final-report`.
The runner resolves the roster into a DAG, computes layers and story waves, tracks node state, counts tokens against per-node caps and the 1M ceiling, checks drift on compaction, and builds the traceability matrix (REQ → design → story → test case → result). Hooks block destructive commands, secrets access, and unapproved terraform apply.
