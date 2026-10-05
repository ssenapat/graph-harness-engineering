# graph-harness-engineering (GHE)

A Claude Code toolkit that turns a requirement (a Jira ticket, a file, or a prompt) into a working, deployed project using a **deterministic graph of specialist agents** instead of an open-ended loop.

The graph is data (`ghe/graph.yaml`). A small Python runner (`ghe/runner/ghe_tool.py`) decides what runs next, enforces budgets, detects no-progress and checks drift. The LLM never decides control flow.

## Prerequisites

- Claude Code, Python 3.9+, and `pip install pyyaml`.
- Jira (optional): authenticate the Atlassian MCP with `/mcp`. Without it, set `tracker: local` in `ghe/config.yaml`.
- Cloud deploy only: AWS region and credentials, supplied by whoever makes the request (env vars, `AWS_PROFILE`, or SSO). GHE never defaults a region or stores credentials.

## Install

Choose one of the two ways.

### Option A: Claude Code plugin (recommended)

```
/plugin marketplace add ssenapat/graph-harness-engineering
/plugin install ghe@graph-harness-engineering
```

Nothing is copied into your project except `ghe/` (see "What GHE creates in your project"). The skills, agents, rules and guard hooks come from the plugin.

Skills are namespaced:

| Command | Purpose |
|---|---|
| `/ghe:setup` | One-time per project. Bootstraps `ghe/`, detects the stack and validation commands, asks for the Jira site and project |
| `/ghe:run <input>` | Starts a run, or resumes a waiting one |
| `/ghe:status` | Shows nodes, rounds, tokens used and what is waiting on a human |

To try it straight from a checkout of this repo: `claude --plugin-dir ./plugin`.

### Option B: copy into the project

```bash
bin/ghe-init /path/to/your/project
```

This copies `.claude/` (agents, skills, hooks, rules, settings), `ghe/` (runner, config, graph, templates, schemas) and the `.gitignore` entries into the target. It never overwrites an existing `.claude/settings.json` or `ghe/config.yaml` unless you pass `--force`. Commands are then un-namespaced: `/ghe-setup`, `/ghe-run`, `/ghe-status`.

## Use it

Open Claude Code in the project you want to work on, then:

1. **Set up once:** `/ghe:setup` (or `/ghe-setup`). It creates `ghe/config.yaml` and `ghe/graph.yaml` if missing, detects your validation commands (test / lint / typecheck / build) and asks for your Jira site, project key and board. The shipped config has placeholders (`https://your-org.atlassian.net`, `<JIRA_PROJECT_KEY>`) and `tracker` defaults to `local`, so nothing real is stored until you fill it in.
2. **Start a run:** `/ghe:run <input>`, where the input is a Jira key or URL, a file path, or free text.

```
/ghe:run GHE-123
/ghe:run path/to/spec.md
/ghe:run "Build a todo REST API (Node/Express, in-memory) with a one-page HTML UI. Deploy locally."
```

3. **Answer questions:** if the run pauses (`WAITING_HUMAN`), answer in the CLI or as a comment on the Jira ticket, then `/ghe:run --resume <run-id>`.
4. **Check progress:** `/ghe:status`. The final report is `RUN-REPORT.md` (see "Where artifacts are stored").

### New project vs existing project

| Mode | Use when | What happens |
|---|---|---|
| `new` | Empty folder / greenfield | Full graph: `discover` → `ba` → `architect` → `qa` → `builder` → `deploy` → `test` → `final-report` |
| `existing` | Adding a feature to a repo | `discover` surveys the repo; the Architect writes an impact analysis instead of a full HLD |
| `maintain` | Bug fix / small change | Lighter: QA and DevOps are dropped, Builder works from a small design |

Flags: `--mode new|existing|maintain`, `--roster ba,architect,...`, `--deploy local|cloud`, `--reviewer` (human review gate after the Architect), `--resume <run-id>`.

The project to change is the folder Claude Code is open in (or the one you pass explicitly). A Jira ticket describes the change but not the location. If you run from elsewhere, point GHE at the repo.

### Tips

- Quote exact requirements in the ticket. Drift checks compare against the verbatim request.
- Review the git log and branch before you merge the run's work into your main branch.

## What GHE creates in your project

Plugin install (Option A):

```
your-project/
├── ghe/
│   ├── config.yaml        # tracker, Jira site/project/board, validation commands, budgets, deploy target
│   ├── graph.yaml         # the default graph and roster rules
│   ├── memory/
│   │   ├── decisions.md   # append-only: what was decided and why
│   │   └── patterns.md    # reusable implementation patterns
│   └── runs/              # one sub-folder per run (see below)
├── .claude/worktrees/     # created while stories build in parallel (isolated git worktrees)
└── .gitignore             # GHE lines added (see below)
```

Copy-in install (Option B) additionally puts these in the project:

```
.claude/{agents,skills,hooks,rules}/   # specialist agents, skills, guard hooks, rules
.claude/settings.json                  # hook wiring
ghe/{runner,templates,schemas}/        # the runner and its templates
```

## What is ignored from check-in

`/ghe:setup` and `ghe-init` add these lines to the project's `.gitignore` (idempotent, never duplicated, existing lines kept):

```
# graph-harness-engineering (local only)
ghe/
.claude/worktrees/
.claude/settings.local.json
```

- **`ghe/`** is ignored entirely: config, graph, memory, runner and all run output stay local.
- **`.claude/worktrees/`**: temporary parallel worktrees.
- **`.claude/settings.local.json`**: personal Claude Code settings.

With the copy-in install, `.claude/{agents,skills,hooks,rules}/` and `.claude/settings.json` are **not** ignored. Commit them so teammates get the same toolkit, or add them to `.gitignore` to keep them personal. If `ghe/` files were already tracked, run `git rm -r --cached ghe`.

## Where artifacts are stored

Every run gets its own folder: `ghe/runs/<run-id>/` (for example `ghe/runs/ghe-2026-10-05-001/`).

```
ghe/runs/<run-id>/
├── state.json                  # node states, rounds, tokens, gates (runner-owned; never edit by hand)
├── graph.resolved.yaml         # the graph this run actually uses, after roster resolution
├── artifacts/
│   ├── source.md               # the request, verbatim (frozen after discover)
│   ├── requirement-spec.md     # REQ-### requirements with acceptance criteria
│   ├── hld.md, lld.md          # high-level and low-level design
│   ├── CONTRACT.md             # interfaces Builder and QA both conform to
│   ├── test-cases.md / .json   # QA test cases mapped to REQ ids
│   ├── plans/                  # stories.json and build-report.md
│   ├── deploy-report.md        # URL, PID/port, how to stop it
│   ├── test-results.json / .md # Tester evidence
│   ├── defects/round-N.json    # defects per test round
│   ├── traceability-matrix.md  # REQ → design → story → test → result
│   ├── context-summary.md      # written when context is compacted
│   ├── escalation-report.md    # only if the run was stopped for a human
│   └── RUN-REPORT.md           # final report
├── nodes/<node>/node-result.json   # each agent's result (status, artifacts, exit criteria)
└── tickets/<key>.md            # ticket text when the tracker is local (see below)
```

Source code the Builder writes goes into your project's normal folders, committed on the work branch with Conventional Commit messages that cite the `REQ-###` ids. It is not stored under `ghe/`.

### If there is no Jira ticket

When the input is a file or a prompt, or `tracker: local`, no Jira issues are created. Epics, tasks and stories are kept as local tickets with `LOCAL-n` keys. The ticket registry is in `ghe/runs/<run-id>/state.json` and each ticket's text is in `ghe/runs/<run-id>/tickets/<key>.md`. The request itself is `ghe/runs/<run-id>/artifacts/source.md`.

With `tracker: jira`, the Epic and tickets live in Jira (linked to the run by labels like `ghe:run-id`), and large documents (HLD, LLD, CONTRACT, test results) are attached to the tickets.

Each run is independent. A new run does not read earlier folders under `ghe/runs/`. Everything it needs comes from the request (the Jira ticket) plus `ghe/memory/` (decisions and patterns that GHE appends to).

## Key properties

- **Deterministic control flow:** the runner and `ghe/graph.yaml` decide order, retries, budgets and gates; the LLM only executes a node.
- **Jira is the system of record** (epic per run, tickets per agent/story). The default is `tracker: local`; set `tracker: jira` with your site and project to use Jira.
- **Hard ceiling of 1,000,000 tokens per run** (input + output + cache write + cache read). At 70% of a node's cap the context is compacted, and a drift check proves the summary still matches the verbatim request and REQ ids.
- **Parallel stories** only with disjoint `owns` globs, each in its own git worktree.
- **Bounded retries:** identical failures escalate as "no progress" instead of looping.
- **Human gates:** requirement review (optional) and cloud `terraform apply` / destroy.
- **AWS region and credentials come from whoever makes the request.** The agent asks if they are missing and never stores them in artifacts, Jira or summaries.

## Claude Code trust note

Run Claude Code interactively in the project once and accept the trust dialog, or set `projects["<path>"].hasTrustDialogAccepted` in `~/.claude.json`. In an untrusted workspace the project hooks and permissions in `.claude/settings.json` are ignored, so the guard hooks do not run.

## Layout (this repo)

```
.claude/{agents,skills,hooks,rules}/   # source of the toolkit
ghe/{runner,config.yaml,graph.yaml,templates,schemas}/
plugin/                                # GENERATED from the above. Do not edit by hand
.claude-plugin/marketplace.json
bin/ghe-init                           # copy-in installer
bin/ghe-build-plugin                   # regenerate plugin/  (--check verifies it is current)
tests/
docs/QUICKSTART.md, docs/ARCHITECTURE.md
```

After changing anything under `.claude/` or `ghe/`, run `bin/ghe-build-plugin` and commit `plugin/`. A test fails if it is out of date.

## Tests

```bash
python3 -m pytest tests -q
```
