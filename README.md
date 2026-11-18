# graph-harness-engineering (GHE)

A Claude Code toolkit that turns a requirement (a Jira ticket, a file, or a prompt) into a working, deployed project using a **deterministic graph of specialist agents** instead of an open-ended loop.

The graph is data (`ghe/graph.yaml`). A small Python runner (`ghe/runner/ghe_tool.py`) decides what runs next, detects no-progress and checks drift. The LLM never decides control flow.

**Runner guarantees** (hardening F01-F04): state writes are atomic; every per-run command holds `RUN_DIR/.lock` (cross-platform, stale locks are broken after a dead owner or 60s); `init` never overwrites a run (`--resume <id>` to continue one); `start` refuses unmet dependencies, running/done nodes, waiting or terminal runs and exhausted budgets; `skip` needs `--reason` and an `optional` node; `finish --status DONE` is refused unless the run really is complete; `result` needs a started node, is idempotent, and only accepts artifacts inside `artifacts/`. The lock is tested on macOS; on Windows it is implemented but not verified.


**Jira tracker rules (runner-enforced).** With `tracker: jira`, `finish` and the planner's `result` are refused while any story in `plans/stories.json` or any defect lacks a ticket, and while a done story's ticket is not Done. `jira.assign_to` (`requester`, an account id, or `none`) controls assignment. `T ticket-status` records transitions and `T ticket-summary` feeds the run report's Tickets section. Setup resolves the site's issue types once into `jira.issue_types`.


**Expense report.** `T tokens --story <id|defect:<id>> [--tokens-in/--tokens-out/--tokens-cache N]` attributes usage to a story or defect. `T expense-report` writes `docs/expenses/<epic-or-run>.md` (cost per story/defect and node, rework, run total, and the epic total across every run of the epic) and returns the Jira comments to post (one per story/defect ticket plus the epic roll-up). Money needs `cost.prices` (per 1M tokens, per model tier) in `ghe/config.yaml`; without it the report shows tokens only. Once prices are set, `finish` requires the report. The reports live in `docs/expenses/` in the repo, so they are committed with the work.

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
| `/ghe:update` | After updating the plugin: brings this project's `ghe/config.yaml` up to date and asks about Graphify (see "Updating") |
| `/ghe:graphify on\|off\|status` | Turns the optional Graphify provider on or off, or shows its state |

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

### Updating

Updating is two steps. `/plugin update` gets the new GHE code. `/ghe:update` brings each project's config up to date.

| Step | Command | What it does |
|---|---|---|
| 1 | `/plugin marketplace update graph-harness-engineering` | Refreshes the marketplace listing so the new version is visible |
| 2 | `/plugin update ghe@graph-harness-engineering` | Installs the new plugin version (skills, agents, rules, hooks, runner) |
| 3 | Restart Claude Code | Loads the new plugin |
| 4 | `/ghe:update` (once in each project) | Migrates that project's `ghe/config.yaml` and asks about Graphify |

You do not need `/ghe:update` to exist in your old install first. It arrives with step 2. Until you run step 4, existing projects keep working with the defaults of the new runner.

```
/plugin marketplace update graph-harness-engineering
/plugin update ghe@graph-harness-engineering
# restart Claude Code, then in each project:
/ghe:update
```

Copy-in install: re-run `bin/ghe-init <project> --force`, then `/ghe-update`.

`/ghe:update` is additive. It never changes a value you set. It adds commented examples for new settings, records `config_version`, and keeps `ghe/config.yaml.bak`. It asks one question: use Graphify? **Yes** (enables it; refused if the `graphify` executable is not found, so you can install it first), **No**, or **Ask me later** (nothing is written; treated as off). Graphify is optional and advisory; GHE works without it. To change the answer later, run `/ghe:update` again or `/ghe:graphify on|off`. `--dry-run` and `/ghe:graphify status` write nothing. Running `init` or `status` also applies the same additive migration and reminds you while Graphify is undecided; it never blocks a run. The update does not touch a run that is in progress.

### New project vs existing project

| Mode | Use when | What happens |
|---|---|---|
| `new` | Empty folder / greenfield | Full graph: `discover` → `ba` → `architect` → `qa` → `builder` → `deploy` → `test` → `final-report` |
| `existing` | Adding a feature to a repo | `discover` surveys the repo; the Architect writes an impact analysis instead of a full HLD |
| `maintain` | Bug fix / small change | Lighter: `devops-prep` is dropped, the BA uses a light variant and the Architect runs only for design-impacting changes. QA/Tester stay in the roster unless you drop them; without a Tester, `finish --status DONE` requires `verify` (the runner executes `validation.commands`) to pass or an explicit `verify --waive --reason ... --by ...` |

Flags: `--mode new|existing|maintain`, `--roster ba,architect,...`, `--deploy local|cloud`, `--reviewer` (human review gate after the Architect), `--resume <run-id>`.

The folder Claude Code is open in is the **workspace**. It can be a repo or any other folder. The repos to change are resolved as described in the next section, so you can name them in the prompt or in the Jira ticket.

## Working across repos (workspace mode)

Install the plugin anywhere and run `/ghe:run` from any folder, even one that is not a repo. That folder is the **workspace**: it holds `ghe/runs` and `ghe/workspace.yaml`. The repos a run works on are resolved like this:

1. Repo names, paths or clone URLs in the prompt win. If the prompt names none, GHE reads them from the Jira ticket. It asks you only when neither does.
2. Each hint is looked up locally first: the current folder, then its child folders, then the parent folder's children (`ghe_tool.py repos find`).
3. Ambiguous or fuzzy matches are shown to you. A clone happens only after you confirm the URL and destination (`repos clone --confirmed`). Clones go into the workspace folder, or next to it when the workspace is itself a repo.
4. Any repo without `ghe/config.yaml` gets `/ghe:setup` run on it (you confirm its test/lint commands) before work starts. Each repo keeps its own validation commands.

### Tips

- Quote exact requirements in the ticket. Drift checks compare against the verbatim request.
- Review the git log and branch before you merge the run's work into your main branch.

## What GHE creates in your project

Plugin install (Option A):

```
your-project/
├── ghe/
│   ├── config.yaml        # tracker, Jira site/project/board, validation commands, deploy target
│   ├── graph.yaml         # the default graph and roster rules
│   ├── workspace.yaml     # repos this workspace works on (written by a run)
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

- **Deterministic control flow:** the runner and `ghe/graph.yaml` decide order, retries and gates; the LLM only executes a node.
- **Jira is the system of record** (epic per run, tickets per agent/story). The default is `tracker: local`; set `tracker: jira` with your site and project to use Jira.
- **No token limits:** usage is recorded and reported (`tokens`, `status`) but never capped. Compaction (with a drift check proving the summary still matches the original request) is optional.
- **Parallel stories** only with disjoint `owns` globs, each in its own git worktree.
- **Bounded retries:** identical failures escalate as "no progress" instead of looping.
- **Human gates:** requirement review (optional) and cloud `terraform apply` / destroy.
- **AWS region and credentials come from whoever makes the request.** The agent asks if they are missing and never stores them in artifacts, Jira or summaries.
- **Runner-enforced rules, not prompts:** state writes are atomic and every per-run command holds `RUN_DIR/.lock` (stale locks are broken after 60s, cross-platform). `start` refuses unmet dependencies, running/done nodes, waiting or terminal runs and an exhausted budget. `finish --status DONE` is refused without `RUN-REPORT.md`, with traceability gaps, or without deterministic verification (`verify`) or a recorded waiver. `result` is idempotent by digest and rejects traversal, absolute and symlink-escape artifact paths. Defects must carry current plan/source evidence (F12/T16).
- **Budgets and corrections:** usage is ingested once per event id, and duplicate events are not double-counted. Retry rules are separate: `node_retry` (technical), `max_rounds` (verification), `max_story_revisions` (substantive correction) and `max_gate_revisions`. Further corrections need an explicit, identified `allowance-grant`; `correction_scope` is `story` (default) or `epic_global`.
- **Controlled actions:** `authorize` records who approved a cloud-apply or destructive action. The `ghe_guard.py` hook is a second layer; it also blocks edits to runner-owned state, a frozen `source.md`, a started `Agent` dispatch over budget, and edits to design files (HLD/LLD/CONTRACT) without an approved design revision (`design-propose` / `design-decide`).
- **Side effects and trackers:** `journal-intent` / `journal-confirm` / `journal-reconcile` record external writes so a crash cannot cause a duplicate; `finish` refuses while one is unresolved. Tickets are idempotent per (kind, node, ref) with epic > story > subtask hierarchy. Tracker outage, switching and human edits are handled explicitly (`tracker-down`, `tracker-switch`, `ticket-attach`, `ticket-field`).
- **Memory and context:** `memory-manifest` builds a bounded, relevance-ranked packet (superseded entries excluded, nothing truncated silently). `context-packet` does the same for large sources. `escalation-report` renders the human escalation package from recorded state with no LLM.
- **Optional Graphify:** off by default. `ghe/runner/ghe_graph.py` (`preflight`, `retrieve`, `status`) falls back to bounded direct file retrieval if Graphify is missing, stale, failing or times out. Graph results are advisory, inferred edges are never authoritative, and nothing is uploaded unless semantic extraction is explicitly approved. See `ghe/runner/GRAPHIFY.md`. The Graphify CLI flags and output format are unverified guesses, configurable in `ghe/config.yaml`.
- **Release:** `release-check`, `release-record` and `release-cleanup` gate production releases on verification and the cloud-apply gate, and record the local process for safe cleanup. `deploy-target` asks the requester for region/account and never stores credentials.

## Claude Code trust note

Run Claude Code interactively in the project once and accept the trust dialog, or set `projects["<path>"].hasTrustDialogAccepted` in `~/.claude.json`. In an untrusted workspace the project hooks and permissions in `.claude/settings.json` are ignored, so the guard hooks do not run.

## Layout (this repo)

```
.claude/{agents,skills,hooks,rules}/   # source of the toolkit
ghe/{runner (ghe_tool.py, ghe_graph.py),config.yaml,graph.yaml,templates,schemas}/
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
bin/ghe-build-plugin --check   # plugin/ must match the sources
```

The tests cover the runner (`test_ghe_tool.py`, `test_ghe_hardening.py`, `test_ghe_stage2.py`, `test_ghe_stage4.py`), hooks, plugin sync, Graphify (`test_ghe_graph.py`) and skill rules (`test_ghe_skills.py`). Agent behaviour that follows the skills is not covered by automated tests, and real Jira API calls and the real Graphify CLI have not been exercised.
