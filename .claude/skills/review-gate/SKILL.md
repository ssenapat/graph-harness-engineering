---
name: review-gate
description: Optional human Reviewer gate after Architect. AI pre-review summary, then approve / request changes / reject by the human (in the CLI or as a Jira comment/transition).
allowed-tools: Read, Write, Glob, Grep, Bash, AskUserQuestion
---
# review-gate (spec §7.4, §7.8)

1. `reviewer` agent writes `review-summary.md` (advice only).
2. Orchestrator: `ghe_tool.py gate --name review-gate --request "<what to review>"` → run is `WAITING_HUMAN`.
3. Ask the human: tracker `local`/CLI → `AskUserQuestion` (Approve / Request changes / Reject); tracker `jira` → post the summary on the architect ticket and wait for a transition or comment; the human resumes with `/ghe-run --resume <run-id>`.
4. Record: `ghe_tool.py gate-decide --name review-gate --decision approve|request_changes|reject --comment "<text>"`.
   - `approve` → continue. `request_changes` → re-run `architect` with the comment (max 2 revisions, then escalated). `reject` → run CANCELLED.
