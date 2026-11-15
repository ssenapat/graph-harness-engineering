# Optional Graphify provider (`ghe_graph.py`)

Spec GF-01..GF-04, CT-04; acceptance T43-T49. Graphify is never required: when disabled, absent,
failing, stale or timed out, `retrieve` reads the current source directly, bounded.

```
python3 ghe/runner/ghe_graph.py preflight --root PROJECT [--force]
python3 ghe/runner/ghe_graph.py retrieve  --root PROJECT --query TEXT [--max-chars N] [--scope REL_DIR]
python3 ghe/runner/ghe_graph.py status    --root PROJECT [--check]
```
One JSON object per command. Exit 0 ok, 1 check failed (`status --check` with non-fresh graph), 2 usage/error
(including path traversal). `preflight` actions: `skip` (disabled / empty_project), `reuse` (fresh),
`build` / `refresh` (one bounded attempt), `fallback` (absent, failed, timeout, bad output, or the same
inputs already failed). Fallback exits 0: use `retrieve`.

## Config (`ghe/config.yaml`, key `graphify:`; defaults applied in code when absent)
| key | default | meaning |
|---|---|---|
| enabled | false | master switch |
| command | graphify | executable (PATH or path) |
| build_args | ["."] | args, cwd = project root |
| semantic_extraction | false | opt-in; also needs `semantic_approved: true` and a `semantic_provider`, else ignored |
| semantic_args | ["--semantic"] | appended only when semantic is fully authorized |
| output_dir | graphify-out | graph file is `<output_dir>/graph.json`; must stay inside root |
| timeout_seconds | 120 | process group killed on expiry; partial output removed |
| max_index_attempts_per_preflight | 1 | hard cap (timeouts and absence are never retried) |
| max_packet_chars | 8000 | default retrieve cap |
| max_graph_bytes | 5000000 | larger graph.json is not loaded |
| min_source_bytes | 20 | non-blank bytes for a file to count as meaningful content |

## Behaviour
- Manifest `ghe/.graph/index-manifest.json` (atomic): schema, repo, indexed_sha, worktree_dirty,
  input_fingerprint (content hash of source files, catches uncommitted edits), producer,
  extraction_scope, graph_hash, indexed_at. Fresh = all of them match the current state.
- `ghe/.graph/last-attempt.json`: a failed/timed-out build is not repeated for identical inputs (`--force` overrides).
- Without semantic authorization, secret-like env vars (API keys, tokens, AWS_*) are removed from the child
  environment and `GRAPHIFY_NO_CLOUD=1` is set.
- Retrieve items carry `provenance` extracted|inferred (unknown = inferred). Graph items are always
  `authoritative: false` and `acceptance_eligible: false`; only direct-source items may support acceptance.
- Skipped: symlinks, .git/node_modules/venv, secret-like filenames, `ghe/`, `.claude/`, the output dir.
