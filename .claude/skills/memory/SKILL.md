---
name: memory
description: Append decisions and patterns to the project's append-only knowledge files so later runs do not re-derive them.
allowed-tools: Read, Write, Edit, Bash
argument-hint: add decision <text> | add pattern <text> | show
---
# memory
- `ghe/memory/decisions.md`: dated one-paragraph decisions (what, why, alternatives rejected). Append only; never delete (superseded entries get a new entry that references the old one).
- `ghe/memory/patterns.md`: reusable implementation patterns.
- Cap what is loaded into any context at the last ~30 entries (config `memory_max_summary_lines` is honoured by the orchestrator when it summarises).
