# Lifecycle and termination (spec §9)

States: PENDING → RUNNING → WAITING_HUMAN | DONE | FAILED | CANCELLED.
- Four distinct, bounded limits (never interchangeable): `node_retry=1` technical retry of one node; `max_rounds=3` verification rounds (test→triage→fix); `max_story_revisions=1` one substantive story correction; `max_gate_revisions=1` one design/plan batch change per gate. Exceeding any escalates.
- A retry with byte-identical output is not a retry: it escalates (`ghe_tool.py result` reports it).
- A fix round whose failing-defect fingerprints are a superset of the previous round's is `no_progress`: stop and escalate.
- DONE means: all executed nodes done, traceability matrix has no gaps, last test round has no failing case (or Tester not in roster), `RUN-REPORT.md` written.
- Escalation package = `RUN_DIR/artifacts/escalation-report.md`: what was asked, what was done, what failed, what the human must decide.
- The runner enforces this, not the agent: `start` refuses unmet dependencies, running/done nodes, a waiting or terminal run and an exhausted budget; `finish --status DONE` refuses unless every node is done/skipped, RUN-REPORT.md exists, traceability has no gaps and the last test round is clean; FAILED/CANCELLED need `--reason`; `skip` needs `--reason` and an `optional` node.
- Every state write is atomic and every per-run command takes `RUN_DIR/.lock` (stale locks - dead owner or older than 60s - are broken automatically).
- No Tester is not "tested": a run without the `test` node can only be DONE after `verify` (runner-executed `validation.commands`, empty list = blocked) passes or a waiver with reason and identity is recorded.

## Side-effect journal (F13)
Before any external write (Jira create/transition/comment, deploy apply) run `ghe_tool.py journal-intent --run $RUN --key <stable-key> --action <what>`; after it succeeds run `journal-confirm --key <stable-key> --evidence <id/url>`. If the intent is already `applied`, skip the write. If it is unresolved (crash, lost response, ambiguous apply) the runner refuses a retry: check the real system, then `journal-reconcile --key <k> --outcome applied|not_applied --evidence <what you checked>`. `finish --status DONE` is refused while an intent is unresolved.

## Correction scope (F11/T61)
`budgets.correction_scope` is `story` by default (each story has its own `max_story_revisions`; unrelated stories never consume one another's allowance). `epic_global` is a strict opt-in: one shared substantive-correction cap across every story in the ledger.

## Stale / unapproved work (F02/T04)
`start` is refused when `source.md` no longer matches the hash recorded at discover, or while a design revision is `proposed` and undecided.

