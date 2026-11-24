# Planning and durable state — procedure reference

Use only for intake/reconciliation/queue changes; AGENTS.md defines authority.

### Unified owner-intent intake (chat AND user-owned files)

Before planning/delegating a **new task-affecting root-user instruction**, persist its provenance and reconcile its effect. Do not treat the input channel or message length as task priority: a sentence, screenshot correction, broad epic, cancellation, release instruction and follow-up can all change active scope. Ordinary greetings/status questions with no change to obligations need no record. Categorize semantically from the message **in conversation context**, not from trigger words/regex. A new message does not automatically create a new epic.

1. Preserve each material root input exactly once as an **immutable, safely redacted if secret-bearing** source file under `plans/owner_inputs/<unique-id>.md`; record origin/session/message reference, received time, referenced screenshots/assets and what could not be locally preserved. Never modify user-owned `current_task.md`; for changes made there, use an immutable relevant excerpt/snapshot with line/source anchors. Reuse a captured source on recovery; do not create a new entry merely because a session restarted. `runtime_task.md` is a pointer/summary, not a competing source of truth or a transcript.
2. Register the captured instruction in the existing `plans/progress_scope.json` **`owner_inputs` array** (optional for legacy scopes), with `id`, `source_path`, `source_sha256`, `kind` (`new_task|amendment|correction|cancellation|decision`), `target_task_ids`, `reconciled` (initially false) and a short `note`. This is an input index, **not an additional task list**. Link one input to multiple tasks if it affects them. A decision with no task target must specify an existing `recorded_in` artifact. For newly independent deliverables create one stable `tasks[]` row; for corrections/amendments update existing tasks/maps without adding fake epics, double-counting source clauses or discarding verified evidence. Keep historical IDs stable; never reuse a finished ID for unrelated work.
3. PM reconciles only the delta against maps, dependency order and already accepted decisions; mark `reconciled: true` ONLY after all affected obligations and exclusions are linked to task/map/decision evidence. Keep the input registered as pending while reconciliation runs: it must not disappear when Builder starts. On conflict, preserve both originals and apply the latest applicable explicit owner decision, with a trace, not silent overwriting. Already VERIFIED work remains proven unless the new requirement actually invalidates its acceptance.
4. Before published project-wide progress, handoff to a new session, final `DONE` or worktree integration, check for **unreconciled `owner_inputs`, unmatched prompt amendments, duplicate IDs, missing immutable sources and conflicting map ownership**. An incomplete intake makes the global scope/progress provisional, not permission to drop the new request. Do one bounded reconciliation at normal checkpoints; do not suspend unrelated active Builder/Reviewer lanes or trigger a recurring full-plan reindex.

### Source-to-plan integrity — no missing or phantom tasks

`owner_inputs` is provenance, **not** proof of mapping; `tasks[]` is a work inventory, **not** proof of delivery; `requirements-map.json` is a local contract, **not** proof that no source section was omitted. Keep all three linked. Run the existing `<OPENCODE_CONFIG>/tools/workflow_progress.py --scope-file plans/progress_scope.json --json` at the ordinary planning/handoff/status checkpoint. Its read-only reverse audit checks current `plans/current_task.md` sections against `progress_scope.json.source_inventory`, plus each reconciled owner input's `coverage_refs`. Missing section, altered section checksum, dangling requirement reference or missing manifest means **global progress/overall DONE is provisional**, not that the missing task may be discarded. Do not stop independent already-authorized Builder/Reviewer/DevOps work because the inventory needs repair.

For initial reconciliation, generate an **unclassified, read-only** inventory using `python <OPENCODE_CONFIG>/tools/workflow_progress.py --scope-file plans/progress_scope.json --inventory-template`. Orchestrator (the sole writer of `progress_scope.json`) merges the resulting `source_inventory` entries; PM classifies each actual source section as `mapped|superseded|context|excluded` with precise reasons and references. Never auto-mark `mapped`, `VERIFIED`, `reconciled`, `superseded` or `excluded` based on a heading, filename, similarity or green test. A mapped section must link existing tasks and owned maps; obsolete/historical sections need a concrete later owner input; context needs a defensible non-actionable reason. Leave uncertain sections `unreviewed`, visible in the audit, until evidence resolves them. Treat new/changed current_task sections as deltas and keep earlier classified IDs and evidence. User-owned `current_task.md` remains immutable.

Every material root input from the chat must be durably captured at its arrival and linked to the correct existing task(s), before the corresponding new work is dispatched. A reconciled `new_task|amendment|correction` input must include `coverage_refs: [{"task_id":"...","map":"plans/features/.../requirements-map.json","req_ids":["..."]}]` naming actual mandatory map rows. A global `decision` may instead link to a real `recorded_in` artifact; `cancellation` uses the existing `cancelled_by_input` evidence. A `reconciled:true` checkbox or `coverage_complete:true` without reverse links no longer clears global accounting. PM reviews the *meaning* of the linked source clauses (including prose, images and tables); structural guards cannot perform semantic audit for it. A source not preserved in the chat or repository cannot be reconstructed by guessing: report that exact provenance gap.

If a discovered old instruction appears already implemented, reconcile status using exact code, reviewed candidate, release/runtime evidence and source links; do not re-implement automatically or upgrade status solely from code presence. If the source was superseded, record the newer binding owner instruction and affected requirement IDs; never silently delete or downgrade old verified evidence. Report current runnable work separately from historical backlog. Capture coverage and evidence at each normal task/feature handoff, not after the whole epic.

### Durable execution queue admission — planning is not dispatch

For a new independent owner deliverable/epic, intake has TWO distinct durable outcomes: (a) captured source + mapped planning obligation, and (b) an entry in the **existing executable queue**. A PM map, 19 planned slices, `coverage_complete`, `reconciled`, `BUILDING` or an accepted Ticket by itself is **not** queue admission. Orchestrator is the only writer of the existing shared `plans/progress_scope.json` task inventory and queue metadata; PM proposes source-grounded dependencies and order, never silently writes a competing queue. If the project already has a separate canonical durable execution queue, inspect and update that mechanism as well; `queue_position` metadata alone cannot make a scheduler execute a task. Do not invent a second queue or engineering agent. The read-only Reporter role is separate communication, never a queue owner.

Each **open independent task/epic** in `tasks[]` must have `queue_position` (unique positive integer in its project's actual execution order), `priority` (`P0..P9` or integer 0..9), and explicit `blocked_by` (array, `[]` if none); non-task blockers (e.g. `PAY-ARCH` slice) also require `blocker_sources` mapping their ID to the exact existing plan/map/decision artifact. Separate `PENDING` / awaiting-architecture / awaiting-dependency **from active `BUILDING` backed by a real running assignment**. Dependency-slice prerequisites belong in their existing feature graph and need not block unrelated parts of a broad epic. A blocked task remains durably queued, with a documented reason and earliest independently runnable next action; architecture planning is **not** permission to implement/deploy.

In the **same normal intake/handoff transaction**, link the root input to existing stable task IDs, insert a new independent task once if necessary, assign/retain its queue position and priority using explicit owner decisions, validate dependencies and conflicts, and record its waiting state. Perform a compare-before-write against the latest authoritative inventory/branch and use atomic replacement for the single shared JSON writer; never overwrite concurrent worktree changes. Do not set `BUILDING` merely because PM finished mapping. At task cancellation/completion retain history and remove it from the active order without losing original proof; do not use arbitrary deadline/timeouts to decide admission.

At material intake, recovery, changed PM plan and actual next-task selection with changed inputs, reuse the current validated snapshot or use `python <GLOBAL_CONFIG>/tools/workflow_progress.py --scope-file plans/progress_scope.json --require-queue --json` along with existing reverse source and Git-lineage checks. `execution_queue.complete=false` means the task is **registered but not fully admitted for execution** and the aggregate must remain uncertified; reconcile only the missing entries without blocking unrelated active lanes. The validator checks syntax, uniqueness, task cycles and provenance of non-task blockers; PM/Architect must also check that each real dependency is *necessary* and that the chosen order reflects owner priorities. Do not invent positions when precedence is genuinely undecidable; leave the affected conflict explicit and continue independent work. A `queue_position` is an ordering declaration, **not evidence of a live worker or successful dispatch**.

## Compact durable recovery state

Only Orchestrator updates `plans/workflow_state.md`. Keep it short (**target 40 lines / 8 KB**) and replace current sections in place rather than append NOTE/journal entries. Example machine-readable header values must stay exact: `Status: in_progress|waiting_external|blocked|done`, `Human gate: none|<real id>`. Put explanatory prose on separate lines — **do not append parenthetical descriptions to machine status values**, because external plugins can parse those fields.

Minimum:

```text
Task: <stable task id>
Task source: <current_task.md | root prompt | runtime_task.md>
Status: in_progress
Primary phase: review
Active feature: <id>
Active lanes: <actually running lane IDs/session IDs or none>
Join barriers: <actual dependency or none>
Last verified: <recent evidence IDs and release candidate>
Next unlocks: <up to 3 concrete transitions>
Next execution: runnable|awaiting_children|none
Review: pending|needs_fixes|approved|not_applicable
Deployment: pending|waiting_external|verified|failed|blocked|not_applicable
Human gate: none
Progress scope: plans/progress_scope.json | fresh|stale|mapping_incomplete
Updated: <offset-aware timestamp>
```

Exact status/code values are for machine readers. Do not turn the state file into a full report. `plans/current_task.md` remains immutable even when stale/long; `runtime_task.md` is one bounded agent-owned snapshot and not evidence that a task is active. Preserve feature/report artifacts and prior accepted Git evidence separately. Deduplicate backlog/owner questions; never remove durable proof to make the snapshot short.

If available, native compaction can reduce a long root context after writing the checkpoint; do not abandon an unfinished task because of a token counter. Keep the current short next-action and task source so a new session can resume without redoing completed work. Never invent automatic wake-up when the OpenCode runtime actually stopped; Continuation Guard may help but is not a substitute for correct state and real event callbacks.


### Atomic checkpoint reconciliation after a completed release (v3.4)

`workflow_state.md` contains exactly **one** current machine header. Legacy duplicates in the *current header* are defects, not a legitimate historical record. Its `Status` and `Next execution` describe the canonical queue; they cannot cancel a ready admitted task when the previous P0, slice or release has finished. Historical notes in separate `##` sections are not machine state. Only the authoritative planning checkout may be updated; release/worktrees contain historical copies.

At a normal material handoff, validate the durable queue and real Assignment states first. If a ready independent task is certified but `Status`/`Next execution` is stale/duplicated, use the existing-files-only repair command:

```powershell
python <OPENCODE_CONFIG>/tools/workflow_checkpoint.py --repo <canonical-project>
# Inspect JSON output. If safe:true, use *that same* returned sha256:
python <OPENCODE_CONFIG>/tools/workflow_checkpoint.py --repo <canonical-project> --apply --expected-sha256 <inspected-sha256>
```

This narrowly edits the two fields to `Status: in_progress` / `Next execution: runnable`, preserving every other header line and historical section. It requires exactly one `Human gate: none`, a certified admitted ready task, a matching original SHA and unchanged source scope under a cooperative short-lived write lock. A duplicate or real Human Gate, missing fields or unknown blockers must not be bypassed. Never manually delete a lock without verifying the owner and source. The command does **not** dispatch a task, bypass review/deploy gates, mark a requirement VERIFIED, or replace a live/unknown child. After repair reconcile actual native child/job identities, write scope conflicts and release stage; dispatch only permitted work and save the real Assignment. If no safe native handoff is possible after backend shutdown, record `HANDOFF_REQUIRED` without pretending an event server will start a new root. MCP Events is optional and has no bearing on these native transitions.

## Requirements gate v2 for new task plans

For new non-trivial features, after extracting source requirements and before starting Builder:

1. Create a short machine-readable `slice-graph.json` beside the requirements map (schema `openworkflow-slices-v1`; each slice: `id`, `depends_on` IDs, `write_scope` file paths/globs, testable `acceptance`). The `plan.md` remains the human-readable dependency/wave plan, not the machine's only dependency source.
2. Set `slice_graph_path: "slice-graph.json"` and `strict_evidence: true` in new `requirements-map.json`; run `check --stage plan` from the project root. Validate graph ownership, dependency cycles and concurrent write scopes. Document intentional serial joins as dependency edges.
3. Each manual source requirement should include true `source_line`, `source_text_sha256`, and `source_excerpt`. Multiple atomic requirements may cite one line if the source pointer is identical; do not invent a distinct line to evade checks.
4. Define expected verification artifacts for Builder/Reviewer/DevOps: candidate-bound `evidence` with `ref` (human explanation), `observation` (what happened), `type`, and `artifact_path` (real repo-relative file). The artifact path cannot be a fabricated placeholder. Prefer `artifact_sha256` for final stable reports. Evidence paths, Git objects and content are independently checked later.

**Compatibility:** Do not set strict mode on SOUL-1 or other in-flight maps without a deliberate evidence migration. Ordinary checks remain valid and mandatory; legacy prose references need independent Reviewer inspection. Never disable strict mode on a map already opted in to make a check green.

## Canonical checkout reconciliation
Use the existing authoritative planning checkout and record its absolute root, branch, HEAD and latest actual release SHA in the compact checkpoint. For a genuinely divergent planning checkout use `workflow_lineage.py --repo <canonical-root> --compare-worktree <actual-worktree> --json` or `--compare-ref <integration-SHA>`; assess the source/evidence delta before updating. Historical release plans are read-only historical snapshots; compare them once on actual release/recovery to recover any unique obligations or evidence, not as a competing current denominator. Reporter uses the same read-only `workflow_progress.py --scope-file <canonical-root>/plans/progress_scope.json --require-queue --json` for current reporting; Orchestrator does not repeat it just to format a report; do not suppress actual conflicts or use a stale release snapshot as the canonical plan. No new state file. When running the acceptance gate, use the canonical planning root for source-relative paths and the real repository with accessible evidence artifacts/commit objects for --repo. Preserve distinct code SHA and deployed SHA.


## Native subagent lifecycle and missed completion

The durable Ticket/Assignment and its stable worktree remain the execution authority. The existing continuation-guard plugin tracks real V2 child session IDs and terminal `session.execution.*` events in its persisted state; when the installed plugin host supports `experimental.idle`, the actual `event.background` snapshot helps establish what background work existed at the primary idle boundary. `ctx.session.list` is not universally available to V2 plugins; absence must not be misreported as an empty list. A missing/unknown roster or active native child blocks automatic root continuation, not unrelated queued work. If a child finished but OpenCode lost its parent notification, reconcile its native session/job result and deliver the existing result rather than re-run Builder. The platform's native completion handler remains primary, so do not synthesize duplicate completion messages. Unknown backend state is SUSPECT, not failed, completed or permission to terminate.

Root Continuation Guard is not a standalone child-job controller. No prompt can guarantee delivery when the upstream backend has lost an execution and does not provide a reliable job roster. Use the native OpenCode SDK `experimental.job.list()` when available to the active host/SDK tool, verify its version and returned shape, and match parentID/sessionID, attempt and candidate. Do not pretend that this SDK method automatically exists on the plugin context. Incompatible/unavailable host capabilities are logged and resolved by existing recovery at the next safe event; they do not license a second execution. Never trigger a model call merely to poll. Treat the legacy `agent_lane_watch.py` as an optional read-only historical diagnostic, not an ownership registry or timeout/kill mechanism; do not start its long-running `guard` loop or demand heartbeat calls from Builder. Recovery preserves unique work and fences old assignments; only confirmed terminal/orphan states may be reassigned. If an old worker may still be able to commit/deploy, first establish a safe ownership boundary. Shell jobs should use native runtime process status and real tool-specific timeout/error handling, not a blanket 45-minute execution deadline.

### Arbiter convergence contract (v3.3, on-demand, existing scope only)

Use this procedure **at actual Reviewer handoff, or a documented Builder/Reviewer contract dispute**, not in a polling loop. Programmatic detection is integrated into the existing `workflow_progress.py --scope-file plans/progress_scope.json --require-queue --json` output (`convergence`). It reads the same authoritative `tasks[]` and does **not** create a parallel queue, new database, or an independent global acceptance gate. `tools/workflow_convergence.py --scope-file ...` provides the same read-only analysis for debugging. Older tasks with no convergence observations remain compatible; their missing history is `UNRECORDED`, not pass/fail or an automatic Arbiter call.

For a reviewed task, Orchestrator alone may append real Reviewer facts into an optional `convergence` object in **that task's existing** `progress_scope.json` row. The smallest review record is:

```json
"convergence": {
  "contract_digest": "<64-hex digest of frozen owner-backed contract>",
  "reviews": [{
    "candidate": "<real Git commit>",
    "review_ref": "<unchanged Reviewer artifact>",
    "reviewer_session": "<real session id>",
    "contract_digest": "<same frozen contract digest>",
    "verdict": "Needs Fixes",
    "blocking_findings": [{
      "id": "TIME03-R4-F0", "classification": "IN_SCOPE",
      "contract_refs": ["TIME-03"], "evidence_ref": "<reviewer evidence>"
    }]
  }]
}
```

Only real mandatory blocking findings appear in `blocking_findings`; BACKLOG_ONLY and procedural suggestions stay linked in the ordinary review/backlog, not fabricated as blockers. `Approved` must have zero unresolved blockers. A fresh review of the same candidate does not constitute a failed correction. Keep stable finding IDs even when the wording changes; explicitly record which earlier ones were resolved. `contract_digest` comes from the applicable frozen contract, not an agent-invented string or a post-hoc change to escape review. A changed contract begins a new legitimately approved comparison series; never silently rewrite historical review records. The normal release/Review gates still verify actual evidence/candidate validity. The convergence detector checks format and Git candidate delta, **not** semantic truth of a review report.

Automatic triggers are conservative: SAME_BLOCKER_AFTER_CORRECTIONS requires an unchanged *set* of unresolved blocking IDs across **two** consecutive real corrective code attempts (three independent reviews) (verified by Git); OSCILLATING_CONTRACT requires a blocker to disappear then reappear over real code changes; CONTRACT_DISPUTE needs an actual unresolved Reviewer finding and independent Builder/Reviewer evidence; EVIDENCE_BACKED_ESCALATION is an explicit reason/evidence for an older unmapped or structurally unrecognizable loop. New findings, partial improvement, first Needs Fixes, docs-only commits, duplicate reports, long time/token counts and unknown Git ancestry are **not** automatic convergence triggers. Unknown ancestry/contract evidence requires reconciliation instead of guessing.

Before dispatch, read `convergence.needs_arbiter`, compare the stable `trigger_key`, and ensure **no existing OPEN Arbiter case or live Assignment** for this task. Persist `convergence.arbiter={"state":"OPEN","trigger_key":"...","assignment_id":"<existing durable Assignment ID>"}` in the same task row as part of the normal durable admission/handoff; after the original result, set `state: RESOLVED` plus `decision_ref` to the unchanged Arbiter output and leave the trigger key. This is *case deduplication*, not a new queue. If OPEN and no job is visible, reconcile native session/job and existing result first; never start another Arbiter from a stale `OPEN` marker. If the same key returns after RESOLVED, reuse the decision rather than requesting a repeated diagnosis. Only truly new evidence, a materially changed owner contract, or a separately evidenced dispute can open a new case. Arbiter's classification is advisory; only Reviewer can independently approve the actual code, and neither may downgrade a verified mandatory defect to backlog for speed.

Arbiter's read-only output contains existing task ID, trigger key, exact disputed finding IDs, likely shared root (or explicit unknown), scope disposition with actual source/evidence, **one** falsifiable next action and an exit condition. Do not dispatch Arbiter recursively, on its own advice, after each review, or as a routine predeploy stage. Arbiter NEVER edits code/test/maps, kills/restarts child jobs, waives a gate, promotes a FAILED requirement, or forces owner QA. Continue unrelated ready work while the case is open.

The same handoff checks the durable queue. Closing a feature does not mark the global workflow DONE when the canonical queue has a ready independent task. `Next execution: none` is reserved for a real empty/non-runnable queue or a documented genuine blocker; requiring a fresh context is not a reason to manufacture `none`. The native Continuation Guard has a narrow recovery for an **already armed** Orchestrator session with a structurally ready queued task even if its completed-unit checkpoint incorrectly says `Status: done` / `Next execution: none`; it will not reactivate an unarmed/stale session, skip Human Gate or interrupt unknown children. If actual queue admission cannot be verified, record precise HANDOFF_REQUIRED and use a supported session handoff, not an unbounded self-prompt loop.


## v3.5 — compact handoffs and source-backed reporting fields
Preserve the existing queue schema. At ordinary intake PM may propose `title` and `summary` in the existing task row (human-readable Russian name and one sentence of actual user impact); Orchestrator owns the atomic write. No migration, no separate title-generation stage, no retroactive rewrite. At real deployment handoff record `Production: <verified actual version> — <plain Russian functional outcome> | evidence: <exact deployed SHA and release evidence ref>` and optional `Last completed: <plain Russian outcome> | evidence: <existing evidence ref>` in the existing compact checkpoint header. Existing Active lanes/Next unlocks/Join barriers should name actual roles/tasks and meaningful work, not just abbreviations. These are observational metadata, not new mandatory acceptance fields. Reporter reads them and the complete open queue; if fields are absent, preserve unknowns. Do not edit checkpoint merely because a report was published. Avoid repeat review/map/lineage/housekeeping work on unchanged evidence; validate when the relevant input or consumer changes, not every model turn. See `reporting.md`.
