# Planning and recovery

Use `plans/current_task.md` and material owner chat/file amendments as original requirements. Do not rewrite them. Save material input once under existing `plans/owner_inputs/`; retain source, stable ID, linked task IDs, actual supersession, and mark reconciled when mapped/recorded. Unrelated ready work continues while intake is reconciled. Preserve existing source inventory and maps; only update affected links. Do not invent or lose scope; no per-line inventory/manifest/graph for ordinary fixes. Canonical planning checkout is explicit; never take release/worktree snapshot as current queue. Use `workflow_progress.py --scope-file plans/progress_scope.json --json` for read-only ordinary count, `--tasks-only` for fast queue view. `--legacy-audit`, `--inventory-template`, `--compare-ref` and `--compare-worktree` are diagnostic options for an actual discrepancy, not preconditions. Existing schema `openworkflow-progress-scope-v1` remains.

Task row: stable `id`, readable `title`, `status`, original `priority` P0..P9, `queue_position` (ordering hint), `blocked_by` (explicit dependencies, [] if known independent), source reference and compact acceptance. Source may be existing original or linked amendment; preserve legacy fields. New lifecycle READY -> IN_PROGRESS -> REVIEW -> READY_TO_SHIP -> DONE; NEEDS_FIXES returns to Builder, BLOCKED is precise real constraint, CANCELLED only explicit owner decision. PENDING/BUILDING/VERIFIED and other old states remain readable. DONE needs delivery record per acceptance.md; legacy VERIFIED/COMPLETED is historical closure, not freshly proven current service. Do not reopen for paperwork. Old WAITING_EXTERNAL marked delivered is not automatically certified: resolve its concrete missing closure when relevant using actual existing evidence, then record DONE if genuinely eligible; do not re-audit all historical tasks. For blocked task include `blocker: {reason, next_action}`; keep priority, do not move to end as substitute for resolution. A cancelled dependency is not automatically satisfied. Correct its edge only after checking source. Bad duplicate/missing/unknown IDs or edges quarantine affected work; equal positions order by priority then ID, not a global failure.

Orchestrator is sole shared-state writer. Record existing Assignment ID, role, actual session/job/worktree, baseline and write boundary before delegation and reconcile result/identity before reuse. No invented live state, overlapping writers, blind cancellation or restart. Keep checkpoint short (exact one current header; history below `##`):
```
Task: <current feature or queue>
Status: in_progress
Human gate: none
Next execution: runnable
Production: <version> — <plain outcome> | evidence: <ref>
Last completed: <plain result>
Active feature: <plain current work>
Next unlocks: <concrete next action>
Join barriers: <actual dependency or none>
Planning root: <actual root>
```
`Human gate: task (<exact ID>)` is supported only with matching passive task row; native unresolved permission remains protected. Multiple/unscoped gates remain conservative; don't guess. `Next execution: awaiting_children` for actual waiting, `none` only no safe action; `Status: done` only whole queue truly finished; `blocked`/`waiting_external` for genuine remaining work. `workflow_checkpoint.py --repo <root>` inspect, `--apply --expected-sha256 <hash>` atomic header repair only if needed (no changes to human field/queue). New source or ready work takes priority over stale done header. Native Guard resumes already armed root, never starts arbitrary sessions or approves work. On loss verify native job/child result, Git and Assignment; use natively supported cancellation only for proven orphan, not age. Real user interruption wins. No busy waits/second watchdog.

Legacy conversion is lazy and local: do not mass migrate plans, force hash refresh or reset old status. Existing maps/receipts/evidence remain historical; when a real issue surfaces, review affected scope, re-open exact missing work, preserve existing evidence. No automatic closure based on labels or partial delivery. Arbiter only for actual material dispute; optional workflow_convergence.py for a concrete case, not every review.
