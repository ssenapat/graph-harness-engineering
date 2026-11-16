#!/usr/bin/env python3
"""PreToolUse guard specific to GHE (spec §13). Defense-in-depth only. Fails closed on matches AND on
unreadable input or protected-action state (T27); it stays silent only in non-GHE projects.

Blocks:
  * Write/Edit of ghe/runs/*/state.json          (only the runner writes it)
  * Write/Edit of an existing hld.md/lld.md/CONTRACT.md without an approved design revision naming it (F19)
  * Write/Edit of ghe/runs/*/artifacts/source.md after discover froze it (hash recorded in state.json)
  * `terraform apply` / `terraform destroy` / destructive AWS CLI calls unless the matching human
    gate (cloud-apply / destructive) is approved in the active run's state.json (the run named by
    GHE_RUN_ID, else the single active run; several active runs and no id = deny)
  * never-override flags: -auto-approve on terraform apply without an approved gate
Exit 2 + stderr message = block (Claude Code hook convention).

Limits: this is a regex safety net against agent mistakes, not a security boundary. Determined
shell tricks (variable-built paths, python -c, ...) can evade it; the runner's own checks and the
human gates remain the real controls. Controlled actions are authorised by the runner
(`ghe_tool.py authorize`); this hook is a second, weaker layer and makes no security-boundary claim.
"""
import glob
import json
import os
import re
import sys

ROOT = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())


def deny(msg):
    sys.stderr.write(f"[ghe_guard] BLOCKED: {msg}\n")
    sys.exit(2)


class Ambiguous(Exception):
    pass


def active_states():
    out = []
    for p in sorted(glob.glob(os.path.join(ROOT, "ghe", "runs", "*", "state.json"))):
        try:
            with open(p, encoding="utf-8") as fh:
                st = json.load(fh)
        except (OSError, ValueError):
            continue
        if st.get("status") in ("RUNNING", "WAITING_HUMAN"):
            out.append(st)
    return out


def active_state():
    """F15: approval identity. Never guess by mtime. Use GHE_RUN_ID if set, else the single
    active run; with several active runs and no id, raise Ambiguous (callers fail closed)."""
    states = active_states()
    want = os.environ.get("GHE_RUN_ID")
    if want:
        for st in states:
            if st.get("run_id") == want:
                return st
        return None
    if len(states) > 1:
        raise Ambiguous(", ".join(str(s.get("run_id")) for s in states))
    return states[0] if states else None


def gate_ok(name):
    try:
        st = active_state()
    except Ambiguous as e:
        deny(f"several active runs ({e}); set GHE_RUN_ID to the run this action belongs to")
    return bool(st and st.get("gates", {}).get(name, {}).get("status") == "approved")


# Match terraform/tofu followed by apply/destroy anywhere in the same shell segment, so global
# flags (-chdir=, -no-color, ...) and wrappers (bash -c '...', env X=1 terraform ...) don't evade it.
TF_APPLY = re.compile(r"\b(?:terraform|tofu)\b[^;&|\n]*?\bapply\b")
TF_DESTROY = re.compile(r"\b(?:terraform|tofu)\b[^;&|\n]*?\bdestroy\b|\b(?:terraform|tofu)\b[^;&|\n]*?\bapply\b[^;&|\n]*-destroy\b")
AWS_DESTRUCTIVE = re.compile(r"\baws\b[^;&|\n]*?\s(?:delete|terminate|remove|deregister|purge|destroy)[\w-]*\b")
# Bash commands that modify files, used to stop shell-level edits of runner-owned files.
SHELL_WRITE = re.compile(r"(?:>|\btee\b|\bsed\s+-\S*i|\bmv\b|\bcp\b|\brm\b|\btruncate\b|\bdd\b|\bpython3?\b|\bperl\b|\bsh\b|\bbash\b)")
# A plain runner call: starts with the runner, no chaining, redirects, substitution or newlines.
# `python3 ghe/runner/ghe_tool.py ...` (toolkit installed in the project) or the same with an absolute
# path to the runner, optionally quoted (toolkit installed as a plugin). No shell metacharacters allowed.
PLAIN_RUNNER_CALL = re.compile(
    r"^\s*python3?\s+(?:\"[^\"\n;&|<>`$()]*/ghe/runner/ghe_tool\.py\"|(?:\./)?(?:[^\s\"';&|<>`$()]*/)?ghe/runner/ghe_tool\.py)"
    r"(?:\s[^;&|<>`$()\n]*)?$")
PROTECTED_IN_CMD = re.compile(r"ghe/runs/[^\s'\"]*(?:state\.json|artifacts/source\.md)")


def norm(path):
    """Resolve .. and symlinks so ghe/runs/x/../x/state.json can't dodge the pattern."""
    p = path if os.path.isabs(path) else os.path.join(ROOT, path)
    return os.path.realpath(p).replace("\\", "/")


def main():
    # Not a GHE project (matters when installed as a plugin): stay out of the way.
    if not os.path.isfile(os.path.join(os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()), "ghe", "config.yaml")):
        sys.exit(0)
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        deny("unparsable hook payload in a GHE project; refusing (fail closed)")
    tool, inp = payload.get("tool_name", ""), payload.get("tool_input", {}) or {}
    if tool in ("Write", "Edit", "MultiEdit"):
        path = norm(inp.get("file_path") or "")
        if re.search(r"ghe/runs/[^/]+/state\.json$", path):
            deny("state.json is owned by the runner (ghe/runner/ghe_tool.py); never edit it directly")
        if re.search(r"ghe/runs/[^/]+/artifacts/source\.md$", path):
            run = re.search(r"(ghe/runs/[^/]+)/", path).group(1)
            sp = os.path.join(ROOT, run, "state.json")
            try:
                with open(sp, encoding="utf-8") as fh:
                    if json.load(fh).get("source_hash"):
                        deny("source.md is frozen after discover (the compaction anchor must stay byte-identical)")
            except (OSError, ValueError):
                deny("run state.json is missing or corrupt; refusing to touch source.md (fail closed)")
        m = re.search(r"(ghe/runs/[^/]+)/artifacts/(hld|lld|CONTRACT)\.md$", path)
        if m:   # F19: an authored design is changed only through an approved design revision
            if os.path.isfile(os.path.join(ROOT, path)) or os.path.isfile(path):
                name = os.path.basename(path)
                try:
                    with open(os.path.join(ROOT, m.group(1), "state.json"), encoding="utf-8") as fh:
                        revs = (json.load(fh).get("design_revisions") or {})
                except (OSError, ValueError):
                    deny("run state.json is missing or corrupt; refusing to touch the design (fail closed)")
                ok = any(isinstance(r, dict) and r.get("status") == "approved"
                         and name in [os.path.basename(f) for f in (r.get("files") or [])]
                         for r in revs.values())
                if not ok:
                    deny(f"{name} is an approved design artifact; propose a change with `design-propose` and "
                         "get it approved with `design-decide` before editing it (F19)")
    elif tool == "Bash":
        cmd = inp.get("command", "")
        if PROTECTED_IN_CMD.search(cmd) and SHELL_WRITE.search(cmd) and not PLAIN_RUNNER_CALL.match(cmd):
            deny("state.json / frozen source.md are runner-owned; do not modify them from the shell (use ghe/runner/ghe_tool.py)")
        if TF_DESTROY.search(cmd) and not gate_ok("destructive"):
            deny("terraform destroy needs the 'destructive' human gate to be approved first")
        if TF_APPLY.search(cmd) and not gate_ok("cloud-apply"):
            deny("terraform apply needs the 'cloud-apply' human gate to be approved first (never use -auto-approve without it)")
        if AWS_DESTRUCTIVE.search(cmd) and not gate_ok("destructive"):
            deny("destructive AWS CLI call needs the 'destructive' human gate to be approved first")
    sys.exit(0)


if __name__ == "__main__":
    main()
