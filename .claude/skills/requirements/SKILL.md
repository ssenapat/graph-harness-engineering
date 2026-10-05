---
name: requirements
description: BA procedure. Produce requirement-spec.md with REQ-### IDs, Given/When/Then acceptance criteria, NFRs, assumptions and open questions.
allowed-tools: Read, Write, Glob, Grep
---
# requirements — BA node procedure

1. Read `source.md` (and `repo-survey.md`, `ghe/memory/decisions.md` if present).
2. Fill `templates/requirement-spec.md` into `RUN_DIR/artifacts/requirement-spec.md`:
   - **Scope / Out of scope**
   - **Functional requirements**: `REQ-001`, `REQ-002`, ... one atomic, testable statement each, sequential, never reused.
   - **Acceptance criteria**: each REQ has ≥1 `Given / When / Then`.
   - **Non-functional requirements** (performance, security, accessibility) only if stated or clearly implied; mark implied ones `(assumed)`.
   - **Assumptions** and **Open questions**.
3. Anything ambiguous or missing: `open_questions` + status `needs_human`. No guessing.
4. Maintain mode: a short "Change impact" section: which current behaviours/tests change.

Exit criteria for node-result: `every_req_has_acceptance_criteria`, `ids_unique_sequential`, `no_invented_scope`, `open_questions_resolved_or_listed`.
