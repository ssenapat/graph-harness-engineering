"""Hardening tests for findings F01-F04 (GHE-F01-F04-Implementation-Plan.md, step 0).

These describe the REQUIRED behaviour. They are written first and are expected to FAIL on the
current runner; each one passes once its fix lands. Run: python3 -m pytest tests/test_ghe_hardening.py -q
"""
import json
import os
import subprocess
import sys

import pytest
import hashlib
import shutil
import yaml

from test_ghe_tool import TOOL, new_run, proj, t, write  # noqa: F401  (proj is a fixture)


# ---------------------------------------------------------------- helpers
def tx(proj, *args):
    """Like t(), but tolerates commands that print no JSON (argparse errors, tracebacks)."""
    p = subprocess.run([sys.executable, TOOL, *args, "--root", str(proj)], capture_output=True, text=True)
    try:
        d = json.loads(p.stdout) if p.stdout.strip() else None
    except ValueError:
        d = None
    return d, p.returncode, p.stderr


def state_path(run):
    return os.path.join(run, "state.json")


def raw_state(run):
    with open(state_path(run), "rb") as fh:
        return fh.read()


def get_state(run):
    return json.loads(raw_state(run))


def set_state(run, fn):
    st = get_state(run)
    fn(st)
    with open(state_path(run), "w") as fh:
        json.dump(st, fh, indent=2)


def refused(d, rc):
    """A refusal must be a clean runner error: non-zero exit AND a JSON body with ok=false."""
    return rc != 0 and d is not None and d.get("ok") is False


def resolved_run(proj, **kw):
    run = new_run(proj, **kw)
    d, rc = t(proj, "resolve", "--run", run)
    assert rc == 0, d
    return run


def write_result(run, node, artifacts=None, declare=None, **over):
    """artifacts are created on disk; `declare` overrides what the result JSON claims.
    Default artifacts satisfy the graph's `produces` (discover -> source.md)."""
    if artifacts is None:
        artifacts = ("source.md", "x.md") if node == "discover" else ("x.md",)
    for rel in artifacts:
        write(run, f"artifacts/{rel}", "content")
    res = {"node": node, "status": "done", "artifacts": list(declare if declare is not None else artifacts),
           "exit_criteria": {"a": True}, "summary": "s"}
    res.update(over)
    write(run, f"nodes/{node}/node-result.json", json.dumps(res))
    return res


def complete(proj, run, node, **kw):
    """Drive one node through start -> result -> done (legitimate path)."""
    d, rc = t(proj, "start", "--run", run, "--node", node)
    assert rc == 0, d
    write_result(run, node, **kw)
    d, rc = t(proj, "result", "--run", run, "--node", node)
    assert rc == 0, d


# ================================================================ F01: init
def test_init_refuses_existing_run(proj):
    run = resolved_run(proj)
    t(proj, "start", "--run", run, "--node", "discover")
    before = raw_state(run)
    d, rc, _ = tx(proj, "init", "--run-id", "r1")
    assert refused(d, rc), "init over an existing run must be refused"
    assert raw_state(run) == before, "existing state.json must be untouched"


def test_init_auto_id_never_reuses_an_existing_id(proj):
    ids = []
    for _ in range(2):
        d, rc = t(proj, "init")
        assert rc == 0
        ids.append(d["run_id"])
    # delete the first run dir; the count-based allocator now points at the surviving run's id
    import shutil
    shutil.rmtree(os.path.join(str(proj), "ghe", "runs", ids[0]))
    survivor = os.path.join(str(proj), "ghe", "runs", ids[1])
    set_state(survivor, lambda st: st.update(tokens_total=777))
    d, rc, _ = tx(proj, "init")
    assert rc == 0 and d["run_id"] not in ids[1:], "auto id collided with an existing run"
    assert get_state(survivor)["tokens_total"] == 777, "surviving run was overwritten"


def test_init_concurrent_calls_get_unique_ids(proj):
    """Probabilistic: the race may not fire on every run, but must never produce duplicates."""
    procs = [subprocess.Popen([sys.executable, TOOL, "init", "--root", str(proj)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(8)]
    ids = []
    for p in procs:
        out, _ = p.communicate()
        if p.returncode == 0:
            ids.append(json.loads(out)["run_id"])
    assert len(ids) == len(set(ids)), f"duplicate run ids allocated: {sorted(ids)}"
    assert len(ids) >= 1


@pytest.mark.parametrize("bad", ["../evil", "a/b", "", ".hidden", "x" * 80])
def test_init_rejects_bad_run_id(proj, bad):
    d, rc, _ = tx(proj, "init", "--run-id", bad)
    assert refused(d, rc), f"run id {bad!r} must be rejected"
    assert not os.path.exists(os.path.join(str(proj), "ghe", "evil"))


def test_resume_existing_ok_missing_fails(proj):
    run = resolved_run(proj)
    before = raw_state(run)
    d, rc, _ = tx(proj, "init", "--resume", "r1")
    assert rc == 0 and d and d["ok"] and d["run_id"] == "r1"
    assert raw_state(run) == before, "resume must not write"
    d, rc, _ = tx(proj, "init", "--resume", "nope")
    assert refused(d, rc)


# ================================================================ F02: start gate
def test_start_refuses_unmet_dependency(proj):
    run = resolved_run(proj)  # ba needs discover, which is still pending
    before = raw_state(run)
    d, rc, _ = tx(proj, "start", "--run", run, "--node", "ba")
    assert refused(d, rc), "ba must not start before discover is done"
    assert raw_state(run) == before, "a refused start must have no side effects"


def test_start_allows_node_once_dependencies_done(proj):
    run = resolved_run(proj)
    complete(proj, run, "discover")
    d, rc, _ = tx(proj, "start", "--run", run, "--node", "ba")
    assert rc == 0 and d["ok"] and d["attempt"] == 1


def test_start_refuses_node_already_running(proj):
    run = resolved_run(proj)
    t(proj, "start", "--run", run, "--node", "discover")
    d, rc, _ = tx(proj, "start", "--run", run, "--node", "discover")
    assert refused(d, rc), "a running node must not be started again"
    assert get_state(run)["nodes"]["discover"]["attempts"] == 1


def test_start_refuses_node_already_done(proj):
    run = resolved_run(proj)
    complete(proj, run, "discover")
    d, rc, _ = tx(proj, "start", "--run", run, "--node", "discover")
    assert refused(d, rc), "a done node must not be restarted"
    assert get_state(run)["nodes"]["discover"]["status"] == "done"


def test_start_refuses_while_a_gate_is_waiting(proj):
    run = resolved_run(proj)
    d, rc = t(proj, "gate", "--run", run, "--name", "review-gate", "--request", "approve?")
    assert rc == 0 and get_state(run)["status"] == "WAITING_HUMAN"
    d, rc, _ = tx(proj, "start", "--run", run, "--node", "discover")
    assert refused(d, rc), "no node may start while a human gate is waiting"
    assert get_state(run)["status"] == "WAITING_HUMAN"


@pytest.mark.parametrize("terminal", ["DONE", "FAILED", "CANCELLED"])
def test_start_refuses_terminal_run(proj, terminal):
    run = resolved_run(proj)
    set_state(run, lambda st: st.update(status=terminal))
    d, rc, _ = tx(proj, "start", "--run", run, "--node", "discover")
    assert refused(d, rc)
    assert get_state(run)["status"] == terminal, "a refused start must not revive a terminal run"




# ================================================================ F03: skip / finish
def cloud_run(proj):
    return resolved_run(proj, roster="[ba, architect, qa, builder, devops, tester]", target="cloud")


def test_skip_requires_a_reason(proj):
    run = cloud_run(proj)
    d, rc, _ = tx(proj, "skip", "--run", run, "--node", "devops-prep")
    assert refused(d, rc), "skip without a reason must be refused"
    assert get_state(run)["nodes"]["devops-prep"]["status"] == "pending"


def test_skip_unknown_node_is_a_clean_error(proj):
    run = resolved_run(proj)
    d, rc, err = tx(proj, "skip", "--run", run, "--node", "no-such-node", "--reason", "x")
    assert refused(d, rc), f"expected a JSON error, got rc={rc} stderr={err[-120:]!r}"
    assert "Traceback" not in err


def test_skip_required_node_is_refused(proj):
    run = resolved_run(proj)
    d, rc, _ = tx(proj, "skip", "--run", run, "--node", "builder")  # builder is not optional
    assert refused(d, rc), "a required node must not be skippable"
    assert get_state(run)["nodes"]["builder"]["status"] == "pending"


def test_skip_optional_node_with_reason_is_recorded(proj):
    run = cloud_run(proj)  # devops-prep is `optional: true` in graph.yaml
    d, rc, _ = tx(proj, "skip", "--run", run, "--node", "devops-prep", "--reason", "no infra needed")
    assert rc == 0 and d and d["ok"]
    n = get_state(run)["nodes"]["devops-prep"]
    assert n["status"] == "skipped" and n.get("skipped_reason") == "no infra needed"


def test_finish_done_refused_with_unfinished_nodes(proj):
    run = resolved_run(proj)
    d, rc, _ = tx(proj, "finish", "--run", run, "--status", "DONE")
    assert refused(d, rc), "DONE with pending nodes must be refused"
    assert d.get("problems"), "refusal must list the reasons"
    assert get_state(run)["status"] != "DONE"


def finish_ready_run(proj):
    """All nodes done, RUN-REPORT.md written, a requirement spec with no trace gaps."""
    run = resolved_run(proj)
    set_state(run, lambda st: [n.update(status="done") for n in st["nodes"].values()])
    write(run, "artifacts/RUN-REPORT.md", "# report")
    write(run, "artifacts/requirement-spec.md", "REQ-001 do the thing")
    return run


def test_finish_done_refused_without_run_report(proj):
    run = finish_ready_run(proj)
    os.remove(os.path.join(run, "artifacts", "RUN-REPORT.md"))
    d, rc, _ = tx(proj, "finish", "--run", run, "--status", "DONE")
    assert refused(d, rc), "DONE without RUN-REPORT.md must be refused"


def test_finish_done_refused_with_trace_gap(proj):
    run = finish_ready_run(proj)
    write(run, "artifacts/test-cases.json", json.dumps({"cases": [{"id": "TC-1", "reqs": []}]}))  # case with no REQ
    d, rc, _ = tx(proj, "finish", "--run", run, "--status", "DONE")
    assert refused(d, rc), "DONE with traceability gaps must be refused"


def test_finish_done_ok_when_complete(proj):
    run = finish_ready_run(proj)
    d, rc, _ = tx(proj, "finish", "--run", run, "--status", "DONE")
    assert rc == 0 and d and d["ok"] and get_state(run)["status"] == "DONE"


@pytest.mark.parametrize("status", ["FAILED", "CANCELLED"])
def test_finish_non_done_requires_a_reason(proj, status):
    run = resolved_run(proj)
    d, rc, _ = tx(proj, "finish", "--run", run, "--status", status)
    assert refused(d, rc), f"{status} without a reason must be refused"
    assert get_state(run)["status"] != status


# ================================================================ F04: result contract
def test_result_requires_a_started_node(proj):
    run = resolved_run(proj)
    write_result(run, "discover")
    d, rc, _ = tx(proj, "result", "--run", run, "--node", "discover")  # never started
    assert refused(d, rc), "a result for a node that never started must be refused"
    assert get_state(run)["nodes"]["discover"]["status"] == "pending"


def test_result_twice_does_not_double_count_tokens(proj):
    run = resolved_run(proj)
    t(proj, "start", "--run", run, "--node", "discover")
    write_result(run, "discover")
    d1, rc1, _ = tx(proj, "result", "--run", run, "--node", "discover", "--tokens", "100")
    d2, rc2, _ = tx(proj, "result", "--run", run, "--node", "discover", "--tokens", "100")
    st = get_state(run)
    assert rc1 == 0 and rc2 == 0 and d2.get("idempotent") is True
    assert st["tokens_total"] == 100, f"tokens double-counted: {st['tokens_total']}"
    assert st["nodes"]["discover"]["tokens"] == 100


def test_conflicting_second_result_is_refused(proj):
    run = resolved_run(proj)
    t(proj, "start", "--run", run, "--node", "discover")
    write_result(run, "discover")
    assert tx(proj, "result", "--run", run, "--node", "discover")[1] == 0
    write_result(run, "discover", summary="a different story")
    d, rc, _ = tx(proj, "result", "--run", run, "--node", "discover")
    assert refused(d, rc), "a different result for an already-finished node must be refused"


def test_result_rejects_artifact_path_traversal(proj):
    run = resolved_run(proj)
    t(proj, "start", "--run", run, "--node", "discover")
    write(run, "outside.md", "not an artifact")  # run/outside.md, one level above artifacts/
    write_result(run, "discover", artifacts=(), declare=["../outside.md"])
    d, rc, _ = tx(proj, "result", "--run", run, "--node", "discover")
    assert rc != 0 and d is not None and any("artifact" in p for p in d["problems"])
    assert get_state(run)["nodes"]["discover"]["status"] != "done"


def test_result_rejects_absolute_artifact_path(proj, tmp_path):
    run = resolved_run(proj)
    t(proj, "start", "--run", run, "--node", "discover")
    outside = tmp_path / "elsewhere.md"
    outside.write_text("x")
    write_result(run, "discover", artifacts=(), declare=[str(outside)])
    d, rc, _ = tx(proj, "result", "--run", run, "--node", "discover")
    assert rc != 0 and d is not None and any("artifact" in p for p in d["problems"])


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_result_rejects_symlink_escape(proj, tmp_path):
    run = resolved_run(proj)
    t(proj, "start", "--run", run, "--node", "discover")
    outside = tmp_path / "secret.txt"
    outside.write_text("x")
    os.symlink(outside, os.path.join(run, "artifacts", "link.md"))
    write_result(run, "discover", artifacts=(), declare=["link.md"])
    d, rc, _ = tx(proj, "result", "--run", run, "--node", "discover")
    assert rc != 0 and d is not None and any("artifact" in p for p in d["problems"])


# ================================================================ run lock + atomic state (P0-0 / P0-1)
def run_env(**kw):
    return {**os.environ, **kw}


def status_cmd(proj, run, **env):
    p = subprocess.run([sys.executable, TOOL, "status", "--run", run, "--root", str(proj)],
                       capture_output=True, text=True, env=run_env(**env))
    return (json.loads(p.stdout) if p.stdout.strip() else None), p.returncode


_EV = iter(range(10**6))


def tokens_cmd(proj, run, add, event=None, **env):
    p = subprocess.run([sys.executable, TOOL, "tokens", "--run", run, "--node", "discover", "--add", str(add),
                        "--event", event or f"ev-{next(_EV)}", "--root", str(proj)], capture_output=True, text=True, env=run_env(**env))
    return (json.loads(p.stdout) if p.stdout.strip() else None), p.returncode


def dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_lock_released_after_command(proj):
    run = resolved_run(proj)
    assert tokens_cmd(proj, run, 1)[1] == 0
    assert not os.path.exists(os.path.join(run, ".lock"))


def test_lock_with_dead_pid_is_broken(proj):
    run = resolved_run(proj)
    import time
    with open(os.path.join(run, ".lock"), "w") as fh:
        fh.write(f"{dead_pid()} {time.time()}")
    d, rc = tokens_cmd(proj, run, 5, GHE_LOCK_TIMEOUT="5")
    assert rc == 0 and d["ok"] and get_state(run)["tokens_total"] == 5


def test_lock_older_than_stale_limit_is_broken(proj):
    run = resolved_run(proj)
    import time
    with open(os.path.join(run, ".lock"), "w") as fh:
        fh.write(f"{os.getpid()} {time.time() - 1000}")  # live pid, but ancient
    d, rc = tokens_cmd(proj, run, 5, GHE_LOCK_TIMEOUT="5", GHE_LOCK_STALE="60")
    assert rc == 0 and d["ok"]


def test_lock_held_by_live_owner_times_out_cleanly(proj):
    run = resolved_run(proj)
    import time
    with open(os.path.join(run, ".lock"), "w") as fh:
        fh.write(f"{os.getpid()} {time.time()}")  # this very test process: alive and fresh
    before = raw_state(run)
    d, rc = tokens_cmd(proj, run, 5, GHE_LOCK_TIMEOUT="0.3")
    assert refused(d, rc) and "locked" in d["error"]
    assert raw_state(run) == before
    assert os.path.exists(os.path.join(run, ".lock")), "must not delete someone else's live lock"


def test_concurrent_commands_serialise_without_lost_updates(proj):
    run = resolved_run(proj)
    procs = [subprocess.Popen([sys.executable, TOOL, "tokens", "--run", run, "--node", "discover", "--add", "1",
                               "--event", f"c{i}", "--root", str(proj)], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
             for i in range(12)]
    for p in procs:
        p.communicate()
        assert p.returncode == 0
    assert get_state(run)["tokens_total"] == 12, "a concurrent update was lost"


def test_state_write_is_atomic_and_leaves_no_temp_files(proj):
    run = resolved_run(proj)
    for i in range(5):
        tokens_cmd(proj, run, 1)
        json.loads(raw_state(run))  # always parseable
    assert [f for f in os.listdir(run) if f.startswith(".tmp-")] == []


def test_tokens_rejects_negative_or_garbage(proj):
    run = resolved_run(proj)
    for bad in ("-5", "abc"):
        d, rc = tokens_cmd(proj, run, bad)
        assert refused(d, rc)
    assert get_state(run)["tokens_total"] == 0


# ================================================================ test loop re-opens nodes (P0-2 wiring)
def test_failing_round_reopens_builder_and_test_for_the_next_round(proj):
    run = resolved_run(proj)
    set_state(run, lambda st: [n.update(status="done", attempts=1) for n in st["nodes"].values()])
    write(run, "artifacts/defects/round-1.json", json.dumps({"round": 1, "defects": [
        {"id": "D1", "case": "TC-1", "error": "boom"}]}))
    d, rc = t(proj, "defects", "--run", run, "--round", "1")
    assert d["verdict"] == "first"
    nodes = get_state(run)["nodes"]
    for k in ("builder", "deploy", "test"):
        assert nodes[k]["status"] == "pending" and nodes[k]["attempts"] == 0, k
    assert nodes["ba"]["status"] == "done", "unrelated nodes must stay done"
    d, rc = tx(proj, "start", "--run", run, "--node", "builder")[:2]
    assert rc == 0, d


# ---- T07: graph-declared `produces` must be listed
def test_t07_done_without_graph_produces_is_refused(proj):
    run = resolved_run(proj)
    t(proj, "start", "--run", run, "--node", "discover")
    write_result(run, "discover", artifacts=("x.md",))
    d, rc = t(proj, "result", "--run", run, "--node", "discover")
    assert rc != 0 and any("source.md" in p for p in d["problems"])


# ---- F20: source.md is immutable; deploy target lives in a separate artifact
def test_f20_no_instruction_to_write_deploy_config_into_source():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for rel in (".claude/skills/ghe-run/SKILL.md", ".claude/skills/deploy/SKILL.md", ".claude/agents/devops.md"):
        txt = open(os.path.join(root, rel)).read()
        assert "region/account in `source.md`" not in txt
        assert "from `source.md` (`aws_region`" not in txt
    assert "deploy-target.md" in open(os.path.join(root, ".claude/skills/ghe-run/SKILL.md")).read()


# ---- F24: the runner is the authority for controlled actions
def test_f24_authorize_requires_approved_gate(proj):
    run = resolved_run(proj)
    d, rc = t(proj, "authorize", "--run", run, "--action", "cloud-apply")
    assert rc != 0 and d["authorized"] is False
    t(proj, "gate", "--run", run, "--name", "cloud-apply", "--request", "plan")
    assert t(proj, "authorize", "--run", run, "--action", "cloud-apply")[1] != 0   # still waiting
    t(proj, "gate-decide", "--run", run, "--name", "cloud-apply", "--decision", "approve", "--by", "alice")
    d, rc = t(proj, "authorize", "--run", run, "--action", "cloud-apply")
    assert rc == 0 and d["authorized"] is True and d["approved_by"] == "alice"
    assert t(proj, "authorize", "--run", run, "--action", "destructive")[1] != 0   # different gate


# ---- F10 / F11: distinct retry meanings, one correction by default
def test_f11_default_policy_is_one_correction():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    b = yaml.safe_load(open(os.path.join(root, "ghe", "config.yaml")))["budgets"]
    assert b["max_story_revisions"] == 1 and b["max_gate_revisions"] == 1
    assert b["node_retry"] == 1 and b["max_rounds"] == 3   # technical retry vs verification rounds stay separate


def test_f10_second_story_revision_escalates_by_default(proj):
    run = resolved_run(proj)
    t(proj, "gate", "--run", run, "--name", "review-gate", "--request", "x")
    d1, _ = t(proj, "gate-decide", "--run", run, "--name", "review-gate", "--decision", "request_changes", "--by", "a")
    d2, _ = t(proj, "gate-decide", "--run", run, "--name", "review-gate", "--decision", "request_changes", "--by", "a")
    assert d1["gate_status"] == "changes_requested" and d2["gate_status"] == "escalated"


def test_f10_bad_budget_values_are_reported(proj):
    cfg = os.path.join(proj, "ghe", "config.yaml")
    c = yaml.safe_load(open(cfg)); c["budgets"]["max_rounds"] = 0; c["budgets"]["node_retry"] = "x"
    yaml.safe_dump(c, open(cfg, "w"))
    run = new_run(proj)
    d, rc = t(proj, "resolve", "--run", run)
    assert rc != 0 and "max_rounds" in json.dumps(d) and "node_retry" in json.dumps(d)


# ---- F21: repo-qualified ownership and actual-diff checks
def _stories(run, stories):
    write(run, "artifacts/plans/stories.json", json.dumps({"stories": stories}))


def test_f21_same_glob_in_different_repos_does_not_serialise(proj):
    run = new_run(proj)
    _stories(run, [{"id": "S-01", "repo": "api", "depends": [], "owns": ["src/**"]},
                   {"id": "S-02", "repo": "web", "depends": [], "owns": ["src/**"]}])
    d, rc = t(proj, "waves", "--run", run)
    assert rc == 0 and d["waves"] == [["S-01", "S-02"]]


def test_f21_same_glob_in_same_repo_still_serialises(proj):
    run = new_run(proj)
    _stories(run, [{"id": "S-01", "repo": "api", "depends": [], "owns": ["src/**"]},
                   {"id": "S-02", "repo": "api", "depends": [], "owns": ["src/**"]}])
    d, rc = t(proj, "waves", "--run", run)
    assert rc == 0 and len(d["waves"]) == 2


def test_f21_own_check_flags_files_outside_owns(proj):
    run = new_run(proj)
    _stories(run, [{"id": "S-01", "repo": "api", "depends": [], "owns": ["src/**"]}])
    d, rc = t(proj, "own-check", "--run", run, "--id", "S-01", "--repo", "api", "--files", "src/a.py", "src/x/b.py")
    assert rc == 0 and d["outside_owns"] == []
    d, rc = t(proj, "own-check", "--run", run, "--id", "S-01", "--repo", "api", "--files", "src/a.py", "docs/x.md", "../evil")
    assert rc == 1 and d["outside_owns"] == ["../evil", "docs/x.md"]


def test_f21_own_check_rejects_wrong_repo(proj):
    run = new_run(proj)
    _stories(run, [{"id": "S-01", "repo": "api", "depends": [], "owns": ["src/**"]}])
    d, rc = t(proj, "own-check", "--run", run, "--id", "S-01", "--repo", "web", "--files", "src/a.py")
    assert rc == 1 and "belongs to repo" in d["error"]


# ---- F19: design edits are proposal -> approved revision -> selective invalidation
def test_f19_proposal_needs_impact_and_files(proj):
    run = resolved_run(proj)
    assert t(proj, "design-propose", "--run", run, "--id", "DR-1", "--summary", "x")[1] != 0
    assert t(proj, "design-propose", "--run", run, "--id", "DR-1", "--summary", "x", "--files", "hld.md")[1] != 0
    assert t(proj, "design-propose", "--run", run, "--id", "../x", "--summary", "x", "--files", "hld.md", "--stories", "none")[1] != 0


def test_f19_approval_invalidates_only_named_stories(proj):
    run = resolved_run(proj)
    for sid in ("S-01", "S-02"):
        t(proj, "story", "--run", run, "--id", sid, "--status", "done")
    assert t(proj, "design-propose", "--run", run, "--id", "DR-1", "--summary", "widen API", "--files", "CONTRACT.md",
             "--stories", "S-01", "--tests", "TC-3",
             "--reason", "S-01=consumes the API", "--reason", "TC-3=asserts old shape")[1] == 0
    assert t(proj, "design-decide", "--run", run, "--id", "DR-1", "--decision", "approve")[1] != 0   # no --by
    d, rc = t(proj, "design-decide", "--run", run, "--id", "DR-1", "--decision", "approve", "--by", "alice")
    assert rc == 0 and d["invalidated_stories"] == ["S-01"] and d["invalidated_tests"] == ["TC-3"]
    s = json.load(open(os.path.join(run, "state.json")))["stories"]
    assert s["S-01"]["status"] == "pending" and s["S-02"]["status"] == "done"
    assert t(proj, "design-decide", "--run", run, "--id", "DR-1", "--decision", "reject", "--by", "bob")[1] != 0


def test_f19_rejected_proposal_changes_nothing_and_batch_limit_applies(proj):
    run = resolved_run(proj)
    t(proj, "story", "--run", run, "--id", "S-01", "--status", "done")
    t(proj, "design-propose", "--run", run, "--id", "DR-1", "--summary", "x", "--files", "hld.md", "--stories", "S-01",
      "--reason", "S-01=depends on it")
    d, rc = t(proj, "design-decide", "--run", run, "--id", "DR-1", "--decision", "reject", "--by", "al")
    assert rc == 0 and d["invalidated_stories"] == []
    assert json.load(open(os.path.join(run, "state.json")))["stories"]["S-01"]["status"] == "done"
    t(proj, "design-propose", "--run", run, "--id", "DR-2", "--summary", "y", "--files", "hld.md", "--stories", "none")
    d, rc = t(proj, "design-propose", "--run", run, "--id", "DR-3", "--summary", "z", "--files", "hld.md", "--stories", "none")
    assert rc != 0 and "batch limit" in d["error"]


# ---- F17 / F18: ticket hierarchy and honest attachment capability
def _tk(proj, run, kind, node, key=None, parent=""):
    args = ["ticket", "--run", run, "--kind", kind, "--node", node]
    _tk.n = getattr(_tk, "n", 0) + 1
    args += ["--key", key or f"GHE-{_tk.n}"]   # fixture tracker is jira: keys must be GHE-n
    if parent:
        args += ["--parent", parent]
    d, rc, _ = tx(proj, *args)
    return d, rc


def test_f17_hierarchy_is_enforced_for_local_tracker(proj):
    run = resolved_run(proj)
    d, rc = _tk(proj, run, "epic", "run"); epic = d["ticket"]["key"]
    assert rc == 0
    assert _tk(proj, run, "story", "S-01")[1] != 0                      # story without parent
    assert _tk(proj, run, "subtask", "S-01-dev", parent=epic)[1] != 0   # subtask directly under epic
    d, rc = _tk(proj, run, "story", "S-01", parent=epic); story = d["ticket"]["key"]
    assert rc == 0 and d["ticket"]["parent"] == epic
    assert _tk(proj, run, "qa-subtask", "S-01-qa", parent=story)[1] == 0
    assert _tk(proj, run, "dev-subtask", "S-01-dev", parent=story)[1] == 0
    assert _tk(proj, run, "epic", "x", parent=epic)[1] != 0             # epic has no parent
    assert _tk(proj, run, "story", "S-01", parent=epic, key=story)[0]["existing"] is True   # idempotent


def test_f17_unknown_parent_and_kind_rejected(proj):
    run = resolved_run(proj)
    assert _tk(proj, run, "story", "S-9", parent="LOCAL-99")[1] != 0
    assert _tk(proj, run, "bogus", "n")[1] != 0


def test_f18_skill_does_not_assume_an_unlisted_attachment_tool():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    txt = open(os.path.join(root, ".claude", "skills", "jira-sync", "SKILL.md")).read()
    allowed = txt.split("allowed-tools:")[1].split("\n")[0]
    assert "uploadAttachmentToJiraIssue" not in txt or "uploadAttachmentToJiraIssue" in allowed
    assert "fallback-comment" in txt and "CAPABILITY" in txt


# ---- F23: maintain / no-Tester runs need real verification or a recorded waiver
def _no_tester_ready(proj):
    run = new_run(proj, roster="[ba, architect, qa, builder, devops]", mode="maintain")
    t(proj, "resolve", "--run", run)
    set_state(run, lambda st: [n.update(status="done") for n in st["nodes"].values()])
    write(run, "artifacts/RUN-REPORT.md", "# report")
    write(run, "artifacts/requirement-spec.md", "REQ-001 x")
    return run


def _set_validation(proj, cmds):
    cfg = os.path.join(proj, "ghe", "config.yaml")
    c = yaml.safe_load(open(cfg)); c["validation"] = {"commands": cmds}
    yaml.safe_dump(c, open(cfg, "w"))


def test_f23_no_tester_done_refused_until_verified(proj):
    run = _no_tester_ready(proj)
    assert "test" not in json.load(open(os.path.join(run, "state.json")))["nodes"]
    d, rc, _ = tx(proj, "finish", "--run", run, "--status", "DONE")
    assert refused(d, rc) and any("verification" in p for p in d["problems"])


def test_f23_empty_validation_commands_is_blocked_not_passed(proj):
    run = _no_tester_ready(proj)
    d, rc, _ = tx(proj, "verify", "--run", run)
    assert rc != 0 and d["verification"]["status"] == "blocked"
    assert refused(*tx(proj, "finish", "--run", run, "--status", "DONE")[:2])


def test_f23_runner_executed_verification_passes_and_allows_done(proj):
    run = _no_tester_ready(proj)
    _set_validation(proj, [f"{sys.executable} -c 'import sys; sys.exit(0)'"])
    d, rc, _ = tx(proj, "verify", "--run", run)
    assert rc == 0 and d["verification"]["status"] == "passed"
    d, rc, _ = tx(proj, "finish", "--run", run, "--status", "DONE")
    assert rc == 0 and d["status"] == "DONE"


def test_f23_failing_verification_keeps_done_refused(proj):
    run = _no_tester_ready(proj)
    _set_validation(proj, [f"{sys.executable} -c 'import sys; sys.exit(3)'"])
    d, rc, _ = tx(proj, "verify", "--run", run)
    assert rc != 0 and d["verification"]["status"] == "failed" and d["verification"]["results"][0]["exit"] == 3
    assert refused(*tx(proj, "finish", "--run", run, "--status", "DONE")[:2])


def test_f23_waiver_needs_reason_and_identity_then_allows_done(proj):
    run = _no_tester_ready(proj)
    assert tx(proj, "verify", "--run", run, "--waive", "--reason", "docs only")[1] != 0
    assert tx(proj, "verify", "--run", run, "--waive", "--by", "alice")[1] != 0
    assert tx(proj, "verify", "--run", run, "--waive", "--reason", "docs only", "--by", "alice")[1] == 0
    assert tx(proj, "finish", "--run", run, "--status", "DONE")[1] == 0


# ---- F22: bounded, relevant, supersession-aware memory manifest
def _memory(proj, decisions, cfg=None):
    d = os.path.join(proj, "ghe", "memory"); os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "decisions.md"), "w").write(decisions)
    if cfg:
        p = os.path.join(proj, "ghe", "config.yaml")
        c = yaml.safe_load(open(p)); c["memory"] = cfg; yaml.safe_dump(c, open(p, "w"))


def _manifest(proj, run, q=""):
    d, rc, _ = tx(proj, "memory-manifest", "--run", run, "--query", q)
    m = None
    if rc == 0:
        m = json.load(open(os.path.join(run, "artifacts", "memory-manifest.json")))
    return d, rc, m


def test_f22_selects_relevant_entries_and_excludes_the_rest(proj):
    _memory(proj, "## Use postgres\nWe chose postgres for orders.\n\n## Brand colours\nUse teal.\n")
    d, rc, m = _manifest(proj, resolved_run(proj), "orders database postgres")
    assert rc == 0 and [e["title"] for e in m["included"]] == ["Use postgres"]
    assert {"title": "Brand colours", "reason": "not relevant to the request"} in m["excluded"]


def test_f22_superseded_entries_are_excluded_but_history_kept(proj):
    _memory(proj, "## Use mysql\nmysql for orders.\n\n## Use postgres\nSupersedes: Use mysql\npostgres for orders.\n")
    d, rc, m = _manifest(proj, resolved_run(proj), "orders")
    assert [e["title"] for e in m["included"]] == ["Use postgres"]
    assert {"title": "Use mysql", "reason": "superseded"} in m["excluded"]
    assert "Use mysql" in open(os.path.join(proj, "ghe", "memory", "decisions.md")).read()


def test_f22_entry_budget_is_enforced_newest_first(proj):
    _memory(proj, "".join(f"## D{i}\norders note {i}\n\n" for i in range(5)), {"max_entries": 2, "max_chars": 6000})
    d, rc, m = _manifest(proj, resolved_run(proj), "orders")
    assert [e["title"] for e in m["included"]] == ["D4", "D3"]
    assert sum(1 for x in m["excluded"] if x["reason"] == "entry budget") == 3


def test_f22_oversized_essential_entry_refuses_no_silent_truncation(proj):
    _memory(proj, "## Big\norders " + "x" * 500 + "\n", {"max_entries": 5, "max_chars": 100})
    d, rc, m = _manifest(proj, resolved_run(proj), "orders")
    assert rc != 0 and "no silent truncation" in d["error"] and m is None


def test_f22_invalid_memory_config_is_rejected(proj):
    run = resolved_run(proj)
    _memory(proj, "## A\nx\n", {"max_entries": 0, "max_chars": "lots"})
    d, rc, _ = tx(proj, "memory-manifest", "--run", run)
    assert rc != 0 and any("memory.max_entries" in p for p in d["problems"])
    assert tx(proj, "resolve", "--run", run)[1] != 0    # and resolve refuses the same config


# ---- F14 / RT-03: allowances and ticket mappings are durable across runs
def _second_run(proj, run_id="r2"):
    r = new_run(proj, roster="[ba, architect, builder, devops, tester]", run_id=run_id) if False else None
    d, rc, _ = tx(proj, "init", "--root", proj, "--run-id", run_id)
    return d["run_dir"]


def _story(proj, run, sid, status=None):
    args = ["story", "--run", run, "--id", sid] + (["--status", status] if status else [])
    return tx(proj, *args)


def test_f14_story_revisions_persist_into_a_new_run(proj):
    r1 = resolved_run(proj)
    assert _story(proj, r1, "S-1", "revise")[0]["revisions"] == 1
    r2 = _second_run(proj)
    d, rc, _ = _story(proj, r2, "S-1", "revise")
    assert d["revisions"] == 2 and rc != 0 and d["status"] == "blocked"   # default cap 1: no replenishment


def test_f14_unrelated_story_is_unaffected(proj):
    r1 = resolved_run(proj)
    _story(proj, r1, "S-1", "revise")
    r2 = _second_run(proj)
    d, rc, _ = _story(proj, r2, "S-2", "revise")
    assert rc == 0 and d["revisions"] == 1


def test_f14_grant_requires_exhausted_scope_reason_and_identity(proj):
    r1 = resolved_run(proj)
    _story(proj, r1, "S-1", "revise")
    g = lambda *x: tx(proj, "allowance-grant", "--run", r1, "--story", "S-1", *x)
    assert g("--add", "1", "--reason", "r", "--by", "alice")[1] != 0          # not exhausted yet
    _story(proj, r1, "S-1", "revise")                                          # now blocked
    assert g("--add", "1", "--by", "alice")[1] != 0
    assert g("--add", "1", "--reason", "r")[1] != 0
    assert g("--add", "0", "--reason", "r", "--by", "alice")[1] != 0
    assert tx(proj, "allowance-grant", "--run", r1, "--story", "NOPE", "--add", "1", "--reason", "r", "--by", "a")[1] != 0
    d, rc, _ = g("--add", "1", "--reason", "customer approved", "--by", "alice")
    assert rc == 0 and d["max_revisions"] == 2
    led = json.load(open(os.path.join(proj, "ghe", "ledger.json")))
    assert led["grants"][0]["by"] == "alice" and led["grants"][0]["story"] == "S-1"


def test_f14_ticket_ref_maps_to_one_ticket_across_runs(proj):
    r1 = resolved_run(proj)
    d1, rc, _ = tx(proj, "ticket", "--run", r1, "--kind", "epic", "--node", "ba", "--ref", "EPIC-A", "--title", "e", "--key", "GHE-90")
    r2 = _second_run(proj)
    d2, rc, _ = tx(proj, "ticket", "--run", r2, "--kind", "epic", "--node", "ba", "--ref", "EPIC-A", "--title", "e", "--key", "GHE-91")
    assert d2["existing"] is True and d2.get("cross_run") and d2["ticket"]["key"] == d1["ticket"]["key"]


# ---- F03 / T06: graph-declared required criteria cannot be omitted or invented around
def _criteria_run(proj, crit):
    run = new_run(proj)
    gp = os.path.join(proj, "ghe", "graph.yaml")
    g = yaml.safe_load(open(gp)); g["nodes"]["discover"]["criteria"] = crit
    yaml.safe_dump(g, open(gp, "w"), sort_keys=False)
    assert t(proj, "resolve", "--run", run)[1] == 0
    t(proj, "start", "--run", run, "--node", "discover")
    return run


def test_t06_done_missing_required_criteria_is_refused(proj):
    run = _criteria_run(proj, ["source_recorded", "verbatim"])
    write_result(run, "discover", exit_criteria={"source_recorded": True})
    d, rc = t(proj, "result", "--run", run, "--node", "discover")
    assert rc != 0 and any("verbatim" in p for p in d["problems"])


def test_t06_all_required_criteria_met_is_accepted(proj):
    run = _criteria_run(proj, ["a"])
    write_result(run, "discover", exit_criteria={"a": True})
    assert t(proj, "result", "--run", run, "--node", "discover")[1] == 0


# ---- F12 / T16: stale-SHA / stale-plan evidence is rejected
def _defects_with_plan(proj, evidence):
    run = resolved_run(proj)
    write(run, "artifacts/plans/stories.json", json.dumps({"stories": []}))
    write(run, "artifacts/defects/round-1.json", json.dumps(evidence))
    return run, t(proj, "defects", "--run", run, "--round", "1")


def test_t16_stale_plan_sha_is_rejected(proj):
    run, (d, rc) = _defects_with_plan(proj, {"round": 1, "defects": [], "plan_sha": "0" * 64})
    assert rc != 0 and d["verdict"] == "incomplete" and "stale" in d["error"]


def test_t16_stale_source_hash_is_rejected(proj):
    run, (d, rc) = _defects_with_plan(proj, {"round": 1, "defects": [], "source_hash": "deadbeef"})
    assert rc != 0 and "stale" in d["error"]


def test_t16_pass_without_plan_binding_is_rejected_when_a_plan_exists(proj):
    run, (d, rc) = _defects_with_plan(proj, {"round": 1, "defects": []})
    assert rc != 0 and d["verdict"] == "incomplete"


def test_t16_matching_plan_sha_passes(proj):
    run = resolved_run(proj)
    body = json.dumps({"stories": []})
    write(run, "artifacts/plans/stories.json", body)
    write(run, "artifacts/defects/round-1.json",
          json.dumps({"round": 1, "defects": [], "plan_sha": hashlib.sha256(body.encode()).hexdigest()}))
    d, rc = t(proj, "defects", "--run", run, "--round", "1")
    assert rc == 0 and d["verdict"] == "pass"


# ---- F09 / T13: concurrent starts cannot over-reserve the run ceiling


# ---- F09 / T62: delegated work with no token cap is blocked (unmetered usage cannot be reserved)




# ---- F13 / T37, T54, T60: side-effect journal and reconciliation
def _j(proj, run, sub, *args):
    return t(proj, sub, "--run", run, *args)


def test_t37_lost_response_blocks_blind_retry_until_reconciled(proj):
    run = resolved_run(proj)
    assert _j(proj, run, "journal-intent", "--key", "jira:create:S-1", "--action", "create ticket")[1] == 0
    # crash / lost response: no confirm. A blind retry must be refused.
    d, rc = _j(proj, run, "journal-intent", "--key", "jira:create:S-1", "--action", "create ticket")
    assert rc != 0 and d["action"] == "reconcile"
    assert _j(proj, run, "journal-reconcile", "--key", "jira:create:S-1", "--outcome", "applied")[1] != 0  # needs evidence
    d, rc = _j(proj, run, "journal-reconcile", "--key", "jira:create:S-1", "--outcome", "applied",
               "--evidence", "found GHE-7 in Jira")
    assert rc == 0 and d["status"] == "applied"
    d, rc = _j(proj, run, "journal-intent", "--key", "jira:create:S-1", "--action", "create ticket")
    assert rc == 0 and d["already_applied"] is True          # no duplicate


def test_t54_not_applied_outcome_allows_a_fresh_attempt(proj):
    run = resolved_run(proj)
    _j(proj, run, "journal-intent", "--key", "deploy:1", "--action", "apply")
    assert _j(proj, run, "journal-reconcile", "--key", "deploy:1", "--outcome", "not_applied", "--evidence", "no stack")[1] == 0
    d, rc = _j(proj, run, "journal-intent", "--key", "deploy:1", "--action", "apply")
    assert rc == 0 and d["status"] == "intent"


def test_t60_confirmed_effect_is_idempotent_and_confirm_needs_intent(proj):
    run = resolved_run(proj)
    assert _j(proj, run, "journal-confirm", "--key", "nope")[1] != 0
    _j(proj, run, "journal-intent", "--key", "k", "--action", "a")
    assert _j(proj, run, "journal-confirm", "--key", "k", "--evidence", "ok")[1] == 0
    assert _j(proj, run, "journal-intent", "--key", "k", "--action", "a")[0]["already_applied"] is True


def test_f13_finish_done_refused_while_an_intent_is_unresolved(proj):
    run = finish_ready_run(proj) if "finish_ready_run" in globals() else None
    if run is None:
        return
    _j(proj, run, "journal-intent", "--key", "k", "--action", "a")
    d, rc = _j(proj, run, "finish", "--status", "DONE")
    assert rc != 0 and any("unresolved" in p for p in d["problems"])


# ---- F12: required-case coverage is computed against test-cases.json
def _cases_run(proj, results):
    run = resolved_run(proj)
    write(run, "artifacts/test-cases.json", json.dumps({"cases": [
        {"id": "TC-001", "reqs": ["REQ-001"], "title": "a", "expected": "x"},
        {"id": "TC-002", "reqs": ["REQ-001"], "title": "b", "expected": "y"}]}))
    write(run, "artifacts/test-results.json", json.dumps({"results": results}))
    write(run, "artifacts/defects/round-1.json", json.dumps({"round": 1, "defects": []}))
    return run, t(proj, "defects", "--run", run, "--round", "1")


def test_f12_uncovered_designed_case_makes_round_incomplete(proj):
    run, (d, rc) = _cases_run(proj, [{"case": "TC-001", "status": "pass"}])
    assert rc != 0 and d["verdict"] == "incomplete" and "TC-002" in d["error"]


def test_f12_all_designed_cases_covered_passes(proj):
    run, (d, rc) = _cases_run(proj, [{"case": "TC-001", "status": "pass"}, {"case": "TC-002", "status": "pass"}])
    assert rc == 0 and d["verdict"] == "pass"


# ---- F13: every runner-written artifact goes through a temp file + replace
def test_f13_runner_has_no_plain_truncating_writes_to_artifacts():
    src = open(TOOL).read()
    import re as _re
    plain = [m.group(0) for m in _re.finditer(r'open\([^\n]*"w"[^\n]*\)', src)
             if not m.group(0).startswith("open(fd,")]
    assert plain == [], plain


def test_f13_atomic_text_write_leaves_no_temp_files(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("ghe_tool_mod", TOOL)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    p = tmp_path / "sub" / "a.md"
    m.atomic_write_text(str(p), "hello\n")
    assert p.read_text() == "hello\n" and [x.name for x in p.parent.iterdir()] == ["a.md"]


# ---- F14 / T20, T35: allowances are scoped to the ORIGINAL story and survive new runs/FIX stories
def test_t20_new_run_or_fix_story_cannot_reset_exhausted_scope(proj):
    r1 = resolved_run(proj)
    _story(proj, r1, "S-1", "revise")
    d, rc, _ = _story(proj, r1, "S-1", "revise")
    assert d["status"] == "blocked"
    r2 = _second_run(proj)
    _story(proj, r2, "FIX-1-01", "revise")                    # an unrelated FIX id gets its own scope...
    d, rc, _ = _story(proj, r2, "S-1", "revise")              # ...but the original stays exhausted
    assert rc != 0 and d["status"] == "blocked" and d["revisions"] >= 3
    r3 = _second_run(proj, "r3")
    d, rc, _ = _story(proj, r3, "S-1", "revise")
    assert rc != 0 and d["status"] == "blocked"


def test_t35_ledger_survives_run_directory_removal(proj):
    r1 = resolved_run(proj)
    _story(proj, r1, "S-1", "revise")
    shutil.rmtree(r1)
    r2 = _second_run(proj)
    assert _story(proj, r2, "S-1", "revise")[0]["revisions"] == 2


# ---- F11 / T61: strict epic_global correction policy (default stays per-story)
def _set_cfg(proj, **budgets):
    cp = os.path.join(proj, "ghe", "config.yaml")
    cfg = yaml.safe_load(open(cp)); cfg["budgets"].update(budgets)
    yaml.safe_dump(cfg, open(cp, "w"), sort_keys=False)


def test_t61_default_scope_is_per_story(proj):
    run = resolved_run(proj)
    assert _story(proj, run, "S-1", "revise")[1] == 0
    assert _story(proj, run, "S-2", "revise")[1] == 0          # unrelated stories do not consume each other's allowance


def test_t61_epic_global_allows_one_episode_across_all_stories(proj):
    _set_cfg(proj, correction_scope="epic_global")
    run = resolved_run(proj)
    assert _story(proj, run, "S-1", "revise")[1] == 0
    d, rc, _ = _story(proj, run, "S-2", "revise")              # a DIFFERENT story is now blocked too
    assert rc != 0 and d["status"] == "blocked"
    assert json.load(open(os.path.join(run, "state.json")))["status"] == "WAITING_HUMAN"


def test_t61_epic_global_grant_raises_the_shared_cap(proj):
    _set_cfg(proj, correction_scope="epic_global")
    run = resolved_run(proj)
    _story(proj, run, "S-1", "revise"); _story(proj, run, "S-2", "revise")
    d, rc, _ = tx(proj, "allowance-grant", "--run", run, "--story", "S-2", "--add", "1", "--reason", "ok", "--by", "alice")
    assert rc == 0 and d["max_revisions"] == 2


def test_t61_invalid_scope_value_is_a_config_error(proj):
    _set_cfg(proj, correction_scope="whatever")
    run = new_run(proj)
    d, rc = t(proj, "resolve", "--run", run)
    assert rc != 0


# ---- F02 / T04: stale or unapproved work cannot start
def test_t04_start_refused_when_source_changed_after_discover(proj):
    run = resolved_run(proj)
    complete(proj, run, "discover")
    with open(os.path.join(run, "artifacts", "source.md"), "a") as fh:
        fh.write("tampered")
    d, rc = t(proj, "start", "--run", run, "--node", "ba")
    assert rc != 0 and "source.md changed" in d["error"]


def test_t04_start_refused_while_a_design_revision_awaits_approval(proj):
    run = resolved_run(proj)
    set_state(run, lambda st: st.setdefault("design_revisions", {}).update({"DR-1": {"status": "proposed"}}))
    d, rc = t(proj, "start", "--run", run, "--node", "discover")
    assert rc != 0 and "DR-1" in d["error"]


# ---- T21 / T31 / T34 / T23 / T56 / T58 (acceptance audit)
def _approve_design(proj, run, story="S-01"):
    t(proj, "story", "--run", run, "--id", story, "--status", "done")
    t(proj, "design-propose", "--run", run, "--id", "DR-1", "--summary", "x", "--files", "hld.md", "--stories", story,
      "--reason", f"{story}=depends on it")
    return t(proj, "design-decide", "--run", run, "--id", "DR-1", "--decision", "approve", "--by", "alice")


def test_t21_post_design_failure_escalates_immediately(proj):
    run = resolved_run(proj)
    _approve_design(proj, run)
    write(run, "artifacts/defects/round-1.json",
          json.dumps({"round": 1, "defects": [{"case": "TC-1", "error": "boom"}]}))
    d, rc = t(proj, "defects", "--run", run, "--round", "1")
    assert d["verdict"] == "post_design_fail" and d["escalate"] is True


def test_t21_without_a_design_change_first_failure_still_gets_a_normal_round(proj):
    run = resolved_run(proj)
    write(run, "artifacts/defects/round-1.json",
          json.dumps({"round": 1, "defects": [{"case": "TC-1", "error": "boom"}]}))
    d, rc = t(proj, "defects", "--run", run, "--round", "1")
    assert d["verdict"] == "first" and d["escalate"] is False


def test_t31_stale_completion_after_approved_design_change_is_fenced(proj):
    run = resolved_run(proj)
    d, rc = _approve_design(proj, run)
    assert d["invalidated_stories"] == ["S-01"]
    d, rc = t(proj, "story", "--run", run, "--id", "S-01", "--status", "done")      # completion from before the change
    assert rc != 0 and "DR-1" in d["error"]
    assert t(proj, "story", "--run", run, "--id", "S-01", "--status", "running")[1] == 0
    assert t(proj, "story", "--run", run, "--id", "S-01", "--status", "done")[1] == 0   # legitimate re-run completes


def test_t34_duplicate_story_id_rejects_the_plan(proj):
    run = resolved_run(proj)
    write(run, "artifacts/plans/stories.json", json.dumps({"stories": [
        {"id": "A", "depends": [], "owns": ["a/**"]}, {"id": "A", "depends": [], "owns": ["b/**"]}]}))
    d, rc = t(proj, "waves", "--run", run)
    assert rc == 1 and any("duplicate story id A" in e for e in d["errors"])


def test_t23_clarification_resume_does_not_touch_source_or_reset_corrections(proj):
    run = resolved_run(proj)
    _story(proj, run, "S-1", "revise")
    src = os.path.join(run, "artifacts", "source.md")
    before = open(src).read()
    set_state(run, lambda st: (st["nodes"]["ba"].update(status="waiting_human", open_questions=["q?"]),
                               st.update(status="WAITING_HUMAN")))
    d, rc, _ = tx(proj, "resolve-wait", "--run", run, "--node", "ba", "--answer", "yes", "--by", "alice", "--outcome", "rerun")
    assert rc == 0
    assert open(src).read() == before
    assert json.load(open(os.path.join(proj, "ghe", "ledger.json")))["stories"]["S-1"]["revisions"] == 1


def test_t56_legacy_state_without_new_keys_loads_and_keeps_history(proj):
    run = resolved_run(proj)
    sp = os.path.join(run, "state.json")
    st = json.load(open(sp))
    for k in ("usage_events", "design_revisions", "stories", "fingerprints", "post_design_check"):
        st.pop(k, None)
    st["tokens_total"] = 1234
    json.dump(st, open(sp, "w"))
    assert tx(proj, "status", "--run", run)[1] == 0
    assert tx(proj, "story", "--run", run, "--id", "S-9", "--status", "running")[1] == 0
    assert json.load(open(sp))["tokens_total"] == 1234


def test_t58_built_plugin_runs_from_a_path_containing_spaces(tmp_path):
    out = tmp_path / "my plug in" / "ghe plugin"
    r = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "ghe-build-plugin"), "--out", str(out)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    tool = out / "ghe" / "runner" / "ghe_tool.py"
    assert tool.is_file()
    p = subprocess.run([sys.executable, str(tool), "--help"], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr


def test_t55_escalation_report_renders_with_budget_exhausted_and_no_llm(proj):
    run = resolved_run(proj)
    set_state(run, lambda st: (st.update(tokens_total=50_000_000, status="WAITING_HUMAN"),
                               st["nodes"]["ba"].update(status="waiting_human", open_questions=["Which DB?"])))
    d, rc = t(proj, "escalation-report", "--run", run)
    assert rc == 0 and d["waiting"] == ["ba"]
    text = open(os.path.join(run, d["path"])).read()
    assert "Which DB?" in text and "WAITING_HUMAN" in text


# ---- T29 / T30: shared API design change - consumers proposed with reasons; reopen keeps history
def test_t29_affected_consumers_need_a_reason_each(proj):
    run = resolved_run(proj)
    base = ["design-propose", "--run", run, "--id", "DR-1", "--summary", "widen API", "--files", "CONTRACT.md"]
    assert t(proj, *base, "--stories", "S-01")[1] != 0                                   # no reason
    assert t(proj, *base, "--stories", "S-01", "--reason", "S-01=")[1] != 0              # empty reason
    assert t(proj, *base, "--stories", "S-01", "--reason", "S-01=uses it", "--reason", "S-09=x")[1] != 0  # stray id
    assert t(proj, *base, "--stories", "S-01", "--tests", "TC-3", "--reason", "S-01=uses it")[1] != 0     # TC-3 bare
    d, rc = t(proj, *base, "--stories", "S-01", "--tests", "TC-3", "--reason", "S-01=uses it",
              "--reason", "TC-3=asserts old shape")
    assert rc == 0
    rev = get_state(run)["design_revisions"]["DR-1"]
    assert rev["reasons"] == {"S-01": "uses it", "TC-3": "asserts old shape"}


def test_t30_reopen_keeps_history_and_leaves_unaffected_stories_done(proj):
    run = resolved_run(proj)
    for sid in ("S-01", "S-02"):
        t(proj, "story", "--run", run, "--id", sid, "--status", "done")
    t(proj, "design-propose", "--run", run, "--id", "DR-1", "--summary", "x", "--files", "hld.md",
      "--stories", "S-01", "--reason", "S-01=uses the changed API")
    d, rc = t(proj, "design-decide", "--run", run, "--id", "DR-1", "--decision", "approve", "--by", "alice")
    assert rc == 0 and d["invalidated_stories"] == ["S-01"]
    st = get_state(run)["stories"]
    assert st["S-01"]["status"] == "pending" and st["S-02"]["status"] == "done"
    h = st["S-01"]["history"]
    assert h[0]["previous_status"] == "done" and h[0]["by_revision"] == "DR-1"
    assert h[0]["reason"] == "uses the changed API" and "S-02" not in str(h)
    assert "history" not in st["S-02"]
