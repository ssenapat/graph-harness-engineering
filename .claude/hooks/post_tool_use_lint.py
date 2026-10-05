#!/usr/bin/env python3
"""post_tool_use_lint.py — PostToolUse advisory hook.

Runs ghe/config.yaml -> lint.rules against the file that Edit/Write/
MultiEdit just touched, and prints the result. Advisory ONLY: this hook
always exits 0, never blocks, regardless of lint findings. If PyYAML isn't
available, or config.yaml/lint.rules doesn't exist yet, it exits 0 silently
(ships as a no-op until a project's lint.rules is filled in by `/setup`).
"""
import fnmatch
import json
import os
import shlex
import subprocess
import sys

CONFIG_PATH = os.path.join(os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()), "ghe", "config.yaml")


def load_lint_rules():
    try:
        import yaml  # type: ignore
    except ImportError:
        return []
    if not os.path.isfile(CONFIG_PATH):
        return []
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            config = yaml.safe_load(fh) or {}
    except Exception:
        return []
    return (config.get("lint") or {}).get("rules") or []


def changed_file_path(payload) -> str:
    tool_input = payload.get("tool_input", {}) or {}
    return tool_input.get("file_path", "") or tool_input.get("path", "") or ""


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    file_path = changed_file_path(payload)
    if not file_path:
        sys.exit(0)

    for rule in load_lint_rules():
        glob = rule.get("glob", "")
        command_tpl = rule.get("command", "")
        if not glob or not command_tpl:
            continue
        if not (fnmatch.fnmatch(os.path.basename(file_path), glob)
                or fnmatch.fnmatch(file_path, glob)):
            continue
        # The path is agent-controlled: quote it so it can never be parsed as shell syntax.
        command = command_tpl.replace("{file}", shlex.quote(file_path))
        try:
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True, timeout=60
            )
        except Exception as exc:
            print(f"[post_tool_use_lint] '{command}' failed to run: {exc}")
            continue
        if result.returncode != 0:
            print(f"[post_tool_use_lint] advisory: '{command}' exited {result.returncode}")
            if result.stdout.strip():
                print(result.stdout.strip())
            if result.stderr.strip():
                print(result.stderr.strip())

    # Always exit 0 — advisory only, never blocks the agent.
    sys.exit(0)


if __name__ == "__main__":
    main()
