"""Stage 2 tests: F05 (T08), F06 (T10), F07/F09 (T11,T13), F08 (T12), F12 (T14, T15)."""
import json
import os

import yaml

from test_ghe_hardening import (complete, get_state, proj, refused, resolved_run, set_state,  # noqa: F401
                                t, tokens_cmd, tx, write, write_result)


def test_f05_result_rejects_traversal_in_artifact_list(proj):  # T08, already true after F04
    run = resolved_run(proj)
    t(proj, "start", "--run", run, "--node", "discover")
    write_result(run, "discover", artifacts=(), declare=["../../etc/passwd"])
    d, rc = t(proj, "result", "--run", run, "--node", "discover")
    assert rc != 0 and any("not allowed" in p for p in d["problems"])


def test_f06_duplicate_result_does_not_double_charge(proj):  # T10
    run = resolved_run(proj)
    t(proj, "start", "--run", run, "--node", "discover")
    write_result(run, "discover")
    t(proj, "result", "--run", run, "--node", "discover", "--tokens", "100")
    d, rc = t(proj, "result", "--run", run, "--node", "discover", "--tokens", "100")
    assert d.get("idempotent") is True
    assert get_state(run)["tokens_total"] == 100


def test_f07_usage_event_is_counted_once(proj):  # one authoritative path
    run = resolved_run(proj)
    tokens_cmd(proj, run, 50, event="u1")
    d, rc = tokens_cmd(proj, run, 50, event="u1")
    assert rc == 0 and d["duplicate"] is True
    assert get_state(run)["tokens_total"] == 50


def test_f07_conflicting_reuse_of_event_id_is_refused(proj):
    run = resolved_run(proj)
    tokens_cmd(proj, run, 50, event="u1")
    d, rc = tokens_cmd(proj, run, 60, event="u1")
    assert refused(d, rc) and get_state(run)["tokens_total"] == 50


def test_f07_usage_requires_event_id(proj):
    run = resolved_run(proj)
    d, rc, _ = tx(proj, "tokens", "--run", run, "--node", "discover", "--add", "5")
    assert refused(d, rc) and get_state(run)["tokens_total"] == 0


def test_f09_negative_and_malformed_usage_rejected(proj):  # T11
    run = resolved_run(proj)
    for bad in ("-5", "abc", "1.5"):
        d, rc = tokens_cmd(proj, run, bad)
        assert rc != 0, bad
    assert get_state(run)["tokens_total"] == 0


def test_f09_result_rejects_negative_tokens(proj):
    run = resolved_run(proj)
    t(proj, "start", "--run", run, "--node", "discover")
    write_result(run, "discover")
    d, rc, _ = tx(proj, "result", "--run", run, "--node", "discover", "--tokens", "-9")
    assert refused(d, rc) and get_state(run)["tokens_total"] == 0


def test_f08_role_cap_applies_to_node_with_that_agent(proj):  # T12: tester cap -> node `test`
    run = resolved_run(proj, roster=["ba", "architect", "qa", "builder", "devops", "tester"])
    cap = yaml.safe_load(open(os.path.join(str(proj), "ghe", "config.yaml")))["budgets"]["max_tokens_per_node"]["tester"]
    d, rc = t(proj, "budget", "--run", run, "--node", "test")
    assert d["node_cap"] == cap
    d, rc = t(proj, "budget", "--run", run, "--node", "deploy")
    assert d["node_cap"] == yaml.safe_load(open(os.path.join(str(proj), "ghe", "config.yaml")))["budgets"]["max_tokens_per_node"]["devops"]


def test_f08_start_enforces_role_cap(proj):
    run = resolved_run(proj, roster=["ba", "architect", "qa", "builder", "devops", "tester"])
    for n in ("discover", "ba", "architect"):
        pass
    set_state(run, lambda st: st["nodes"]["test"].update(tokens=10**6 - 1))
    set_state(run, lambda st: st["nodes"]["deploy"].update(status="done"))
    set_state(run, lambda st: st["nodes"]["qa"].update(status="done"))
    set_state(run, lambda st: st.update(tokens_total=0))
    d, rc, _ = tx(proj, "start", "--run", run, "--node", "test")
    assert refused(d, rc) and d.get("action") == "stop_node"


def test_f08_unknown_cap_key_is_a_config_error(proj):
    cfg = os.path.join(str(proj), "ghe", "config.yaml")
    c = yaml.safe_load(open(cfg))
    c["budgets"]["max_tokens_per_node"]["nonesuch"] = 1000
    yaml.safe_dump(c, open(cfg, "w"))
    run = resolved_run.__wrapped__(proj) if hasattr(resolved_run, "__wrapped__") else None
    from test_ghe_tool import new_run
    run = new_run(proj)
    d, rc = t(proj, "resolve", "--run", run)
    assert rc != 0 and "nonesuch" in json.dumps(d)


def test_f09_start_refuses_when_run_budget_reserved_by_running_nodes(proj):
    run = resolved_run(proj)
    set_state(run, lambda st: (st.update(tokens_total=999_000),
                               st["nodes"]["discover"].update(status="running")))
    set_state(run, lambda st: st["nodes"]["discover"].update(tokens=0))
    # discover has no cap -> reserves nothing; ba would start. Give builder a cap-heavy running state:
    set_state(run, lambda st: st["nodes"]["builder"].update(status="running", tokens=0))
    d, rc, _ = tx(proj, "start", "--run", run, "--node", "ba")
    assert refused(d, rc)


def _defects(run, n, body):
    write(run, f"artifacts/defects/round-{n}.json", body if isinstance(body, str) else json.dumps(body))


def test_f12_missing_defect_file_is_incomplete_not_pass(proj):  # T14
    run = resolved_run(proj)
    d, rc = t(proj, "defects", "--run", run, "--round", "1")
    assert rc != 0 and d["verdict"] == "incomplete"
    assert "1" not in get_state(run)["fingerprints"]


def test_f12_malformed_defect_file_is_incomplete(proj):
    run = resolved_run(proj)
    for body in ("{not json", json.dumps({"round": 1}), json.dumps({"round": 1, "defects": "none"}),
                 json.dumps([1, 2])):
        _defects(run, 1, body)
        d, rc = t(proj, "defects", "--run", run, "--round", "1")
        assert rc != 0 and d["verdict"] == "incomplete", body


def test_f12_wrong_round_and_blocked_coverage_are_incomplete(proj):  # T15
    run = resolved_run(proj)
    _defects(run, 1, {"round": 2, "defects": []})
    assert t(proj, "defects", "--run", run, "--round", "1")[0]["verdict"] == "incomplete"
    _defects(run, 1, {"round": 1, "defects": [], "blocked": ["TC-3"]})
    assert t(proj, "defects", "--run", run, "--round", "1")[0]["verdict"] == "incomplete"


def test_f12_explicit_empty_defects_is_still_a_pass(proj):
    run = resolved_run(proj)
    _defects(run, 1, {"round": 1, "defects": []})
    d, rc = t(proj, "defects", "--run", run, "--round", "1")
    assert rc == 0 and d["verdict"] == "pass"
