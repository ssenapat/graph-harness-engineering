# Changelog
## [Unreleased]

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
