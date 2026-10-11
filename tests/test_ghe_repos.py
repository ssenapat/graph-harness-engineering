"""Tests for the multi-repo workspace commands (ghe_tool.py repos ...)."""
import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "ghe", "runner", "ghe_tool.py")


def git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def mkrepo(path, remote=None):
    path.mkdir(parents=True)
    git(path, "init", "-q")
    if remote:
        git(path, "remote", "add", "origin", remote)
    return path


def repos(ws, *args, ok=True):
    p = subprocess.run([sys.executable, TOOL, "repos", *args, "--root", str(ws)], capture_output=True, text=True)
    d = json.loads(p.stdout)
    assert d["ok"] is ok, d
    return d


@pytest.fixture()
def tree(tmp_path):
    top = tmp_path / "top"
    ws = top / "work"  # the folder ghe is run from (not a repo)
    ws.mkdir(parents=True)
    return top, ws


def test_finds_repo_in_cwd_child_before_parent(tree):
    top, ws = tree
    mkrepo(ws / "api")
    mkrepo(top / "api")  # same name one level up must lose
    r = repos(ws, "find", "--hint", "api")["results"][0]
    assert r["status"] == "found" and r["path"] == str(ws / "api") and r["source"] == "cwd-child"


def test_finds_repo_in_parent_dir(tree):
    top, ws = tree
    mkrepo(top / "web")
    r = repos(ws, "find", "--hint", "web")["results"][0]
    assert r["status"] == "found" and r["path"] == str(top / "web") and r["source"] == "parent"


def test_cwd_itself_is_the_repo(tree):
    top, ws = tree
    repo = mkrepo(top / "svc")
    r = repos(repo, "find", "--hint", "svc")["results"][0]
    assert r["status"] == "found" and r["path"] == str(repo) and r["source"] == "cwd"


def test_url_matches_by_remote_even_if_dir_name_differs(tree):
    top, ws = tree
    mkrepo(top / "renamed", remote="git@github.com:acme/billing.git")
    r = repos(ws, "find", "--hint", "https://github.com/acme/billing")["results"][0]
    assert r["status"] == "found" and r["path"] == str(top / "renamed")


def test_missing_url_reports_clone_dest_and_unresolved(tree):
    top, ws = tree
    d = repos(ws, "find", "--hint", "https://github.com/acme/new-thing.git")
    r = d["results"][0]
    assert r["status"] == "missing" and r["clone_dest"] == str(ws / "new-thing")
    assert d["unresolved"] == ["https://github.com/acme/new-thing.git"]


def test_clone_base_is_parent_when_workspace_is_a_repo(tree):
    top, ws = tree
    repo = mkrepo(top / "host")
    d = repos(repo, "find", "--hint", "https://github.com/acme/other.git")
    assert d["results"][0]["clone_dest"] == str(top / "other")


def test_fuzzy_match_needs_confirmation_and_is_not_found(tree):
    top, ws = tree
    mkrepo(ws / "billing-api")
    d = repos(ws, "find", "--hint", "billing")
    assert d["results"][0]["status"] == "fuzzy" and d["unresolved"] == ["billing"]


def test_nested_repos_below_depth_one_are_not_scanned(tree):
    top, ws = tree
    mkrepo(ws / "x" / "deep")
    assert repos(ws, "find", "--hint", "deep")["results"][0]["status"] == "missing"


def test_explicit_path_hint(tree):
    top, ws = tree
    mkrepo(top / "elsewhere")
    r = repos(ws, "find", "--hint", "../elsewhere")["results"][0]
    assert r["status"] == "found" and r["source"] == "path"
    assert repos(ws, "find", "--hint", "./nope")["results"][0]["status"] == "missing"


def test_clone_requires_confirmation_and_safe_url(tree):
    top, ws = tree
    repos(ws, "clone", "--url", "https://github.com/acme/x.git", ok=False)  # no --confirmed
    repos(ws, "clone", "--url", "ext::sh -c touch /tmp/pwn", "--confirmed", ok=False)
    repos(ws, "clone", "--url=--upload-pack=evil", "--confirmed", ok=False)
    assert not (ws / "x").exists()


def test_clone_refuses_existing_destination(tree):
    top, ws = tree
    (ws / "x").mkdir()
    d = repos(ws, "clone", "--url", "https://github.com/acme/x.git", "--confirmed", ok=False)
    assert "already exists" in d["error"]


def test_add_and_list_report_which_repos_need_setup(tree):
    top, ws = tree
    src = mkrepo(top / "origin-src")
    repos(ws, "add", "--name", "origin-src", "--path", str(src))
    d = repos(ws, "list")
    assert [r["name"] for r in d["repos"]] == ["origin-src"] and d["needs_setup"] == ["origin-src"]
    (src / "ghe").mkdir()
    (src / "ghe" / "config.yaml").write_text("x: 1\n")
    (src / "ghe" / "graph.yaml").write_text("x: 1\n")
    assert repos(ws, "list")["needs_setup"] == []


def test_bootstrap_workspace_flag_skips_gitignore_in_plain_folder(tree):
    top, ws = tree
    p = subprocess.run([sys.executable, TOOL, "bootstrap", "--workspace", "--root", str(ws)], capture_output=True, text=True)
    assert json.loads(p.stdout)["gitignore_added"] == []
    assert not (ws / ".gitignore").exists() and (ws / "ghe" / "config.yaml").exists()
