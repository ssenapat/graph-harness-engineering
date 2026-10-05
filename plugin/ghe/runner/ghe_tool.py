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
import datetime
import fnmatch
import hashlib
import json
import os
import re
import shutil
import sys

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
    return yaml.safe_load(text) or {}


def load_json(path, default=None):
    text = read(path)
    if text is None:
        return default
    try:
        return json.loads(text)
    except ValueError as exc:
        out({"ok": False, "error": f"invalid JSON in {path}: {exc}"}, 2)


def write_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
        fh.write("\n")


def frontmatter(text):
    m = re.match(r"^---\n(.*?)\n---\n", text or "", re.S)
    return (yaml.safe_load(m.group(1)) or {}) if m else {}


class Ctx:
    def __init__(self, args):
        self.root = os.path.abspath(args.root or os.getcwd())
        self.run = os.path.abspath(args.run) if getattr(args, "run", None) else None
        cfg_path = os.path.join(self.root, "ghe", "config.yaml")
        graph_path = os.path.join(self.root, "ghe", "graph.yaml")
        if getattr(args, "cmd", None) == "bootstrap":  # bootstrap is what creates these files
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


def cmd_init(a, c):
    if not c.config or not c.graph:
        out({"ok": False, "error": "GHE is not set up in this project (ghe/config.yaml or ghe/graph.yaml is missing). "
             "Run /ghe-setup (or /ghe:setup when installed as a plugin) first."}, 2)
    bad = jira_config_problems(c.config)
    if bad:
        out({"ok": False, "error": "tracker is jira but ghe/config.yaml is not configured: " + "; ".join(bad)
             + ". Run /ghe-setup (/ghe:setup as a plugin) or set tracker: local."}, 2)
    runs = os.path.join(c.root, "ghe", "runs")
    os.makedirs(runs, exist_ok=True)
    day = datetime.date.today().isoformat()
    n = 1 + sum(1 for d in os.listdir(runs) if d.startswith(f"ghe-{day}-"))
    run_id = a.run_id or f"ghe-{day}-{n:03d}"
    run = os.path.join(runs, run_id)
    for sub in ("artifacts", "artifacts/plans", "artifacts/defects", "nodes"):
        os.makedirs(os.path.join(run, sub), exist_ok=True)
    st = {"run_id": run_id, "status": "PENDING", "created": now(), "source_hash": None,
          "tokens_total": 0, "round": 0, "nodes": {}, "gates": {}, "compactions": [],
          "fingerprints": {}, "warnings": []}
    write_json(os.path.join(run, "state.json"), st)
    out({"ok": True, "run_id": run_id, "run_dir": run})


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
    with open(os.path.join(c.run, "graph.resolved.yaml"), "w", encoding="utf-8") as fh:
        yaml.safe_dump(resolved, fh, sort_keys=False)
    st = c.state()
    st["status"] = "RUNNING"
    st["warnings"] = warnings
    for n in nodes:
        st["nodes"].setdefault(n, {"status": "pending", "attempts": 0, "tokens": 0, "output_hash": None})
    c.save(st)
    out({"ok": True, **resolved})


# ----------------------------------------------------------------- node lifecycle
def node_artifact_hash(c, artifacts, result_text=""):
    h = hashlib.sha256()
    h.update((result_text or "").encode())
    for rel in sorted(artifacts):
        h.update(rel.encode())
        h.update((read(c.art(rel), "") or "").encode())
    return h.hexdigest()


def cmd_start(a, c):
    st = c.state()
    n = st["nodes"].get(a.node)
    if n is None:
        out({"ok": False, "error": f"unknown node {a.node}"}, 2)
    cap = 1 + c.config["budgets"].get("node_retry", 1)
    if n["attempts"] >= cap:
        out({"ok": False, "error": f"node {a.node} exhausted {cap} attempts", "action": "escalate"}, 1)
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
        for rel in res.get("artifacts") or []:
            if not os.path.exists(c.art(rel)):
                problems.append(f"artifact missing: {rel}")
    unchanged = False
    new_hash = node_artifact_hash(c, res.get("artifacts") or [], read(path, ""))
    if not problems and n.get("output_hash") and n["output_hash"] == new_hash and n["attempts"] > 1:
        unchanged = True
        problems.append("output identical to previous attempt (spec §9.3): not retrying unchanged")
    tokens = int(a.tokens or 0)
    if tokens:
        n["tokens"] += tokens
        st["tokens_total"] += tokens
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
    c.save(st)
    out({"ok": not problems, "node_status": n["status"], "problems": problems,
         "retry_allowed": retry_allowed, "open_questions": n.get("open_questions", []),
         "tickets": n.get("tickets", [])}, 0 if not problems else 1)


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
    n.setdefault("human_answers", []).append({"at": now(), "answer": a.answer or ""})
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
    st["nodes"][a.node]["status"] = "skipped"
    c.save(st)
    out({"ok": True})


def cmd_status(a, c):
    st = c.state()
    out({"ok": True, "run_id": st["run_id"], "status": st["status"], "round": st["round"],
         "tokens_total": st["tokens_total"], "gates": st["gates"],
         "nodes": {k: v["status"] for k, v in st["nodes"].items()},
         "pending_gate": next((k for k, v in st["gates"].items() if v.get("status") == "waiting"), None)})


def cmd_finish(a, c):
    st = c.state()
    st["status"] = a.status
    c.save(st)
    out({"ok": True, "status": a.status})


# ----------------------------------------------------------------- tokens + budgets (spec §11)
def cmd_tokens(a, c):
    st = c.state()
    st["nodes"].setdefault(a.node, {"status": "pending", "attempts": 0, "tokens": 0, "output_hash": None})
    st["nodes"][a.node]["tokens"] += int(a.add)
    st["tokens_total"] += int(a.add)
    c.save(st)
    out({"ok": True, "tokens_total": st["tokens_total"]})


def cmd_budget(a, c):
    st, b = c.state(), c.config["budgets"]
    ceiling = b["max_tokens_per_run"]
    total = st["tokens_total"]
    comp = b.get("compaction", {})
    node_cap = b.get("max_tokens_per_node", {}).get(a.node) if a.node else None
    node_tok = st["nodes"].get(a.node, {}).get("tokens", 0) if a.node else 0
    actions = []
    if total >= ceiling:
        actions.append("stop")
    elif total >= comp.get("run_soft_threshold", 0.8) * ceiling:
        actions.append("compact_run")
    if node_cap and node_tok >= node_cap:
        actions.append("stop_node")
    elif node_cap and node_tok >= comp.get("node_context_threshold", 0.7) * node_cap:
        actions.append("compact_node")
    action = actions[0] if actions else "ok"
    out({"ok": True, "action": action, "actions": actions, "tokens_total": total, "ceiling": ceiling,
         "pct": round(100 * total / ceiling, 1), "node_tokens": node_tok, "node_cap": node_cap})


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
    return any(globs_overlap(x, y) for x in s1.get("owns", []) for y in s2.get("owns", []))


def cmd_waves(a, c):
    data = load_json(a.stories or c.art("plans", "stories.json"))
    if not data:
        out({"ok": False, "error": "stories.json not found"}, 2)
    stories = {s["id"]: s for s in data["stories"]}
    errors = []
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
def cmd_ticket(a, c):
    """Idempotent ticket registry. Key is (kind, node, ref); a repeat add returns the existing entry."""
    st = c.state()
    reg = st.setdefault("tickets", {})
    if a.list:
        out({"ok": True, "tickets": reg})
    ident = f"{a.kind}:{a.node}:{a.ref or '-'}"
    if ident in reg:
        out({"ok": True, "existing": True, "ticket": reg[ident]})
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
    reg[ident] = {"key": a.key, "kind": a.kind, "node": a.node, "ref": a.ref, "title": a.title, "created": now()}
    c.save(st)
    out({"ok": True, "existing": False, "ticket": reg[ident]})


def cmd_story(a, c):
    st = c.state()
    stories = st.setdefault("stories", {})
    s = stories.setdefault(a.id, {"status": "pending", "revisions": 0})
    if a.status:
        s["status"] = a.status
        if a.status == "revise":
            s["revisions"] += 1
            if s["revisions"] > c.config["budgets"].get("max_story_revisions", 2):
                s["status"] = "blocked"
                st["status"] = "WAITING_HUMAN"
    c.save(st)
    out({"ok": s["status"] != "blocked", "story": a.id, **s,
         "max_revisions": c.config["budgets"].get("max_story_revisions", 2)}, 0 if s["status"] != "blocked" else 1)


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
    data = load_json(path, {"round": n, "defects": []})
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
    elif n > 1 and cur >= prev and prev:
        verdict = "no_progress"
    elif n >= maxr:
        verdict = "max_rounds"
    else:
        verdict = "progress" if n > 1 else "first"
    c.save(st)
    out({"ok": True, "round": n, "failing": len(cur), "fixed": sorted(prev - cur),
         "regressions": sorted(cur - prev) if n > 1 else [], "verdict": verdict,
         "escalate": verdict in ("no_progress", "max_rounds"), "fingerprints": fps})


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
def cmd_trace(a, c):
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
    with open(c.art("traceability.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(md) + "\n")
    out({"ok": not gaps, "requirements": len(reqs), "gaps": gaps}, 0 if not gaps else 1)


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
    g["decision"], g["comment"], g["decided"] = dec, a.comment, now()
    maxrev = c.config["budgets"].get("max_gate_revisions", 2)
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


# ----------------------------------------------------------------- main
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

    add("init", cmd_init, ("--run-id", {}), run=False)
    add("bootstrap", cmd_bootstrap, run=False)
    add("resolve", cmd_resolve)
    add("start", cmd_start, ("--node", {"required": True}))
    add("result", cmd_result, ("--node", {"required": True}), ("--tokens", {"default": 0}))
    add("resolve-wait", cmd_resolve_wait, ("--node", {"required": True}), ("--answer", {"default": ""}),
        ("--outcome", {"default": "done", "choices": ["done", "rerun"]}))
    add("skip", cmd_skip, ("--node", {"required": True}))
    add("status", cmd_status)
    add("finish", cmd_finish, ("--status", {"required": True, "choices": ["DONE", "FAILED", "CANCELLED", "WAITING_HUMAN"]}))
    add("tokens", cmd_tokens, ("--node", {"required": True}), ("--add", {"required": True}))
    add("budget", cmd_budget, ("--node", {}))
    add("waves", cmd_waves, ("--stories", {}))
    add("defects", cmd_defects, ("--round", {"required": True}))
    add("drift-check", cmd_drift, ("--summary", {"required": True}), ("--trigger", {}), ("--before", {"type": int}), ("--after", {"type": int}))
    add("trace", cmd_trace)
    add("ticket", cmd_ticket, ("--kind", {"default": "task"}), ("--node", {"default": "-"}), ("--ref", {"default": ""}),
        ("--key", {}), ("--title", {"default": ""}), ("--list", {"action": "store_true"}))
    add("story", cmd_story, ("--id", {"required": True}), ("--status", {"choices": ["pending", "running", "done", "revise", "blocked"]}))
    add("gate", cmd_gate, ("--name", {"required": True}), ("--request", {"required": True}))
    add("gate-decide", cmd_gate_decide, ("--name", {"required": True}),
        ("--decision", {"required": True, "choices": ["approve", "request_changes", "reject"]}), ("--comment", {"default": ""}))
    a = p.parse_args()
    c = Ctx(a)
    a.fn(a, c)


if __name__ == "__main__":
    main()
