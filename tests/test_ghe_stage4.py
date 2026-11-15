"""Stage 4: T36 attachments/issue types, T38 field conflicts, T39 tracker outage, T41/T42 bounded context,
T51-T53 release controls (runner behaviour)."""
import json
import os
import subprocess
import sys

import yaml

from test_ghe_hardening import _tk, resolved_run, set_state, get_state, tx, write
from test_ghe_tool import TOOL, proj  # noqa: F401


def _ticket(proj):
    run = resolved_run(proj)
    d, rc = _tk(proj, run, "epic", "run")
    assert rc == 0
    return run, d["ticket"]["key"]


# ---- T36
def test_t36_attachment_supported_is_recorded(proj):
    run, key = _ticket(proj)
    write(run, "artifacts/hld.md", "design")
    d, rc, _ = tx(proj, "ticket-attach", "--run", run, "--key", key, "--path", "hld.md", "--capability", "supported")
    assert rc == 0 and d["attached"] is True


def test_t36_unsupported_attachment_falls_back_with_limitation(proj):
    run, key = _ticket(proj)
    write(run, "artifacts/hld.md", "design text")
    d, rc, _ = tx(proj, "ticket-attach", "--run", run, "--key", key, "--path", "hld.md", "--capability", "unsupported")
    assert rc == 0 and d["attached"] is False and "limitation" in d and d["fallback"]
    t = get_state(run)["tickets"]
    assert any(v.get("attach_fallback") for v in t.values())


def test_t36_attach_refuses_path_escape(proj):
    run, key = _ticket(proj)
    d, rc, _ = tx(proj, "ticket-attach", "--run", run, "--key", key, "--path", "../../etc/passwd", "--capability", "supported")
    assert rc != 0


def test_t36_missing_issue_type_is_actionable_not_silent(proj):
    cfg = os.path.join(str(proj), "ghe", "config.yaml")
    c = yaml.safe_load(open(cfg))
    c["jira"]["issue_types"] = {"subtask": None}
    yaml.safe_dump(c, open(cfg, "w"), sort_keys=False)
    run = resolved_run(proj)
    d, rc = _tk(proj, run, "epic", "run")
    story = _tk(proj, run, "story", "S-1", parent=d["ticket"]["key"])[0]["ticket"]["key"]
    d, rc = _tk(proj, run, "subtask", "S-1-x", parent=story)
    assert rc != 0 and "issue type" in d["error"]


# ---- T38
def test_t38_human_edit_is_a_conflict_not_overwritten(proj):
    run, key = _ticket(proj)
    assert tx(proj, "ticket-field", "--run", run, "--key", key, "--field", "summary", "--write", "ours")[1] == 0
    d, rc, _ = tx(proj, "ticket-field", "--run", run, "--key", key, "--field", "summary", "--remote", "edited by human")
    assert rc != 0 and d["conflict"] is True and d["action"] == "ask_human"
    assert tx(proj, "ticket-field", "--run", run, "--key", key, "--field", "summary", "--write", "again")[1] != 0


def test_t38_resolution_needs_a_person(proj):
    run, key = _ticket(proj)
    tx(proj, "ticket-field", "--run", run, "--key", key, "--field", "summary", "--write", "ours")
    tx(proj, "ticket-field", "--run", run, "--key", key, "--field", "summary", "--remote", "theirs")
    assert tx(proj, "ticket-field", "--run", run, "--key", key, "--field", "summary", "--resolve", "overwrite")[1] != 0
    assert tx(proj, "ticket-field", "--run", run, "--key", key, "--field", "summary", "--resolve", "overwrite", "--by", "alice")[1] == 0
    assert tx(proj, "ticket-field", "--run", run, "--key", key, "--field", "summary", "--write", "again")[1] == 0


def test_t38_unchanged_remote_is_no_conflict(proj):
    run, key = _ticket(proj)
    tx(proj, "ticket-field", "--run", run, "--key", key, "--field", "summary", "--write", "same")
    d, rc, _ = tx(proj, "ticket-field", "--run", run, "--key", key, "--field", "summary", "--remote", "same")
    assert rc == 0 and d["conflict"] is False


# ---- T39
def test_t39_tracker_down_blocks_tickets_and_finish(proj):
    run, key = _ticket(proj)
    assert tx(proj, "tracker-down", "--run", run, "--error", "503")[1] == 0
    assert _tk(proj, run, "epic", "other")[1] != 0
    set_state(run, lambda st: [n.update(status="done") for n in st["nodes"].values()])
    write(run, "artifacts/RUN-REPORT.md", "# r")
    write(run, "artifacts/requirement-spec.md", "REQ-001 x")
    d, rc, _ = tx(proj, "finish", "--run", run, "--status", "DONE")
    assert rc != 0 and any("tracker" in p for p in d["problems"])


def test_t39_resume_needs_a_person_and_clears(proj):
    run, key = _ticket(proj)
    tx(proj, "tracker-down", "--run", run, "--error", "503")
    assert tx(proj, "tracker-resume", "--run", run)[1] != 0
    assert tx(proj, "tracker-resume", "--run", run, "--by", "alice")[1] == 0
    assert "tracker_unavailable" not in get_state(run)


def test_t39_switch_needs_actor_reason_and_matching_config(proj):
    run, key = _ticket(proj)
    assert tx(proj, "tracker-switch", "--run", run, "--to", "local")[1] != 0
    assert tx(proj, "tracker-switch", "--run", run, "--to", "local", "--by", "a", "--reason", "r")[1] != 0   # config still jira


def test_t39_config_flip_without_switch_is_refused(proj):
    run, key = _ticket(proj)
    cfg = os.path.join(str(proj), "ghe", "config.yaml")
    c = yaml.safe_load(open(cfg)); c["tracker"] = "local"
    yaml.safe_dump(c, open(cfg, "w"), sort_keys=False)
    assert _tk(proj, run, "epic", "other")[1] != 0


# ---- T41 / T42
def _src(run, name, text):
    write(run, f"artifacts/{name}", text)
    return f"{name.split('.')[0]}={name}"


def test_t41_packet_is_bounded_and_relevant(proj):
    run = resolved_run(proj)
    big = "\n\n".join(f"## note {i}\ncolour palette item {i}" if i % 50 else f"## note {i}\ndatabase migration plan {i}"
                      for i in range(400))
    s = _src(run, "comments.md", big)
    d, rc, _ = tx(proj, "context-packet", "--run", run, "--query", "database migration", "--source", s)
    assert rc == 0 and d["included"] <= 12 and d["chars"] <= 6000 and d["total_candidates"] == 400
    packet = json.load(open(os.path.join(run, "artifacts", "context-packet.json")))
    assert all("database" in e["text"] for e in packet["included"])


def test_t42_essential_over_budget_is_refused_not_truncated(proj):
    run = resolved_run(proj)
    s = _src(run, "m.md", "## big\n[essential] " + "x" * 7000)
    d, rc, _ = tx(proj, "context-packet", "--run", run, "--query", "x", "--source", s)
    assert rc != 0 and d["action"] == "split_or_pause"
    assert not os.path.exists(os.path.join(run, "artifacts", "context-packet.json"))


def test_t42_essential_fits_is_always_included(proj):
    run = resolved_run(proj)
    s = _src(run, "m.md", "## must\n[essential] keep me\n\n## other\nnothing relevant")
    d, rc, _ = tx(proj, "context-packet", "--run", run, "--query", "zzz", "--source", s)
    assert rc == 0 and d["included"] == 1


def test_t41_source_read_is_capped(proj):
    run = resolved_run(proj)
    cfg = os.path.join(str(proj), "ghe", "config.yaml")
    c = yaml.safe_load(open(cfg)); c["context"] = {"max_source_bytes": 1000}
    yaml.safe_dump(c, open(cfg, "w"), sort_keys=False)
    s = _src(run, "h.md", "\n\n".join(f"## e{i}\nword{i}" for i in range(5000)))
    d, rc, _ = tx(proj, "context-packet", "--run", run, "--query", "word", "--source", s)
    assert rc == 0 and d["sources"]["h"]["capped"] is True and d["total_candidates"] < 200


def test_t41_invalid_context_config_and_bad_source_refused(proj):
    run = resolved_run(proj)
    assert tx(proj, "context-packet", "--run", run, "--query", "x", "--source", "a=../../etc/passwd")[1] != 0
    cfg = os.path.join(str(proj), "ghe", "config.yaml")
    c = yaml.safe_load(open(cfg)); c["context"] = {"max_chars": 0}
    yaml.safe_dump(c, open(cfg, "w"), sort_keys=False)
    s = _src(run, "h.md", "## a\nx")
    assert tx(proj, "context-packet", "--run", run, "--query", "x", "--source", s)[1] != 0


# ---- T52
def test_t52_missing_target_asks_human(proj):
    run = resolved_run(proj)
    d, rc, _ = tx(proj, "deploy-target", "--run", run)
    assert rc == 2 and d["status"] == "needs_human"


def test_t52_requester_target_accepted(proj):
    run = resolved_run(proj)
    write(run, "artifacts/deploy-target.md", "---\naws_account: '123456789012'\naws_region: eu-west-1\nprovided_by: requester\n---\n")
    d, rc, _ = tx(proj, "deploy-target", "--run", run)
    assert rc == 0 and d["aws_region"] == "eu-west-1" and get_state(run)["deploy_target"]["aws_account"] == "123456789012"


def test_t52_guessed_or_malformed_target_refused(proj):
    run = resolved_run(proj)
    write(run, "artifacts/deploy-target.md", "---\naws_account: '123456789012'\naws_region: eu-west-1\nprovided_by: agent\n---\n")
    assert tx(proj, "deploy-target", "--run", run)[1] == 2
    write(run, "artifacts/deploy-target.md", "---\naws_account: abc\naws_region: nowhere\nprovided_by: requester\n---\n")
    assert tx(proj, "deploy-target", "--run", run)[1] == 2


def test_t52_credentials_in_target_file_refused_and_not_stored(proj):
    run = resolved_run(proj)
    write(run, "artifacts/deploy-target.md",
          "---\naws_account: '123456789012'\naws_region: eu-west-1\nprovided_by: requester\n---\nAKIAABCDEFGHIJKLMNOP\n")
    d, rc, _ = tx(proj, "deploy-target", "--run", run)
    assert rc == 1 and "credential" in d["error"]
    assert "AKIA" not in open(os.path.join(run, "state.json")).read()


# ---- T51
def test_t51_production_blocked_without_verification(proj):
    run = resolved_run(proj)
    d, rc, _ = tx(proj, "release-check", "--run", run, "--env", "production")
    assert rc == 1 and d["allowed"] is False
    assert tx(proj, "release-check", "--run", run, "--env", "test")[1] == 0


def test_t51_production_needs_approved_gate_even_when_verified(proj):
    run = resolved_run(proj)
    set_state(run, lambda st: st.update(verification={"status": "passed"}))
    d, rc, _ = tx(proj, "release-check", "--run", run, "--env", "production")
    assert rc == 1 and "cloud-apply" in d["error"]
    set_state(run, lambda st: st.setdefault("gates", {}).update({"cloud-apply": {"status": "approved", "decided_by": "alice"}}))
    d, rc, _ = tx(proj, "release-check", "--run", run, "--env", "production")
    assert rc == 0 and d["allowed"] is True


def test_t51_waiver_counts_as_verification(proj):
    run = resolved_run(proj)
    set_state(run, lambda st: (st.update(verification={"status": "waived"}),
                               st.setdefault("gates", {}).update({"cloud-apply": {"status": "approved"}})))
    assert tx(proj, "release-check", "--run", run, "--env", "production")[1] == 0


# ---- T53
def test_t53_record_needs_candidate_and_health(proj):
    run = resolved_run(proj)
    assert tx(proj, "release-record", "--run", run, "--health", "passed")[1] != 0
    assert tx(proj, "release-record", "--run", run, "--candidate", "abc123", "--health", "maybe")[1] != 0
    d, rc, _ = tx(proj, "release-record", "--run", run, "--candidate", "abc123", "--health", "passed", "--pid", "1", "--port", "8080")
    assert rc == 0 and d["recorded"]["candidate"] == "abc123"


def test_t53_cleanup_stops_only_recorded_pid(proj):
    run = resolved_run(proj)
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        tx(proj, "release-record", "--run", run, "--candidate", "abc", "--health", "passed", "--pid", str(victim.pid))
        d, rc, _ = tx(proj, "release-cleanup", "--run", run)
        assert rc == 0 and d["killed"] is True
        victim.wait(timeout=5)
        assert bystander.poll() is None
        assert tx(proj, "release-cleanup", "--run", run)[0]["already_cleaned"] is True
    finally:
        bystander.kill(); victim.kill()


def test_t53_cleanup_without_record_refused_and_dead_pid_not_killed(proj):
    run = resolved_run(proj)
    assert tx(proj, "release-cleanup", "--run", run)[1] != 0
    dead = subprocess.Popen([sys.executable, "-c", "pass"]); dead.wait()
    tx(proj, "release-record", "--run", run, "--candidate", "abc", "--health", "failed", "--pid", str(dead.pid))
    d, rc, _ = tx(proj, "release-cleanup", "--run", run)
    assert rc == 0 and d["killed"] is False
