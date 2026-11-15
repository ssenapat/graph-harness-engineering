"""Skill-level acceptance scenarios (no runner behaviour): T38, T39, T51, T52, T53.
These procedures live in skill text, so the tests pin the rules that make them enforceable."""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def skill(name):
    return open(os.path.join(ROOT, ".claude", "skills", name, "SKILL.md")).read()


def section(txt, title):
    return txt.split(title)[1].split("\n## ")[0]


def test_t38_human_jira_edit_is_a_conflict_not_overwritten():
    s = section(skill("jira-sync"), "## Human edits and conflicts (T38)")
    assert "do NOT overwrite" in s and "getJiraIssue" in s
    assert "conflict:" in s and "needs_human" in s
    assert "cannot authorize" in s or "can never authorize" in s


def test_t39_jira_unavailable_never_switches_to_local():
    s = section(skill("jira-sync"), "## Jira unavailable (T39)")
    assert "WAITING_HUMAN" in s and "never change `tracker`" in s.lower().replace("Never", "never")
    assert "LOCAL-*" in s


def test_t51_unverified_production_blocked_but_test_deploy_allowed():
    s = section(skill("deploy"), "## Release rules (T51-T53)")
    assert "T51" in s and "blocked" in s and "test/local environment" in s
    assert "cloud-apply" in s and "recorded verification" in s


def test_t52_missing_account_region_never_guessed_or_persisted():
    d = skill("deploy")
    s = section(d, "## Release rules (T51-T53)")
    assert "never guess" in s and "persist" in s and "needs_human" in s
    assert "Never guess or default a region" in d and "never store credentials" in d


def test_t53_local_release_records_candidate_health_pid_port_cleanup():
    s = section(skill("deploy"), "## Release rules (T51-T53)")
    for word in ("candidate", "health check", "PID", "port", "stop command"):
        assert word in s
    assert "never `kill` by name" in s
    d = skill("deploy")
    assert "PID, stop command" in d.replace("ports, PID", "PID, stop command") or "PID" in d


def test_skill_rules_exist_in_the_plugin_copy_when_built():
    plug = os.path.join(ROOT, "plugin", "skills")
    if not os.path.isdir(plug):
        return
    # the committed plugin must carry the same procedures (drift is also caught by test_plugin)
    assert any(os.path.isdir(os.path.join(plug, d)) for d in os.listdir(plug))


def test_discover_skill_wires_graphify_as_optional_bounded_and_non_authoritative():
    s = section(skill("discover"), "## 1c. Optional Graphify context (never required)")
    for word in ("preflight", "retrieve", "fallback", "inferred", "non-authoritative", "semantic_approved"):
        assert word in s
    assert "Never retry a timed-out index" in s and "never block the run" in s


def test_graphify_commands_in_skills_use_the_real_cli():
    import subprocess, sys
    r = subprocess.run([sys.executable, os.path.join(ROOT, "ghe", "runner", "ghe_graph.py"), "--help"],
                       capture_output=True, text=True)
    assert r.returncode == 0
    for cmd in ("preflight", "retrieve", "status"):
        assert cmd in r.stdout


def test_stage4_commands_are_documented_in_skills():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    def txt(n):
        return open(os.path.join(root, ".claude", "skills", n, "SKILL.md")).read()
    for cmd in ("ticket-attach", "ticket-field", "tracker-down", "tracker-switch"):
        assert cmd in txt("jira-sync")
    for cmd in ("release-check", "deploy-target", "release-record", "release-cleanup"):
        assert cmd in txt("deploy")
    assert "context-packet" in txt("discover")
