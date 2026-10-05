# graph-harness-engineering (GHE)

A Claude Code toolkit that turns a requirement (Jira ticket, file, or prompt) into a working, deployed project using a **deterministic graph of specialist agents** instead of an open-ended loop.

The graph is data (`ghe/graph.yaml`). A small Python runner (`ghe/runner/ghe_tool.py`) decides what runs next, enforces budgets, detects non-progress, and checks drift. The LLM never decides control flow.

## Prerequisites
- Claude Code, Python 3.9+, `pip install pyyaml`, git.
- Jira tracker (optional): authenticate the Atlassian MCP with `/mcp`. Without it, set `tracker: local` in `ghe/config.yaml`.
- Cloud deploys only: AWS region and credentials, supplied by whoever makes the request (env vars, `AWS_PROFILE`, or SSO). GHE never defaults a region or stores credentials.

## Install into a project (both scenarios)
```bash
bin/ghe-init /path/to/project              # works for an empty folder or an existing repo
cd /path/to/project && pip install pyyaml
```
`ghe-init` copies agents, skills, hooks, rules, the runner, templates and schemas. It never overwrites an existing `.claude/settings.json` or `ghe/config.yaml` unless you pass `--force`. Then open Claude Code in the project and run `/ghe-setup` once. It detects the stack, fills `validation.commands` (test/lint/typecheck/build) and asks for your Jira site, project key and board in `ghe/config.yaml`. The shipped config holds only placeholders (`https://<your-org>.atlassian.net`, `<JIRA_PROJECT_KEY>`), and `tracker` defaults to `local` until you configure Jira. No tokens, cloud ids, account ids or AWS values are stored in any file.

## Create a new project
1. `bin/ghe-init ./my-app`, then `cd my-app` and `pip install pyyaml`.
2. In Claude Code: `/ghe-setup`.
3. Describe what to build. The input can be a Jira key, a file, or free text:
   ```
   /ghe-run "Build a todo REST API (Node/Express, in-memory) with a one-page HTML UI. Deploy locally."
   /ghe-run GHE-123
   /ghe-run path/to/spec.md
   ```
4. The runner walks the graph: `discover -> ba -> architect -> qa -> builder -> deploy -> tester -> final-report`.
   - `discover` normalises the input into `source.md` (frozen, with a hash).
   - `ba` writes the requirement spec with `REQ-###` IDs and acceptance criteria.
   - `architect` writes HLD, LLD and `CONTRACT.md` (the interfaces Builder and QA both follow).
   - `qa` writes test cases mapped to REQ IDs. `builder` implements stories in parallel git worktrees, and only where their `owns` globs are disjoint.
   - `deploy` (local by default; cloud via Terraform behind a human gate), then `tester` runs the cases. Failures are triaged and fixed in bounded rounds.
5. If the status becomes `WAITING_HUMAN`, answer the open question in the CLI or on the Jira ticket, then `/ghe-run --resume <run-id>`.
6. Read `ghe/runs/<run-id>/artifacts/RUN-REPORT.md` and `traceability-matrix.md`. Check progress any time with `/ghe-status`.

Optional: pass `--reviewer` (or set `reviewer: true` in `ghe/config.yaml`) for a human review gate after the Architect. Default deploy is local; use `--deploy cloud` only when you want it.

## Add features or fix bugs in an existing project
1. `bin/ghe-init /path/to/repo`, `pip install pyyaml`, then `/ghe-setup`. This is important because the validation commands protect your existing code.
2. Describe the change. For a bug or small change use maintain mode:
   ```
   /ghe-run "Add pagination to GET /todos (limit/offset)" --mode existing
   /ghe-run "Fix: PUT /todos/:id returns 500 on blank title" --mode maintain
   ```
   - `existing`: `discover` surveys the repo (structure, entry points, tests, conventions). The Architect writes an **impact analysis** instead of a full HLD, and the BA writes only the changed or added requirements.
   - `maintain`: lighter. QA and DevOps are dropped, the Builder works from a small design, and the existing validation commands act as the gate.
3. Decisions and patterns are appended to `ghe/memory/decisions.md` and `patterns.md` so later runs reuse them.
4. Review the git branch or worktree merge, plus `RUN-REPORT.md`, before merging to your main branch.

Tips: keep each request to one coherent feature. Quote exact requirements, because the verbatim source is what the drift check compares against. If the repo has no tests, the Builder adds them for the touched code.

## Day to day
| Command | Use |
|---|---|
| `/ghe-setup` | One-time per project. Detect stack, set validation commands and Jira config. |
| `/ghe-run "<input>"` | Start a run. Flags: `--mode new\|existing\|maintain`, `--roster ba,architect,...`, `--deploy local\|cloud`, `--reviewer`. |
| `/ghe-run --resume <run-id>` | Continue after a human answer or gate decision. |
| `/ghe-status` | Nodes, rounds, tokens used (out of 1,000,000), and what is being waited on. |

Run state lives in `ghe/runs/<run-id>/` (`state.json`, `graph.resolved.yaml`, `artifacts/`, `nodes/`). Bounded retries, a 1M-token ceiling and no-progress detection stop runaway loops. When a run stops, the escalation report says why.

## Git: what GHE ignores
`ghe-init` (and `/ghe-setup`, which re-checks) adds these lines to the project's `.gitignore`, without duplicating them: `ghe/`, `.claude/worktrees/`, `.claude/settings.local.json`. The `ghe/` folder holds your config, the runner, run state and memory, so it stays local. If `ghe/` was already committed, `ghe-init` warns you to run `git rm -r --cached ghe`. Note: `.claude/` (agents, skills, hooks) is not ignored, and its hooks call `ghe/runner/`, so teammates need to run `bin/ghe-init` too.

## Claude Code trust note
Run `claude` interactively in the project once and accept the trust dialog. In an untrusted workspace the project hooks and permissions in `.claude/settings.json` are ignored.

## Key properties
- With `tracker: jira`, your configured Jira project (`jira.project`) is the system of record (epic per run, tickets per agent/story). The default is `tracker: local`.
- Hard ceiling **1,000,000 tokens** per run. At 70% of a node's cap (and 80% of the run) context is compacted; a drift check proves the summary still matches the verbatim request and requirement IDs.
- Parallel stories only with disjoint `owns` globs, each in its own git worktree.
- Bounded retries; identical failures escalate as "no progress" instead of looping.
- Human gates: review (optional) and cloud `terraform apply` / destroy. AWS credentials and region come from whoever makes the request. The agent asks if missing and never defaults.

## Layout
`.claude/agents` (roles), `.claude/skills` (procedures), `.claude/hooks` (guards), `.claude/rules`, `ghe/` (graph, config, runner), `templates/`, `schemas/`, `tests/`. See `docs/QUICKSTART.md` and `CLAUDE.md`.

## Tests
`python3 -m pytest tests -q`
