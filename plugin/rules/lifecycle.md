# Lifecycle and termination (spec §9)

States: PENDING → RUNNING → WAITING_HUMAN | DONE | FAILED | CANCELLED.
- Every retry is bounded: node_retry=1, max_rounds=3, max_story_revisions=2, max_gate_revisions=2.
- A retry with byte-identical output is not a retry: it escalates (`ghe_tool.py result` reports it).
- A fix round whose failing-defect fingerprints are a superset of the previous round's is `no_progress`: stop and escalate.
- DONE means: all executed nodes done, traceability matrix has no gaps, last test round has no failing case (or Tester not in roster), `RUN-REPORT.md` written.
- Escalation package = `RUN_DIR/artifacts/escalation-report.md`: what was asked, what was done, what failed, what the human must decide.
