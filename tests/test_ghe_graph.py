"""Graphify (optional provider) acceptance tests T43-T49 for ghe/runner/ghe_graph.py."""
import json
import os
import stat
import subprocess
import sys
import time

import pytest
import yaml

TOOL = os.path.join(os.path.dirname(__file__), "..", "ghe", "runner", "ghe_graph.py")

GOOD_GRAPH = {
    "nodes": [{"id": "n1", "label": "billing_total", "file": "app.py"},
              {"id": "n2", "label": "invoice_send", "file": "mail.py"}],
    "edges": [{"source": "n1", "target": "n2", "relation": "calls", "provenance": "EXTRACTED"},
              {"source": "n1", "target": "n2", "relation": "likely_affects_billing", "provenance": "INFERRED"}],
}


def run(root, *args, env=None):
    e = dict(os.environ)
    e.update(env or {})
    p = subprocess.run([sys.executable, TOOL, args[0], "--root", str(root), *args[1:]],
                       capture_output=True, text=True, env=e, timeout=60)
    return p.returncode, json.loads(p.stdout)


def script(tmp_path, name, body):
    path = tmp_path / "bin" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def calls(tmp_path):
    f = tmp_path / "bin" / "calls.log"
    return f.read_text().splitlines() if f.exists() else []


def good_graphify(tmp_path, extra=""):
    log = tmp_path / "bin" / "calls.log"
    body = (f'echo "$*|sem=$GHE_GRAPHIFY_SEMANTIC|key=$OPENAI_API_KEY" >> "{log}"\n{extra}\n'
            "mkdir -p graphify-out\ncat > graphify-out/graph.json <<'EOF'\n"
            + json.dumps(GOOD_GRAPH) + "\nEOF\n")
    return script(tmp_path, "graphify", body)


def project(tmp_path, command=None, enabled=True, code=True, **extra):
    root = tmp_path / "proj"
    (root / "ghe").mkdir(parents=True)
    cfg = {"enabled": enabled, "timeout_seconds": 3}
    if command:
        cfg["command"] = str(command)
    cfg.update(extra)
    (root / "ghe" / "config.yaml").write_text(yaml.safe_dump({"graphify": cfg}))
    if code:
        (root / "app.py").write_text("def billing_total(items):\n    return sum(items)\n")
    return root


# ---------------------------------------------------------------- T43
def test_T43_graphify_absent_falls_back_to_bounded_direct_retrieval(tmp_path):
    root = project(tmp_path, command=tmp_path / "bin" / "nope")
    (root / "big.py").write_text("billing_total = 1\n" * 200000)  # ~3.6MB, must not be loaded whole
    code, pre = run(root, "preflight")
    assert code == 0 and pre["action"] == "fallback" and pre["reason"] == "graphify_absent"
    code, r = run(root, "retrieve", "--query", "billing_total", "--max-chars", "1500")
    assert code == 0 and r["ok"] and r["items"] and not r["graph_used"]
    assert len(json.dumps(r)) <= 1500 and r["truncated"]
    assert all(i["source"] == "direct" for i in r["items"])


def test_T43_graphify_failing_does_not_crash(tmp_path):
    bad = script(tmp_path, "graphify", "echo boom >&2\nexit 3\n")
    root = project(tmp_path, command=bad)
    code, pre = run(root, "preflight")
    assert code == 0 and pre["action"] == "fallback" and pre["reason"] == "index_failed"
    assert not (root / "ghe" / ".graph" / "index-manifest.json").exists()
    code, r = run(root, "retrieve", "--query", "billing_total")
    assert code == 0 and r["items"] and not r["graph_used"]


# ---------------------------------------------------------------- T44
def test_T44_empty_project_skips_index_build(tmp_path):
    g = good_graphify(tmp_path)
    root = project(tmp_path, command=g, code=False)
    (root / "README.md").write_text("# hello\n")
    code, pre = run(root, "preflight")
    assert code == 0 and pre["action"] == "skip" and pre["reason"] == "empty_project"
    assert calls(tmp_path) == [] and not (root / "graphify-out").exists()


def test_T44_disabled_by_default_never_builds(tmp_path):
    g = good_graphify(tmp_path)
    root = tmp_path / "proj"
    root.mkdir()
    (root / "app.py").write_text("def billing_total(items):\n    return 1\n")
    code, pre = run(root, "preflight")  # no config at all
    assert pre["action"] == "skip" and pre["reason"] == "disabled" and calls(tmp_path) == []


# ---------------------------------------------------------------- T45
def test_T45_fresh_graph_is_reused_without_rebuild(tmp_path):
    root = project(tmp_path, command=good_graphify(tmp_path))
    _, first = run(root, "preflight")
    assert first["action"] == "build"
    m = json.loads((root / "ghe" / ".graph" / "index-manifest.json").read_text())
    for k in ("schema", "repo", "indexed_sha", "worktree_dirty", "graph_hash", "indexed_at",
              "input_fingerprint"):
        assert k in m
    _, second = run(root, "preflight")
    assert second["action"] == "reuse" and second["reason"] == "fresh"
    assert len(calls(tmp_path)) == 1


# ---------------------------------------------------------------- T46
def test_T46_stale_graph_refreshes_exactly_once(tmp_path):
    root = project(tmp_path, command=good_graphify(tmp_path))
    run(root, "preflight")
    (root / "app.py").write_text("def billing_total(items):\n    return sum(items) + 1  # dirty edit\n")
    _, st = run(root, "preflight")
    assert st["action"] == "refresh" and st["attempts"] == 1
    assert len(calls(tmp_path)) == 2
    _, again = run(root, "preflight")
    assert again["action"] == "reuse" and len(calls(tmp_path)) == 2


def test_T46_wrong_repo_manifest_is_not_authoritative(tmp_path):
    root = project(tmp_path, command=good_graphify(tmp_path))
    run(root, "preflight")
    mp = root / "ghe" / ".graph" / "index-manifest.json"
    m = json.loads(mp.read_text())
    m["repo"] = "/some/other/repo"
    mp.write_text(json.dumps(m))
    _, s = run(root, "status")
    assert s["fresh"] is False and s["reason"] == "wrong_repo"
    _, r = run(root, "retrieve", "--query", "billing_total")
    assert not r["graph_used"] and r["items"]  # current-source fallback


def test_T46_stale_refresh_failure_falls_back_to_source(tmp_path):
    g = good_graphify(tmp_path)
    root = project(tmp_path, command=g)
    run(root, "preflight")
    script(tmp_path, "graphify", "exit 1\n")  # graphify now breaks
    (root / "app.py").write_text("def billing_total(items):\n    return 42\n")
    _, st = run(root, "preflight")
    assert st["action"] == "fallback" and st["stale_reason"] in ("stale_inputs", "stale_dirty_worktree")
    _, r = run(root, "retrieve", "--query", "billing_total")
    assert not r["graph_used"] and r["fallback_reason"].startswith("stale")
    assert any("return 42" in i["snippet"] or "billing_total" in i["snippet"] for i in r["items"])


def test_T46_dirty_git_worktree_detected(tmp_path):
    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        pytest.skip("git unavailable")
    root = project(tmp_path, command=good_graphify(tmp_path))
    g = lambda *a: subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=t", *a],
                                  cwd=root, capture_output=True, check=True)
    g("init", "-q")
    g("add", "app.py")
    g("commit", "-qm", "init")
    run(root, "preflight")
    m = json.loads((root / "ghe" / ".graph" / "index-manifest.json").read_text())
    assert m["indexed_sha"] and m["worktree_dirty"] is False
    (root / "app.py").write_text("def billing_total(items):\n    return 0  # uncommitted\n")
    _, s = run(root, "status")
    assert s["fresh"] is False and s["reason"] == "stale_dirty_worktree"


# ---------------------------------------------------------------- T47
def test_T47_semantic_extraction_default_off_no_upload(tmp_path):
    root = project(tmp_path, command=good_graphify(tmp_path))
    _, pre = run(root, "preflight", env={"OPENAI_API_KEY": "sk-secret"})
    assert pre["semantic_effective"] is False
    line = calls(tmp_path)[0]
    assert "--semantic" not in line and "sem=0" in line and "sk-secret" not in line


def test_T47_semantic_requested_without_approval_is_ignored(tmp_path):
    root = project(tmp_path, command=good_graphify(tmp_path), semantic_extraction=True)
    _, pre = run(root, "preflight", env={"OPENAI_API_KEY": "sk-secret"})
    assert pre["semantic_effective"] is False and "semantic_note" in pre
    line = calls(tmp_path)[0]
    assert "--semantic" not in line and "sk-secret" not in line


def test_T47_semantic_only_with_full_authorization(tmp_path):
    root = project(tmp_path, command=good_graphify(tmp_path), semantic_extraction=True,
                   semantic_approved=True, semantic_provider="local-llm")
    _, pre = run(root, "preflight")
    assert pre["semantic_effective"] is True and "--semantic" in calls(tmp_path)[0]


# ---------------------------------------------------------------- T48
def test_T48_inferred_edges_keep_provenance_and_are_never_acceptance_eligible(tmp_path):
    root = project(tmp_path, command=good_graphify(tmp_path))
    run(root, "preflight")
    _, r = run(root, "retrieve", "--query", "billing_total", "--max-chars", "20000")
    assert r["graph_used"]
    edges = [i for i in r["items"] if i["source"] == "graph" and i["kind"] == "edge"]
    provs = {e["relation"]: e["provenance"] for e in edges}
    assert provs["calls"] == "extracted" and provs["likely_affects_billing"] == "inferred"
    for i in r["items"]:
        assert i["provenance"] in ("extracted", "inferred")
        if i["source"] == "graph":
            assert i["authoritative"] is False and i["acceptance_eligible"] is False
    assert any(i["source"] == "direct" and i["acceptance_eligible"] for i in r["items"])
    assert "sole source" in r["acceptance_note"]


# ---------------------------------------------------------------- T49
def test_T49_timeout_is_bounded_cleaned_and_not_retried(tmp_path):
    marker = tmp_path / "bin" / "calls.log"
    slow = script(tmp_path, "graphify",
                  f'echo run >> "{marker}"\nmkdir -p graphify-out\necho partial > graphify-out/x\nsleep 30\n')
    root = project(tmp_path, command=slow, timeout_seconds=1, max_index_attempts_per_preflight=3)
    t0 = time.time()
    code, pre = run(root, "preflight")
    assert time.time() - t0 < 15
    assert code == 0 and pre["action"] == "fallback" and pre["reason"] == "index_timeout"
    assert pre["attempts"] == 1 and len(calls(tmp_path)) == 1  # no retry loop even with cap 3
    assert not (root / "graphify-out").exists()  # partial output cleaned
    _, again = run(root, "preflight")  # same inputs: no rebuild loop across preflights either
    assert again["action"] == "fallback" and again["reason"].startswith("previous_attempt_timeout")
    assert len(calls(tmp_path)) == 1
    _, r = run(root, "retrieve", "--query", "billing_total")
    assert r["items"] and not r["graph_used"]


# ---------------------------------------------------------------- safety
def test_path_traversal_refused(tmp_path):
    root = project(tmp_path, command=good_graphify(tmp_path))
    code, r = run(root, "retrieve", "--query", "billing", "--scope", "../..")
    assert code == 2 and not r["ok"]
    root2 = project(tmp_path / "x", enabled=True, output_dir="../escape")
    code, r = run(root2, "status")
    assert code == 2 and "traversal" in r["error"]


def test_manifest_write_is_atomic_no_temp_left(tmp_path):
    root = project(tmp_path, command=good_graphify(tmp_path))
    run(root, "preflight")
    names = os.listdir(root / "ghe" / ".graph")
    assert not [n for n in names if n.startswith(".tmp-")]
