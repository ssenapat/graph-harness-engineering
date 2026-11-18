"""Expense report: per story/defect cost, rework, epic roll-up, Jira comments."""
import json, os
import yaml
from test_ghe_hardening import resolved_run, tx
from test_ghe_tool import proj  # noqa: F401

PRICES = {"cost": {"currency": "USD", "prices": {"mid": {"in": 3, "out": 15, "cache": 0.3}}}}

def _cfg(proj, **extra):
    p = os.path.join(str(proj), "ghe", "config.yaml")
    c = yaml.safe_load(open(p)); c.update(extra); yaml.safe_dump(c, open(p, "w"), sort_keys=False)

def _tok(proj, run, node, add, event, story="", split=None):
    a = ["tokens", "--run", run, "--node", node, "--add", str(add), "--event", event]
    if story: a += ["--story", story]
    if split: a += ["--tokens-in", str(split[0]), "--tokens-out", str(split[1]), "--tokens-cache", str(split[2])]
    return tx(proj, *a)

def _report(proj, run):
    d, rc, _ = tx(proj, "expense-report", "--run", run); assert rc == 0, d; return d

def test_cost_per_story_and_exact_price(proj):
    _cfg(proj, **PRICES); run = resolved_run(proj)
    _tok(proj, run, "discover", 2_000_000, "e1", "S-01", (1_000_000, 1_000_000, 0))
    d = _report(proj, run)
    assert d["run_tokens"] == 2_000_000 and abs(d["run_cost"] - 18.0) < 1e-9 and d["priced"]
    md = open(os.path.join(str(proj), d["path"])).read()
    assert "| S-01 |" in md and "18.0000 USD" in md

def test_split_must_sum_and_duplicates_not_double_counted(proj):
    _cfg(proj, **PRICES); run = resolved_run(proj)
    assert _tok(proj, run, "discover", 10, "e1", "S-01", (1, 1, 1))[1] != 0
    _tok(proj, run, "discover", 1_000_000, "e2", "S-01", (1_000_000, 0, 0))
    _tok(proj, run, "discover", 1_000_000, "e2", "S-01", (1_000_000, 0, 0))   # replay
    assert _report(proj, run)["run_tokens"] == 1_000_000

def test_no_prices_means_tokens_only(proj):
    run = resolved_run(proj); _tok(proj, run, "discover", 500, "e1", "S-01")
    d = _report(proj, run)
    assert d["run_cost"] is None and d["priced"] is False
    assert "n/a" in open(os.path.join(str(proj), d["path"])).read()

def test_missing_split_is_flagged_as_estimate(proj):
    _cfg(proj, **PRICES); run = resolved_run(proj); _tok(proj, run, "discover", 1_000_000, "e1", "S-01")
    d = _report(proj, run)
    assert abs(d["run_cost"] - 3.0) < 1e-9
    assert "estimate" in open(os.path.join(str(proj), d["path"])).read()

def test_defect_and_epic_rollup_and_jira_comments(proj):
    _cfg(proj, **PRICES); run = resolved_run(proj)
    epic = tx(proj, "ticket", "--run", run, "--kind", "epic", "--node", "run", "--key", "GHE-1")[0]["ticket"]["key"]
    tx(proj, "ticket", "--run", run, "--kind", "story", "--node", "S-01", "--ref", "S-01", "--key", "GHE-2", "--parent", epic)
    tx(proj, "ticket", "--run", run, "--kind", "task", "--node", "d", "--ref", "defect:D1", "--key", "GHE-3")
    _tok(proj, run, "discover", 1_000_000, "e1", "S-01", (1_000_000, 0, 0))
    _tok(proj, run, "discover", 1_000_000, "e2", "defect:D1", (1_000_000, 0, 0))
    d = _report(proj, run)
    keys = {c["key"]: c["comment"] for c in d["jira_comments"]}
    assert set(keys) == {"GHE-1", "GHE-2", "GHE-3"} and "roll-up" in keys["GHE-1"]
    assert d["epic_tokens"] == 2_000_000 and d["path"].endswith("GHE-1.md")

def test_epic_consolidates_across_runs(proj):
    from test_ghe_hardening import _second_run
    _cfg(proj, **PRICES)
    run1 = resolved_run(proj)
    run2 = _second_run(proj)
    tx(proj, "resolve", "--run", run2)
    for i, run in ((1, run1), (2, run2)):
        tx(proj, "ticket", "--run", run, "--kind", "epic", "--node", "run", "--key", "GHE-1")
        _tok(proj, run, "discover", 1_000_000, f"e{i}", f"S-0{i}", (1_000_000, 0, 0))
        d = _report(proj, run)
    assert d["epic_tokens"] == 2_000_000

def test_invalid_prices_refused_and_finish_needs_report(proj):
    _cfg(proj, cost={"currency": "USD", "prices": {"mid": {"in": -1}}}); run = resolved_run(proj)
    assert tx(proj, "expense-report", "--run", run)[1] != 0
    _cfg(proj, **PRICES)
    d, rc, _ = tx(proj, "finish", "--run", run, "--status", "DONE")
    assert "expense report not written" in json.dumps(d)
    _report(proj, run)
    d, rc, _ = tx(proj, "finish", "--run", run, "--status", "DONE")
    assert "expense report not written" not in json.dumps(d)
