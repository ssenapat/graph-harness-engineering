---
name: graphify
description: Turn the optional Graphify provider on or off for this project, or show its state.
allowed-tools: Read, Bash
argument-hint: on | off | status
---
# ghe-graphify
- `on`: `python3 "${CLAUDE_PLUGIN_ROOT}/ghe/runner/ghe_tool.py" update --graphify true`. If the result is `action: not_installed`, stop and tell the user to install Graphify first (or choose `off`); do not force it.
- `off`: `python3 "${CLAUDE_PLUGIN_ROOT}/ghe/runner/ghe_tool.py" update --graphify false`.
- `status` (default): `python3 "${CLAUDE_PLUGIN_ROOT}/ghe/runner/ghe_tool.py" update --check` and print `graphify.decision`, `enabled`, `installed`, `command`. `--check` writes nothing.
Graphify is optional and advisory: with it off, missing, failing or timed out, GHE reads the source directly. This only records the decision in `ghe/config.yaml`; it does not build or refresh a graph (the orchestrator does that when enabled).
