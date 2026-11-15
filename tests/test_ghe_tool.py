"""Tests for ghe/runner/ghe_tool.py. Run: python3 -m pytest tests -q"""
import json
import os
import shutil
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
TOOL = os.path.join(REPO, "ghe", "runner", "ghe_tool.py")


@pytest.fixture()
def proj(tmp_path):
    root = tmp_path / "proj"
    (root / "ghe").mkdir(parents=True)
    cfg = open(os.path.join(REPO, "ghe", "config.yaml")).read()
    cfg = (cfg.replace("tracker: local ", "tracker: jira  ", 1)
              .replace("https://<your-org>.atlassian.net", "https://example.atlassian.net")
              .replace("<JIRA_PROJECT_KEY>", "GHE"))
    (root / "ghe" / "config.yaml").write_text(cfg)
    shutil.copy(os.path.join(REPO, "ghe", "graph.yaml"), root / "ghe" / "graph.yaml")
    return root


def t(proj, *args):
    p = subprocess.run([sys.executable, TOOL, *args, "--root", str(proj)], capture_output=True, text=True)
    assert p.stdout.strip(), p.stderr
    return json.loads(p.stdout), p.returncode


def new_run(proj, roster="[ba, architect, qa, builder, devops, tester]", extra="", mode="new", target="local"):
    d, _ = t(proj, "init", "--run-id", "r1")
    run = d["run_dir"]
    (open(os.path.join(run, "artifacts", "source.md"), "w")).write(
        f"---\nmode: {mode}\nroster: {roster}\ndeploy_target: {target}\n{extra}---\n# T\nBuild a todo app.\n")
    return run


def mark_done(run, *nodes):
    """Mark upstream nodes done so a node under test can legitimately start (F02 dependency gate)."""
    sp = os.path.join(run, "state.json")
    st = json.load(open(sp))
    for n in nodes:
        st["nodes"][n]["status"] = "done"
    json.dump(st, open(sp, "w"))


def write(run, rel, text):
    p = os.path.join(run, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w").write(text)


def test_default_graph_layers(proj):
    run = new_run(proj)
    d, rc = t(proj, "resolve", "--run", run)
    assert rc == 0 and d["layers"] == [["discover"], ["ba"], ["architect"], ["qa", "builder"], ["deploy"], ["test"], ["final-report"]]
    assert d["nodes"]["deploy"]["needs"] == ["builder"]


def test_reviewer_gate_inserted_and_cloud_prep(proj):
    run = new_run(proj, roster="[ba, architect, qa, builder, devops, tester, reviewer]", target="cloud")
    d, rc = t(proj, "resolve", "--run", run)
    assert rc == 0
    assert d["nodes"]["qa"]["needs"] == ["review-gate"] and d["nodes"]["review-gate"]["needs"] == ["architect"]
    assert "devops-prep" in d["nodes"] and set(d["nodes"]["deploy"]["needs"]) == {"builder", "devops-prep"}


def test_rejections(proj):
    run = new_run(proj, roster="[ba, architect, qa]")
    d, rc = t(proj, "resolve", "--run", run)
    assert rc == 1 and "Nothing to build" in d["reason"]
    run2 = new_run_second(proj, "[ba, qa, builder]")
    d, rc = t(proj, "resolve", "--run", run2)
    assert rc == 1 and "Architect omitted" in d["reason"]


def new_run_second(proj, roster):
    d, _ = t(proj, "init", "--run-id", "r2")
    run = d["run_dir"]
    open(os.path.join(run, "artifacts", "source.md"), "w").write(f"---\nmode: new\nroster: {roster}\n---\n# T\n")
    return run


def test_omitted_agents_warn_and_rewire(proj):
    run = new_run(proj, roster="[ba, architect, builder]")
    d, rc = t(proj, "resolve", "--run", run)
    assert rc == 0 and d["layers"][-2:] == [["builder"], ["final-report"]]
    assert len(d["warnings"]) == 3


def test_result_validation_and_retry_cap(proj):
    run = new_run(proj)
    t(proj, "resolve", "--run", run)
    mark_done(run, "discover")
    t(proj, "start", "--run", run, "--node", "ba")
    write(run, "nodes/ba/node-result.json", json.dumps({"node": "ba", "status": "done", "artifacts": ["x.md"],
          "exit_criteria": {"a": False}, "summary": "s"}))
    d, rc = t(proj, "result", "--run", run, "--node", "ba")
    assert rc == 1 and d["retry_allowed"] and any("exit criterion" in p for p in d["problems"])
    t(proj, "start", "--run", run, "--node", "ba")
    d, rc = t(proj, "start", "--run", run, "--node", "ba")
    assert rc == 1 and d["action"] == "escalate"


def test_fixed_retry_accepted(proj):
    run = new_run(proj)
    t(proj, "resolve", "--run", run)
    mark_done(run, "discover")
    write(run, "artifacts/requirement-spec.md", "REQ-001")
    res = {"node": "ba", "status": "done", "artifacts": ["requirement-spec.md"], "exit_criteria": {"a": True}, "summary": "s"}
    write(run, "nodes/ba/node-result.json", json.dumps({**res, "exit_criteria": {"a": False}}))
    t(proj, "start", "--run", run, "--node", "ba")
    assert t(proj, "result", "--run", run, "--node", "ba")[1] == 1
    t(proj, "start", "--run", run, "--node", "ba")
    write(run, "nodes/ba/node-result.json", json.dumps(res))
    assert t(proj, "result", "--run", run, "--node", "ba")[1] == 0


def test_identical_retry_is_not_a_retry(proj):
    run = new_run(proj)
    t(proj, "resolve", "--run", run)
    mark_done(run, "discover")
    res = {"node": "ba", "status": "failed", "artifacts": [], "exit_criteria": {}, "summary": "s"}
    write(run, "nodes/ba/node-result.json", json.dumps(res))
    t(proj, "start", "--run", run, "--node", "ba")
    d, rc = t(proj, "result", "--run", run, "--node", "ba")
    assert rc == 0 and d["retry_allowed"]
    t(proj, "start", "--run", run, "--node", "ba")
    d, rc = t(proj, "result", "--run", run, "--node", "ba")
    assert rc == 1 and any("identical" in p for p in d["problems"]) and not d["retry_allowed"]


def test_waves_serialise_overlap(proj):
    run = new_run(proj)
    write(run, "artifacts/plans/stories.json", json.dumps({"stories": [
        {"id": "S-01", "depends": [], "owns": ["server/**"]}, {"id": "S-02", "depends": [], "owns": ["client/**"]},
        {"id": "S-03", "depends": [], "owns": ["server/routes/**"]}, {"id": "S-04", "depends": ["S-01"], "owns": ["docs/**"]}]}))
    d, rc = t(proj, "waves", "--run", run)
    assert rc == 0 and d["waves"] == [["S-01", "S-02"], ["S-03"], ["S-04"]]
    assert d["serialised_due_to_overlap"] == [{"story": "S-03", "after": "S-01"}]


def test_waves_reject_missing_owns_and_cycles(proj):
    run = new_run(proj)
    write(run, "artifacts/plans/stories.json", json.dumps({"stories": [{"id": "A", "depends": [], "owns": []}]}))
    assert t(proj, "waves", "--run", run)[1] == 1
    write(run, "artifacts/plans/stories.json", json.dumps({"stories": [
        {"id": "A", "depends": ["B"], "owns": ["a/**"]}, {"id": "B", "depends": ["A"], "owns": ["b/**"]}]}))
    assert t(proj, "waves", "--run", run)[1] == 1


def test_no_progress_detection(proj):
    run = new_run(proj)
    t(proj, "resolve", "--run", run)
    for n, err in ((1, "Timeout after 30s at /a/x.js:12"), (2, "Timeout after 45s at /b/y.js:99")):
        write(run, f"artifacts/defects/round-{n}.json", json.dumps({"round": n, "defects": [{"id": "D1", "case": "TC-1", "error": err}]}))
    d1, _ = t(proj, "defects", "--run", run, "--round", "1")
    d2, rc = t(proj, "defects", "--run", run, "--round", "2")
    assert d1["verdict"] == "first" and d2["verdict"] == "no_progress" and d2["escalate"]


def test_defects_progress_and_pass(proj):
    run = new_run(proj)
    t(proj, "resolve", "--run", run)
    write(run, "artifacts/defects/round-1.json", json.dumps({"round": 1, "defects": [
        {"id": "D1", "case": "TC-1", "error": "boom"}, {"id": "D2", "case": "TC-2", "error": "bang"}]}))
    write(run, "artifacts/defects/round-2.json", json.dumps({"round": 2, "defects": [{"id": "D2", "case": "TC-2", "error": "bang"}]}))
    write(run, "artifacts/defects/round-3.json", json.dumps({"round": 3, "defects": []}))
    t(proj, "defects", "--run", run, "--round", "1")
    d2, _ = t(proj, "defects", "--run", run, "--round", "2")
    d3, _ = t(proj, "defects", "--run", run, "--round", "3")
    assert d2["verdict"] == "progress" and len(d2["fixed"]) == 1 and d3["verdict"] == "pass"


def _frozen(proj):
    run = new_run(proj)
    t(proj, "resolve", "--run", run)
    write(run, "nodes/discover/node-result.json", json.dumps({"node": "discover", "status": "done", "artifacts": ["source.md"],
          "exit_criteria": {"ok": True}, "summary": "s"}))
    t(proj, "start", "--run", run, "--node", "discover")
    t(proj, "result", "--run", run, "--node", "discover")
    write(run, "artifacts/requirement-spec.md", "REQ-001 a\nREQ-002 b\n")
    return run


def test_drift_check_pass_and_fail(proj):
    run = _frozen(proj)
    src = open(os.path.join(run, "artifacts", "source.md")).read()
    good = f"## Request\n<!-- ANCHOR:BEGIN -->\n{src}<!-- ANCHOR:END -->\nREQ-001 REQ-002\n## Work remaining\n- builder\n"
    write(run, "good.md", good)
    assert t(proj, "drift-check", "--run", run, "--summary", os.path.join(run, "good.md"))[1] == 0
    write(run, "bad.md", "## Request\n<!-- ANCHOR:BEGIN -->\nChanged\n<!-- ANCHOR:END -->\nREQ-001 REQ-009\n")
    d, rc = t(proj, "drift-check", "--run", run, "--summary", os.path.join(run, "bad.md"))
    assert rc == 1 and len(d["failures"]) == 3


def test_drift_work_remaining_cannot_list_done_nodes(proj):
    run = _frozen(proj)
    src = open(os.path.join(run, "artifacts", "source.md")).read()
    write(run, "s.md", f"<!-- ANCHOR:BEGIN -->\n{src}<!-- ANCHOR:END -->\nREQ-001 REQ-002\n## Work remaining\n- discover again\n")
    d, rc = t(proj, "drift-check", "--run", run, "--summary", os.path.join(run, "s.md"))
    assert rc == 1 and any("finished node" in f for f in d["failures"])


def test_budget_actions(proj):
    run = new_run(proj)
    t(proj, "resolve", "--run", run)
    assert t(proj, "budget", "--run", run, "--node", "architect")[0]["action"] == "ok"
    t(proj, "tokens", "--run", run, "--node", "architect", "--add", "90000", "--event", "e0")
    assert t(proj, "budget", "--run", run, "--node", "architect")[0]["actions"] == ["compact_node"]
    t(proj, "tokens", "--run", run, "--node", "builder", "--add", "760000", "--event", "e1")
    assert t(proj, "budget", "--run", run, "--node", "builder")[0]["actions"] == ["compact_run", "stop_node"]
    t(proj, "tokens", "--run", run, "--node", "tester", "--add", "200000", "--event", "e2")
    assert t(proj, "budget", "--run", run)[0]["action"] == "stop"


def test_trace_gaps(proj):
    run = new_run(proj)
    t(proj, "resolve", "--run", run)
    write(run, "artifacts/requirement-spec.md", "REQ-001 a\nREQ-002 b\n")
    write(run, "artifacts/test-cases.json", json.dumps({"cases": [{"id": "TC-1", "reqs": ["REQ-001"]}, {"id": "TC-9", "reqs": ["REQ-777"]}]}))
    d, rc = t(proj, "trace", "--run", run)
    assert rc == 1 and "REQ-002: no test case" in d["gaps"] and "TC-9: references unknown REQ-777" in d["gaps"]


def test_gate_revisions_escalate(proj):
    run = new_run(proj)
    t(proj, "resolve", "--run", run)
    t(proj, "gate", "--run", run, "--name", "review-gate", "--request", "x")
    stat = [t(proj, "gate-decide", "--run", run, "--name", "review-gate", "--decision", "request_changes", "--by", "tester")[0]["gate_status"] for _ in range(2)]
    assert stat == ["changes_requested", "escalated"]   # F11: one design batch change, then escalate
    t(proj, "gate", "--run", run, "--name", "cloud-apply", "--request", "x")
    assert t(proj, "gate-decide", "--run", run, "--name", "cloud-apply", "--decision", "approve", "--by", "tester")[0]["gate_status"] == "approved"


def test_ticket_registry_and_project_guard(proj):
    run = new_run(proj)
    d, rc = t(proj, "ticket", "--run", run, "--kind", "task", "--node", "qa", "--key", "OTHER-5")
    assert rc == 1 and "only project GHE" in d["error"]
    d, rc = t(proj, "ticket", "--run", run, "--kind", "task", "--node", "qa", "--key", "GHE-7", "--title", "QA")
    assert rc == 0 and not d["existing"]
    d, _ = t(proj, "ticket", "--run", run, "--kind", "task", "--node", "qa", "--key", "GHE-8")
    assert d["existing"] and d["ticket"]["key"] == "GHE-7"
    assert t(proj, "ticket", "--run", run, "--kind", "epic", "--node", "-")[1] == 2  # jira tracker needs a key


def test_story_revision_cap(proj):
    run = new_run(proj)
    t(proj, "resolve", "--run", run)
    r = [t(proj, "story", "--run", run, "--id", "S-01", "--status", "revise") for _ in range(2)]
    assert [x[1] for x in r] == [0, 1] and r[1][0]["status"] == "blocked"


def test_resolve_wait_clears_waiting_without_burning_retry(proj):
    run = new_run(proj)
    t(proj, "resolve", "--run", run)
    mark_done(run, "discover")
    t(proj, "start", "--run", run, "--node", "ba")
    sp = os.path.join(run, "state.json")
    st = json.load(open(sp))
    st["nodes"]["ba"].update(status="waiting_human", open_questions=["q?"])
    st["status"] = "WAITING_HUMAN"
    json.dump(st, open(sp, "w"))
    d, rc = t(proj, "resolve-wait", "--run", run, "--node", "ba", "--answer", "a", "--outcome", "rerun", "--by", "tester")
    assert rc == 0 and d["node_status"] == "pending" and d["attempts"] == 0 and d["run_status"] == "RUNNING"
    d, rc = t(proj, "start", "--run", run, "--node", "ba")
    assert rc == 0 and d["attempt"] == 1
    d, rc = t(proj, "resolve-wait", "--run", run, "--node", "ba")
    assert rc == 1 and "not waiting_human" in d["error"]


def test_placeholder_jira_config_is_refused(tmp_path):
    root = tmp_path / "p2"
    (root / "ghe").mkdir(parents=True)
    cfg = open(os.path.join(REPO, "ghe", "config.yaml")).read().replace("tracker: local ", "tracker: jira  ", 1)
    (root / "ghe" / "config.yaml").write_text(cfg)
    shutil.copy(os.path.join(REPO, "ghe", "graph.yaml"), root / "ghe" / "graph.yaml")
    d, rc = t(root, "init", "--run-id", "r1")
    assert rc == 2 and "/ghe-setup" in d["error"]


def test_shipped_config_has_no_personal_values():
    cfg = open(os.path.join(REPO, "ghe", "config.yaml")).read()
    assert "senapathi" not in cfg and "tracker: local" in cfg and "<JIRA_PROJECT_KEY>" in cfg


# ---- F16: validated human decisions (approver identity, no blind done)
def _wait_ba(proj, with_result):
    run = new_run(proj)
    t(proj, "resolve", "--run", run)
    mark_done(run, "discover")
    t(proj, "start", "--run", run, "--node", "ba")
    sp = os.path.join(run, "state.json")
    st = json.load(open(sp))
    st["nodes"]["ba"].update(status="waiting_human", open_questions=["q?"])
    if with_result:
        st["nodes"]["ba"]["result"] = {"digest": "abc"}
    st["status"] = "WAITING_HUMAN"
    json.dump(st, open(sp, "w"))
    return run, sp


def test_f16_resolve_wait_requires_approver_and_answer(proj):
    run, sp = _wait_ba(proj, True)
    before = open(sp).read()
    for extra in (["--answer", "a"], ["--by", "me"], ["--answer", "  ", "--by", "me"]):
        d, rc = t(proj, "resolve-wait", "--run", run, "--node", "ba", "--outcome", "done", *extra)
        assert rc != 0 and not d["ok"]
    assert open(sp).read() == before


def test_f16_done_without_recorded_result_is_refused(proj):
    run, sp = _wait_ba(proj, False)
    d, rc = t(proj, "resolve-wait", "--run", run, "--node", "ba", "--answer", "a", "--by", "me", "--outcome", "done")
    assert rc != 0 and "rerun" in d["error"]
    assert json.load(open(sp))["nodes"]["ba"]["status"] == "waiting_human"


def test_f16_approver_recorded_and_rerun_refunds(proj):
    run, sp = _wait_ba(proj, True)
    d, rc = t(proj, "resolve-wait", "--run", run, "--node", "ba", "--answer", "a", "--by", "alice", "--outcome", "done")
    assert rc == 0
    assert json.load(open(sp))["nodes"]["ba"]["human_answers"][0]["by"] == "alice"


def test_f16_gate_decide_requires_by_and_cannot_redecide(proj):
    run = new_run(proj)
    t(proj, "gate", "--run", run, "--name", "cloud-apply", "--request", "plan")
    d, rc = t(proj, "gate-decide", "--run", run, "--name", "cloud-apply", "--decision", "approve")
    assert rc != 0 and "--by" in d["error"]
    assert t(proj, "gate-decide", "--run", run, "--name", "cloud-apply", "--decision", "reject", "--by", "al")[1] == 0
    d, rc = t(proj, "gate-decide", "--run", run, "--name", "cloud-apply", "--decision", "approve", "--by", "bob")
    assert rc != 0 and "cannot be re-decided" in d["error"]
    g = json.load(open(os.path.join(run, "state.json")))["gates"]["cloud-apply"]
    assert g["status"] == "rejected" and g["decided_by"] == "al"
