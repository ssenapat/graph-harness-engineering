---
name: ghe-setup
description: One-time per project. Detect the project's test/lint/typecheck/build commands, fill ghe/config.yaml (validation + lint), and confirm the Jira project and tracker mode.
allowed-tools: Read, Write, Edit, Glob, Grep, Bash, AskUserQuestion
---
# ghe-setup
1. Detect stack from files (`package.json` scripts, `pyproject.toml`/`pytest.ini`, `go.mod`, `Cargo.toml`, `Makefile`, `terraform/`). Propose `validation.commands` (test, lint, typecheck, build) and `lint.rules` (`[{glob, command with {file}}]`). Never invent a command that does not exist; an empty list is better than a wrong one.
2. Confirm with the user (`AskUserQuestion`): keep tracker `jira` (project GHE) or switch to `local`; `deploy_target` default; whether the Reviewer gate is on.
3. Write only those keys into `ghe/config.yaml` (preserve the rest and comments).
4. Run each validation command once to confirm it works; report failures instead of leaving broken commands in.
