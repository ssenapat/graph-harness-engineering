#!/usr/bin/env python3
"""stop_validate.py — Stop gate hook.

Runs ghe/config.yaml -> validation.commands before letting a session
stop. Ships safe-by-default: if validation.commands is empty (the shipped
default), this is a no-op (exit 0) — a project opts in by filling in real
commands via the `setup` skill. Blocks (exit 2 + stderr reason) only when a
configured command fails, so Claude sees the failure and can fix it before
stopping. Respects `stop_hook_active` to avoid looping on itself.
"""
import json
import os
import subprocess
import sys

CONFIG_PATH = "ghe/config.yaml"


def load_validation_commands():
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
    return (config.get("validation") or {}).get("commands") or []


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    # Avoid an infinite loop if Claude already stopped once because of this
    # same hook.
    if payload.get("stop_hook_active"):
        sys.exit(0)

    commands = load_validation_commands()
    if not commands:
        sys.exit(0)

    failures = []
    for command in commands:
        try:
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True, timeout=1800
            )
        except Exception as exc:
            failures.append(f"'{command}' failed to run: {exc}")
            continue
        if result.returncode != 0:
            tail = (result.stdout + result.stderr).strip().splitlines()[-20:]
            failures.append(f"'{command}' exited {result.returncode}\n" + "\n".join(tail))

    if failures:
        sys.stderr.write(
            "[stop_validate] validation.commands failed — fix before stopping:\n\n"
            + "\n\n".join(failures)
            + "\n"
        )
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
