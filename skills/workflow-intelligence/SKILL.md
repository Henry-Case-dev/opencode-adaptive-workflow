---
name: workflow-intelligence
description: Repository lookup/cache, Git-incremental code indexing, feature manifests, managed scratch and test lifecycle/portfolio-audit policy for the global OpenCode workflow.
compatibility: OpenCode V2; Python 3 available for workflow_intel backend.
metadata:
  scope: global
  cache: sqlite-git-incremental
---

# Workflow intelligence
Use native LSP, exact reads and Git first. `workflow_intel` is an optional disposable repository navigation/cache tool, not state, proof or prerequisite. `locate`/`refs`/`impact` for symbols, `search` for text, `feature_context`/`manifest_validate` only for existing relevant manifest, `test_audit` for candidate duplicate/obsolete/pin/slow tests (verify manually; static counts are not pytest collection), `stats` and `scratch_status` on demand. A failed cache falls back immediately to direct tools. Never force full index or create a manifest for every change. Cache is per worktree under WorkspaceData; no duplicate project database.

Read only the relevant reference when needed:
- planning.md: canonical queue, recovery, legacy transition.
- acceptance.md: two actual checkpoints and compact delivery record.
- quality.md: QA, test lifecycle, authorized live sandbox.
- storage.md: scoped cleanup and preservation.
- reporting.md: human-readable report and native continuation.
- memory.md: optional configured Memory MCP lookup/write.
These are procedures, not extra gates. Use legacy map/lineage/convergence tools only for concrete diagnostic questions. No routine full audit of all tasks/tests.
