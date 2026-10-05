import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
INIT = os.path.join(os.path.dirname(HERE), "bin", "ghe-init")
WANT = ["ghe/", ".claude/worktrees/", ".claude/settings.local.json"]


def run_init(target):
    return subprocess.run(["bash", INIT, str(target)], capture_output=True, text=True)


def test_init_adds_gitignore_entries_idempotently(tmp_path):
    (tmp_path / ".gitignore").write_text("node_modules/")  # no trailing newline
    assert run_init(tmp_path).returncode == 0
    run_init(tmp_path)
    lines = (tmp_path / ".gitignore").read_text().splitlines()
    assert lines[0] == "node_modules/"
    for p in WANT:
        assert lines.count(p) == 1


def test_init_creates_gitignore_and_warns_if_tracked(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "ghe").mkdir()
    (tmp_path / "ghe" / "x.txt").write_text("x")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-f", "ghe/x.txt"], check=True)
    r = run_init(tmp_path)
    assert "git rm -r --cached ghe" in r.stdout
    assert all(p in (tmp_path / ".gitignore").read_text() for p in WANT)
