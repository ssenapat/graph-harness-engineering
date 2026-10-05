---
name: story-planner
description: Builder procedure. Split the design into stories with owns-globs and dependencies, then integrate and validate after the waves complete.
allowed-tools: Read, Write, Edit, Glob, Grep, Bash
---
# story-planner — Builder node procedure (spec §7.5)

## PHASE=plan
1. Read `requirement-spec.md`, `lld.md`, `CONTRACT.md`.
2. Write `plans/stories.json` (schema `${CLAUDE_PLUGIN_ROOT}/schemas/stories.schema.json`) and `plans/stories.md`:
   story = one coherent slice (one component or one vertical slice), `reqs`, `depends`, **`owns` globs**, `dod` (3–6 checkable bullets), `contract_refs`.
3. Rules: every REQ covered; no story without `owns`; stories that may run together own disjoint paths; shared files (package.json, lockfiles) go in the first story and others `depends` on it.
4. Run `python3 "${CLAUDE_PLUGIN_ROOT}/ghe/runner/ghe_tool.py" waves --run RUN_DIR`. If it reports `serialised_due_to_overlap`, either accept (less parallel) or tighten `owns`. Exit criteria: `all_reqs_covered`, `all_stories_have_owns_and_dod`, `waves_computed`.

## PHASE=integrate
After all waves merged: run every `validation.commands` entry; fix only integration gaps (imports, wiring, config); write `build-report.md`: run command, ports, env var NAMES, health-check URL, known gaps. Exit criteria: `validation_commands_pass` (or none configured and said so), `build_report_written`, `no_extra_features`.
