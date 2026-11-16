# Token usage and compaction (spec §11)

- There are no token limits or caps. Usage is recorded and reported, never enforced.
- Keep prompts small: pass artifact PATHS, not contents. Use the cheapest model tier that fits (`models:` in config).
- Record each node's tokens with `ghe_tool.py tokens --node <n> --add <total>`; `ghe_tool.py budget` reports the totals only.
- The `compact` skill is optional: use it when a node's context gets large.
- A summary may never replace the request: `source.md` is the anchor, quoted verbatim, and `ghe_tool.py drift-check` must pass.
