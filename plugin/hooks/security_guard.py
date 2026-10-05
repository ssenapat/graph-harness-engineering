#!/usr/bin/env python3
"""security_guard.py — PreToolUse deny hook.

Blocks a short, explicit list of destructive/irreversible actions before they
run. Everything not on the list is allowed — this is a narrow guardrail, not
a sandbox. Exit 2 + a stderr message blocks the tool call (Claude sees the
reason); exit 0 allows it. Must never hang and must fail open (allow) if the
hook payload itself can't be parsed, rather than break every tool call.
"""
import json
import os
import re
import sys

DENY_BASH_PATTERNS = [
    (r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f\b.*(\s/(\s|$)|\s~(\s|$)|\s\.\.(\s|$))",
     "rm -rf targeting root, home, or a parent directory"),
    (r"\bgit\s+push\b[^\n]*(--force\b|(?<!--)\s-f\b)",
     "force-push (git push --force / -f)"),
    (r"\bgit\s+reset\s+--hard\b[^\n]*origin/",
     "hard reset onto a remote ref (discards local history)"),
    (r"\bgit\s+clean\s+-[a-zA-Z]*[df][a-zA-Z]*[df]?\b",
     "git clean -fd (irreversible untracked-file deletion)"),
    (r"\bcurl\b[^\n]*\|\s*(sudo\s+)?(bash|sh)\b",
     "piping a remote download straight into a shell"),
    (r"\bwget\b[^\n]*\|\s*(sudo\s+)?(bash|sh)\b",
     "piping a remote download straight into a shell"),
    (r"\bchmod\s+-R\s+777\b", "recursive chmod 777"),
    (r"\bdd\s+if=", "raw disk write via dd"),
    (r":\(\)\s*\{\s*:\s*\|\s*:\s*;\s*\}\s*;\s*:", "fork bomb"),
]

SENSITIVE_PATH_RE = re.compile(
    r"(^|/)\.env(\.[a-zA-Z0-9_-]+)?$"
    r"|(^|/)id_(rsa|ed25519|ecdsa|dsa)$"
    r"|(^|/)\.ssh/"
    r"|(^|/)\.aws/credentials$"
    r"|(^|/)\.netrc$"
)


def deny(reason: str) -> None:
    sys.stderr.write(f"[security_guard] blocked: {reason}\n")
    sys.exit(2)


def main() -> None:
    # Not a GHE project (matters when installed as a plugin): stay out of the way.
    if not os.path.isfile(os.path.join(os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()), "ghe", "config.yaml")):
        sys.exit(0)
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}

    if tool_name == "Bash":
        command = tool_input.get("command", "") or ""
        for pattern, reason in DENY_BASH_PATTERNS:
            if re.search(pattern, command):
                deny(f"{reason} — command: {command!r}")

    if tool_name in ("Read", "Write", "Edit", "MultiEdit"):
        path = tool_input.get("file_path", "") or tool_input.get("path", "") or ""
        if path and SENSITIVE_PATH_RE.search(path):
            deny(f"access to a sensitive credentials/secrets path: {path!r}")

    sys.exit(0)


if __name__ == "__main__":
    main()
