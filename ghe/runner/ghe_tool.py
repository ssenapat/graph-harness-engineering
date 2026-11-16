#!/usr/bin/env python3
"""ghe_tool.py — deterministic helpers for graph-harness-engineering (spec §8-§11).

Everything the spec says must be "enforced by the runner, not the LLM" lives here:
roster resolution, DAG validation, node-result validation, state/ledger, budgets,
story waves + file-ownership overlap, defect fingerprints + no-progress detection,
context-compaction drift checks, and the traceability matrix.

Every command prints ONE JSON object on stdout and exits 0 (ok) / 1 (check failed) / 2 (usage/error).
Stdlib + PyYAML only. Never calls an LLM.

Usage:  python3 ghe/runner/ghe_tool.py <command> [--root DIR] [--run DIR] [options]
"""
import argparse
import contextlib
import datetime
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

try:
    import yaml
except ImportError:  # pragma: no cover
    print(json.dumps({"ok": False, "error": "PyYAML is required: pip install pyyaml"}))
    sys.exit(2)

REQ_RE = re.compile(r"\bREQ-\d{3}\b")
NODE_STATUSES = {"done", "blocked", "needs_human", "failed"}
ALWAYS_ON = {"discover", "final-report"}
ANCHOR_BEGIN = "<!-- ANCHOR:BEGIN -->"
ANCHOR_END = "<!-- ANCHOR:END -->"


# ----------------------------------------------------------------- helpers
def out(obj, code=0):
    print(json.dumps(obj, indent=2, sort_keys=False))
    sys.exit(code)


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha(text):
    if isinstance(text, str):
        text = text.encode("utf-8")
    return hashlib.sha256(text).hexdigest()


def read(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return default


def load_yaml(path):
    text = read(path)
    if text is None:
        out({"ok": False, "error": f"missing file: {path}"}, 2)
    try:
        return yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        out({"ok": False, "error": f"{path} is not valid YAML: {str(exc).splitlines()[0] if str(exc) else exc}"}, 2)


def load_json(path, default=None):
    text = read(path)
    if text is None:
        return default
    try:
        return json.loads(text)
    except ValueError as exc:
        out({"ok": False, "error": f"invalid JSON in {path}: {exc}"}, 2)


def _replace_with_retry(src, dst, tries=5):
    """os.replace is atomic everywhere; on Windows it can briefly raise PermissionError if dst is open."""
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.02 * (i + 1))


def atomic_write_json(path, obj):
    """Write to a temp file in the same directory, flush+fsync, then os.replace: never a half-written file."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        _replace_with_retry(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def atomic_write_text(path, text):
    """Same guarantee as atomic_write_json for any text artifact (YAML, Markdown): temp + fsync + replace."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        _replace_with_retry(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def write_json(path, obj):
    atomic_write_json(path, obj)


def _pid_alive(pid):
    if os.name == "nt":  # no cheap, reliable check: rely on lock age only
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _lock_info(path):
    try:
        with open(path, encoding="utf-8") as fh:
            pid, ts = fh.read().split()
        return int(pid), float(ts)
    except (OSError, ValueError):
        return None, None


def _lock_is_stale(path, stale_after):
    pid, ts = _lock_info(path)
    if pid is None:  # unreadable/garbled: judge by file age only
        try:
            return time.time() - os.path.getmtime(path) > stale_after
        except OSError:
            return False
    return (not _pid_alive(pid)) or (time.time() - ts > stale_after)


@contextlib.contextmanager
def run_lock(run_dir):
    """Serialise every command that touches one run (cross-platform: O_CREAT|O_EXCL lock file).
    The lock holds `pid timestamp`; a lock whose owner is dead, or older than GHE_LOCK_STALE seconds
    (default 60), is broken under a guard file so two waiters cannot both break it."""
    timeout = float(os.environ.get("GHE_LOCK_TIMEOUT", "30"))
    stale_after = float(os.environ.get("GHE_LOCK_STALE", "60"))
    path = os.path.join(run_dir, ".lock")
    guard = path + ".break"
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _lock_is_stale(path, stale_after):
                try:
                    gfd = os.open(guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                except FileExistsError:
                    if _lock_is_stale(guard, stale_after):
                        with contextlib.suppress(OSError):
                            os.remove(guard)
                else:
                    try:
                        os.close(gfd)
                        if _lock_is_stale(path, stale_after):  # re-check while holding the guard
                            with contextlib.suppress(OSError):
                                os.remove(path)
                    finally:
                        with contextlib.suppress(OSError):
                            os.remove(guard)
                continue
            if time.time() >= deadline:
                out({"ok": False, "error": "run is locked by another command (waited "
                     f"{timeout:g}s); if no ghe command is running, delete {path}"}, 1)
            time.sleep(0.05)
            continue
        try:
            os.write(fd, f"{os.getpid()} {time.time()}".encode())
        finally:
            os.close(fd)
        break
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            os.remove(path)


def frontmatter(text):
    m = re.match(r"^---\n(.*?)\n---\n", text or "", re.S)
    return (yaml.safe_load(m.group(1)) or {}) if m else {}


class Ctx:
    def __init__(self, args):
        self.root = os.path.abspath(args.root or os.getcwd())
        self.run = os.path.abspath(args.run) if getattr(args, "run", None) else None
        cfg_path = os.path.join(self.root, "ghe", "config.yaml")
        graph_path = os.path.join(self.root, "ghe", "graph.yaml")
        if getattr(args, "cmd", None) in ("bootstrap", "repos"):  # bootstrap creates these files; repos runs before setup
            self.config, self.graph = {}, {}
            return
        missing = [p for p in (cfg_path, graph_path) if not os.path.isfile(p)]
        if missing:
            out({"ok": False, "error": "GHE is not set up in this project (missing: "
                 + ", ".join(os.path.relpath(p, self.root) for p in missing)
                 + "). Run /ghe-setup (/ghe:setup as a plugin) first."}, 2)
        self.config = load_yaml(cfg_path)
        self.graph = load_yaml(graph_path)

    @property
    def state_path(self):
        return os.path.join(self.run, "state.json")

    def art(self, *p):
        return os.path.join(self.run, "artifacts", *p)

    def state(self):
        st = load_json(self.state_path)
        if st is None:
            out({"ok": False, "error": f"no state.json in {self.run} (run `init` first)"}, 2)
        return st

    def save(self, st):
        write_json(self.state_path, st)


# ----------------------------------------------------------------- init
def jira_config_problems(config):
    """Return a list of problems if tracker is jira but the jira block is still placeholders/invalid."""
    if config.get("tracker") != "jira":
        return []
    j = config.get("jira") or {}
    site, proj = str(j.get("site") or ""), str(j.get("project") or "")
    problems = []
    if not re.match(r"^https://[A-Za-z0-9.-]+\.atlassian\.net/?$", site) or "<" in site:
        problems.append(f"jira.site is not set ({site or 'empty'})")
    if not re.match(r"^[A-Z][A-Z0-9]+$", proj):
        problems.append(f"jira.project is not a valid project key ({proj or 'empty'})")
    return problems


RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def refuse(msg, code=1, **kw):
    out({"ok": False, "error": msg, **kw}, code)


def cmd_init(a, c):
    if not c.config or not c.graph:
        out({"ok": False, "error": "GHE is not set up in this project (ghe/config.yaml or ghe/graph.yaml is missing). "
             "Run /ghe-setup (or /ghe:setup when installed as a plugin) first."}, 2)
    bad = jira_config_problems(c.config)
    if bad:
        out({"ok": False, "error": "tracker is jira but ghe/config.yaml is not configured: " + "; ".join(bad)
             + ". Run /ghe-setup (/ghe:setup as a plugin) or set tracker: local."}, 2)
    if a.resume is not None and a.run_id is not None:
        refuse("use either --run-id or --resume, not both", 2)
    runs = os.path.join(c.root, "ghe", "runs")
    os.makedirs(runs, exist_ok=True)
    if a.resume is not None:  # read-only: validate and report, never write
        if not RUN_ID_RE.match(a.resume):
            refuse(f"invalid run id {a.resume!r} (letters, digits, . _ - ; max 64; must start alphanumeric)")
        run = os.path.join(runs, a.resume)
        st = load_json(os.path.join(run, "state.json"))
        if not isinstance(st, dict):
            refuse(f"no resumable run {a.resume!r} (state.json missing or unreadable)")
        res = {"ok": True, "run_id": a.resume, "run_dir": run, "status": st.get("status"), "resumed": True}
        note = config_notice(c)
        out({**res, "config_update": note} if note else res)
    if a.run_id is not None:
        if not RUN_ID_RE.match(a.run_id):
            refuse(f"invalid run id {a.run_id!r} (letters, digits, . _ - ; max 64; must start alphanumeric)")
        run_id = a.run_id
        run = os.path.join(runs, run_id)
        try:
            os.makedirs(run, exist_ok=False)
        except FileExistsError:
            refuse(f"run {run_id} already exists; pass --resume {run_id} to continue it", run_id=run_id)
    else:  # auto id: max existing suffix + 1, claimed atomically by mkdir (safe under concurrent inits)
        day = datetime.date.today().isoformat()
        pat = re.compile(rf"^ghe-{day}-(\d+)$")
        for _ in range(200):
            used = [int(m.group(1)) for d in os.listdir(runs) for m in [pat.match(d)] if m]
            run_id = f"ghe-{day}-{1 + max(used, default=0):03d}"
            run = os.path.join(runs, run_id)
            try:
                os.makedirs(run, exist_ok=False)
                break
            except FileExistsError:
                continue
        else:
            refuse("could not allocate a unique run id", 2)
    for sub in ("artifacts", "artifacts/plans", "artifacts/defects", "nodes"):
        os.makedirs(os.path.join(run, sub), exist_ok=True)
    st = {"run_id": run_id, "status": "PENDING", "created": now(), "source_hash": None,
          "tokens_total": 0, "round": 0, "nodes": {}, "gates": {}, "compactions": [],
          "fingerprints": {}, "warnings": []}
    write_json(os.path.join(run, "state.json"), st)
    res = {"ok": True, "run_id": run_id, "run_dir": run}
    note = config_notice(c)
    out({**res, "config_update": note} if note else res)


GITIGNORE_LINES = ("ghe/", ".claude/worktrees/", ".claude/settings.local.json")


def cmd_bootstrap(a, c):
    """One-time, idempotent project setup used by /ghe-setup: copy default ghe/config.yaml and
    ghe/graph.yaml (never overwrites), create ghe/runs and ghe/memory, add GHE lines to .gitignore."""
    toolkit = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # <toolkit>/ghe
    gdir = os.path.join(c.root, "ghe")
    created, kept = [], []
    os.makedirs(os.path.join(gdir, "runs"), exist_ok=True)
    os.makedirs(os.path.join(gdir, "memory"), exist_ok=True)
    for name in ("config.yaml", "graph.yaml"):
        dst = os.path.join(gdir, name)
        if os.path.exists(dst):
            kept.append(f"ghe/{name}")
            continue
        src = next((p for p in (os.path.join(toolkit, "defaults", name), os.path.join(toolkit, name))
                    if os.path.isfile(p)), None)
        if src is None:
            out({"ok": False, "error": f"no default {name} found next to the runner"}, 2)
        shutil.copyfile(src, dst)
        created.append(f"ghe/{name}")
    for name in ("decisions.md", "patterns.md"):
        path = os.path.join(gdir, "memory", name)
        if not os.path.exists(path):
            open(path, "a", encoding="utf-8").close()
    gi = os.path.join(c.root, ".gitignore")
    text = read(gi) or ""
    have = {ln.strip() for ln in text.splitlines()}
    missing = [ln for ln in GITIGNORE_LINES if not ({ln, ln.rstrip("/"), "/" + ln, "/" + ln.rstrip("/")} & have)]
    if getattr(a, "workspace", False) and not os.path.exists(os.path.join(c.root, ".git")):
        missing = []  # a plain workspace folder (not a repo): never drop a .gitignore into it
    if missing:
        block = ("" if not text or text.endswith("\n") else "\n") + "# graph-harness-engineering (local only)\n" + "\n".join(missing) + "\n"
        with open(gi, "a", encoding="utf-8") as fh:
            fh.write(block)
    out({"ok": True, "created": created, "kept": kept, "gitignore_added": missing})


# ----------------------------------------------------------------- roster resolution (spec §5.3)
def cmd_resolve(a, c):
    src = read(os.path.join(c.run, "artifacts", "source.md"))
    if src is None:
        out({"ok": False, "error": "artifacts/source.md not found (discover has not run)"}, 2)
    fm = frontmatter(src)
    g, cfg = c.graph, c.config
    rules = g.get("roster_rules", {})
    roster = list(fm.get("roster") or rules.get("default_roster") or cfg["defaults"]["roster"])
    if fm.get("reviewer") or "reviewer" in roster:
        has_reviewer = True
        roster = [r for r in roster if r != "reviewer"]
    else:
        has_reviewer = bool(cfg.get("gates", {}).get("reviewer"))
    mode = fm.get("mode", "new")
    target = fm.get("deploy_target", cfg["defaults"]["deploy_target"])
    tags = set(fm.get("tags") or [])
    warnings, drop = [], set()

    def reject(reason):
        out({"ok": False, "rejected": True, "reason": reason}, 1)

    for agent_name in rules.get("default_roster", []):
        if agent_name in roster:
            continue
        rule = rules.get("omit", {}).get(agent_name, {})
        if rule.get("reject"):
            reject(rule.get("reason", f"{agent_name} is required"))
        if agent_name == "architect" and rule.get("only_if_tag") and rule["only_if_tag"] not in tags:
            reject(f"Architect omitted but ticket is not tagged '{rule['only_if_tag']}' (no contract => unsafe parallel build)")
        if agent_name == "ba":
            spec = read(c.art("requirement-spec.md"))
            if not spec or not REQ_RE.search(spec):
                reject("BA omitted but no valid requirement-spec.md (needs REQ-### IDs) was supplied")
        drop.update(rule.get("drop_nodes", []))
        if rule.get("warn"):
            warnings.append(rule["warn"])
    m = g.get("modes", {}).get(mode, {})
    drop.update(m.get("drop_nodes", []))
    if mode == "maintain" and "design-impact" not in tags:
        drop.add("architect")
    if not has_reviewer:
        drop.add("review-gate")
    if target != "cloud" and "devops-prep" not in tags:
        drop.add("devops-prep")
    nodes = {n: dict(v) for n, v in g["nodes"].items() if n not in drop}
    dropped = {n: g["nodes"][n].get("needs", []) for n in drop if n in g["nodes"]}

    def expand(deps):
        res = []
        for d in deps:
            if d in nodes:
                res.append(d)
            elif d in dropped:
                res.extend(expand(dropped[d]))
        seen = []
        for d in res:
            if d not in seen:
                seen.append(d)
        return seen

    for n, v in nodes.items():
        v["needs"] = expand(v.get("needs", []))
    # Reviewer gate sits between architect and every node that consumed the design.
    if "review-gate" in nodes:
        for n, v in nodes.items():
            if n != "review-gate" and "architect" in v["needs"]:
                v["needs"] = [("review-gate" if d == "architect" else d) for d in v["needs"]]
    # Transitive reduction: drop a dependency already implied by another dependency.
    def ancestors(n, acc=None):
        acc = set() if acc is None else acc
        for d in nodes[n]["needs"]:
            if d not in acc:
                acc.add(d)
                ancestors(d, acc)
        return acc
    for n, v in nodes.items():
        v["needs"] = [d for d in v["needs"] if not any(d in ancestors(o) for o in v["needs"] if o != d)]
    # DAG validation + layering
    depth, visiting = {}, set()

    def dfs(n):
        if n in depth:
            return depth[n]
        if n in visiting:
            reject(f"cycle detected at node {n}")
        visiting.add(n)
        depth[n] = 1 + max([dfs(d) for d in nodes[n]["needs"]], default=-1)
        visiting.discard(n)
        return depth[n]

    for n in nodes:
        dfs(n)
    sinks = [n for n in nodes if not any(n in v["needs"] for v in nodes.values())]
    if sinks != ["final-report"]:
        reject(f"graph must have a unique terminal node 'final-report', got {sinks}")
    if not any(v.get("agent") == "builder" for v in nodes.values()):
        reject("no builder node in resolved graph")
    layers = {}
    for n, d in depth.items():
        layers.setdefault(d, []).append(n)
    resolved = {"run_id": c.state().get("run_id"), "mode": mode, "deploy_target": target,
                "roster": roster, "reviewer": has_reviewer, "warnings": warnings,
                "nodes": nodes, "layers": [layers[k] for k in sorted(layers)],
                "loop": g.get("loop", {}) if "test" in nodes and "builder" in nodes else None}
    atomic_write_text(os.path.join(c.run, "graph.resolved.yaml"), yaml.safe_dump(resolved, sort_keys=False))
    bad_budget = config_budget_problems(c) + memory_config_problems(c.config)
    if bad_budget:  # F08: a cap that can never apply is a configuration error, not a silent no-op
        reject("invalid budgets in ghe/config.yaml: " + "; ".join(bad_budget))
    st = c.state()
    st["status"] = "RUNNING"
    st["warnings"] = warnings
    for n in nodes:
        st["nodes"].setdefault(n, {"status": "pending", "attempts": 0, "tokens": 0, "output_hash": None})
    c.save(st)
    out({"ok": True, **resolved})


# ----------------------------------------------------------------- node lifecycle
def safe_artifact(c, rel):
    """Absolute path of artifact `rel` if it is a relative path that stays inside RUN/artifacts after
    resolving symlinks; otherwise None (traversal, absolute path, or symlink escape)."""
    if not isinstance(rel, str) or not rel or os.path.isabs(rel) or rel.startswith(("/", "\\")):
        return None
    base = os.path.realpath(c.art())
    p = os.path.realpath(os.path.join(base, rel))
    return p if p == base or p.startswith(base + os.sep) else None


def node_artifact_hash(c, artifacts, result_text=""):
    h = hashlib.sha256()
    h.update((result_text or "").encode())
    for rel in sorted(str(x) for x in artifacts):
        h.update(rel.encode())
        p = safe_artifact(c, rel)
        h.update(((read(p, "") if p else "") or "").encode())
    return h.hexdigest()


TERMINAL = ("DONE", "FAILED", "CANCELLED")


def resolved_nodes(c):
    return (load_yaml(os.path.join(c.run, "graph.resolved.yaml")) or {}).get("nodes", {}) \
        if os.path.isfile(os.path.join(c.run, "graph.resolved.yaml")) else {}


def ancestors(nodes, name, seen=None):
    seen = set() if seen is None else seen
    for d in (nodes.get(name) or {}).get("needs", []):
        if d not in seen:
            seen.add(d)
            ancestors(nodes, d, seen)
    return seen


def config_budget_problems(c):
    """Validate the retry / round limits. Token usage is recorded but never capped."""
    problems = []
    for k in ("node_retry", "max_rounds", "max_story_revisions", "max_gate_revisions"):
        v = c.config["budgets"].get(k)
        if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < (0 if k != "max_rounds" else 1)):
            problems.append(f"budgets.{k} must be a {'positive' if k == 'max_rounds' else 'non-negative'} integer")
    if c.config["budgets"].get("correction_scope", "story") not in ("story", "epic_global"):
        problems.append("budgets.correction_scope must be 'story' or 'epic_global'")
    return problems


def ingest_usage(st, node, amount, event):
    """Exactly-once usage ingestion (F06/F07/F09). Returns None, or an error string."""
    if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
        return f"token usage must be a non-negative integer, got {amount!r}"
    if not event:
        return "a usage event id is required (--event) so usage is counted exactly once"
    seen = st.setdefault("usage_events", {})
    if event in seen:
        if seen[event] != {"node": node, "tokens": amount}:
            return f"usage event {event} was already recorded with different values"
        return "duplicate"
    seen[event] = {"node": node, "tokens": amount}
    st["nodes"][node]["tokens"] += amount
    st["tokens_total"] += amount
    return None


def cmd_start(a, c):
    st = c.state()
    n = st["nodes"].get(a.node)
    if n is None:
        out({"ok": False, "error": f"unknown node {a.node}"}, 2)
    cap = 1 + c.config["budgets"].get("node_retry", 1)
    if n["attempts"] >= cap:
        out({"ok": False, "error": f"node {a.node} exhausted {cap} attempts", "action": "escalate"}, 1)
    if st["status"] in TERMINAL:
        refuse(f"run is {st['status']}; no node can start")
    waiting_gates = [k for k, g in st["gates"].items() if g.get("status") == "waiting"]
    if st["status"] == "WAITING_HUMAN" or waiting_gates:
        refuse("run is waiting for a human decision" + (f" (gate: {', '.join(waiting_gates)})" if waiting_gates else "")
               + "; answer it (gate-decide / resolve-wait) before starting a node")
    if n["status"] not in ("pending", "failed"):
        refuse(f"node {a.node} is {n['status']}; only a pending or failed node can be started"
               + (" (use resolve-wait --outcome rerun)" if n["status"] == "waiting_human" else ""))
    unmet = [d for d in (resolved_nodes(c).get(a.node) or {}).get("needs", [])
             if st["nodes"].get(d, {}).get("status") not in ("done", "skipped")]
    if unmet:
        refuse(f"node {a.node} has unmet dependencies: {', '.join(unmet)}", unmet=unmet)
    # T04: stale / unapproved work. The frozen source must be unchanged and no design change may be pending.
    if st.get("source_hash") and os.path.isfile(c.art("source.md")) and sha(read(c.art("source.md"), "")) != st["source_hash"]:
        refuse("source.md changed after discover (hash mismatch); work built on it is stale", action="escalate")
    pending_rev = sorted(k for k, r in (st.get("design_revisions") or {}).items()
                         if isinstance(r, dict) and r.get("status") == "proposed")
    if pending_rev:
        refuse(f"design revision(s) awaiting approval: {', '.join(pending_rev)}; decide them (design-decide) first")
    n["attempts"] += 1
    n["status"] = "running"
    n["started"] = now()
    st["status"] = "RUNNING"
    c.save(st)
    out({"ok": True, "node": a.node, "attempt": n["attempts"], "max_attempts": cap})


def cmd_result(a, c):
    """Validate nodes/<node>/node-result.json and update state."""
    st = c.state()
    n = st["nodes"].get(a.node)
    if n is None:
        out({"ok": False, "error": f"unknown node {a.node}"}, 2)
    path = os.path.join(c.run, "nodes", a.node, "node-result.json")
    digest = sha(read(path, "") or "")
    if n["status"] != "running":
        prior = n.get("result")
        if prior and prior.get("digest") == digest:  # same result again: report it, change nothing
            out({**prior["response"], "idempotent": True}, prior["rc"])
        if prior:
            refuse(f"node {a.node} already has a recorded result ({n['status']}); a different result is refused")
        refuse(f"node {a.node} is {n['status']}, not running; run `start` first")
    res = load_json(path)
    problems = []
    if res is None:
        problems.append("node-result.json missing")
        res = {}
    for k in ("node", "status", "artifacts", "exit_criteria", "summary"):
        if k not in res:
            problems.append(f"missing key: {k}")
    if res.get("node") not in (None, a.node):
        problems.append(f"node mismatch: {res.get('node')} != {a.node}")
    if res.get("status") not in NODE_STATUSES:
        problems.append(f"bad status: {res.get('status')}")
    if res.get("status") == "done":
        for k, v in (res.get("exit_criteria") or {}).items():
            if v is not True:
                problems.append(f"exit criterion not met: {k}")
        if not res.get("exit_criteria"):
            problems.append("exit_criteria is empty")
        ec = res.get("exit_criteria")
        for need in (resolved_nodes(c).get(a.node) or {}).get("criteria") or []:  # T06: graph-required criteria
            if not isinstance(ec, dict) or need not in ec:
                problems.append(f"required exit criterion missing: {need} (graph criteria)")
    arts = res.get("artifacts")
    if arts is not None and not isinstance(arts, list):
        problems.append("artifacts must be a list")
        arts, res["artifacts"] = [], []
    for rel in arts or []:
        p = safe_artifact(c, rel)
        if p is None:
            problems.append(f"artifact path not allowed (must stay inside artifacts/): {rel}")
        elif res.get("status") == "done" and not os.path.exists(p):
            problems.append(f"artifact missing: {rel}")
    if res.get("status") == "done" and isinstance(arts, list) and not arts:
        problems.append("artifacts is empty for a done node")
    if res.get("status") == "done" and isinstance(arts, list):  # T07: graph-declared outputs are mandatory
        have = {os.path.normpath(x) for x in arts if isinstance(x, str)}
        for need in (resolved_nodes(c).get(a.node) or {}).get("produces") or []:
            if os.path.normpath(need) not in have:
                problems.append(f"required artifact not listed: {need} (graph produces)")
    unchanged = False
    new_hash = node_artifact_hash(c, res.get("artifacts") or [], read(path, ""))
    if not problems and n.get("output_hash") and n["output_hash"] == new_hash and n["attempts"] > 1:
        unchanged = True
        problems.append("output identical to previous attempt (spec §9.3): not retrying unchanged")
    try:
        tokens = int(a.tokens or 0)
    except (TypeError, ValueError):
        tokens = -1
    err = ingest_usage(st, a.node, tokens, f"result:{a.node}:{n['attempts']}")
    if err and err != "duplicate":
        refuse(err, 2)
    n["ended"] = now()
    n["output_hash"] = new_hash
    if problems:
        n["status"] = "failed"
        n["problems"] = problems
    else:
        n["status"] = {"done": "done", "blocked": "blocked", "needs_human": "waiting_human",
                       "failed": "failed"}[res["status"]]
        n["tickets"] = res.get("tickets", [])
        n["open_questions"] = res.get("open_questions", [])
    if a.node == "discover" and n["status"] == "done":
        st["source_hash"] = sha(read(c.art("source.md"), ""))
    retry_allowed = n["status"] == "failed" and not unchanged and n["attempts"] < 1 + c.config["budgets"].get("node_retry", 1)
    if n["status"] in ("waiting_human",) or (n["status"] == "failed" and not retry_allowed) or n["status"] == "blocked":
        st["status"] = "WAITING_HUMAN"
    response = {"ok": not problems, "node_status": n["status"], "problems": problems,
                "retry_allowed": retry_allowed, "open_questions": n.get("open_questions", []),
                "tickets": n.get("tickets", [])}
    rc = 0 if not problems else 1
    n["result"] = {"digest": digest, "rc": rc, "response": response}
    c.save(st)
    out(response, rc)


def cmd_resolve_wait(a, c):
    """A human answered a node's open question: leave waiting_human without burning a retry.
    --outcome done  : the node's work stands (answer only unblocked it) -> done
    --outcome rerun : the node must run again -> pending, the waiting attempt is refunded."""
    st = c.state()
    n = st["nodes"].get(a.node)
    if n is None:
        out({"ok": False, "error": f"unknown node {a.node}"}, 2)
    if n["status"] != "waiting_human":
        out({"ok": False, "error": f"node {a.node} is {n['status']}, not waiting_human"}, 1)
    by = (getattr(a, "by", "") or "").strip()
    if not by:
        out({"ok": False, "error": "resolve-wait needs --by <who answered> (approver identity)"}, 1)
    if not (a.answer or "").strip():
        out({"ok": False, "error": "resolve-wait needs a non-empty --answer"}, 1)
    if a.outcome == "done":  # revalidate: the node must still have a recorded, non-failed result
        r = n.get("result") or {}
        if not r.get("digest") or n.get("status") == "failed":
            out({"ok": False, "error": f"node {a.node} has no recorded valid result; use --outcome rerun"}, 1)
    n.setdefault("human_answers", []).append({"at": now(), "by": by, "answer": a.answer or "",
                                               "outcome": a.outcome})
    n["resolved_questions"] = n.get("open_questions", [])
    n["open_questions"] = []
    if a.outcome == "rerun":
        n["status"] = "pending"
        n["attempts"] = max(0, n["attempts"] - 1)
    else:
        n["status"] = "done"
        n["ended"] = now()
    still = [k for k, v in st["nodes"].items() if v["status"] == "waiting_human"]
    pending_gate = any(g.get("status") == "waiting" for g in st["gates"].values())
    if not still and not pending_gate and st["status"] == "WAITING_HUMAN":
        st["status"] = "RUNNING"
    c.save(st)
    out({"ok": True, "node": a.node, "node_status": n["status"], "attempts": n["attempts"],
         "run_status": st["status"], "still_waiting": still})


def cmd_skip(a, c):
    st = c.state()
    n = st["nodes"].get(a.node)
    if n is None:
        out({"ok": False, "error": f"unknown node {a.node}"}, 2)
    if not (a.reason or "").strip():
        refuse("skip requires --reason")
    if st["status"] in TERMINAL:
        refuse(f"run is {st['status']}")
    if not (resolved_nodes(c).get(a.node) or {}).get("optional"):
        refuse(f"node {a.node} is not optional and cannot be skipped")
    if n["status"] != "pending":
        refuse(f"node {a.node} is {n['status']}; only a pending node can be skipped")
    n["status"] = "skipped"
    n["skipped_reason"] = a.reason.strip()
    n["ended"] = now()
    c.save(st)
    out({"ok": True, "node": a.node, "node_status": "skipped", "reason": n["skipped_reason"]})


def cmd_status(a, c):
    st = c.state()
    res = {"ok": True, "run_id": st["run_id"], "status": st["status"], "round": st["round"],
           "tokens_total": st["tokens_total"], "gates": st["gates"],
           "nodes": {k: v["status"] for k, v in st["nodes"].items()},
           "pending_gate": next((k for k, v in st["gates"].items() if v.get("status") == "waiting"), None)}
    note = config_notice(c)
    out({**res, "config_update": note} if note else res)


def finish_problems(c, st):
    """Why the run cannot be DONE yet (spec §9: DONE means executed nodes done, no trace gaps,
    last test round clean, RUN-REPORT.md written)."""
    problems = []
    for k, v in st["nodes"].items():
        if v["status"] not in ("done", "skipped"):
            problems.append(f"node {k} is {v['status']}")
    for k, g in st["gates"].items():
        if g.get("status") == "waiting":
            problems.append(f"gate {k} is waiting for a human")
    for k in unresolved_intents(c):  # F13: an unreconciled side effect means the real state is unknown
        problems.append(f"side-effect intent {k} is unresolved (journal-reconcile it)")
    if st.get("tracker_unavailable"):   # T39: no silent local authority switch
        problems.append("tracker is unavailable (tracker-resume, or tracker-switch --by ...)")
    if not os.path.isfile(c.art("RUN-REPORT.md")):
        problems.append("RUN-REPORT.md has not been written")
    # F23: no Tester is not automatically "tested": require runner-executed verification or a recorded waiver
    if (st["nodes"].get("test") or {}).get("status") != "done":
        v = st.get("verification") or {}
        if v.get("status") not in ("passed", "waived"):
            problems.append("no Tester ran and no deterministic verification passed or was waived "
                            "(run `verify`, or `verify --waive --reason ... --by ...`)")
    gaps = trace_compute(c)[1]
    problems += [f"traceability gap: {g}" for g in gaps]
    if st.get("round", 0) > 0:
        last = load_json(c.art("defects", f"round-{st['round']}.json"), {}) or {}
        if last.get("defects"):
            problems.append(f"last test round ({st['round']}) still has {len(last['defects'])} failing defect(s)")
    return problems


def memory_config_problems(cfg):
    m = cfg.get("memory") or {}
    return [f"memory.{k} must be a positive integer" for k in ("max_entries", "max_chars")
            if k in m and (not isinstance(m[k], int) or isinstance(m[k], bool) or m[k] < 1)]


def memory_entries(root):
    """Append-only memory files -> entries. An entry starts at a `## ` heading (else paragraphs).
    `Supersedes: <heading text>` marks an older entry as no longer applicable (history is kept)."""
    entries = []
    for name in ("decisions.md", "patterns.md"):
        try:
            text = open(os.path.join(root, "ghe", "memory", name), encoding="utf-8").read()
        except OSError:
            continue
        blocks = re.split(r"(?m)^(?=## )", text) if re.search(r"(?m)^## ", text) else re.split(r"\n\s*\n", text)
        for b in blocks:
            b = b.strip()
            if not b or b.startswith("# "):
                continue
            head = b.splitlines()[0].lstrip("# ").strip()
            sup = [x.strip().lower() for x in re.findall(r"(?im)^\**supersedes:?\**\s*(.+)$", b)]
            entries.append({"file": name, "title": head, "text": b, "supersedes": sup, "order": len(entries)})
    return entries


def cmd_memory_manifest(a, c):
    """F22: runner-built, bounded, relevance-scoped memory packet. Never loads memory wholesale and never
    truncates silently: if the most relevant entry alone exceeds max_chars the command refuses (split/pause)."""
    m = c.config.get("memory") or {}
    max_entries, max_chars = m.get("max_entries", 12), m.get("max_chars", 6000)
    problems = memory_config_problems(c.config)
    if problems:
        refuse("invalid memory config", problems=problems)
    entries = memory_entries(c.root)
    superseded = {t for e in entries for t in e["supersedes"]}
    words = set(re.findall(r"[a-z0-9]{3,}", (a.query or "").lower()))
    scored, excluded = [], []
    for e in entries:
        if e["title"].lower() in superseded:
            excluded.append({"title": e["title"], "reason": "superseded"})
            continue
        overlap = len(words & set(re.findall(r"[a-z0-9]{3,}", e["text"].lower())))
        if words and overlap == 0:
            excluded.append({"title": e["title"], "reason": "not relevant to the request"})
            continue
        scored.append((overlap, e["order"], e))
    scored.sort(key=lambda x: (-x[0], -x[1]))   # most relevant first, newest first on ties
    chosen, used = [], 0
    for overlap, _, e in scored:
        if len(chosen) >= max_entries:
            excluded.append({"title": e["title"], "reason": "entry budget"})
        elif used + len(e["text"]) > max_chars:
            if not chosen:
                refuse(f"essential memory entry '{e['title']}' ({len(e['text'])} chars) exceeds memory.max_chars="
                       f"{max_chars}; split the entry or raise the budget (no silent truncation)")
            excluded.append({"title": e["title"], "reason": "character budget"})
        else:
            chosen.append(e)
            used += len(e["text"])
    manifest = {"query": a.query, "max_entries": max_entries, "max_chars": max_chars, "chars": used,
                "included": [{"file": e["file"], "title": e["title"], "text": e["text"]} for e in chosen],
                "excluded": excluded}
    write_json(c.art("memory-manifest.json"), manifest)
    out({"ok": True, "included": len(chosen), "excluded": len(excluded), "chars": used,
         "manifest": "artifacts/memory-manifest.json"})


def cmd_verify(a, c):
    """F23: deterministic verification. The RUNNER executes ghe/config.yaml validation.commands (no shell) and
    records exit codes; an empty command list is BLOCKED, not passed. A waiver needs a reason and an identity."""
    st = c.state()
    if a.waive:
        if not (a.reason or "").strip() or not (a.by or "").strip():
            refuse("a verification waiver needs --reason and --by", problems=["reason/identity missing"])
        st["verification"] = {"status": "waived", "reason": a.reason.strip(), "by": a.by.strip(), "at": now()}
        c.save(st)
        out({"ok": True, "verification": st["verification"]})
    cmds = (c.config.get("validation") or {}).get("commands") or []
    if not cmds or not all(isinstance(x, str) and x.strip() for x in cmds):
        st["verification"] = {"status": "blocked", "reason": "no validation.commands configured", "at": now()}
        c.save(st)
        out({"ok": False, "error": "verification blocked: no validation.commands configured; run /ghe-setup "
             "or waive explicitly with `verify --waive --reason ... --by ...`", "verification": st["verification"]}, 1)
    results = []
    for cmd in cmds:
        try:
            r = subprocess.run(shlex.split(cmd), cwd=c.root, capture_output=True, text=True, timeout=a.timeout)
            results.append({"command": cmd, "exit": r.returncode})
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            results.append({"command": cmd, "exit": -1, "error": type(exc).__name__})
    ok = all(x["exit"] == 0 for x in results)
    st["verification"] = {"status": "passed" if ok else "failed", "results": results, "at": now()}
    c.save(st)
    out({"ok": ok, "verification": st["verification"]}, 0 if ok else 1)


def cmd_finish(a, c):
    st = c.state()
    if st["status"] in TERMINAL and st["status"] != a.status:
        refuse(f"run is already {st['status']}; it cannot become {a.status}")
    if a.status == "DONE":
        problems = finish_problems(c, st)
        if problems:
            refuse("run cannot be DONE yet", problems=problems)
    elif a.status in ("FAILED", "CANCELLED"):
        if not (a.reason or "").strip():
            refuse(f"finish --status {a.status} requires --reason", problems=["reason missing"])
        st["finish_reason"] = a.reason.strip()
    else:  # WAITING_HUMAN
        waiting = [k for k, v in st["nodes"].items() if v["status"] == "waiting_human"] + \
                  [k for k, g in st["gates"].items() if g.get("status") == "waiting"]
        if not waiting:
            refuse("WAITING_HUMAN needs a pending gate or a node with an open question",
                   problems=["nothing is waiting for a human"])
        if (a.reason or "").strip():
            st["finish_reason"] = a.reason.strip()
    st["status"] = a.status
    st["finished"] = now()
    c.save(st)
    out({"ok": True, "status": a.status})


# ----------------------------------------------------------------- tokens + budgets (spec §11)
def cmd_tokens(a, c):
    st = c.state()
    try:
        add = int(a.add)
    except (TypeError, ValueError):
        add = -1
    if add < 0:
        refuse(f"--add must be a non-negative integer, got {a.add!r}", 2)
    st["nodes"].setdefault(a.node, {"status": "pending", "attempts": 0, "tokens": 0, "output_hash": None})
    err = ingest_usage(st, a.node, add, a.event)
    if err and err != "duplicate":
        refuse(err, 2)
    if not err:
        c.save(st)
    out({"ok": True, "tokens_total": st["tokens_total"], "duplicate": err == "duplicate"})


def cmd_budget(a, c):
    """Report token usage. There are no token limits: this never asks to stop."""
    st = c.state()
    node_tok = st["nodes"].get(a.node, {}).get("tokens", 0) if a.node else 0
    out({"ok": True, "action": "ok", "actions": [], "tokens_total": st["tokens_total"], "node_tokens": node_tok})


# ----------------------------------------------------------------- stories -> waves (spec §7.5, §8.3)
def static_prefix(g):
    m = re.search(r"[*?\[]", g)
    return g[: m.start()] if m else g


def globs_overlap(a, b):
    pa, pb = static_prefix(a), static_prefix(b)
    if pa.startswith(pb) or pb.startswith(pa):
        return True
    return fnmatch.fnmatch(pa, b) or fnmatch.fnmatch(pb, a)


def stories_overlap(s1, s2):
    """F21: ownership is repo-qualified; the same glob in two different repos never overlaps."""
    if (s1.get("repo") or "") != (s2.get("repo") or ""):
        return False
    return any(globs_overlap(x, y) for x in s1.get("owns", []) for y in s2.get("owns", []))


def cmd_waves(a, c):
    data = load_json(a.stories or c.art("plans", "stories.json"))
    if not data:
        out({"ok": False, "error": "stories.json not found"}, 2)
    stories = {s["id"]: s for s in data["stories"]}
    errors = []
    ids = [s.get("id") for s in data["stories"]]          # T34: a duplicate id would silently drop a story
    errors += [f"duplicate story id {i}" for i in sorted({i for i in ids if ids.count(i) > 1})]
    for s in stories.values():
        if not s.get("owns"):
            errors.append(f"{s['id']}: no owns globs")
        for d in s.get("depends", []):
            if d not in stories:
                errors.append(f"{s['id']}: unknown dependency {d}")
    if errors:
        out({"ok": False, "errors": errors}, 1)
    layer, state = {}, {}

    def depth(i):
        if state.get(i) == 1:
            out({"ok": False, "errors": [f"dependency cycle at {i}"]}, 1)
        if i in layer:
            return layer[i]
        state[i] = 1
        layer[i] = 1 + max([depth(d) for d in stories[i].get("depends", [])], default=-1)
        state[i] = 2
        return layer[i]

    for i in stories:
        depth(i)
    waves, serialised = [], []
    for lv in sorted(set(layer.values())):
        pending = [i for i in stories if layer[i] == lv]
        while pending:
            wave = []
            for i in list(pending):
                clash = next((j for j in wave if stories_overlap(stories[i], stories[j])), None)
                if clash:
                    serialised.append({"story": i, "after": clash})
                    continue
                wave.append(i)
            waves.append(wave)
            pending = [i for i in pending if i not in wave]
    cap = c.config["defaults"].get("max_parallel_agents", 4)
    out({"ok": True, "waves": waves, "serialised_due_to_overlap": serialised, "max_parallel": cap})


# ----------------------------------------------------------------- tickets + stories (spec §7.5, §10.3)
TICKET_PARENT_KIND = {"epic": None, "task": None, "story": ("epic",), "subtask": ("story", "task"),
                      "qa-subtask": ("story",), "dev-subtask": ("story",)}


def cmd_ticket(a, c):
    """Idempotent ticket registry. Key is (kind, node, ref); a repeat add returns the existing entry."""
    st = c.state()
    reg = st.setdefault("tickets", {})
    if a.list:
        out({"ok": True, "tickets": reg})
    if st.get("tracker_unavailable"):   # T39
        refuse("tracker is unavailable: no new tickets (tracker-resume, or tracker-switch --by ... --reason ...)")
    auth = st.setdefault("tracker_authority", c.config.get("tracker"))
    if not a.list and auth != c.config.get("tracker"):   # T39: authority changed without an approved switch
        refuse(f"tracker authority changed from {auth} to {c.config.get('tracker')} without tracker-switch")
    ident = f"{a.kind}:{a.node}:{a.ref or '-'}"
    if ident in reg:
        out({"ok": True, "existing": True, "ticket": reg[ident]})
    if a.ref and not a.list:   # F14: a stable ref maps to ONE ticket across runs (same tracker config)
        with ledger_txn(c) as led:
            prior = led["tickets"].get(f"{c.config.get('tracker')}:{a.kind}:{a.ref}")
        if prior:
            reg[ident] = dict(prior, node=a.node)
            c.save(st)
            out({"ok": True, "existing": True, "cross_run": True, "ticket": reg[ident]})
    # F17: hierarchy epic > story > subtask (task is a flat per-node ticket under the epic). Identical for
    # tracker local and jira, so a local run has the same structure a Jira run would.
    need = TICKET_PARENT_KIND.get(a.kind)
    if need is not None:
        parent = next((t for t in reg.values() if t["key"] == a.parent), None)
        if not a.parent or parent is None:
            out({"ok": False, "error": f"a {a.kind} ticket needs --parent <key of an existing {'/'.join(need)} ticket>"}, 1)
        if parent["kind"] not in need:
            out({"ok": False, "error": f"a {a.kind} cannot sit under a {parent['kind']} (needs {'/'.join(need)})"}, 1)
    elif a.parent:
        out({"ok": False, "error": f"a {a.kind} ticket has no parent"}, 1)
    if not a.key:
        if c.config.get("tracker") == "jira":
            out({"ok": False, "error": "tracker is jira: pass --key with the key returned by Jira"}, 2)
        a.key = f"LOCAL-{len(reg) + 1}"
    if c.config.get("tracker") == "jira":
        bad = jira_config_problems(c.config)
        if bad:
            out({"ok": False, "error": "jira not configured: " + "; ".join(bad) + ". Run /ghe-setup."}, 2)
        proj = c.config["jira"]["project"]
        if not re.match(rf"^{re.escape(proj)}-\d+$", a.key):
            out({"ok": False, "error": f"refusing key {a.key}: only project {proj} is allowed (spec §13)"}, 1)
    # T36: a Jira site may lack an issue type: jira.issue_types maps a kind to its configured equivalent, or null
    # to declare it unsupported (an actionable error, never a silent downgrade).
    itypes = (c.config.get("jira") or {}).get("issue_types") or {}
    if c.config.get("tracker") == "jira" and a.kind in itypes and not itypes[a.kind]:
        out({"ok": False, "error": f"this Jira site has no {a.kind} issue type: set jira.issue_types.{a.kind} "
             "to the equivalent type in ghe/config.yaml (or use tracker: local)"}, 2)
    reg[ident] = {"key": a.key, "kind": a.kind, "node": a.node, "ref": a.ref, "title": a.title, "created": now()}
    if c.config.get("tracker") == "jira" and itypes.get(a.kind):
        reg[ident]["issue_type"] = str(itypes[a.kind])
    if a.parent:
        reg[ident]["parent"] = a.parent
    c.save(st)
    if a.ref:
        with ledger_txn(c) as led:
            led["tickets"].setdefault(f"{c.config.get('tracker')}:{a.kind}:{a.ref}", dict(reg[ident]))
    out({"ok": True, "existing": False, "ticket": reg[ident]})


def cmd_own_check(a, c):
    """F21: compare a story's ACTUAL changed files with its repo-qualified `owns` globs."""
    data = load_json(c.art("plans", "stories.json")) or {}
    story = next((x for x in data.get("stories", []) if x.get("id") == a.id), None)
    if story is None:
        out({"ok": False, "error": f"unknown story {a.id}"}, 2)
    if (story.get("repo") or "") != (a.repo or ""):
        out({"ok": False, "error": f"story {a.id} belongs to repo '{story.get('repo') or ''}', not '{a.repo or ''}'"}, 1)
    files = list(a.files or [])
    if a.git:
        r = subprocess.run(["git", "-C", a.git, "diff", "--name-only", a.base or "HEAD~1", "HEAD"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            out({"ok": False, "error": "git diff failed: " + r.stderr.strip()[:200]}, 2)
        files += [f for f in r.stdout.splitlines() if f]
    owns = story.get("owns", [])
    outside = sorted(f for f in files if ".." in f.split("/") or os.path.isabs(f)
                     or not any(fnmatch.fnmatch(f, g) or fnmatch.fnmatch(f, g.rstrip("/") + "/*") for g in owns))
    out({"ok": not outside, "story": a.id, "checked": len(files), "outside_owns": outside}, 0 if not outside else 1)


@contextlib.contextmanager
def ledger_txn(c):
    """F14/RT-03: project-level durable ledger (ghe/ledger.json), outside any run's lifecycle. Correction
    allowances and cross-run ticket mappings live here so a new run id, restart or new run dir cannot
    replenish them. Serialised with its own lock; written atomically."""
    d = os.path.join(c.root, "ghe")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "ledger.json")
    with run_lock(d):
        led = load_json(path, {}) or {}
        led.setdefault("stories", {})
        led.setdefault("tickets", {})
        led.setdefault("grants", [])
        yield led
        atomic_write_json(path, led)


def is_epic_global(c):
    return c.config["budgets"].get("correction_scope", "story") == "epic_global"


def story_cap(c, ent, led=None):
    """Per-story cap (default). Strict `epic_global` (T61): one shared cap across every story in the ledger."""
    base = c.config["budgets"].get("max_story_revisions", 1)
    if is_epic_global(c) and led is not None:
        return base + sum(e.get("allowance", 0) for e in led["stories"].values())
    return base + ent.get("allowance", 0)


def story_used(c, ent, led):
    if is_epic_global(c):
        return sum(e.get("revisions", 0) for e in led["stories"].values())
    return ent["revisions"]


def cmd_story(a, c):
    st = c.state()
    stories = st.setdefault("stories", {})
    with ledger_txn(c) as led:
        ent = led["stories"].setdefault(a.id, {"revisions": 0, "allowance": 0})
        s = stories.setdefault(a.id, {"status": "pending", "revisions": 0})
        s["revisions"] = ent["revisions"]            # durable count wins over per-run state
        if a.status == "done" and s.get("invalidated_by"):
            # T31: a design change invalidated this story; a completion from before it must not stand
            refuse(f"story {a.id} was invalidated by {s['invalidated_by']}; re-run it (--status running) before it can be done")
        if a.status:
            s["status"] = a.status
            if a.status == "running":
                s.pop("invalidated_by", None)
            if a.status == "revise":
                ent["revisions"] += 1
                s["revisions"] = ent["revisions"]
        if story_used(c, ent, led) > story_cap(c, ent, led):
            s["status"] = "blocked"
            st["status"] = "WAITING_HUMAN"
        cap_now = story_cap(c, ent, led)
    c.save(st)
    out({"ok": s["status"] != "blocked", "story": a.id, **s, "max_revisions": cap_now},
        0 if s["status"] != "blocked" else 1)


def cmd_allowance_grant(a, c):
    """RT-03: only an explicit, identified change authorization can add a correction allowance, and it must
    link to an existing (exhausted) story scope."""
    problems = []
    if not isinstance(a.add, int) or a.add < 1:
        problems.append("--add must be a positive integer")
    if not (a.reason or "").strip():
        problems.append("--reason is required")
    if not (a.by or "").strip():
        problems.append("--by (authorizing identity) is required")
    if problems:
        refuse("invalid allowance grant", problems=problems)
    with ledger_txn(c) as led:
        ent = led["stories"].get(a.story)
        if ent is None:
            refuse(f"unknown story scope {a.story}: an allowance must link to an existing story",
                   problems=["no such scope in ledger"])
        if story_used(c, ent, led) <= story_cap(c, ent, led):
            refuse(f"story {a.story} is not exhausted yet ({story_used(c, ent, led)}/{story_cap(c, ent, led)})",
                   problems=["scope not exhausted"])
        ent["allowance"] = ent.get("allowance", 0) + a.add
        led["grants"].append({"story": a.story, "add": a.add, "reason": a.reason.strip(), "by": a.by.strip(),
                              "at": now(), "limit_after": story_cap(c, ent, led)})
        cap_after = story_cap(c, ent, led)
    out({"ok": True, "story": a.story, "allowance": ent["allowance"], "max_revisions": cap_after})


# ----------------------------------------------------------------- defects + no-progress (spec §9.3)
def normalise_error(s):
    s = (s or "").lower()
    s = re.sub(r"0x[0-9a-f]+|\b[0-9a-f]{8,}\b", "<hex>", s)
    s = re.sub(r"\d{4}-\d\d-\d\d[t ]\d\d:\d\d:\d\d[\w.:+-]*", "<ts>", s)
    s = re.sub(r"(/[\w.@-]+)+", "<path>", s)
    s = re.sub(r"\d+", "<n>", s)
    return re.sub(r"\s+", " ", s).strip()


def cmd_defects(a, c):
    st = c.state()
    n = int(a.round)
    path = c.art("defects", f"round-{n}.json")
    data, why = None, None
    if not os.path.isfile(path):
        why = f"defect evidence missing: {os.path.relpath(path, c.run)}"
    else:
        try:
            data = json.loads(read(path, ""))
        except ValueError:
            why = "defect evidence invalid: not valid JSON"
    if why is None and (not isinstance(data, dict) or not isinstance(data.get("defects"), list)):
        why = "defect evidence invalid: expected a JSON object with a `defects` list"
    if why is None and data.get("round") not in (None, n):
        why = f"defect evidence is for round {data.get('round')}, not {n}"
    if why is None and (data.get("blocked") or data.get("incomplete") or data.get("missing_cases")):
        why = "defect evidence reports blocked/missing/incomplete test coverage"
    if why is None:  # T16: evidence must belong to the current plan/source, never an earlier one
        plan_p = c.art("plans", "stories.json")
        cur_plan = sha(read(plan_p, "")) if os.path.isfile(plan_p) else None
        if data.get("plan_sha") is not None and cur_plan is not None and data["plan_sha"] != cur_plan:
            why = "defect evidence is stale: plan_sha does not match the current plans/stories.json"
        elif data.get("source_hash") is not None and data["source_hash"] != st.get("source_hash"):
            why = "defect evidence is stale: source_hash does not match this run's source"
        elif cur_plan is not None and not data["defects"] and data.get("plan_sha") is None:
            why = "defect evidence cannot prove a pass: it must carry plan_sha for the current plan"
    if why is None:  # F12: every designed test case needs a recorded result before a round counts
        want = [x.get("id") for x in (load_json(c.art("test-cases.json"), {}) or {}).get("cases", []) if x.get("id")]
        done_ids = {r.get("case") for r in (load_json(c.art("test-results.json"), {}) or {}).get("results", [])
                    if isinstance(r, dict)}
        missing = [i for i in want if i not in done_ids]
        if missing:
            why = f"defect evidence incomplete: no test result for case(s) {', '.join(missing[:8])}"
    if why:  # never imply a pass (F12); state is left untouched
        out({"ok": False, "round": n, "verdict": "incomplete", "escalate": False, "error": why,
             "action": "rerun the Tester so it writes complete defect evidence"}, 1)
    for d in data.get("defects", []):
        d["fingerprint"] = sha(f"{d.get('case')}|{normalise_error(d.get('error') or d.get('actual'))}")[:12]
    write_json(path, data)
    fps = sorted({d["fingerprint"] for d in data["defects"]})
    st["fingerprints"][str(n)] = fps
    st["round"] = n
    prev = set(st["fingerprints"].get(str(n - 1), []))
    cur = set(fps)
    maxr = c.config["budgets"]["max_rounds"]
    if not cur:
        verdict = "pass"
        st.pop("post_design_check", None)
    elif st.get("post_design_check"):
        verdict = "post_design_fail"             # T21: no extra ordinary retry after an approved design change
    elif n > 1 and cur >= prev and prev:
        verdict = "no_progress"
    elif n >= maxr:
        verdict = "max_rounds"
    else:
        verdict = "progress" if n > 1 else "first"
    if verdict in ("first", "progress"):  # another round will run: re-open builder -> ... -> test
        nodes = resolved_nodes(c)
        loop = {"builder", "test"} | {k for k in nodes if "builder" in ancestors(nodes, k) and k in ancestors(nodes, "test")}
        for k in loop:
            if k in st["nodes"] and st["nodes"][k]["status"] in ("done", "failed"):
                st["nodes"][k].update(status="pending", attempts=0, output_hash=None)
        if st["status"] != "WAITING_HUMAN":
            st["status"] = "RUNNING"
    c.save(st)
    out({"ok": True, "round": n, "failing": len(cur), "fixed": sorted(prev - cur),
         "regressions": sorted(cur - prev) if n > 1 else [], "verdict": verdict,
         "escalate": verdict in ("no_progress", "max_rounds", "post_design_fail"), "fingerprints": fps})


# ----------------------------------------------------------------- drift check (spec §11.1)
def cmd_drift(a, c):
    st = c.state()
    summary = read(a.summary)
    spec = read(c.art("requirement-spec.md"), "")
    if summary is None:
        out({"ok": False, "failures": ["summary file missing"]}, 1)
    fails = []
    b, e = summary.find(ANCHOR_BEGIN), summary.find(ANCHOR_END)
    if b < 0 or e < b:
        fails.append("anchor markers missing")
    else:
        body = summary[b + len(ANCHOR_BEGIN): e].strip("\n")
        original = (read(c.art("source.md"), "") or "").strip("\n")
        if sha(read(c.art("source.md"), "")) != st.get("source_hash"):
            fails.append("source.md changed since discover (hash mismatch)")
        if body != original:
            fails.append("anchor text is not byte-identical to source.md")
    spec_ids, sum_ids = set(REQ_RE.findall(spec)), set(REQ_RE.findall(summary))
    if spec_ids:
        missing = sorted(spec_ids - sum_ids)
        if missing:
            fails.append(f"requirement IDs missing from summary: {missing}")
    invented = sorted(sum_ids - spec_ids) if spec_ids else []
    if invented:
        fails.append(f"requirement IDs not in the spec (invented): {invented}")
    m = re.search(r"##\s*Work remaining\s*\n(.*?)(?:\n##\s|\Z)", summary, re.S | re.I)
    if m:
        for node, v in st["nodes"].items():
            if v["status"] == "done" and re.search(rf"\b{re.escape(node)}\b", m.group(1)):
                fails.append(f"'Work remaining' mentions finished node: {node}")
    if a.trigger:
        st["compactions"].append({"at": now(), "trigger": a.trigger, "before": a.before, "after": a.after,
                                  "ok": not fails, "failures": fails})
        c.save(st)
    out({"ok": not fails, "failures": fails, "next": "run small-model judge (check 5)" if not fails else
         "regenerate once, else fall back to truncation (anchor + last N turns)"}, 0 if not fails else 1)


# ----------------------------------------------------------------- traceability (spec §8.4)
def trace_compute(c):
    """Return (markdown lines, gaps, requirement count) without writing anything."""
    spec = read(c.art("requirement-spec.md"), "")
    reqs = {}
    for line in spec.splitlines():
        for r in REQ_RE.findall(line):
            reqs.setdefault(r, re.sub(r"^[#*\-\s|]*" + r + r"[\s:|*-]*", "", line).strip()[:70])
    designs = {"HLD": read(c.art("hld.md"), ""), "LLD": read(c.art("lld.md"), ""),
               "CONTRACT": read(c.art("CONTRACT.md"), "")}
    stories = (load_json(c.art("plans", "stories.json"), {}) or {}).get("stories", [])
    cases = (load_json(c.art("test-cases.json"), {}) or {}).get("cases", [])
    results = {r["case"]: r["status"] for r in (load_json(c.art("test-results.json"), {}) or {}).get("results", [])}
    rows, gaps = [], []
    for r in sorted(reqs):
        d = [k for k, v in designs.items() if r in v]
        s = [x["id"] for x in stories if r in x.get("reqs", [])]
        t = [x["id"] for x in cases if r in x.get("reqs", [])]
        res = ",".join(sorted({results.get(i, "not run") for i in t})) if t else "-"
        rows.append(f"| {r} | {', '.join(d) or '-'} | {', '.join(s) or '-'} | {', '.join(t) or '-'} | {res} |")
        if cases and not t:
            gaps.append(f"{r}: no test case")
        if designs["HLD"] and not d:
            gaps.append(f"{r}: no design reference")
    for x in cases:
        for r in x.get("reqs", []):
            if r not in reqs:
                gaps.append(f"{x['id']}: references unknown {r}")
        if not x.get("reqs"):
            gaps.append(f"{x['id']}: test case with no requirement")
    md = ["# Traceability matrix", "", "| REQ | Design ref | Story | Test case(s) | Result (last round) |",
          "|---|---|---|---|---|"] + rows
    if gaps:
        md += ["", "## Gaps"] + [f"- {g}" for g in gaps]
    return md, gaps, len(reqs)


def cmd_trace(a, c):
    md, gaps, nreqs = trace_compute(c)
    atomic_write_text(c.art("traceability.md"), "\n".join(md) + "\n")
    out({"ok": not gaps, "requirements": nreqs, "gaps": gaps}, 0 if not gaps else 1)


# ----------------------------------------------------------------- human gates (spec §7.8, §9.4)
def cmd_gate(a, c):
    st = c.state()
    g = st["gates"].setdefault(a.name, {"revisions": 0})
    g.update({"status": "waiting", "request": a.request, "asked": now()})
    st["status"] = "WAITING_HUMAN"
    c.save(st)
    out({"ok": True, "gate": a.name, "status": "waiting", "resume": f"/ghe-run --resume {st['run_id']}"})


def cmd_gate_decide(a, c):
    st = c.state()
    g = st["gates"].get(a.name)
    if g is None:
        out({"ok": False, "error": f"no such gate {a.name}"}, 2)
    dec = a.decision
    by = (getattr(a, "by", "") or "").strip()
    if not by:
        out({"ok": False, "error": "gate-decide needs --by <approver identity>"}, 1)
    if g.get("status") not in ("waiting", "changes_requested"):
        out({"ok": False, "error": f"gate {a.name} is {g.get('status')}; a decided gate cannot be re-decided"}, 1)
    g["decision"], g["comment"], g["decided"], g["decided_by"] = dec, a.comment, now(), by
    g.setdefault("history", []).append({"at": g["decided"], "by": by, "decision": dec, "comment": a.comment})
    maxrev = c.config["budgets"].get("max_gate_revisions", 1)
    if dec == "request_changes":
        g["revisions"] += 1
        g["status"] = "escalated" if g["revisions"] > maxrev else "changes_requested"
    else:
        g["status"] = "approved" if dec == "approve" else "rejected"
    if g["status"] in ("approved",):
        st["status"] = "RUNNING"
        if a.name in st["nodes"]:
            st["nodes"][a.name]["status"] = "done"
    if g["status"] == "rejected":
        st["status"] = "CANCELLED"
    c.save(st)
    out({"ok": True, "gate": a.name, "gate_status": g["status"], "revisions": g["revisions"], "max": maxrev})


def cmd_design_propose(a, c):
    """F19: a design/contract change is a PROPOSAL with an impact statement, never a direct edit."""
    st = c.state()
    revs = st.setdefault("design_revisions", {})
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", a.id or ""):
        out({"ok": False, "error": "invalid design revision id"}, 1)
    if a.id in revs:
        out({"ok": False, "error": f"design revision {a.id} already exists"}, 1)
    if not (a.summary or "").strip() or not a.files:
        out({"ok": False, "error": "a proposal needs --summary and at least one --files (design artifact)"}, 1)
    if not a.stories and not a.tests:
        out({"ok": False, "error": "a proposal must state its impact (--stories and/or --tests); state none-affected explicitly with --stories none"}, 1)
    # T29: every affected consumer story / test is proposed WITH a reason (--reason ID=why), never a bare list
    reasons = {}
    for item in a.reason or []:
        k, sep, why = item.partition("=")
        if not sep or not k.strip() or not why.strip():
            out({"ok": False, "error": f"invalid --reason {item!r}: use ID=why"}, 1)
        reasons[k.strip()] = why.strip()
    affected = [x for x in list(a.stories or []) + list(a.tests or []) if x != "none"]
    missing_reason = [x for x in affected if x not in reasons]
    if missing_reason:
        out({"ok": False, "error": "affected stories/tests need a reason each (--reason ID=why): "
             + ", ".join(missing_reason)}, 1)
    unknown_reason = [k for k in reasons if k not in affected]
    if unknown_reason:
        out({"ok": False, "error": "reason given for an id that is not listed as affected: "
             + ", ".join(unknown_reason)}, 1)
    cap = c.config["budgets"].get("max_design_batches", 1)
    if sum(1 for r in revs.values() if r["status"] in ("proposed", "approved")) >= cap:
        out({"ok": False, "error": f"design batch limit ({cap}) reached; escalate to the human (spec §9)"}, 1)
    revs[a.id] = {"status": "proposed", "summary": a.summary.strip(), "files": a.files,
                  "stories": [x for x in a.stories if x != "none"], "tests": list(a.tests or []),
                  "reasons": reasons, "proposed": now(), "round": a.round}
    c.save(st)
    out({"ok": True, "id": a.id, "status": "proposed"})


def cmd_design_decide(a, c):
    """Approval makes the revision citable and selectively invalidates ONLY the stories/tests it names."""
    st = c.state()
    r = (st.get("design_revisions") or {}).get(a.id)
    if r is None:
        out({"ok": False, "error": f"unknown design revision {a.id}"}, 2)
    if r["status"] != "proposed":
        out({"ok": False, "error": f"design revision {a.id} is {r['status']}; cannot be re-decided"}, 1)
    if not (a.by or "").strip():
        out({"ok": False, "error": "design-decide needs --by <approver identity>"}, 1)
    r.update(status="approved" if a.decision == "approve" else "rejected", decided=now(), decided_by=a.by.strip())
    invalidated = []
    if a.decision == "approve":
        for sid in r["stories"]:
            s = st.setdefault("stories", {}).setdefault(sid, {"status": "pending", "revisions": 0})
            if s.get("status") != "pending":
                # T30: reopening keeps the story's history (what it was, why it was reopened)
                s.setdefault("history", []).append({"at": now(), "event": "reopened", "by_revision": a.id,
                                                    "previous_status": s.get("status"),
                                                    "reason": (r.get("reasons") or {}).get(sid, "")})
                s["status"] = "pending"
                invalidated.append(sid)
            s["invalidated_by"] = a.id            # T31: fences stale completions, even for in-flight stories
        st["post_design_check"] = a.id            # T21: the next failing defect round escalates immediately
    c.save(st)
    out({"ok": True, "id": a.id, "status": r["status"], "invalidated_stories": invalidated,
         "invalidated_tests": r["tests"] if a.decision == "approve" else []})


def cmd_escalation_report(a, c):
    """T55: render the escalation package from recorded state only. No LLM, no network, works with budgets exhausted."""
    st = c.state()
    L = [f"# Escalation report: {st.get('run_id')}", "",
         f"- Status: {st.get('status')}", f"- Round: {st.get('round', 0)}",
         f"- Tokens: {st.get('tokens_total', 0)}", "",
         "## Nodes", "| Node | Status | Attempts | Tokens |", "|---|---|---|---|"]
    for k, n in sorted(st["nodes"].items()):
        L.append(f"| {k} | {n.get('status')} | {n.get('attempts', 0)} | {n.get('tokens', 0)} |")
    waiting = [(k, n.get("open_questions") or []) for k, n in st["nodes"].items() if n.get("status") == "waiting_human"]
    L += ["", "## Waiting for a human"] + ([f"- node `{k}`: {'; '.join(q) or '(no question recorded)'}" for k, q in waiting] or ["- none"])
    gates = [(k, g.get("status")) for k, g in (st.get("gates") or {}).items() if g.get("status") != "approved"]
    L += ["", "## Gates not approved"] + ([f"- {k}: {s}" for k, s in gates] or ["- none"])
    stories = [(k, v.get("status"), v.get("revisions", 0)) for k, v in (st.get("stories") or {}).items()
               if v.get("status") in ("blocked", "revise")]
    L += ["", "## Stories needing a decision"] + ([f"- {k}: {s} (revisions {r})" for k, s, r in stories] or ["- none"])
    path = c.art("escalation-report.md")
    atomic_write_text(path, "\n".join(L) + "\n")
    out({"ok": True, "path": os.path.relpath(path, c.run), "waiting": [k for k, _ in waiting]})


ACTION_GATES = {"cloud-apply": "cloud-apply", "destructive": "destructive"}


def cmd_authorize(a, c):
    """F24: the runner is the authority for controlled actions. Exit 0 only if the run is active and the
    matching gate is approved in a valid state; the PreToolUse hook is just a second layer."""
    gate = ACTION_GATES.get(a.action)
    if gate is None:
        out({"ok": False, "error": f"unknown action {a.action}"}, 2)
    st = c.state()
    if st.get("status") not in ("RUNNING", "WAITING_HUMAN"):
        out({"ok": False, "authorized": False, "error": f"run is {st.get('status')}"}, 1)
    g = (st.get("gates") or {}).get(gate)
    if not isinstance(g, dict) or g.get("status") != "approved":
        out({"ok": False, "authorized": False, "error": f"gate {gate} is not approved"}, 1)
    out({"ok": True, "authorized": True, "action": a.action, "approved_by": g.get("decided_by")})


# ----------------------------------------------------------------- side-effect journal (F13: T37, T54, T60)
# Intent is recorded BEFORE an external side effect and confirmed AFTER it. A crash or a lost response leaves
# an unresolved intent, which blocks a blind retry (no duplicate irreversible action) until it is reconciled.
def _journal(c):
    return load_json(os.path.join(c.run, "journal.json"), {}) or {}


def _save_journal(c, j):
    atomic_write_json(os.path.join(c.run, "journal.json"), j)


def unresolved_intents(c):
    return sorted(k for k, e in _journal(c).items() if e.get("status") == "intent")


def cmd_journal_intent(a, c):
    j = _journal(c)
    e = j.get(a.key)
    if e and e.get("status") == "applied":
        out({"ok": True, "key": a.key, "already_applied": True, "action": "skip: do not repeat the side effect"})
    if e and e.get("status") == "intent":
        refuse(f"intent {a.key} is unresolved (outcome unknown); verify the real state, then run "
               "`journal-reconcile --outcome applied|not_applied` before retrying", action="reconcile")
    j[a.key] = {"action": a.action, "status": "intent", "intent_at": now(), "history": (e or {}).get("history", [])}
    _save_journal(c, j)
    out({"ok": True, "key": a.key, "status": "intent"})


def cmd_journal_confirm(a, c):
    j = _journal(c)
    e = j.get(a.key)
    if not e or e.get("status") != "intent":
        refuse(f"no open intent {a.key} to confirm")
    e.update(status="applied", confirmed_at=now(), evidence=a.evidence)
    _save_journal(c, j)
    out({"ok": True, "key": a.key, "status": "applied"})


def cmd_journal_reconcile(a, c):
    if not a.evidence.strip():
        refuse("reconcile needs --evidence (what you checked in the real system)")
    j = _journal(c)
    e = j.get(a.key)
    if not e or e.get("status") != "intent":
        refuse(f"no unresolved intent {a.key} to reconcile")
    e["history"] = e.get("history", []) + [{"intent_at": e.get("intent_at"), "resolved_at": now(), "outcome": a.outcome}]
    e.update(status=a.outcome, evidence=a.evidence, reconciled_at=now())
    _save_journal(c, j)
    out({"ok": True, "key": a.key, "status": a.outcome})



# ----------------------------------------------------------------- tracker, context and release controls
# T36 attachments / T38 human field edits / T39 tracker outage / T41-T42 bounded context / T51-T53 release.
def _ticket_by_key(st, key):
    return next((t for t in (st.get("tickets") or {}).values() if t.get("key") == key), None)


def cmd_ticket_attach(a, c):
    """T36: attach an artifact to a ticket if the tracker supports it; otherwise record an explicit fallback
    (an excerpt comment) and an actionable limitation. Never claims an attachment that was not made."""
    st = c.state()
    t = _ticket_by_key(st, a.key)
    if t is None:
        refuse(f"no ticket {a.key}")
    path = safe_artifact(c, a.path)
    if path is None or not os.path.isfile(path):
        refuse(f"artifact path not allowed or missing: {a.path}")
    if a.capability == "supported":
        t.setdefault("attachments", []).append({"path": a.path, "at": now()})
        c.save(st)
        out({"ok": True, "attached": True, "key": a.key, "path": a.path})
    with open(path, encoding="utf-8", errors="replace") as fh:
        excerpt = fh.read(500)
    t.setdefault("attach_fallback", []).append({"path": a.path, "excerpt": excerpt, "at": now()})
    c.save(st)
    out({"ok": True, "attached": False, "fallback": "comment-with-excerpt", "key": a.key,
         "limitation": "this tracker/site cannot take attachments: the excerpt is recorded as a comment; "
                       "enable attachments or set a link-based convention in ghe/config.yaml to attach files"})


def cmd_ticket_field(a, c):
    """T38: a human edit of a ticket field is never overwritten. `--write` records what GHE wrote; `--remote`
    reports what the tracker holds now. A difference is a conflict that blocks further writes until a named
    person resolves it (keep-remote adopts the human value; overwrite needs --by)."""
    st = c.state()
    t = _ticket_by_key(st, a.key)
    if t is None:
        refuse(f"no ticket {a.key}")
    f = t.setdefault("fields", {}).setdefault(a.field, {})
    if a.resolve:
        if not f.get("conflict"):
            refuse(f"no conflict on {a.field}")
        if a.resolve == "overwrite" and not (a.by or "").strip():
            refuse("overwrite needs --by (who authorises replacing the human edit)")
        if a.resolve == "keep-remote":
            f["written"] = f["conflict"]["remote"]
        f["resolved"] = {"how": a.resolve, "by": (a.by or "").strip() or None, "at": now()}
        f.pop("conflict")
        c.save(st)
        out({"ok": True, "resolved": a.resolve, "field": a.field})
    if a.remote is not None:
        if "written" in f and a.remote != f["written"]:
            f["conflict"] = {"ours": f["written"], "remote": a.remote, "at": now()}
            c.save(st)
            out({"ok": False, "conflict": True, "action": "ask_human", "field": a.field, "ours": f["written"],
                 "remote": a.remote, "error": "a human changed this field; not overwritten"}, 1)
        c.save(st)
        out({"ok": True, "conflict": False})
    if a.write is None:
        refuse("pass --write, --remote or --resolve")
    if f.get("conflict"):
        refuse(f"unresolved conflict on {a.field}: ticket-field --resolve keep-remote|overwrite --by ...")
    f["written"] = a.write
    c.save(st)
    out({"ok": True, "written": True, "field": a.field})


def cmd_tracker_down(a, c):
    """T39: record that the configured tracker is unavailable. The run does not fall back to local tickets and
    cannot finish until a person resumes it (the tracker is reachable) or explicitly switches authority."""
    st = c.state()
    st["tracker_unavailable"] = {"error": a.error, "tracker": c.config.get("tracker"), "at": now()}
    c.save(st)
    out({"ok": True, "waiting": "human", "tracker": c.config.get("tracker"),
         "action": "retry, or tracker-switch --by to change authority explicitly; no silent local fallback"})


def cmd_tracker_resume(a, c):
    st = c.state()
    if not st.get("tracker_unavailable"):
        refuse("tracker is not marked unavailable")
    if not (a.by or "").strip():
        refuse("tracker-resume needs --by")
    st.pop("tracker_unavailable")
    c.save(st)
    out({"ok": True, "resumed": True})


def cmd_tracker_switch(a, c):
    """T39: the tracker authority is fixed per run; changing it (jira <-> local) is an explicit, attributed act
    and must match ghe/config.yaml."""
    st = c.state()
    if not (a.by or "").strip() or not (a.reason or "").strip():
        refuse("tracker-switch needs --by and --reason")
    if a.to != c.config.get("tracker"):
        refuse(f"set tracker: {a.to} in ghe/config.yaml first, then record the switch")
    st.setdefault("tracker_history", []).append({"from": st.get("tracker_authority"), "to": a.to,
                                                 "by": a.by.strip(), "reason": a.reason.strip(), "at": now()})
    st["tracker_authority"] = a.to
    st.pop("tracker_unavailable", None)
    c.save(st)
    out({"ok": True, "tracker": a.to})


def context_config_problems(cfg):
    m = cfg.get("context") or {}
    return [f"context.{k} must be a positive integer" for k in ("max_entries", "max_chars", "max_source_bytes")
            if k in m and (not isinstance(m[k], int) or isinstance(m[k], bool) or m[k] < 1)]


def _split_entries(text):
    blocks = re.split(r"(?m)^(?=## )", text) if re.search(r"(?m)^## ", text) else re.split(r"\n\s*\n", text)
    out_ = []
    for b in blocks:
        b = b.strip()
        if b:
            out_.append({"title": b.splitlines()[0].lstrip("# ").strip(), "text": b})
    return out_


def cmd_context_packet(a, c):
    """T41/T42: build a BOUNDED, relevance-selected packet from large sources (ticket comments, history, memory,
    graph extracts) given as artifact-relative paths. Sources are read capped, never emitted whole. An entry
    marked [essential] that cannot fit makes the command refuse (split or pause) - nothing is truncated."""
    cc = c.config.get("context") or {}
    bad = context_config_problems(c.config)
    if bad:
        refuse("invalid context config: " + "; ".join(bad))
    max_entries, max_chars = cc.get("max_entries", 12), cc.get("max_chars", 6000)
    cap = cc.get("max_source_bytes", 2_000_000)
    words = set(re.findall(r"[a-z0-9]{3,}", (a.query or "").lower()))
    cands, stats = [], {}
    for spec in a.source or []:
        name, _, rel = spec.partition("=")
        path = safe_artifact(c, rel)
        if not name or path is None or not os.path.isfile(path):
            refuse(f"source {spec!r} must be name=<artifact path> inside the run")
        with open(path, "rb") as fh:
            raw = fh.read(cap + 1)
        capped = len(raw) > cap
        entries = _split_entries(raw[:cap].decode("utf-8", errors="replace"))
        stats[name] = {"entries": len(entries), "capped": capped}
        for i, e in enumerate(entries):
            e.update(source=name, order=i, essential="[essential]" in e["text"].lower())
            cands.append(e)
    essential = [e for e in cands if e["essential"]]
    need = sum(len(e["text"]) for e in essential)
    if len(essential) > max_entries or need > max_chars:
        refuse(f"essential context ({len(essential)} entries, {need} chars) exceeds the packet bounds "
               f"({max_entries} entries, {max_chars} chars): split the work or pause; nothing was truncated",
               action="split_or_pause")
    rest = [e for e in cands if not e["essential"]]
    for e in rest:
        e["score"] = len(words & set(re.findall(r"[a-z0-9]{3,}", e["text"].lower())))
    rest.sort(key=lambda e: (-e["score"], -e["order"]))
    chosen, used = list(essential), need
    for e in rest:
        if e["score"] == 0 or len(chosen) >= max_entries or used + len(e["text"]) > max_chars:
            continue
        chosen.append(e)
        used += len(e["text"])
    packet = {"query": a.query, "max_entries": max_entries, "max_chars": max_chars, "chars": used,
              "sources": stats, "total_candidates": len(cands),
              "included": [{"source": e["source"], "title": e["title"], "text": e["text"]} for e in chosen]}
    atomic_write_json(c.art("context-packet.json"), packet)
    out({"ok": True, "included": len(chosen), "total_candidates": len(cands), "chars": used, "sources": stats})


SECRET_RE = re.compile(r"AKIA[0-9A-Z]{16}|(?i:aws_secret_access_key|secret_access_key|session_token)\s*[:=]|"
                       r"(?<![A-Za-z0-9/+])[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+])")


def cmd_deploy_target(a, c):
    """T52: AWS account and region must come from the requester (artifacts/deploy-target.md), never guessed,
    defaulted or inferred, and a file that contains credentials is refused and never copied into state."""
    path = c.art("deploy-target.md")
    if not os.path.isfile(path):
        out({"ok": False, "status": "needs_human", "action": "ask",
             "error": "no AWS account/region provided: ask the requester and write artifacts/deploy-target.md "
                      "(aws_account, aws_region, provided_by: requester); credentials are never stored"}, 2)
    text = open(path, encoding="utf-8").read()
    if SECRET_RE.search(text):
        out({"ok": False, "error": "deploy-target.md contains something that looks like a credential; remove it "
             "(credentials come from the environment, never from files or state)"}, 1)
    fm = frontmatter(text) or {}
    acct, region = str(fm.get("aws_account") or ""), str(fm.get("aws_region") or "")
    if str(fm.get("provided_by") or "").lower() != "requester":
        out({"ok": False, "status": "needs_human", "error": "provided_by must be 'requester' (never guess a target)"}, 2)
    if not re.fullmatch(r"\d{12}", acct) or not re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d", region):
        out({"ok": False, "status": "needs_human", "error": "aws_account must be 12 digits and aws_region a "
             "region id (e.g. eu-west-1)"}, 2)
    st = c.state()
    st["deploy_target"] = {"aws_account": acct, "aws_region": region, "at": now()}
    c.save(st)
    out({"ok": True, "aws_account": acct, "aws_region": region})


def cmd_release_check(a, c):
    """T51: a production release needs verified evidence (or a waiver with a named person) AND an approved
    cloud-apply gate. A test environment deploy stays possible without them."""
    st = c.state()
    if a.env == "test":
        out({"ok": True, "env": "test", "allowed": True})
    v = (st.get("verification") or {}).get("status")
    if v not in ("passed", "waived") and (st["nodes"].get("test") or {}).get("status") != "done":
        out({"ok": False, "env": "production", "allowed": False,
             "error": "production release blocked: no passed verification, Tester result or waiver; "
                      "a test-environment deploy is still possible (--env test)"}, 1)
    g = (st.get("gates") or {}).get("cloud-apply")
    if not isinstance(g, dict) or g.get("status") != "approved":
        out({"ok": False, "env": "production", "allowed": False,
             "error": "production release blocked: gate cloud-apply is not approved"}, 1)
    out({"ok": True, "env": "production", "allowed": True, "approved_by": g.get("decided_by")})


def cmd_release_record(a, c):
    """T53: local release evidence - the exact candidate, health result, PID and port are recorded so cleanup
    can only ever stop that process."""
    if not (a.candidate or "").strip():
        refuse("a release needs the exact --candidate (commit SHA or artifact hash)")
    if a.health not in ("passed", "failed"):
        refuse("--health must be passed or failed")
    st = c.state()
    st["local_release"] = {"candidate": a.candidate.strip(), "health": a.health, "health_url": a.health_url,
                           "pid": a.pid, "port": a.port, "at": now(), "cleaned": False}
    c.save(st)
    out({"ok": True, "recorded": st["local_release"]})


def cmd_release_cleanup(a, c):
    """T53: stop ONLY the recorded PID (never by name or pattern), and only if it is still alive."""
    st = c.state()
    r = st.get("local_release")
    if not r or not r.get("pid"):
        refuse("no recorded local release PID: nothing to clean up (never kill by name or pattern)")
    if r.get("cleaned"):
        out({"ok": True, "already_cleaned": True})
    killed = False
    if _pid_alive(r["pid"]):
        try:
            os.kill(r["pid"], signal.SIGTERM)
            killed = True
        except OSError as exc:
            refuse(f"could not stop pid {r['pid']}: {exc}")
    r["cleaned"], r["killed"], r["cleaned_at"] = True, killed, now()
    c.save(st)
    out({"ok": True, "pid": r["pid"], "killed": killed})


# ----------------------------------------------------------------- main
# ----------------------------------------------------------------- multi-repo workspace
# The directory GHE is run from is the WORKSPACE: it owns ghe/runs and ghe/workspace.yaml. The repos a run
# works on are found next to it (cwd, then its children, then the parent's children) or cloned (only after
# the human confirmed the URL and destination). Each repo keeps its own ghe/config.yaml from /ghe:setup.
URL_RE = re.compile(r"^(https?://[^\s/]+/\S+|ssh://\S+|git@[\w.-]+:\S+)$")
WS_FILE = os.path.join("ghe", "workspace.yaml")


def repo_name(hint):
    h = hint.strip().rstrip("/")
    h = re.split(r"[/:]", h)[-1]
    return h[:-4] if h.endswith(".git") else h


def norm_remote(url):
    u = (url or "").strip().rstrip("/").lower()
    u = re.sub(r"^(https?://|ssh://)([^@/]+@)?", "", u)
    u = re.sub(r"^git@", "", u).replace(":", "/", 1) if u.startswith("git@") else u
    return u[:-4] if u.endswith(".git") else u


def git_remote(path):
    try:
        r = subprocess.run(["git", "-C", path, "remote", "get-url", "origin"], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def is_git(path):
    return os.path.exists(os.path.join(path, ".git"))


def repo_dirs(base):
    """Git repos that are direct children of base (hidden dirs skipped, no deeper walk)."""
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    return [os.path.join(base, n) for n in names
            if not n.startswith(".") and os.path.isdir(os.path.join(base, n)) and is_git(os.path.join(base, n))]


def clone_base(root):
    """Where clones go: the workspace folder, or its parent when the workspace is itself a repo
    (never nest a clone inside another repo)."""
    return os.path.dirname(root) if is_git(root) else root


def find_repo(hint, root):
    hint = hint.strip()
    url = hint if URL_RE.match(hint) else ""
    res = {"hint": hint, "name": repo_name(hint), "url": url, "status": "missing", "path": None, "source": None,
           "candidates": [], "clone_dest": os.path.join(clone_base(root), repo_name(hint)) if url else None}
    if not url and (os.sep in hint or hint.startswith(("~", "."))):
        p = os.path.abspath(os.path.join(root, os.path.expanduser(hint)))
        if os.path.isdir(p):
            res.update(status="found", path=p, source="path", name=os.path.basename(p))
        return res
    name = res["name"].lower()
    parent = os.path.dirname(root)
    tiers = [("cwd", [root] if is_git(root) else []), ("cwd-child", repo_dirs(root)), ("parent", repo_dirs(parent))]
    if parent == root:
        tiers = tiers[:2]

    def exact(p):
        if url and git_remote(p) and norm_remote(git_remote(p)) == norm_remote(url):
            return True
        return os.path.basename(p).lower() == name

    for label, paths in tiers:
        hits = [p for p in paths if exact(p)]
        if len(hits) == 1:
            res.update(status="found", path=hits[0], source=label)
            return res
        if len(hits) > 1:
            res.update(status="ambiguous", candidates=hits, source=label)
            return res
    fuzzy = [p for _, paths in tiers for p in paths
             if len(name) >= 3 and (name in os.path.basename(p).lower() or os.path.basename(p).lower() in name)
             and len(os.path.basename(p)) >= 3]
    if fuzzy:
        res.update(status="fuzzy", candidates=sorted(set(fuzzy)))
    return res


def load_workspace(root):
    path = os.path.join(root, WS_FILE)
    return (load_yaml(path) or {}).get("repos") or [] if os.path.isfile(path) else []


def save_workspace(root, repos):
    path = os.path.join(root, WS_FILE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    atomic_write_text(path, yaml.safe_dump({"repos": repos}, sort_keys=False))


def repo_setup_state(path):
    g = os.path.join(path, "ghe")
    return os.path.isfile(os.path.join(g, "config.yaml")) and os.path.isfile(os.path.join(g, "graph.yaml"))


def register_repo(root, name, path, url="", origin="found"):
    repos = [r for r in load_workspace(root) if r.get("name") != name]
    repos.append({"name": name, "path": os.path.abspath(path), "url": url or git_remote(path), "origin": origin})
    save_workspace(root, repos)


def cmd_repos(a, c):
    act = a.action
    if act == "find":
        if not a.hint:
            out({"ok": False, "error": "repos find needs at least one --hint (repo name, path or clone URL)"}, 2)
        results = [find_repo(h, c.root) for h in a.hint]
        for r in results:
            r["setup"] = repo_setup_state(r["path"]) if r["path"] else None
        out({"ok": True, "workspace": c.root, "clone_base": clone_base(c.root), "results": results,
             "unresolved": [r["hint"] for r in results if r["status"] != "found"]})
    if act == "list":
        repos = load_workspace(c.root)
        for r in repos:
            r["exists"] = os.path.isdir(r["path"])
            r["setup"] = r["exists"] and repo_setup_state(r["path"])
        out({"ok": True, "workspace": c.root, "repos": repos, "needs_setup": [r["name"] for r in repos if r["exists"] and not r["setup"]]})
    if act == "add":
        if not (a.name and a.path):
            out({"ok": False, "error": "repos add needs --name and --path"}, 2)
        p = os.path.abspath(os.path.expanduser(a.path))
        if not os.path.isdir(p):
            out({"ok": False, "error": f"not a directory: {p}"}, 2)
        register_repo(c.root, a.name, p, a.url or "", "found")
        out({"ok": True, "name": a.name, "path": p, "setup": repo_setup_state(p)})
    if act == "clone":
        url = (a.url or "").strip()
        if not URL_RE.match(url) or url.startswith("-"):
            out({"ok": False, "error": "refusing to clone: only https://, ssh:// and git@host: URLs are allowed"}, 2)
        if not a.confirmed:
            out({"ok": False, "error": "clone needs the human's confirmation of the URL and destination: ask first, then pass --confirmed"}, 2)
        name = a.name or repo_name(url)
        dest = os.path.abspath(os.path.expanduser(a.path)) if a.path else os.path.join(clone_base(c.root), name)
        if os.path.exists(dest):
            out({"ok": False, "error": f"destination already exists: {dest}. Use it as an existing repo (repos add) or pick another path"}, 2)
        r = subprocess.run(["git", "clone", "--", url, dest], capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            out({"ok": False, "error": "git clone failed: " + (r.stderr.strip()[-300:] or "unknown error")}, 2)
        register_repo(c.root, name, dest, url, "cloned")
        out({"ok": True, "name": name, "path": dest, "url": url, "setup": False})
    out({"ok": False, "error": f"unknown repos action {act}"}, 2)


# --------------------------------------------------------------------------- config update / migration
# `update` brings an existing project's ghe/config.yaml up to date after the toolkit was updated.
# It is ADDITIVE: it never changes a value the user set. Optional new settings are appended as
# commented examples; the only value it ever writes is graphify.enabled, and only when the user
# decided (--graphify true|false). Every change keeps ghe/config.yaml.bak and is validated.
CONFIG_VERSION = 2
CONFIG_MARKER = "# --- added by `ghe update` (config_version {v}) ---"
CONFIG_NOTES = {
    "context": ("# Bounded context packets for large sources (these are the defaults; the keys are optional)\n"
                "#context:\n#  max_entries: 12\n#  max_chars: 6000\n#  max_source_bytes: 2000000\n"),
    "graphify": ("# Optional Graphify provider (advisory only; GHE works without it).\n"
                 "# Decide with `/ghe:graphify on|off` (or `/ghe-graphify`), which writes `enabled: true|false` here.\n"
                 "#graphify:\n#  enabled: false\n#  command: graphify\n#  timeout_seconds: 120\n"),
}


def _config_paths(root):
    p = os.path.join(root, "ghe", "config.yaml")
    return p, p + ".bak"


def graphify_decision(cfg):
    """('enabled'|'disabled'|'undecided'|'invalid', bool_or_None). Absent key = undecided (treated as off)."""
    g = (cfg or {}).get("graphify")
    en = g.get("enabled") if isinstance(g, dict) else None
    if isinstance(en, bool):
        return ("enabled" if en else "disabled"), en
    return ("undecided" if en is None else "invalid"), None


def graphify_command(cfg):
    g = (cfg or {}).get("graphify")
    cmd = g.get("command") if isinstance(g, dict) else None
    return cmd.strip() if isinstance(cmd, str) and cmd.strip() else "graphify"


def graphify_installed(cfg):
    cmd = graphify_command(cfg)
    return bool(shutil.which(cmd)) if not os.path.dirname(cmd) else os.access(cmd, os.X_OK)


def _set_top_level_scalar(text, key, value_text):
    """Set `key: value` at the top level of a YAML text, preserving everything else. Appends when absent."""
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        if re.match(rf"^{re.escape(key)}\s*:", ln):
            lines[i] = f"{key}: {value_text}"
            return "\n".join(lines)
    if lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + f"\n{key}: {value_text}\n"


def _set_graphify_enabled_text(text, value):
    """Write graphify.enabled by editing the text (comments and other keys survive)."""
    val = "true" if value else "false"
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        m = re.match(r"^graphify\s*:\s*(.*)$", ln)
        if not m:
            continue
        rest = re.sub(r"\s+#.*$", "", m.group(1)).strip()
        if rest in ("", ):  # block mapping
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or lines[j][:1] in (" ", "\t", "#")):
                j += 1
            for k in range(i + 1, j):
                mm = re.match(r"^(\s+)enabled\s*:\s*[^#\n]*?(\s*#.*)?$", lines[k])
                if mm:
                    lines[k] = f"{mm.group(1)}enabled: {val}{mm.group(2) or ''}"
                    return "\n".join(lines)
            lines.insert(i + 1, f"  enabled: {val}")
            return "\n".join(lines)
        if rest in ("null", "~", "{}"):
            lines[i] = f"graphify:\n  enabled: {val}"
            return "\n".join(lines)
        raise ValueError("graphify: is written in a form `update` will not edit; set enabled: %s by hand" % val)
    if lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + f"\ngraphify:\n  enabled: {val}\n"


def migrate_config(root, graphify=None, dry_run=False, force_enable=False):
    """Return a result dict (never raises for user-data problems; ok=False + error instead)."""
    path, bak = _config_paths(root)
    if not os.path.isfile(path):
        return {"ok": False, "error": "GHE is not set up in this project (ghe/config.yaml is missing). Run /ghe-setup first."}
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        old = yaml.safe_load(text) if text.strip() else {}
    except (OSError, yaml.YAMLError) as exc:
        return {"ok": False, "error": f"ghe/config.yaml is unreadable: {exc}"}
    if not isinstance(old, dict):
        return {"ok": False, "error": "ghe/config.yaml must be a YAML mapping"}
    v_old = old.get("config_version", 1)
    v_old = v_old if isinstance(v_old, int) and not isinstance(v_old, bool) else 1
    if v_old > CONFIG_VERSION:
        return {"ok": False, "error": f"ghe/config.yaml is config_version {v_old}, newer than this toolkit ({CONFIG_VERSION}); update the toolkit"}
    decision, _ = graphify_decision(old)
    if decision == "invalid":
        return {"ok": False, "error": "graphify.enabled must be true or false"}
    installed = graphify_installed(old)
    if graphify is True and not installed and not force_enable:
        return {"ok": False, "action": "not_installed", "installed": False,
                "error": f"graphify executable {graphify_command(old)!r} was not found. Install Graphify first, "
                         "or choose false (GHE works without it); you can run this again later."}
    new, added = text, []
    if v_old < CONFIG_VERSION:
        notes = [k for k in CONFIG_NOTES if k not in old and not (k == "graphify" and graphify is not None)]
        if notes:
            if not new.endswith("\n"):
                new += "\n"
            new += "\n" + CONFIG_MARKER.format(v=CONFIG_VERSION) + "\n" + "".join(CONFIG_NOTES[k] for k in notes)
            added += [f"{k} (commented example)" for k in notes]
    if graphify is not None:
        if decision != ("enabled" if graphify else "disabled"):
            try:
                new = _set_graphify_enabled_text(new, graphify)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            added.append(f"graphify.enabled: {'true' if graphify else 'false'}")
    if v_old < CONFIG_VERSION:
        new = _set_top_level_scalar(new, "config_version", str(CONFIG_VERSION))
        added.append(f"config_version: {CONFIG_VERSION}")
    # validate: parses, and nothing the user set changed (except graphify.enabled, which they asked for)
    try:
        cur = yaml.safe_load(new) if new.strip() else {}
    except yaml.YAMLError as exc:
        return {"ok": False, "error": f"update would produce invalid YAML ({exc}); nothing was changed"}
    expect = json.loads(json.dumps(old))
    expect["config_version"] = max(v_old, CONFIG_VERSION) if v_old < CONFIG_VERSION else v_old
    if graphify is not None:
        g = expect.get("graphify") if isinstance(expect.get("graphify"), dict) else {}
        g["enabled"] = graphify
        expect["graphify"] = g
    if not isinstance(cur, dict) or json.loads(json.dumps(cur)) != expect:
        return {"ok": False, "error": "update would change existing settings; nothing was changed (please report this)"}
    changed = new != text
    if changed and not dry_run:
        atomic_write_text(bak, text)
        atomic_write_text(path, new)
    d2, en2 = graphify_decision(cur)
    warnings = []
    runs = os.path.join(root, "ghe", "runs")
    for name in sorted(os.listdir(runs)) if os.path.isdir(runs) else []:
        s = load_json(os.path.join(runs, name, "state.json"))
        if isinstance(s, dict) and s.get("status") in ("RUNNING", "WAITING_HUMAN"):
            warnings.append(f"run {name} is {s['status']}; the update is additive and does not touch run state")
    res = {"ok": True, "changed": changed, "dry_run": bool(dry_run), "from_version": v_old, "to_version": max(v_old, CONFIG_VERSION),
           "added": added, "backup": os.path.relpath(bak, root) if changed and not dry_run else None,
           "graphify": {"decision": d2, "enabled": bool(en2), "installed": installed, "command": graphify_command(cur)},
           "needs_decision": d2 == "undecided", "warnings": warnings}
    if d2 == "enabled" and not installed:
        res["warnings"].append(f"graphify is enabled but {graphify_command(cur)!r} was not found; GHE falls back to reading the source directly")
    return res


def config_notice(c):
    """Automatic additive migration for init/status. Never blocks or breaks a run; returns a small notice or None."""
    try:
        r = migrate_config(c.root)
    except Exception as exc:  # noqa: BLE001  the run must never fail because of the notice
        return {"error": f"config check failed: {exc}"}
    if not r.get("ok"):
        return {"error": r.get("error")}
    if r["changed"] or r["needs_decision"]:
        n = {"changed": r["changed"], "added": r["added"], "needs_decision": r["needs_decision"]}
        if r["needs_decision"]:
            n["graphify"] = r["graphify"]
            n["hint"] = "Graphify is undecided (treated as off). Ask the user, then run: update --graphify true|false"
        return n
    return None


def cmd_update(a, c):
    gf = None if a.graphify is None else (a.graphify == "true")
    r = migrate_config(c.root, graphify=gf, dry_run=a.dry_run or a.check, force_enable=a.force)
    out(r, 0 if r.get("ok") else 1)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, *opts, run=True):
        sp = sub.add_parser(name)
        sp.add_argument("--root")
        if run:
            sp.add_argument("--run", required=True)
        for o, kw in opts:
            sp.add_argument(o, **kw)
        sp.set_defaults(fn=fn)

    add("init", cmd_init, ("--run-id", {}), ("--resume", {}), run=False)
    add("update", cmd_update, ("--graphify", {"choices": ["true", "false"]}), ("--dry-run", {"action": "store_true"}),
        ("--check", {"action": "store_true"}), ("--force", {"action": "store_true"}), run=False)
    add("bootstrap", cmd_bootstrap, ("--workspace", {"action": "store_true"}), run=False)
    add("repos", cmd_repos, ("action", {"choices": ["find", "list", "add", "clone"]}), ("--hint", {"action": "append"}),
        ("--name", {}), ("--path", {}), ("--url", {}), ("--confirmed", {"action": "store_true"}), run=False)
    add("resolve", cmd_resolve)
    add("start", cmd_start, ("--node", {"required": True}))
    add("result", cmd_result, ("--node", {"required": True}), ("--tokens", {"default": 0}))
    add("resolve-wait", cmd_resolve_wait, ("--node", {"required": True}), ("--answer", {"default": ""}),
        ("--outcome", {"default": "done", "choices": ["done", "rerun"]}), ("--by", {"default": ""}))
    add("skip", cmd_skip, ("--node", {"required": True}), ("--reason", {"default": ""}))
    add("status", cmd_status)
    add("finish", cmd_finish, ("--status", {"required": True, "choices": ["DONE", "FAILED", "CANCELLED", "WAITING_HUMAN"]}), ("--reason", {"default": ""}))
    add("tokens", cmd_tokens, ("--node", {"required": True}), ("--add", {"required": True}),
        ("--event", {"default": ""}))
    add("budget", cmd_budget, ("--node", {}))
    add("waves", cmd_waves, ("--stories", {}))
    add("defects", cmd_defects, ("--round", {"required": True}))
    add("drift-check", cmd_drift, ("--summary", {"required": True}), ("--trigger", {}), ("--before", {"type": int}), ("--after", {"type": int}))
    add("trace", cmd_trace)
    add("ticket", cmd_ticket, ("--kind", {"default": "task", "choices": sorted(TICKET_PARENT_KIND)}), ("--node", {"default": "-"}), ("--ref", {"default": ""}),
        ("--key", {}), ("--title", {"default": ""}), ("--list", {"action": "store_true"}),
        ("--parent", {"default": ""}))
    add("story", cmd_story, ("--id", {"required": True}), ("--status", {"choices": ["pending", "running", "done", "revise", "blocked"]}))
    add("gate", cmd_gate, ("--name", {"required": True}), ("--request", {"required": True}))
    add("gate-decide", cmd_gate_decide, ("--name", {"required": True}),
        ("--decision", {"required": True, "choices": ["approve", "request_changes", "reject"]}), ("--comment", {"default": ""}), ("--by", {"default": ""}))
    add("own-check", cmd_own_check, ("--id", {"required": True}), ("--repo", {"default": ""}),
        ("--files", {"nargs": "*", "default": []}), ("--git", {"default": ""}), ("--base", {"default": ""}))
    add("design-propose", cmd_design_propose, ("--id", {"required": True}), ("--summary", {"default": ""}),
        ("--files", {"nargs": "*", "default": []}), ("--stories", {"nargs": "*", "default": []}),
        ("--tests", {"nargs": "*", "default": []}), ("--reason", {"action": "append", "default": []}),
        ("--round", {"type": int, "default": 0}))
    add("design-decide", cmd_design_decide, ("--id", {"required": True}),
        ("--decision", {"required": True, "choices": ["approve", "reject"]}), ("--by", {"default": ""}))
    add("allowance-grant", cmd_allowance_grant, ("--story", {"required": True}),
        ("--add", {"type": int, "required": True}), ("--reason", {"default": ""}), ("--by", {"default": ""}))
    add("memory-manifest", cmd_memory_manifest, ("--query", {"default": ""}))
    add("verify", cmd_verify, ("--waive", {"action": "store_true"}), ("--reason", {"default": ""}),
        ("--by", {"default": ""}), ("--timeout", {"type": int, "default": 600}))
    add("journal-intent", cmd_journal_intent, ("--key", {"required": True}), ("--action", {"required": True}))
    add("journal-confirm", cmd_journal_confirm, ("--key", {"required": True}), ("--evidence", {"default": ""}))
    add("journal-reconcile", cmd_journal_reconcile, ("--key", {"required": True}),
        ("--outcome", {"required": True, "choices": ["applied", "not_applied"]}), ("--evidence", {"default": ""}))
    add("authorize", cmd_authorize, ("--action", {"required": True, "choices": ["cloud-apply", "destructive"]}))
    add("escalation-report", cmd_escalation_report)
    add("ticket-attach", cmd_ticket_attach, ("--key", {"required": True}), ("--path", {"required": True}),
        ("--capability", {"required": True, "choices": ["supported", "unsupported"]}))
    add("ticket-field", cmd_ticket_field, ("--key", {"required": True}), ("--field", {"required": True}),
        ("--write", {"default": None}), ("--remote", {"default": None}),
        ("--resolve", {"default": None, "choices": ["keep-remote", "overwrite"]}), ("--by", {"default": ""}))
    add("tracker-down", cmd_tracker_down, ("--error", {"required": True}))
    add("tracker-resume", cmd_tracker_resume, ("--by", {"default": ""}))
    add("tracker-switch", cmd_tracker_switch, ("--to", {"required": True, "choices": ["local", "jira"]}),
        ("--by", {"default": ""}), ("--reason", {"default": ""}))
    add("context-packet", cmd_context_packet, ("--query", {"default": ""}), ("--source", {"action": "append"}))
    add("deploy-target", cmd_deploy_target)
    add("release-check", cmd_release_check, ("--env", {"required": True, "choices": ["production", "test"]}))
    add("release-record", cmd_release_record, ("--candidate", {"default": ""}), ("--health", {"default": ""}),
        ("--health-url", {"default": ""}), ("--pid", {"type": int, "default": None}),
        ("--port", {"type": int, "default": None}))
    add("release-cleanup", cmd_release_cleanup)
    a = p.parse_args()
    c = Ctx(a)
    if getattr(a, "run", None) and a.cmd != "status" and os.path.isdir(c.run):
        with run_lock(c.run):  # one command at a time per run: no lost updates
            a.fn(a, c)
    else:
        a.fn(a, c)


if __name__ == "__main__":
    main()
