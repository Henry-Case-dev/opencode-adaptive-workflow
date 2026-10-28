---
name: workflow-intelligence
description: Repository lookup/cache, Git-incremental code indexing, feature manifests, managed scratch and test lifecycle/portfolio-audit policy for the global OpenCode workflow.
compatibility: OpenCode V2; Python 3 available for workflow_intel backend.
metadata:
  scope: global
  cache: sqlite-git-incremental
---

# Workflow intelligence

Load this skill when a task needs repository rediscovery, impact/navigation help, a feature manifest, test-maintenance analysis, or scratch hygiene.

## 1. Repository lookup

**Scope boundary:** This Skill is for navigating symbols, references, code impact and tests. It is NOT needed to read known task/state artifacts such as `plans/current_task.md`, `plans/workflow_state.md`, `plans/progress_scope.json`, requirements maps or Git status. For those, read exact paths / Git directly. `locate` takes a CODE SYMBOL query, not a file path or workflow status key. Never block session recovery or task continuation on the availability of this cache. If `workflow_intel` fails or is unavailable, use normal targeted file reads/grep/git immediately; do not keep retrying an optional indexer.

Use the cheapest reliable source first:

1. Native LSP if the current runtime actually exposes a working LSP tool for this language. Prefer it for definition/reference/call-hierarchy questions.
2. `workflow_intel` for cached structural navigation:
   - `locate` — exact/partial symbol definition;
   - `refs` — static references to one symbol;
   - `search` — symbol/doc/full-text chunk lookup;
   - `impact` — definitions + reference files as a bounded impact hint;
   - `feature_context` — manifest-owned files/tests/symbols;
   - `stats` — current cache inventory.
3. Open only the returned current files/ranges and verify the claim there.
4. On miss or contradiction, use targeted grep/glob/read.
5. Broad repository scan only when the unknown is architectural/dependency-wide.

The SQLite cache is disposable. It refreshes from Git and dirty file content on each tool call. Current code/runtime always overrides the cache.

Do not use historical memory as a current symbol-location database.

## 2. Cache behavior

`workflow_intel` resolves the Git worktree automatically and stores one database per real repository under:

`OPENCODE_WORKFLOW_CACHE_ROOT/<repo-name>-<path-hash>/repo-intel.sqlite3`

The global plugin places this cache under `<GLOBAL_CONFIG>/WorkspaceData/cache/intelligence` automatically; this is reproducible across projects and OSes without absolute path configuration.

Refresh behavior:

- first use / incompatible cache schema -> full index;
- same HEAD + same dirty-content fingerprint -> no reindex;
- changed commit -> Git diff since indexed HEAD;
- dirty/untracked changes -> only affected files;
- branch/history discontinuity -> safe full rebuild;
- large files over `OPENCODE_WORKFLOW_MAX_FILE_BYTES` (default 2 MB) -> location recorded but content indexing skipped.

Python gets AST symbols/references/imports/tests. JS/TS/Go/Rust/Java get lightweight structural indexing. LSP, when available, remains more authoritative for language semantics.

## Coverage gate separate from navigation cache

The tool `<OPENCODE_CONFIG>/tools/requirements_gate.py` in the workflow overlay is the **parent task acceptance gate** for non-trivial work, unlike the optional compact `manifest.json` navigation cache described below. The gate inventories source headings/list items, checks exact source hashes, mapping, evidence, review candidate, and deployed SHA. PM must manually add prose/screenshot/table constraints it cannot extract. Run at `plan`, `review --slice`, and `release` stages. See `WORKFLOW_PATCH_README.md` for exact commands. Never treat a green gate as independent proof of correct runtime behavior.


## 3. Feature manifests

Do not create a manifest for trivial edits.

For a non-trivial feature in a project already using `plans`, use:

`plans/features/<feature>/manifest.json`

Minimal shape:

```json
{
  "feature_id": "mca23-semantic-planner",
  "status": "building",
  "requirements": [
    {"id": "REQ-1", "source": "current_task §...", "summary": "..."}
  ],
  "files": [
    {"path": "services/example.py", "role": "runtime-entrypoint"}
  ],
  "tests": [
    {"path": "tests/test_example.py", "kind": "REGRESSION", "owner": "REQ-1"}
  ],
  "evidence": []
}
```

Rules:

- PM owns requirement mapping and decides whether a manifest is worth having.
- Architect defines stable boundaries/entrypoints when architecture is material.
- Builder records files/tests actually implemented; never speculative paths.
- Reviewer validates the manifest against the exact candidate with `workflow_intel manifest_validate` when useful.
- Keep current mappings only. No chronological diary, logs or raw test output.
- Manifest metadata is not proof of correctness.

Projects without a `plans` workflow do not need manifests; the global repository cache still works.

## 4. Test lifecycle

New tests should be classified by why they deserve to remain:

- `CONTRACT` — explicit owner/public contract;
- `REGRESSION` — reproduced bug/root cause;
- `INTEGRATION` — meaningful component/provider/data/UI seam;
- `PROVISIONAL` — temporary investigation/probe.

Classification may live in the feature manifest or near the test:

```text
TEST-LIFECYCLE: REGRESSION owner=MCA-23/F2
```

Rules:

- Do not bulk-retrofit thousands of legacy tests just to satisfy metadata.
- Existing unclassified tests are legacy inventory, not automatic failures.
- A new feature-owned `PROVISIONAL` test must be promoted to CONTRACT/REGRESSION/INTEGRATION or removed before final approval.
- If a test encodes behavior contrary to the actual owner contract, fix the implementation and the test; do not preserve a wrong green suite.

## 5. Test portfolio audit

Use `workflow_intel test_audit` only when maintenance is justified: task/epic boundary, material test growth, provisional accumulation, recurring duplicates/flakes/bounds/version pins, or explicit user request.

The audit produces candidates, never deletion authority. Inspect candidates and classify the reason before changing tests.

Useful candidate classes:

- duplicate test bodies;
- lingering PROVISIONAL tests;
- permanently skipped tests;
- stale bounds/hash/version/snapshot/pin tests;
- tests for removed/superseded architecture;
- multiple regressions protecting one root cause when one parameterized test is clearer;
- mock-only tests that no longer protect a meaningful contract;
- flaky tests treated by rerun instead of root-cause repair.

Cleanup is Builder work and still passes Reviewer.

Mutation testing is targeted and optional. Use it only for a critical contract with suspiciously weak tests and only when the project already has a suitable runner or the dependency cost is explicitly justified.

## 6. Scratch hygiene

The global `workflow.intelligence` V2 plugin injects:

- `OPENCODE_WORKFLOW_SCRATCH`
- `TMP`
- `TEMP`
- `TMPDIR`
- `PYTHONPYCACHEPREFIX`

and controllable package-cache variables into shell execution. The per-repository managed root is derived from the installed global plugin location under `<GLOBAL_CONFIG>/WorkspaceData/temp/intelligence`, not the OS temporary directory.

Put disposable probes/artifacts there. For parallel Builder lanes, prefer:

`$OPENCODE_WORKFLOW_SCRATCH/lanes/<LANE_ID>/`

Promote only required evidence/source before task completion. At a verified epic boundary, the existing workflow owner calls `tools/workflow_housekeeping.py epic-close --repo <root> --epic <stable-epic-id> --purge-disposable-cache` (preview) and adds `--confirmed-complete --confirmed-idle --apply` after inspection. No automatic age-based cleanup. The helper may delete only marker-owned safe scratch and an identified disposable index after the final user of the cache has finished; it never clears arbitrary OS paths or durable proof.

## 7. Efficiency invariants

- No extra LLM call merely for cache/index maintenance.
- No index daemon; refresh lazily on tool use.
- No broad scan after a valid targeted hit unless the task truly needs architecture-wide discovery.
- No manifest/test audit/full suite just because the feature exists.
- Reuse exact current evidence for an unchanged candidate/environment.
