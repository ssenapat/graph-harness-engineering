# Changelog
## [Unreleased]

## [0.3.1]
### Added
- Expense report (`expense-report`, `tokens --story`, `cost` config): cost per story/defect, rework, epic roll-up across runs, Jira cost comments. Reports are written to `docs/expenses/`.
- Jira tracker: story and defect tickets are required before `finish`; `jira.assign_to`; `ticket-status` and `ticket-summary` commands; run report Tickets section; setup asks for assignee policy and resolves issue types.
### Removed
- Token caps and budget enforcement (per-run, per-node, reserve, compaction thresholds). Usage is still recorded and reported; nothing stops or refuses work because of it. Existing config keys are ignored.

## [0.3.0]
### Added
- `ghe_tool.py update` (skill `/ghe-update`, plugin `/ghe:update`): additive, validated, backed-up (`config.yaml.bak`) migration of an existing project's `ghe/config.yaml`. Never changes values you set; adds commented examples for the new `context` and `graphify` keys and `config_version: 2`. `--dry-run`/`--check` write nothing.
- Graphify decision is asked, not assumed: `/ghe-update` and `/ghe-setup` ask Yes / No / Ask me later; `--graphify true|false` writes `graphify.enabled`. Choosing true when the executable is not found is refused (`not_installed`) unless `--force`. Re-run any time, or use `/ghe-graphify on|off|status`.
- `init` and `status` apply the additive migration automatically and report `config_update` (including an undecided Graphify) without blocking a run.
### Upgrading from 0.2.x
`/plugin marketplace update graph-harness-engineering`, `/plugin update ghe@graph-harness-engineering`, restart Claude Code, then `/ghe:update` in each project. Copy-in installs: re-run `bin/ghe-init <project> --force`, then `/ghe-update`.

## [0.2.0]
### Added
- Multi-repo workspace mode: the folder Claude Code is opened in is the workspace. Repos are named in the prompt or the Jira ticket (prompt wins), found in the current or parent folder, cloned only after you confirm the URL, and set up automatically if they lack `ghe/config.yaml`. New `ghe_tool.py repos` command and `ghe/workspace.yaml`.
- Per-repo validation commands and per-repo worktrees for stories.
### Changed
- README, quickstart and plugin README describe workspace mode.

## [0.1.1]
### Changed
- `ghe-setup` now detects the project's stack (Node/front-end, Python, Go, Rust, Java, Terraform, Makefile, monorepo) and proposes matching validation and lint commands, never another ecosystem's tools. Playwright is suggested only when installed. Failing validation commands are reported, not silently kept.

## [0.1.0]
### Added
- Initial implementation: graph, runner, agents, skills, hooks, rules, templates, schemas, ghe-init, tests.
