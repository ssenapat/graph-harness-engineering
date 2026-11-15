---
name: update
description: Bring this project's GHE config up to date after the toolkit was updated, and decide whether Graphify is used. Safe to re-run any time.
allowed-tools: Read, Bash, AskUserQuestion
argument-hint: [--graphify true|false] [--dry-run]
---
# ghe-update
Run after updating the plugin (`/plugin marketplace update ...` then `/plugin update ghe@graph-harness-engineering`, then restart). Also the way to change the Graphify decision later.
The runner does the work; you only ask the questions. Never edit `ghe/config.yaml` by hand for this.

1. Preview: `python3 "${CLAUDE_PLUGIN_ROOT}/ghe/runner/ghe_tool.py" update --dry-run`. Show the user what would be added (commented examples, `config_version`) and any `warnings` (e.g. a run still RUNNING / WAITING_HUMAN - the update is additive and does not touch it).
2. Apply: `python3 "${CLAUDE_PLUGIN_ROOT}/ghe/runner/ghe_tool.py" update` (keeps `ghe/config.yaml.bak`). It never changes a value the user already set.
3. Graphify. Read `graphify.decision` from the result:
   - `undecided` (or the user passed an argument): ask with `AskUserQuestion`: "Use Graphify for large codebases?" with options **Yes (enable)**, **No (disable)**, **Ask me later**. Say plainly: Graphify is optional and advisory; GHE works without it; Yes needs the `graphify` executable installed on this machine.
     - Yes: `python3 "${CLAUDE_PLUGIN_ROOT}/ghe/runner/ghe_tool.py" update --graphify true`. If it answers `action: not_installed`, tell the user to install Graphify first, then offer to set `false` now (`update --graphify false`) or to re-run `/ghe:update` after installing. Never use `--force` unless the user says Graphify is installed somewhere the runner cannot find; then set `graphify.command` first.
     - No: `python3 "${CLAUDE_PLUGIN_ROOT}/ghe/runner/ghe_tool.py" update --graphify false`.
     - Later: change nothing; the key stays absent (treated as off). `status` and `init` remind once per run until it is decided.
   - `enabled` / `disabled`: report it and say how to change it (`/ghe:graphify on|off`).
4. Report: from/to `config_version`, what was added, the Graphify decision, `installed` true/false, warnings, backup path. Do not claim Graphify works; the runner only checks the executable exists.
