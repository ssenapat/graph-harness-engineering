---
name: design
description: Architect procedure. Produce hld.md, lld.md and CONTRACT.md from requirement-spec.md (or an impact analysis for existing repos).
allowed-tools: Read, Write, Glob, Grep
---
# design — Architect node procedure

1. Read `requirement-spec.md`, `source.md` (+ `repo-survey.md` in existing mode).
2. `hld.md` from `templates/hld.md`: components, data flow, tech choices with reasons, NFR strategy, risks; a table mapping every `REQ-###` → component. Existing mode: replace with an **impact analysis** (touched modules, blast radius, regression risk, migration needs).
3. `lld.md` from `templates/lld.md`: per component: folder/file layout, data models, error handling, and `owns` globs that do not overlap between components.
4. `CONTRACT.md`: use the `contract-first` skill.
5. List non-obvious decisions (what, why, alternatives rejected) in the `decisions` array of your node result. Do NOT write memory files yourself: the orchestrator persists them to `ghe/memory/decisions.md` through the `memory` skill. A failed or skipped memory write must never block this node.
6. If a requirement cannot be satisfied or is contradictory: `open_questions`, status `needs_human`.

Exit criteria: `every_req_mapped`, `contract_covers_interfaces`, `owns_globs_disjoint`.
Also list in `tickets_requested` one `qa` ticket and one `builder` ticket (each with `reqs`), plus a `devops` ticket when DevOps is in the roster.
