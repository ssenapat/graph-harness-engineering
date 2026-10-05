---
name: contract-first
description: How to write and verify CONTRACT.md so parallel Builder/QA work cannot drift. Used by Architect, Builder and QA.
allowed-tools: Read, Write, Glob, Grep, Bash
---
# contract-first — the shared source of truth for parallel nodes (spec §8.4)

`CONTRACT.md` sections (use OpenAPI / JSON Schema fenced blocks where possible):
1. **Base URLs and ports** (e.g. `http://localhost:3000`).
2. **Endpoints/events**: method, path, request schema, response schema, status codes, error shape.
3. **Data formats**: file formats, env vars (names only), CLI args.
4. **Invariants**: ordering, idempotency, limits.
5. **REQ map**: which `REQ-###` each interface satisfies.

Conformance check ("contract-conformance is a validation command"): the Builder integrator adds a command to the story validation (e.g. a script that requests each route and compares shapes). QA test cases cite contract entries by name. If an implementer must deviate, they raise `open_questions` to the Architect; they never edit the contract.
