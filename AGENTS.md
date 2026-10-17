# OpenCode Adaptive Workflow — global rules

These instructions apply to the global seven-agent workflow. Repository-specific `AGENTS.md` may add project constraints. Current code, the user's latest explicit request and accepted contracts outrank historical notes.

## Scope and efficiency

- Small isolated changes: Builder -> independent Reviewer, and DevOps only when delivery is needed. Do not invoke the full team or write specs as a ritual.
- Non-trivial parent tasks: preserve the exact user-owned source; PM first inventories atomic requirements, then creates independently testable slices with explicit dependencies and integration ownership. Reviewer checks the source against the plan before BUILD.
- Keep `plans/current_task.md` read-only for every agent. Use `plans/runtime_task.md` for a prompt-originated task requiring recovery. `plans/workflow_state.md` is a compact current snapshot, not an append-only journal.
- Current Git tree and real tests/observations outrank search excerpts, old reports and Memory MCP. For code navigation use LSP if available, then targeted searches and direct file inspection. Broaden only as needed.
- Use task-local temporary storage such as `plans/.scratch/` for disposable probes; do not delete outside the owned scratch path. Promote only durable test/evidence artifacts.
- Avoid repeated research, redundant scans, tests and agent handoffs. A narrow bugfix needs focused proof; a contract/security/data regression still blocks release even if the fix is tiny.

## Requirements and release gates

For a non-trivial task, maintain `plans/features/<feature>/requirements-map.json`. Source headings/lists are only a partial inventory: PM also extracts requirements from prose, tables and screenshots; Reviewer compares the complete source independently. Run the global `tools/requirements_gate.py` plan check before BUILD; review checks for the exact candidate/slice; release checks after mandatory live verification. A passing gate checks metadata, not semantic correctness. `READY_FOR_LIVE` is never parent DONE.

For **new** non-trivial maps, plan `slice-graph.json` (`openworkflow-slices-v1`) and set `slice_graph_path`, `strict_evidence: true` only when the evidence convention is established. Use `--repo .` from the actual project Git root for strict review/release checks; refer to real local Git objects and candidate-bound repo-relative `artifact_path`. The gate does not prove what a remote server runs. Existing non-strict maps remain compatible; do not silently change their format/flags or drop old evidence.


**Strict candidate prerequisite:** before the first `--stage review` on a strict map, freeze the product changes as a **local candidate Git commit** (isolated branch/worktree when appropriate). Reviewer inspects that exact local commit, not an uncommitted moving worktree. Reviewer rejection produces a corrected new candidate commit and re-review; publication/merge/deploy happens only after approval. Map/evidence bookkeeping may remain separate and must identify the candidate SHA without changing its product bytes. If immutable local candidate preparation is not possible, disclose the limitation and do not pretend that the strict review gate passed.

## Human-readable reporting (all roles)

The root Orchestrator is the **one reporting voice** for intermediate and final user-facing updates. Specialists hand back compact factual outcomes, blockers, evidence and next actions, not duplicate narratives. Do not create a Writer subagent just to rewrite reports.

Use plain Russian: describe what actually changed and why it matters to the user; explain the current work, outstanding mandatory checks and next step. Translate lane IDs, acronyms and state-machine labels into ordinary language. Use technical IDs only as optional supporting detail. Avoid logs, internal deliberation, bureaucratic reports and claims that test counts alone prove a feature.

Every **meaningful progress report** should have a fresh execution-host date/time including UTC offset, the scope/task, and honest verified progress when measurable: `done/total`, integer percent and a 20-character ASCII bar. For multi-map tasks pass **all active maps** to the read-only global `tools/workflow_progress.py`. It distinguishes fully verified slices from individually verified required clauses. A slice is done only if all its required clauses are `VERIFIED`; a pending live test remains incomplete. Do not combine unrelated epics or cherry-pick completed maps. If the maps are stale/incomplete or no auditable denominator exists, state that the percentage is unavailable instead of inventing one. Label the date as `время среды` unless the user's timezone is independently confirmed.

Emit updates at meaningful transitions (plan accepted, implementation done, review result, deployment, blocker or next slice), and a short handoff at turn end. Do not create heartbeat chatter, timers or periodic polling merely to report progress. Do not edit `workflow_state.md` only to produce a progress bar or timestamp. Keep technical state machine fields unchanged.

## Converge decisions; don't wait hours in agent sessions

Resolve a factual uncertainty once with authoritative evidence. After a decision, act or return a concrete blocker; do not reiterate the same alternatives without new information. On future external dependencies (cron tick, reply, provider availability), do **not** sleep for hours, keep a specialist session open, or repeatedly poll. Save `WAITING_EXTERNAL`, `NOT_BEFORE_UTC`, outstanding acceptance, evidence, exact re-entry condition and owner; continue independent ready work. If no real timed resume scheduler exists, be explicit that a later user/system invocation is needed: native background completion is not a wall-clock alarm. Future checks never count as VERIFIED or a Human Gate.

## Memory and safety

Use standard `memory_*` MCP only for stable accepted decisions/root causes, not source-code locations, current progress, raw logs or secrets. Keep `plans/MEMORY.md` compact; current source/code is authoritative. Missing optional memory must not stop safe product work. Reviewer remains an independent blocker for missing requirements, security/data integrity failures and unverified critical behavior. DevOps must bind actual deployed version to approved candidate and preserve rollback readiness; never fabricate evidence or bypass access controls.

## Tool paths

`<OPENCODE_CONFIG>` refers to the actual global config directory, typically `%USERPROFILE%\.config\opencode` on Windows (`$env:USERPROFILE\.config\opencode` in PowerShell) or `~/.config/opencode` elsewhere. Run the global Python tools with `py -3` (Windows) or `python3` (Unix) from the **project Git root**; resolve placeholders to real paths. Gate/tool errors are not success and must not be suppressed.
