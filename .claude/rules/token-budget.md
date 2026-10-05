# Token budget and compaction (spec §11)

- The run ceiling is 1,000,000 tokens (in + out + cache write + cache read). Per-node caps are in `ghe/config.yaml`.
- Keep prompts small: pass artifact PATHS, not contents. Use the cheapest model tier that fits (`models:` in config).
- The orchestrator calls `ghe_tool.py budget --node <n>` before each node and after each node's tokens are recorded.
  - `compact_node` (node at 70% of its cap): the agent must write `context-summary.md` per the `compact` skill.
  - `compact_run` (run at 80%): compact shared state, skip optional work, warn.
  - `stop_node` / `stop`: stop that node (or the run) and escalate. Never silently continue past a cap.
- A summary may never replace the request: `source.md` is the anchor, quoted verbatim, and `ghe_tool.py drift-check` must pass.
