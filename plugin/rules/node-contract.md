# Node contract (applies to every GHE agent) — spec §7.9

You run as ONE node of a deterministic graph. The runner decided you run now; you do not decide what runs next.

## Inputs you receive in your prompt
- `RUN_DIR` — e.g. `ghe/runs/ghe-2026-10-05-001`
- `NODE` — your node id (e.g. `architect`)
- `ATTEMPT` — 1 or 2 (a retry gets the previous `problems` list; fix exactly those)
- the list of upstream artifacts to read (paths under `RUN_DIR/artifacts/`)

## Reading
- Read ONLY the artifacts listed in your prompt plus `RUN_DIR/artifacts/source.md`. Do not re-scan the whole run directory.
- If `RUN_DIR/artifacts/context-summary.md` exists, read it FIRST. It contains the verbatim request anchor; the anchor wins over any summary text.

## Writing
- Write artifacts under `RUN_DIR/artifacts/` using the exact file names your skill specifies.
- Never edit `state.json`, `source.md` or another node's artifacts (hooks block it).
- Never read or write `.env`, credential files or secret stores. Never print secrets.

## The last thing you do: write `RUN_DIR/nodes/<NODE>/node-result.json`
```json
{
  "node": "architect",
  "status": "done",
  "artifacts": ["hld.md", "lld.md", "CONTRACT.md"],
  "tickets_requested": [{"kind": "task", "agent": "qa", "title": "...", "reqs": ["REQ-001"], "blocked_by": []}],
  "exit_criteria": {"every_req_mapped": true, "contract_covers_interfaces": true},
  "open_questions": [],
  "summary": "<= 5 lines: what you did and any risks",
  "tokens": {"in": 0, "out": 0}
}
```
- `status`: `done` | `blocked` | `needs_human` | `failed`.
- `artifacts` are paths relative to `RUN_DIR/artifacts/`; every one must exist.
- `exit_criteria`: your skill lists the criteria; set each to `true` ONLY if you verified it. If any is false, status is NOT `done`.
- `open_questions`: anything a human must answer (ambiguous requirement, missing AWS region/credentials, missing deploy target). Then `status` is `needs_human`. NEVER guess an answer to a question the source does not settle.
- No free-form "I'm done": the runner trusts only this file, and it validates it.

## Stuck rule
If you fail twice on the same cause, stop: `status: "blocked"` with a clear `summary` of the root cause. Do not loop and do not spawn sub-agents.

## Requirement IDs
Every requirement is `REQ-###`. Never invent an ID that is not in `requirement-spec.md`; never drop one.
