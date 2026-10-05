---
name: compact
description: Summarise a node's or the run's context to save tokens without drifting from the original request (anchor + structured summary + drift check).
allowed-tools: Read, Write, Bash, Agent
---
# compact — context compaction without drift (spec §11.1)

Triggers (from `ghe_tool.py budget`): `compact_node` (node ≥ 70% of its cap) or `compact_run` (run ≥ 80% of 1,000,000).

## Output: `RUN_DIR/artifacts/context-summary.md`
```markdown
## Request
<!-- ANCHOR:BEGIN -->
<contents of source.md, byte-identical, nothing added or removed>
<!-- ANCHOR:END -->
## Requirements status
REQ-001: done | in progress | not started — where it lives (artifact/story/ticket)
## Decisions and why
## Work remaining
(only nodes/stories that are NOT done)
## Known problems
```
Rules:
- The anchor is copied from `source.md` verbatim (use a script/`cat`, never retype). Nothing else may restate or reinterpret the request.
- List EVERY `REQ-###` from `requirement-spec.md` exactly as written there; never invent or drop IDs.
- "Work remaining" lists no finished node. Decisions come from `ghe/memory/decisions.md` and artifacts, not memory.

## Verify
1. `python3 "${CLAUDE_PLUGIN_ROOT}/ghe/runner/ghe_tool.py" drift-check --run RUN_DIR --summary RUN_DIR/artifacts/context-summary.md --trigger node|run --before <tokens> --after <tokens>`.
2. If it passes, ask a small-model (`haiku`) `Agent` judge: "Here is the request and the summary. Answer: does the summary preserve every requirement and constraint, or alter, add or drop any? Reply `OK` or list the differences." Any difference → treat as failure.
3. On failure: regenerate ONCE with the failure list. If it fails again: fall back to **truncation** = anchor + last N turns, no free-form summary, and add a warning to the run report.
4. After a pass, downstream nodes read the summary FIRST and the anchor wins on any conflict.
