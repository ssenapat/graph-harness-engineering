---
name: ghe-status
description: One-screen view of a GHE run - node states, rounds, tokens vs budget, and what it is waiting on.
allowed-tools: Read, Bash, Glob
argument-hint: [run-id]
---
# ghe-status
1. Run id = argument, else the newest dir in `ghe/runs/` (by mtime).
2. `python3 ghe/runner/ghe_tool.py status --run ghe/runs/<id>` and `... budget --run ghe/runs/<id>`.
3. Print a table: node | status | attempts | tokens; then round, `tokens_total / 1,000,000 (pct)`, gates (status, revisions), compactions count, and "waiting on": pending gate or open questions from the node records. Finish with the exact command to continue: `/ghe-run --resume <id>`.
