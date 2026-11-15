---
name: memory
description: Append decisions and patterns to the project's append-only knowledge files so later runs do not re-derive them.
allowed-tools: Read, Write, Edit, Bash
argument-hint: add decision <text> | add pattern <text> | show
---
# memory
- `ghe/memory/decisions.md`: dated one-paragraph decisions (what, why, alternatives rejected). Append only; never delete (superseded entries get a new entry that references the old one).
- `ghe/memory/patterns.md`: reusable implementation patterns.
- Never load these files wholesale. Retrieval is runner-built: `T memory-manifest --query "<request keywords>"` writes `artifacts/memory-manifest.json` with only the relevant, non-superseded entries within `memory.max_entries` / `memory.max_chars` (config), and lists what was excluded and why. Read that manifest, not the raw files.
- An entry starts with a `## <title>` heading. To replace an older decision, append a NEW entry containing `Supersedes: <old title>`; never delete or edit history.
- If the manifest refuses because an essential entry exceeds the budget, split the entry or raise `memory.max_chars`; nothing is truncated silently.
