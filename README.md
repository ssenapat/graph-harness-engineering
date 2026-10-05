# graph-harness-engineering (GHE)

A Claude Code toolkit that turns a requirement (Jira ticket, file, or prompt) into a working, deployed project using a **deterministic graph of specialist agents** instead of an open-ended loop.

The graph is data (`ghe/graph.yaml`). A small Python runner (`ghe/runner/ghe_tool.py`) decides what runs next, enforces budgets, detects non-progress, and checks drift. The LLM never decides control flow.

## Quick start
```bash
bin/ghe-init /path/to/your/project        # copies agents, skills, hooks, runner, templates
cd /path/to/your/project && pip install pyyaml
# in Claude Code:
/ghe-setup                                 # detect stack, set validation commands
/ghe-run "GHE-123"                         # or a file path, or free text
/ghe-status                                # state of the latest run
```

## Key properties
- Jira project **GHE** is the system of record (epic per run, tickets per agent/story).
- Hard ceiling **1,000,000 tokens** per run. At 70% of a node's cap (and 80% of the run) context is compacted; a drift check proves the summary still matches the verbatim request and requirement IDs.
- Parallel stories only with disjoint `owns` globs, each in its own git worktree.
- Bounded retries; identical failures escalate as "no progress" instead of looping.
- Human gates: review (optional) and cloud `terraform apply` / destroy. AWS credentials and region come from whoever makes the request. The agent asks if missing and never defaults.

## Layout
`.claude/agents` (roles), `.claude/skills` (procedures), `.claude/hooks` (guards), `.claude/rules`, `ghe/` (graph, config, runner), `templates/`, `schemas/`, `tests/`. See `docs/QUICKSTART.md` and `CLAUDE.md`.

## Tests
`python3 -m pytest tests -q`
