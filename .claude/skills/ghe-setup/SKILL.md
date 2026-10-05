---
name: ghe-setup
description: One-time per project. Detect the project's test/lint/typecheck/build commands, fill ghe/config.yaml (validation + lint), and confirm the Jira project and tracker mode.
allowed-tools: Read, Write, Edit, Glob, Grep, Bash, AskUserQuestion
---
# ghe-setup
1. Detect stack from files (`package.json` scripts, `pyproject.toml`/`pytest.ini`, `go.mod`, `Cargo.toml`, `Makefile`, `terraform/`). Propose `validation.commands` (test, lint, typecheck, build) and `lint.rules` (`[{glob, command with {file}}]`). Never invent a command that does not exist; an empty list is better than a wrong one.
2. Confirm with the user (`AskUserQuestion`): tracker `jira` or `local` (default `local`); `deploy_target` default; whether the Reviewer gate is on. If `jira`: ask for the Jira site URL (`https://<org>.atlassian.net`), the project key and optionally the board id, then write them to `jira.*`. Never assume or copy values from another project. Do not store tokens, cloud ids or account ids anywhere: auth is the Atlassian MCP (`/mcp`).
3. Write only those keys (tracker, jira.site/project/board_id, validation, lint, defaults, gates) into `ghe/config.yaml` (preserve the rest and comments).
4. Verify the target's `.gitignore` contains `ghe/`, `.claude/worktrees/` and `.claude/settings.local.json` (add any missing line; never remove existing lines). If `git ls-files ghe` lists tracked files, tell the user to run `git rm -r --cached ghe`; do not run it yourself.
5. Run each validation command once to confirm it works; report failures instead of leaving broken commands in.
