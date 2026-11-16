"""Jira tracker feedback: story/defect tickets enforced, assignee policy, status sync, run-report line."""
import json, os, subprocess, sys
import yaml
from test_ghe_hardening import resolved_run, tx, set_state
from test_ghe_tool import proj  # noqa: F401

def _cfg(proj, **jira):
    p = os.path.join(str(proj), "ghe", "config.yaml")
    c = yaml.safe_load(open(p))
    c["jira"].update(jira)
    yaml.safe_dump(c, open(p, "w"), sort_keys=False)

def _stories(run, ids):
    d = os.path.join(run, "artifacts", "plans"); os.makedirs(d, exist_ok=True)
    json.dump({"stories": [{"id": i, "reqs": ["REQ-001"], "owns": ["x/**"], "dod": ["d"]} for i in ids]},
              open(os.path.join(d, "stories.json"), "w"))

def _epic(proj, run):
    return tx(proj, "ticket", "--run", run, "--kind", "epic", "--node", "run", "--key", "GHE-1")[0]["ticket"]["key"]

def test_finish_refuses_story_without_ticket(proj):
    run = resolved_run(proj); _stories(run, ["S-01"])
    d, rc, _ = tx(proj, "finish", "--run", run, "--status", "DONE")
    assert rc != 0 and "story S-01 has no Jira ticket" in json.dumps(d)

def test_story_ticket_then_status_sync_required(proj):
    run = resolved_run(proj); _stories(run, ["S-01"]); epic = _epic(proj, run)
    d, rc, _ = tx(proj, "ticket", "--run", run, "--kind", "story", "--node", "S-01", "--ref", "S-01",
                  "--key", "GHE-2", "--parent", epic)
    assert rc == 0
    set_state(run, lambda st: st.setdefault("stories", {}).update({"S-01": {"status": "done"}}))
    d, rc, _ = tx(proj, "finish", "--run", run, "--status", "DONE")
    assert "ticket GHE-2 is" in json.dumps(d)
    assert tx(proj, "ticket-status", "--run", run, "--key", "GHE-2", "--status", "Done")[1] == 0
    d, rc, _ = tx(proj, "finish", "--run", run, "--status", "DONE")
    assert "ticket GHE-2" not in json.dumps(d)

def test_blocked_needs_reason_and_unknown_status_refused(proj):
    run = resolved_run(proj); epic = _epic(proj, run)
    assert tx(proj, "ticket-status", "--run", run, "--key", epic, "--status", "Blocked")[1] != 0
    assert tx(proj, "ticket-status", "--run", run, "--key", epic, "--status", "Nope")[1] != 0
    assert tx(proj, "ticket-status", "--run", run, "--key", "GHE-99", "--status", "Done")[1] != 0

def test_assign_to_requester_needs_assignee_and_records_it(proj):
    _cfg(proj, assign_to="requester"); run = resolved_run(proj)
    d, rc, _ = tx(proj, "ticket", "--run", run, "--kind", "epic", "--node", "run", "--key", "GHE-1")
    assert rc != 0 and "assignee" in json.dumps(d)
    d, rc, _ = tx(proj, "ticket", "--run", run, "--kind", "epic", "--node", "run", "--key", "GHE-1", "--assignee", "acc-1")
    assert rc == 0 and d["ticket"]["assignee"] == "acc-1"

def test_assign_to_account_id_applies_automatically(proj):
    _cfg(proj, assign_to="acc-42"); run = resolved_run(proj)
    d, rc, _ = tx(proj, "ticket", "--run", run, "--kind", "epic", "--node", "run", "--key", "GHE-1")
    assert d["ticket"]["assignee"] == "acc-42"

def test_defect_needs_a_ticket(proj):
    run = resolved_run(proj)
    d = os.path.join(run, "artifacts", "defects"); os.makedirs(d, exist_ok=True)
    json.dump({"round": 1, "defects": [{"id": "D1", "case": "TC-1", "error": "boom"}]}, open(os.path.join(d, "round-1.json"), "w"))
    d, rc, _ = tx(proj, "finish", "--run", run, "--status", "DONE")
    assert "defect D1 has no Jira ticket" in json.dumps(d)

def test_summary_counts(proj):
    _cfg(proj, assign_to="acc-1"); run = resolved_run(proj); epic = _epic(proj, run)
    tx(proj, "ticket-status", "--run", run, "--key", epic, "--status", "In Progress")
    d, rc, _ = tx(proj, "ticket-summary", "--run", run)
    assert (d["created"], d["assigned"], d["transitioned"]) == (1, 1, 1)

def test_local_tracker_is_not_affected(proj):
    _cfg(proj); p = os.path.join(str(proj), "ghe", "config.yaml")
    c = yaml.safe_load(open(p)); c["tracker"] = "local"; yaml.safe_dump(c, open(p, "w"), sort_keys=False)
    run = resolved_run(proj); _stories(run, ["S-01"])
    d, rc, _ = tx(proj, "finish", "--run", run, "--status", "DONE")
    assert "no Jira ticket" not in json.dumps(d)

def test_skills_describe_ticket_rules():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    s = open(os.path.join(root, ".claude", "skills", "jira-sync", "SKILL.md")).read()
    for w in ("Story tickets are mandatory", "jira.assign_to", "ticket-status"):
        assert w in s
