"""ghe update: additive config migration + Graphify decision. Never changes values the user set."""
import json, os, shutil, subprocess, sys
import pytest, yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(REPO, "ghe", "runner", "ghe_tool.py")
OLD = """# my config
tracker: local   # keep me
budgets:
  max_rounds: 7   # tuned
memory:
  max_entries: 3
"""


def run(proj, *args):
    p = subprocess.run([sys.executable, TOOL, *args, "--root", str(proj)], capture_output=True, text=True)
    try:
        d = json.loads(p.stdout)
    except ValueError:
        d = None
    return p.returncode, d


@pytest.fixture
def proj(tmp_path):
    (tmp_path / "ghe").mkdir()
    shutil.copy(os.path.join(REPO, "ghe", "graph.yaml"), tmp_path / "ghe" / "graph.yaml")
    (tmp_path / "ghe" / "config.yaml").write_text(OLD)
    return tmp_path


def cfg(p):
    return yaml.safe_load((p / "ghe" / "config.yaml").read_text())


def test_update_is_additive_and_keeps_user_values_and_comments(proj):
    rc, d = run(proj, "update")
    assert rc == 0 and d["changed"] and d["from_version"] == 1 and d["to_version"] == 2
    text = (proj / "ghe" / "config.yaml").read_text()
    assert "# keep me" in text and "# tuned" in text and "max_rounds: 7" in text
    c = cfg(proj)
    assert c["config_version"] == 2 and c["budgets"]["max_rounds"] == 7 and c["memory"] == {"max_entries": 3}
    assert "graphify" not in c and "#graphify:" in text      # only a commented example
    assert (proj / "ghe" / "config.yaml.bak").read_text() == OLD
    assert d["needs_decision"] is True and d["graphify"]["decision"] == "undecided"


def test_update_is_idempotent(proj):
    run(proj, "update")
    before = (proj / "ghe" / "config.yaml").read_text()
    rc, d = run(proj, "update")
    assert rc == 0 and d["changed"] is False and (proj / "ghe" / "config.yaml").read_text() == before


def test_dry_run_and_check_write_nothing(proj):
    for flag in ("--dry-run", "--check"):
        rc, d = run(proj, "update", flag)
        assert rc == 0 and d["dry_run"] is True and d["changed"] is True
        assert (proj / "ghe" / "config.yaml").read_text() == OLD
        assert not (proj / "ghe" / "config.yaml.bak").exists()


def test_graphify_false_is_recorded_and_needs_no_install(proj):
    rc, d = run(proj, "update", "--graphify", "false")
    assert rc == 0 and cfg(proj)["graphify"] == {"enabled": False}
    assert d["graphify"]["decision"] == "disabled" and d["needs_decision"] is False


def test_graphify_true_refused_when_not_installed_and_nothing_changes(proj):
    (proj / "ghe" / "config.yaml").write_text(OLD + "graphify:\n  command: definitely-not-installed-xyz\n")
    before = (proj / "ghe" / "config.yaml").read_text()
    rc, d = run(proj, "update", "--graphify", "true")
    assert rc == 1 and d["action"] == "not_installed" and d["installed"] is False
    assert (proj / "ghe" / "config.yaml").read_text() == before


def test_graphify_true_when_installed_and_force_override(proj):
    exe = proj / "fakegraphify"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    (proj / "ghe" / "config.yaml").write_text(OLD + f"graphify:\n  command: {exe}\n  timeout_seconds: 9\n")
    rc, d = run(proj, "update", "--graphify", "true")
    assert rc == 0 and d["graphify"]["enabled"] is True and d["graphify"]["installed"] is True
    g = cfg(proj)["graphify"]
    assert g["enabled"] is True and g["timeout_seconds"] == 9 and g["command"] == str(exe)
    (proj / "ghe" / "config.yaml").write_text(OLD + "graphify:\n  command: nope-xyz\n")
    assert run(proj, "update", "--graphify", "true", "--force")[0] == 0
    assert cfg(proj)["graphify"]["enabled"] is True


def test_decision_can_be_changed_later(proj):
    run(proj, "update", "--graphify", "false")
    rc, d = run(proj, "update", "--graphify", "true", "--force")
    assert rc == 0 and cfg(proj)["graphify"]["enabled"] is True
    rc, d = run(proj, "update", "--graphify", "false")
    assert rc == 0 and cfg(proj)["graphify"]["enabled"] is False


def test_existing_graphify_block_with_comments_is_edited_in_place(proj):
    (proj / "ghe" / "config.yaml").write_text(OLD + "graphify:\n  enabled: false   # off for now\n  build_args: [x]\n")
    run(proj, "update", "--graphify", "true", "--force")
    text = (proj / "ghe" / "config.yaml").read_text()
    assert "enabled: true   # off for now" in text and cfg(proj)["graphify"]["build_args"] == ["x"]


def test_invalid_graphify_value_and_bad_yaml_are_refused_untouched(proj):
    (proj / "ghe" / "config.yaml").write_text(OLD + "graphify:\n  enabled: maybe\n")
    rc, d = run(proj, "update")
    assert rc == 1 and "true or false" in d["error"]
    (proj / "ghe" / "config.yaml").write_text("a: [unclosed\n")
    rc, d = run(proj, "update")
    assert rc != 0 and "not valid YAML" in d["error"]
    assert (proj / "ghe" / "config.yaml").read_text() == "a: [unclosed\n"


def test_newer_config_version_is_refused(proj):
    (proj / "ghe" / "config.yaml").write_text(OLD + "config_version: 99\n")
    rc, d = run(proj, "update")
    assert rc == 1 and "newer than this toolkit" in d["error"]


def test_missing_config_points_to_setup(proj):
    os.remove(proj / "ghe" / "config.yaml")
    rc, d = run(proj, "update")
    assert rc != 0 and "ghe-setup" in d["error"]


def test_running_run_is_warned_not_touched(proj):
    run(proj, "update", "--graphify", "false")
    rc, d = run(proj, "init", "--run-id", "r1")
    assert rc == 0
    sp = proj / "ghe" / "runs" / "r1" / "state.json"
    j = json.loads(sp.read_text()); j["status"] = "RUNNING"; sp.write_text(json.dumps(j))
    st = sp.read_bytes()
    (proj / "ghe" / "config.yaml").write_text(OLD)
    rc, d = run(proj, "update")
    assert any("r1" in w for w in d["warnings"])
    assert (proj / "ghe" / "runs" / "r1" / "state.json").read_bytes() == st


def test_init_and_status_migrate_automatically_and_report(proj):
    rc, d = run(proj, "init", "--run-id", "r1")
    assert rc == 0 and d["config_update"]["changed"] is True and d["config_update"]["needs_decision"] is True
    assert cfg(proj)["config_version"] == 2
    rc, d = run(proj, "status", "--run", str(proj / "ghe" / "runs" / "r1"))
    assert rc == 0 and d["config_update"]["needs_decision"] is True and d["config_update"]["changed"] is False
    run(proj, "update", "--graphify", "false")
    rc, d = run(proj, "status", "--run", str(proj / "ghe" / "runs" / "r1"))
    assert "config_update" not in d


def test_update_skills_exist_and_are_namespaced_in_plugin():
    for n in ("ghe-update", "ghe-graphify"):
        t = open(os.path.join(REPO, ".claude", "skills", n, "SKILL.md")).read()
        assert f"name: {n}" in t
    assert "AskUserQuestion" in open(os.path.join(REPO, ".claude", "skills", "ghe-update", "SKILL.md")).read()
    assert "Ask me later" in open(os.path.join(REPO, ".claude", "skills", "ghe-setup", "SKILL.md")).read()
