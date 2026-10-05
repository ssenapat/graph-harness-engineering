"""Tests for the guard and lint hooks."""
import json, os, subprocess, sys
import pytest

HOOKS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".claude", "hooks")


GATED = ("ghe_guard.py", "security_guard.py")  # these stay silent unless the project has ghe/config.yaml


def run(hook, payload, root, ghe_project=True):
    if ghe_project and hook in GATED:
        cfg = os.path.join(str(root), "ghe", "config.yaml")
        if not os.path.exists(cfg):
            os.makedirs(os.path.dirname(cfg), exist_ok=True)
            open(cfg, "w").close()
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(root)}
    return subprocess.run([sys.executable, os.path.join(HOOKS, hook)], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, cwd=root)


def bash(root, cmd):
    return run("ghe_guard.py", {"tool_name": "Bash", "tool_input": {"command": cmd}}, root).returncode


@pytest.mark.parametrize("cmd", [
    "terraform apply", "terraform -chdir=infra apply -auto-approve", "terraform -no-color apply",
    "tofu apply", "bash -c 'terraform  apply'", "env X=1 terraform apply", "terraform destroy",
    "terraform apply -destroy", "aws ec2 terminate-instances --instance-ids i-1", "aws s3 rb s3://b; aws s3api delete-bucket --bucket b",
])
def test_blocks_without_gate(tmp_path, cmd):
    assert bash(tmp_path, cmd) == 2


@pytest.mark.parametrize("cmd", ["terraform plan", "terraform validate", "ls -la", "aws sts get-caller-identity"])
def test_allows_safe(tmp_path, cmd):
    assert bash(tmp_path, cmd) == 0


def test_gate_approval_allows_apply(tmp_path):
    d = tmp_path / "ghe" / "runs" / "r1"; d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps({"status": "RUNNING", "gates": {"cloud-apply": {"status": "approved"}}}))
    assert bash(tmp_path, "terraform apply") == 0
    assert bash(tmp_path, "terraform destroy") == 2  # different gate


@pytest.mark.parametrize("path", ["ghe/runs/r1/state.json", "ghe/runs/r1/../r1/state.json", "./ghe/runs/r1/state.json"])
def test_blocks_state_json_edit(tmp_path, path):
    r = run("ghe_guard.py", {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / path)}}, tmp_path)
    assert r.returncode == 2


@pytest.mark.parametrize("cmd", ["sed -i '' s/a/b/ ghe/runs/r1/state.json", "echo {} > ghe/runs/r1/state.json",
                                 "python3 -c 'open(\"ghe/runs/r1/state.json\",\"w\")'", "tee ghe/runs/r1/artifacts/source.md"])
def test_blocks_shell_edit_of_state(tmp_path, cmd):
    assert bash(tmp_path, cmd) == 2


def test_allows_reading_state_and_runner(tmp_path):
    assert bash(tmp_path, "cat ghe/runs/r1/state.json") == 0
    assert bash(tmp_path, "python3 ghe/runner/ghe_tool.py status --run ghe/runs/r1/state.json") == 0


def test_lint_hook_does_not_execute_filename(tmp_path):
    (tmp_path / "ghe").mkdir()
    marker = tmp_path / "pwned"
    (tmp_path / "ghe" / "config.yaml").write_text('lint:\n  rules:\n    - glob: "*.js"\n      command: "echo linting {file}"\n')
    evil = f"x$(touch {marker}).js; touch {marker}"
    r = run("post_tool_use_lint.py", {"tool_name": "Write", "tool_input": {"file_path": evil}}, tmp_path)
    assert r.returncode == 0 and not marker.exists()


@pytest.mark.parametrize("cmd", [
    "echo ghe_tool.py; echo x > ghe/runs/r1/state.json",
    "python3 ghe/runner/ghe_tool.py status --run r; sed -i s/a/b/ ghe/runs/r1/state.json",
])
def test_runner_name_does_not_exempt_chained_writes(tmp_path, cmd):
    assert bash(tmp_path, cmd) == 2


def test_plain_runner_call_still_allowed(tmp_path):
    assert bash(tmp_path, "python3 ghe/runner/ghe_tool.py status --run ghe/runs/r1") == 0


# ---- plugin-mode behaviour --------------------------------------------------------------------
@pytest.mark.parametrize("hook,payload", [
    ("ghe_guard.py", {"tool_name": "Bash", "tool_input": {"command": "terraform apply -auto-approve"}}),
    ("ghe_guard.py", {"tool_name": "Write", "tool_input": {"file_path": "ghe/runs/r1/state.json"}}),
    ("security_guard.py", {"tool_name": "Bash", "tool_input": {"command": "git push --force origin main"}}),
])
def test_guards_stay_silent_outside_a_ghe_project(tmp_path, hook, payload):
    """Installed as a plugin, the hooks load in every project; they must not touch non-GHE ones."""
    r = run(hook, payload, tmp_path, ghe_project=False)
    assert r.returncode == 0 and r.stderr == ""


def test_security_guard_still_blocks_inside_a_ghe_project(tmp_path):
    r = run("security_guard.py", {"tool_name": "Bash", "tool_input": {"command": "git push --force origin main"}}, tmp_path)
    assert r.returncode == 2


@pytest.mark.parametrize("cmd", [
    'python3 "/home/u/.claude/plugins/cache/ghe/ghe/1.0.0/ghe/runner/ghe_tool.py" status --run ghe/runs/r1',
    'python3 /home/u/.claude/plugins/cache/ghe/ghe/1.0.0/ghe/runner/ghe_tool.py status --run ghe/runs/r1',
    'python3 "/Users/jo bloggs/.claude/plugins/ghe/ghe/runner/ghe_tool.py" tokens --run ghe/runs/r1 --node a --add 5',
])
def test_plugin_path_runner_call_is_a_plain_runner_call(tmp_path, cmd):
    (tmp_path / "ghe" / "runs" / "r1").mkdir(parents=True)
    assert bash(tmp_path, cmd) == 0


@pytest.mark.parametrize("cmd", [
    'python3 "/p/ghe/runner/ghe_tool.py" status; sed -i s/a/b/ ghe/runs/r1/state.json',
    'python3 /p/ghe/runner/ghe_tool.py status --run ghe/runs/r1/state.json > ghe/runs/r1/state.json',
    'python3 "/p/ghe/runner/ghe_tool.py" status && echo {} > ghe/runs/r1/state.json',
])
def test_plugin_path_does_not_exempt_chained_writes(tmp_path, cmd):
    (tmp_path / "ghe" / "runs" / "r1").mkdir(parents=True)
    assert bash(tmp_path, cmd) == 2
