#!/usr/bin/env python3
"""ghe_graph.py - optional Graphify context provider for GHE (spec GF-01..GF-04, CT-04; T43-T49).

Graphify is OPTIONAL. Nothing here requires it; when it is absent, disabled, failing, stale or
timed out, `retrieve` still returns a bounded packet read directly from the current source.

Every command prints ONE JSON object on stdout and exits 0 (ok) / 1 (check failed) / 2 (usage/error).
Stdlib + PyYAML only. Never calls an LLM. Never uploads anything.

Usage:
  python3 ghe_graph.py preflight --root PROJECT [--force]
  python3 ghe_graph.py retrieve  --root PROJECT --query TEXT [--max-chars N] [--scope REL_DIR]
  python3 ghe_graph.py status    --root PROJECT [--check]     (--check: exit 1 unless graph is fresh)

preflight  decides action = skip | reuse | build | refresh | fallback, with a `reason`; exit 0 for all of
           them (fallback is a degraded-but-working outcome: use `retrieve`, which reads source directly).
retrieve   bounded direct-source packet (<= max chars). Uses the graph ONLY if the manifest is fresh.
           Every item carries provenance extracted|inferred; inferred items are authoritative=false and
           no graph item is ever acceptance_eligible (graph edges may suggest impact, never authorize
           completion/reopening - GF-03). Only direct-source items are acceptance_eligible.
status     config in effect, manifest, freshness verdict, last attempt.

Config: <root>/ghe/config.yaml, key `graphify:` (this module applies defaults when absent; config.yaml
is not edited by this module):
  enabled: false                       # master switch; default OFF
  command: graphify                    # executable (looked up on PATH or a path); never required
  build_args: ["."]                    # args after the command; run with cwd=root
  semantic_extraction: false           # opt-in; default OFF (GF-04)
  semantic_approved: false             # semantic also needs this explicit approval ...
  semantic_provider: ""                # ... plus a named provider; otherwise it is ignored
  semantic_args: ["--semantic"]        # appended only when semantic is fully authorized
  output_dir: graphify-out             # relative to root; graph file = <output_dir>/graph.json
  timeout_seconds: 120                 # hard wall-clock limit for one build; process group killed
  max_index_attempts_per_preflight: 1  # hard cap on builds per preflight (never a retry loop)
  max_packet_chars: 8000               # default retrieve cap
  max_graph_bytes: 5000000             # larger graph.json is not loaded
  min_source_bytes: 20                 # non-blank bytes for a file to count as meaningful content
Manifest: <root>/ghe/.graph/index-manifest.json (atomic temp+fsync+replace); last attempt:
<root>/ghe/.graph/last-attempt.json. The graph is fresh only if repo, schema, indexed SHA, the
dirty-input fingerprint (content hash of source files, so uncommitted edits are detected) and the
graph file hash all match. Anything else is non-authoritative (GF-02).
"""
import argparse
import contextlib
import datetime
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile

try:
    import yaml
except ImportError:  # pragma: no cover
    print(json.dumps({"ok": False, "error": "PyYAML is required: pip install pyyaml"}))
    sys.exit(2)

SCHEMA = "ghe-graph-manifest/1"
DEFAULTS = {
    "enabled": False,
    "command": "graphify",
    "build_args": ["."],
    "semantic_extraction": False,
    "semantic_approved": False,
    "semantic_provider": "",
    "semantic_args": ["--semantic"],
    "output_dir": "graphify-out",
    "timeout_seconds": 120,
    "max_index_attempts_per_preflight": 1,
    "max_packet_chars": 8000,
    "max_graph_bytes": 5_000_000,
    "min_source_bytes": 20,
}
CODE_EXTS = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".java", ".rs", ".rb", ".c", ".h", ".cpp",
             ".cs", ".kt", ".swift", ".php", ".sh", ".sql"}
DOC_EXTS = {".md", ".txt", ".yaml", ".yml", ".json", ".toml"}
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".tox",
             ".mypy_cache", ".pytest_cache"}
TOP_SKIP = {"ghe", ".claude", ".ghe"}  # toolkit/run state is not project source
SECRET_RE = re.compile(r"(^\.env|\.pem$|\.key$|id_rsa|id_ed25519|credentials|secrets?\.|\.p12$|\.pfx$)", re.I)
ENV_SCRUB_RE = re.compile(r"(API_?KEY|TOKEN|SECRET|PASSWORD|ANTHROPIC|OPENAI|AWS_|GOOGLE_APPLICATION)", re.I)
MAX_SCAN_FILES = 5000
MAX_FILE_READ = 64 * 1024
MAX_SNIPPET = 200


class GraphError(Exception):
    pass


# ----------------------------------------------------------------- helpers
def out(obj, code=0):
    print(json.dumps(obj, indent=2))
    sys.exit(code)


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_write_json(path, obj):
    """temp file in same dir + flush + fsync + os.replace: never a half-written file."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def read_json(path, default=None, max_bytes=None):
    try:
        if max_bytes is not None and os.path.getsize(path) > max_bytes:
            return default
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def safe_join(root, rel):
    """Join rel under root; refuse absolute paths, '..' and symlink escapes."""
    if not isinstance(rel, str) or not rel or os.path.isabs(rel) or "\x00" in rel:
        raise GraphError(f"unsafe path: {rel!r}")
    if ".." in rel.replace("\\", "/").split("/"):
        raise GraphError(f"path traversal refused: {rel!r}")
    real_root = os.path.realpath(root)
    full = os.path.realpath(os.path.join(real_root, rel))
    if full != real_root and not full.startswith(real_root + os.sep):
        raise GraphError(f"path escapes project root: {rel!r}")
    return full


def resolve_root(arg):
    if not arg or not os.path.isdir(arg):
        raise GraphError(f"--root is not a directory: {arg}")
    return os.path.realpath(arg)


def load_config(root):
    cfg = dict(DEFAULTS)
    path = os.path.join(root, "ghe", "config.yaml")
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except OSError:
        data = {}
    except yaml.YAMLError as exc:
        raise GraphError(f"invalid YAML in {path}: {exc}")
    sec = data.get("graphify") if isinstance(data, dict) else None
    if isinstance(sec, dict):
        for k in DEFAULTS:
            if k in sec and sec[k] is not None:
                cfg[k] = sec[k]
    # normalise / bound
    cfg["enabled"] = cfg["enabled"] is True
    cfg["semantic_extraction"] = cfg["semantic_extraction"] is True
    cfg["semantic_approved"] = cfg["semantic_approved"] is True
    for k, lo, hi in (("timeout_seconds", 1, 3600), ("max_index_attempts_per_preflight", 1, 3),
                      ("max_packet_chars", 200, 200000), ("max_graph_bytes", 1000, 50_000_000),
                      ("min_source_bytes", 1, 100000)):
        try:
            cfg[k] = max(lo, min(hi, int(cfg[k])))
        except (TypeError, ValueError):
            cfg[k] = DEFAULTS[k]
    if isinstance(cfg["build_args"], str):
        cfg["build_args"] = cfg["build_args"].split()
    if isinstance(cfg["semantic_args"], str):
        cfg["semantic_args"] = cfg["semantic_args"].split()
    cfg["build_args"] = [str(a) for a in (cfg["build_args"] or [])]
    cfg["semantic_args"] = [str(a) for a in (cfg["semantic_args"] or [])]
    cfg["command"] = str(cfg["command"] or "graphify")
    cfg["output_dir"] = str(cfg["output_dir"] or "graphify-out")
    # semantic needs opt-in AND approval AND provider (GF-04); otherwise it is ignored
    cfg["semantic_effective"] = bool(cfg["semantic_extraction"] and cfg["semantic_approved"]
                                     and str(cfg["semantic_provider"]).strip())
    return cfg


def paths(root, cfg):
    gdir = safe_join(root, os.path.join("ghe", ".graph"))
    out_dir = safe_join(root, cfg["output_dir"])  # refuses traversal in config
    return {
        "manifest": os.path.join(gdir, "index-manifest.json"),
        "attempt": os.path.join(gdir, "last-attempt.json"),
        "out_dir": out_dir,
        "graph": os.path.join(out_dir, "graph.json"),
    }


# ----------------------------------------------------------------- source scan
def iter_files(root, cfg, exts, scope=None):
    """Bounded, deterministic walk. Skips symlinks, ignored dirs, secrets and the generated output dir."""
    base = root if scope is None else scope
    out_rel = cfg["output_dir"].strip("/").split("/")[0]
    count = 0
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        top = rel_dir.split(os.sep)[0] if rel_dir != "." else ""
        if top in TOP_SKIP or top == out_rel:
            dirnames[:] = []
            continue
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and d != out_rel
                             and not os.path.islink(os.path.join(dirpath, d))
                             and not (rel_dir == "." and d in TOP_SKIP))
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            if os.path.splitext(fn)[1].lower() not in exts or SECRET_RE.search(fn) or os.path.islink(full):
                continue
            count += 1
            if count > MAX_SCAN_FILES:
                return
            yield full, os.path.relpath(full, root)


def read_head(path, limit=MAX_FILE_READ):
    try:
        with open(path, "rb") as fh:
            return fh.read(limit)
    except OSError:
        return b""


def meaningful_content(root, cfg):
    n = 0
    for full, _ in iter_files(root, cfg, CODE_EXTS):
        if len(b"".join(read_head(full, 4096).split())) >= cfg["min_source_bytes"]:
            n += 1
    return n


def input_fingerprint(root, cfg):
    h = hashlib.sha256()
    for full, rel in iter_files(root, cfg, CODE_EXTS | DOC_EXTS):
        h.update(rel.encode())
        h.update(b"\0")
        h.update(hashlib.sha256(read_head(full)).digest())
        with contextlib.suppress(OSError):
            h.update(str(os.path.getsize(full)).encode())
    return h.hexdigest()


def git(root, *args):
    try:
        r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def git_state(root, cfg):
    sha = git(root, "rev-parse", "HEAD")
    if sha is None:
        return None, False
    excl = [":!" + cfg["output_dir"].strip("/"), ":!ghe", ":!.claude"]
    porcelain = git(root, "status", "--porcelain", "--", ".", *excl)
    return sha, bool(porcelain)


def sha256_file(path):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


# ----------------------------------------------------------------- freshness
def freshness(root, cfg, p):
    """Return (fresh: bool, reason: str, manifest|None). Anything uncertain is non-authoritative."""
    m = read_json(p["manifest"])
    if not isinstance(m, dict):
        return False, "missing_manifest", None
    if m.get("schema") != SCHEMA:
        return False, "schema_mismatch", m
    if m.get("repo") != root:
        return False, "wrong_repo", m
    if not os.path.isfile(p["graph"]):
        return False, "missing_graph", m
    if m.get("graph_hash") != sha256_file(p["graph"]):
        return False, "graph_hash_mismatch", m
    sha, dirty = git_state(root, cfg)
    if m.get("indexed_sha") != sha:
        return False, "stale_sha", m
    if m.get("input_fingerprint") != input_fingerprint(root, cfg):
        return False, ("stale_dirty_worktree" if dirty else "stale_inputs"), m
    return True, "fresh", m


# ----------------------------------------------------------------- build
def build_env(cfg):
    env = dict(os.environ)
    if not cfg["semantic_effective"]:
        for k in list(env):
            if ENV_SCRUB_RE.search(k):
                del env[k]
        env["GRAPHIFY_NO_CLOUD"] = "1"
        env["GHE_GRAPHIFY_SEMANTIC"] = "0"
    else:
        env["GHE_GRAPHIFY_SEMANTIC"] = "1"
    return env


def run_build(root, cfg, p):
    """One bounded build. Returns dict(status=ok|failed|timeout|absent|bad_output, detail)."""
    exe = shutil.which(cfg["command"]) or (cfg["command"] if os.path.isfile(cfg["command"])
                                           and os.access(cfg["command"], os.X_OK) else None)
    if not exe:
        return {"status": "absent", "detail": f"command not found: {cfg['command']}"}
    argv = [exe] + cfg["build_args"] + (cfg["semantic_args"] if cfg["semantic_effective"] else [])
    existed = os.path.isdir(p["out_dir"])
    try:
        proc = subprocess.Popen(argv, cwd=root, env=build_env(cfg), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
    except OSError as exc:
        return {"status": "failed", "detail": f"cannot start: {exc}"}
    try:
        so, se = proc.communicate(timeout=cfg["timeout_seconds"])
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError, ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            proc.communicate(timeout=5)
        if not existed:  # bounded cleanup of partial output we created
            shutil.rmtree(p["out_dir"], ignore_errors=True)
        else:
            with contextlib.suppress(OSError):
                os.remove(p["graph"] + ".partial")
        return {"status": "timeout", "detail": f"killed after {cfg['timeout_seconds']}s"}
    if proc.returncode != 0:
        if not existed:
            shutil.rmtree(p["out_dir"], ignore_errors=True)
        return {"status": "failed", "detail": f"exit {proc.returncode}: {(se or so or '').strip()[-300:]}"}
    graph = read_json(p["graph"], max_bytes=cfg["max_graph_bytes"])
    if not isinstance(graph, dict):
        return {"status": "bad_output", "detail": f"no valid {cfg['output_dir']}/graph.json produced"}
    return {"status": "ok", "detail": "built"}


def write_manifest(root, cfg, p):
    sha, dirty = git_state(root, cfg)
    m = {
        "schema": SCHEMA,
        "repo": root,
        "indexed_sha": sha,
        "worktree_dirty": dirty,
        "input_fingerprint": input_fingerprint(root, cfg),
        "producer": os.path.basename(cfg["command"]),
        "extraction_scope": "semantic" if cfg["semantic_effective"] else "code-only",
        "graph_path": cfg["output_dir"].rstrip("/") + "/graph.json",
        "graph_hash": sha256_file(p["graph"]),
        "indexed_at": now(),
    }
    atomic_write_json(p["manifest"], m)
    return m


# ----------------------------------------------------------------- commands
def cmd_preflight(a):
    root = resolve_root(a.root)
    cfg = load_config(root)
    p = paths(root, cfg)
    res = {"ok": True, "command": "preflight", "action": None, "reason": None, "attempts": 0,
           "semantic_effective": cfg["semantic_effective"], "fallback": False}
    if cfg["semantic_extraction"] and not cfg["semantic_effective"]:
        res["semantic_note"] = "semantic_extraction requested but not approved/provider-configured: ignored (code-only)"
    if not cfg["enabled"]:
        res.update(action="skip", reason="disabled")
        out(res)
    if meaningful_content(root, cfg) == 0:
        res.update(action="skip", reason="empty_project")  # T44: no index build, no subprocess
        out(res)
    fresh, why, _ = freshness(root, cfg, p)
    if fresh:
        res.update(action="reuse", reason="fresh")  # T45
        out(res)
    had_graph = why not in ("missing_manifest", "missing_graph")
    kind = "refresh" if had_graph else "build"
    fp = input_fingerprint(root, cfg)
    last = read_json(p["attempt"])
    if (not a.force and isinstance(last, dict) and last.get("status") != "ok"
            and last.get("input_fingerprint") == fp and last.get("repo") == root):
        # T49: never loop on a failed/timed-out build for identical inputs
        res.update(action="fallback", reason=f"previous_attempt_{last.get('status')}_same_inputs",
                   stale_reason=why, fallback=True)
        out(res)
    result = None
    for _ in range(cfg["max_index_attempts_per_preflight"]):
        res["attempts"] += 1
        result = run_build(root, cfg, p)
        if result["status"] in ("ok", "timeout", "absent"):  # never retry timeouts/absence
            break
    status = result["status"]
    atomic_write_json(p["attempt"], {"status": status, "detail": result["detail"], "at": now(),
                                     "repo": root, "input_fingerprint": fp, "kind": kind})
    if status == "ok":
        m = write_manifest(root, cfg, p)
        res.update(action=kind, reason=why, manifest=m)
        out(res)
    reason = {"absent": "graphify_absent", "timeout": "index_timeout"}.get(status, f"index_{status}")
    res.update(action="fallback", reason=reason, stale_reason=why, detail=result["detail"], fallback=True)
    out(res)


def terms_of(query):
    seen, terms = set(), []
    for t in re.findall(r"[A-Za-z0-9_]{3,}", query or ""):
        tl = t.lower()
        if tl not in seen:
            seen.add(tl)
            terms.append(tl)
    return terms[:12]


def direct_items(root, cfg, terms, scope, cap_items=200):
    items = []
    for full, rel in iter_files(root, cfg, CODE_EXTS | DOC_EXTS, scope):
        data = read_head(full).decode("utf-8", "replace")
        for i, line in enumerate(data.splitlines(), 1):
            low = line.lower()
            if any(t in low for t in terms):
                items.append({"source": "direct", "path": rel.replace(os.sep, "/"), "line": i,
                              "snippet": line.strip()[:MAX_SNIPPET], "provenance": "extracted",
                              "authoritative": True, "acceptance_eligible": True})
                if len(items) >= cap_items:
                    return items
    return items


def norm_prov(v):
    v = str(v or "").strip().lower()
    return "extracted" if v == "extracted" else "inferred"  # unknown/uncertain => inferred


def graph_items(graph, terms, cap_items=100):
    items = []
    nodes = {n.get("id"): n for n in graph.get("nodes", []) if isinstance(n, dict)}
    for n in nodes.values():
        blob = f"{n.get('label', '')} {n.get('file', '')} {n.get('id', '')}".lower()
        if any(t in blob for t in terms):
            items.append({"source": "graph", "kind": "node", "id": str(n.get("id"))[:120],
                          "label": str(n.get("label", ""))[:120], "path": n.get("file"),
                          "provenance": norm_prov(n.get("provenance") or n.get("confidence") or "extracted"),
                          "authoritative": False, "acceptance_eligible": False})
    for e in graph.get("edges", []) if isinstance(graph.get("edges"), list) else []:
        if not isinstance(e, dict):
            continue
        s, t = e.get("source"), e.get("target")
        blob = f"{s} {t} {nodes.get(s, {}).get('label', '')} {nodes.get(t, {}).get('label', '')}".lower()
        if any(x in blob for x in terms):
            prov = norm_prov(e.get("provenance") or e.get("confidence"))
            items.append({"source": "graph", "kind": "edge", "from": str(s)[:120], "to": str(t)[:120],
                          "relation": str(e.get("relation", ""))[:60], "provenance": prov,
                          "authoritative": False, "acceptance_eligible": False,
                          **({"note": "inferred: suggests impact only; read current source to confirm"}
                             if prov == "inferred" else {})})
        if len(items) >= cap_items:
            break
    for it in items:  # graph items never authoritative: must be confirmed in current source
        it["authoritative"] = False
    return items


def cmd_retrieve(a):
    root = resolve_root(a.root)
    cfg = load_config(root)
    p = paths(root, cfg)
    scope = safe_join(root, a.scope) if a.scope else None
    if scope is not None and not os.path.isdir(scope):
        raise GraphError(f"--scope is not a directory: {a.scope}")
    cap = max(200, min(200000, a.max_chars if a.max_chars else cfg["max_packet_chars"]))
    terms = terms_of(a.query)
    res = {"ok": True, "command": "retrieve", "query": a.query, "graph_used": False,
           "graph_status": "disabled" if not cfg["enabled"] else None, "items": [],
           "truncated": False, "max_chars": cap,
           "acceptance_note": "graph items are never sole source for acceptance; confirm in current source"}
    if not terms:
        res["graph_status"] = res["graph_status"] or "n/a"
        out(res)
    pool = []
    if cfg["enabled"]:
        fresh, why, _ = freshness(root, cfg, p)
        res["graph_status"] = why
        if fresh:
            g = read_json(p["graph"], max_bytes=cfg["max_graph_bytes"])
            if isinstance(g, dict):
                res["graph_used"] = True
                pool.extend(graph_items(g, terms))
            else:
                res["graph_status"] = "graph_unreadable"
        else:
            res["fallback_reason"] = why
    pool.extend(direct_items(root, cfg, terms, scope))
    used = len(json.dumps(res)) + 200
    for it in pool:
        cost = len(json.dumps(it)) + 2
        if used + cost > cap:
            res["truncated"] = True
            break
        res["items"].append(it)
        used += cost
    res["item_count"] = len(res["items"])
    out(res)


def cmd_status(a):
    root = resolve_root(a.root)
    cfg = load_config(root)
    p = paths(root, cfg)
    fresh, why, m = freshness(root, cfg, p)
    res = {"ok": True, "command": "status", "enabled": cfg["enabled"], "fresh": fresh, "reason": why,
           "semantic_effective": cfg["semantic_effective"], "command_available": bool(
               shutil.which(cfg["command"]) or os.path.isfile(cfg["command"])),
           "manifest": m, "last_attempt": read_json(p["attempt"]),
           "meaningful_source_files": meaningful_content(root, cfg)}
    if a.check and not fresh:
        res["ok"] = False
        out(res, 1)
    out(res)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ghe_graph.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("preflight")
    s.add_argument("--root", required=True)
    s.add_argument("--force", action="store_true", help="retry even if the last attempt failed on identical inputs")
    s = sub.add_parser("retrieve")
    s.add_argument("--root", required=True)
    s.add_argument("--query", required=True)
    s.add_argument("--max-chars", type=int, default=0)
    s.add_argument("--scope", default=None)
    s = sub.add_parser("status")
    s.add_argument("--root", required=True)
    s.add_argument("--check", action="store_true")
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        sys.exit(2 if e.code else 0)
    try:
        {"preflight": cmd_preflight, "retrieve": cmd_retrieve, "status": cmd_status}[a.cmd](a)
    except GraphError as exc:
        out({"ok": False, "error": str(exc)}, 2)


if __name__ == "__main__":
    main()
