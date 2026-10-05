"""Tests for the Claude Code plugin packaging (bin/ghe-build-plugin -> plugin/)."""
import json
import os
import re
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(REPO, "bin", "ghe-build-plugin")
PLUGIN = os.path.join(REPO, "plugin")
RUNNER = os.path.join(PLUGIN, "ghe", "runner", "ghe_tool.py")


def sh(args, cwd=None):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


def runner(proj, *args):
    r = sh([sys.executable, RUNNER, *args, "--root", str(proj)])
    return r.returncode, (json.loads(r.stdout) if r.stdout.strip() else {}), r


# ------------------------------------------------------------------ the committed plugin/ is derived, never hand-edited
def test_committed_plugin_is_in_sync_with_the_sources():
    r = sh([sys.executable, BUILD, "--check"])
    assert r.returncode == 0, r.stderr


def test_manifest_matches_version_and_marketplace_points_at_plugin():
    manifest = json.load(open(os.path.join(PLUGIN, ".claude-plugin", "plugin.json")))
    version = open(os.path.join(REPO, "VERSION")).read().strip()
    assert manifest["name"] == "ghe" and manifest["version"] == version
    market = json.load(open(os.path.join(REPO, ".claude-plugin", "marketplace.json")))
    entry = market["plugins"][0]
    assert entry["name"] == manifest["name"] and entry["version"] == version
    assert os.path.isfile(os.path.join(REPO, entry["source"], ".claude-plugin", "plugin.json"))


# ------------------------------------------------------------------ path rewriting
def md_files():
    for sub in ("skills", "agents", "rules"):
        for dirpath, _, files in os.walk(os.path.join(PLUGIN, sub)):
            for f in files:
                if f.endswith(".md"):
                    yield os.path.join(dirpath, f)


def test_no_project_relative_toolkit_paths_remain():
    bad = []
    for path in md_files():
        text = open(path, encoding="utf-8").read()
        for rx in (r"\.claude/(skills|agents|rules|hooks)/", r"python3 ghe/runner/ghe_tool\.py",
                   r"(?<![\w/{}.-])ghe/runner/ghe_tool\.py", r"(?<![\w/.{}-])(templates|schemas)/",
                   r"(?<![\w/.-])/ghe-(run|setup|status)\b"):
            if re.search(rx, text):
                bad.append((os.path.relpath(path, PLUGIN), rx))
    assert not bad, bad


def test_every_plugin_root_reference_points_at_a_real_file():
    """Each ${CLAUDE_PLUGIN_ROOT}/<path> mentioned in a skill/agent/rule must exist in plugin/."""
    missing = []
    for path in md_files():
        for ref in re.findall(r"\$\{CLAUDE_PLUGIN_ROOT\}/([\w./-]+)", open(path, encoding="utf-8").read()):
            ref = ref.rstrip(".,;:)`\"'")
            target = os.path.join(PLUGIN, ref)
            if not (os.path.exists(target) or ref.endswith("/")):
                missing.append((os.path.relpath(path, PLUGIN), ref))
            elif ref.endswith("/") and not os.path.isdir(target):
                missing.append((os.path.relpath(path, PLUGIN), ref))
    assert not missing, missing


def test_user_facing_commands_are_namespaced():
    assert sorted(d for d in os.listdir(os.path.join(PLUGIN, "skills")) if d in ("run", "setup", "status")) == ["run", "setup", "status"]
    assert not os.path.exists(os.path.join(PLUGIN, "skills", "ghe-run"))
    head = open(os.path.join(PLUGIN, "skills", "run", "SKILL.md"), encoding="utf-8").read().split("---")[1]
    assert re.search(r"(?m)^name:\s*run$", head)


def test_hooks_json_uses_plugin_root_and_every_script_exists():
    hooks = json.load(open(os.path.join(PLUGIN, "hooks", "hooks.json")))["hooks"]
    cmds = [h["command"] for group in hooks.values() for entry in group for h in entry["hooks"]]
    assert cmds
    for cmd in cmds:
        assert "${CLAUDE_PLUGIN_ROOT}/hooks/" in cmd and "CLAUDE_PROJECT_DIR" not in cmd
        script = re.search(r"\$\{CLAUDE_PLUGIN_ROOT\}/(hooks/[\w.]+\.py)", cmd).group(1)
        assert os.path.isfile(os.path.join(PLUGIN, script))


def test_runner_and_hooks_are_byte_identical_to_the_sources():
    pairs = [("ghe/runner/ghe_tool.py", "ghe/runner/ghe_tool.py")]
    pairs += [(f".claude/hooks/{f}", f"hooks/{f}") for f in os.listdir(os.path.join(REPO, ".claude", "hooks")) if f.endswith(".py")]
    for src, dst in pairs:
        assert open(os.path.join(REPO, src), "rb").read() == open(os.path.join(PLUGIN, dst), "rb").read(), src


# ------------------------------------------------------------------ bootstrap (what /ghe:setup runs first)
def test_init_refuses_until_the_project_is_set_up(tmp_path):
    rc, out, _ = runner(tmp_path, "init")
    assert rc == 2 and out["ok"] is False and "not set up" in out["error"]


def test_bootstrap_creates_project_files_and_gitignore(tmp_path):
    rc, out, _ = runner(tmp_path, "bootstrap")
    assert rc == 0 and out["ok"] is True
    assert sorted(out["created"]) == ["ghe/config.yaml", "ghe/graph.yaml"]
    for rel in ("ghe/config.yaml", "ghe/graph.yaml", "ghe/memory/decisions.md", "ghe/memory/patterns.md"):
        assert (tmp_path / rel).is_file(), rel
    assert (tmp_path / "ghe" / "runs").is_dir()
    lines = (tmp_path / ".gitignore").read_text().splitlines()
    for want in ("ghe/", ".claude/worktrees/", ".claude/settings.local.json"):
        assert lines.count(want) == 1, want
    # defaults ship placeholders, never real Jira / AWS values
    cfg = (tmp_path / "ghe" / "config.yaml").read_text()
    hosts = re.findall(r"https?://([^/\s\"']+)\.atlassian\.net", cfg)
    assert hosts and all(h.startswith("<") and h.endswith(">") for h in hosts), hosts  # placeholder only
    assert "<JIRA_PROJECT_KEY>" in cfg and "us-east-1" not in cfg


def test_bootstrap_is_idempotent_and_never_overwrites(tmp_path):
    runner(tmp_path, "bootstrap")
    cfg = tmp_path / "ghe" / "config.yaml"
    cfg.write_text(cfg.read_text() + "\n# my edit\n")
    before_cfg, before_gi = cfg.read_text(), (tmp_path / ".gitignore").read_text()
    rc, out, _ = runner(tmp_path, "bootstrap")
    assert rc == 0 and out["created"] == [] and out["gitignore_added"] == []
    assert cfg.read_text() == before_cfg and (tmp_path / ".gitignore").read_text() == before_gi


def test_bootstrap_appends_to_an_existing_gitignore_without_a_trailing_newline(tmp_path):
    (tmp_path / ".gitignore").write_text("node_modules/\n.claude/worktrees")  # no newline; one line already covered
    rc, out, _ = runner(tmp_path, "bootstrap")
    assert rc == 0 and out["gitignore_added"] == ["ghe/", ".claude/settings.local.json"]
    lines = (tmp_path / ".gitignore").read_text().splitlines()
    assert lines[:2] == ["node_modules/", ".claude/worktrees"]
    assert "ghe/" in lines and ".claude/settings.local.json" in lines and ".claude/worktrees/" not in lines


def test_a_run_can_start_after_bootstrap_using_only_the_plugin_files(tmp_path):
    runner(tmp_path, "bootstrap")
    rc, out, _ = runner(tmp_path, "init", "--run-id", "ghe-plugin-test")
    assert rc == 0 and out["ok"] is True
    assert (tmp_path / "ghe" / "runs" / "ghe-plugin-test" / "state.json").is_file()


def test_bootstrap_in_the_toolkit_repo_itself_keeps_its_files(tmp_path):
    """Running the source runner (not the plugin copy) must not clobber an existing config."""
    proj = tmp_path
    (proj / "ghe").mkdir()
    (proj / "ghe" / "config.yaml").write_text("tracker: local\n")
    r = sh([sys.executable, os.path.join(REPO, "ghe", "runner", "ghe_tool.py"), "bootstrap", "--root", str(proj)])
    assert r.returncode == 0
    assert (proj / "ghe" / "config.yaml").read_text() == "tracker: local\n"
    assert (proj / "ghe" / "graph.yaml").is_file()
