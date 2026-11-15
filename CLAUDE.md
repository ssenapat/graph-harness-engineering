# CLAUDE.md — graph-harness-engineering
This repo is the toolkit. It is not an app. Work on it as follows:
- The runner `ghe/runner/ghe_tool.py` is the only place control flow lives. Change behaviour there plus a test in `tests/`.
- `ghe/graph.yaml` and `ghe/config.yaml` are data. Keep them consistent with the runner.
- Agents (`.claude/agents`) and skills (`.claude/skills`) must follow `.claude/rules/node-contract.md`.
- Never add a default AWS region, account or credentials. Never relax the guard hooks.
- Run `python3 -m pytest tests -q` before finishing any change.
- Install into a target project with `bin/ghe-init <dir>`. Never run it against this repo.
- Git identity for this repo is always `ssenapat` (remote `https://ssenapat@github.com/ssenapat/graph-harness-engineering.git`, repo-local `user.name ssenapat`). Never commit or push as `qtpgitadmin`; before committing, check `git config --local user.name` and `git log -1 --format=%an`.
