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

## Human-facing reporting and actionable owner questions

This is a **presentation contract**, not a replacement for technical handoffs, product tests, requirement maps, reviewer gates or canonical state. Orchestrator alone composes the consolidated Russian-language user report. Other agents return concrete technical facts; do NOT summon a new Writer agent.

For meaningful **intermediate and final** reports, the root response must state in natural Russian: the system-local date/time and UTC offset, the exact human name of the feature changed and what it does, what has actually passed checks, who is running now and on which user-visible function, what failed/remains, and what happens next. Internal identifiers, commit IDs, release jargon, hashes and test counts belong in project evidence. Do not turn the human report into a translation of internal labels. Never claim an agent is currently working from an old assignment after it has finished.

**Progress formatting is strict.** Run the existing `<OPENCODE_CONFIG>/tools/workflow_progress.py` once per meaningful report with **all current maps** for the active task. Trust `VERIFIED`, not implementation claims. Put `Проверено X из Y требований (Z%). Осталось N.` in its own paragraph, then an empty line, then **exactly one standalone 20-character ASCII bar inside a fenced `text` code block**, followed by a blank line before the next section. The bar must never appear inline with prose, inside a heading, table cell or after another sentence. For example, the code block's *content shape* is `[<20 cells>] Z% (X/Y)`; replace all placeholders from the tool's real output. If maps are incomplete or stale, explicitly explain why progress cannot be measured and show **no invented percentage or bar**. If independent epics cannot be combined honestly, show separately scoped counts.

**Do not turn a status counter into a request for decisions.** `BLOCKED_REAL_HUMAN_GATE` rows, legacy `owner gates` labels and `18 awaiting owner` in `workflow_state.md` are provisional classifications, **not** proof that eighteen choices are needed. Before reporting such a number, inspect the actual requirement-map rows, source passages, active decisions and delivered evidence, and deduplicate; classify each as:
- `USER_DECISION`: a genuine unresolved tradeoff (state the precise choice and the user-visible consequence);
- `USER_ACTION`: credential/authorization or hands-on device acceptance that only the user can provide (state exactly what to do and what evidence to return);
- `AGENT_CHECK`: a test, inspection, deploy smoke or audit agents can do themselves;
- `WAITING_EXTERNAL`: a future tick, provider availability or other external event.

Only the first two categories require the user's participation. Do not silently mark tests or live acceptance as VERIFIED, discard required manual acceptance, or block unrelated work. Do not repeat the old count as «18 решений от вас» without a sourced, individual enumeration. If old data is ambiguous, report «N пунктов в старой карте помечены как требующие участия; проверяю, сколько из них действительно нужно обсудить» and arrange classification. Correct derived labels/statuses only with source-backed evidence and under the existing requirements-map editing/review rules; never fabricate approvals or alter user-owned `plans/current_task.md`.

When real owner participation exists, keep a **short actionable queue**, not an invisible count. Create/update `plans/owner_questions.md` **only if necessary** (otherwise do not create an empty document). Each outstanding entry: stable ID, human-readable question/action, 2–3 real options (when a choice exists), what the options change, exact source reference, blocking scope, status `TO_ASK | ASKED | ANSWERED | ACTION_PENDING | VERIFIED`, and a link to the answer/evidence. Keep the queue compact, deduplicated and current; point `workflow_state.md` to it. In every user-facing report say where the queue is, how many items genuinely require user action, and state the immediate blocking question **in full**, not a cryptic ID. Avoid dumping dozens of choices in a progress report: surface urgent blockers and offer the queue for the rest.

For an active blocking `USER_DECISION`, **Orchestrator itself** should invoke OpenCode's built-in `question` tool with a short header, a clear question and meaningful options when the tool is genuinely available and authorized in the current UI; free-form answers remain possible. Check tool availability/permission once, do not invent calls or assume a subagent can display a root-session form. If the UI/tool is unavailable or rejected, ask the same concrete question directly in ordinary chat and mark it `ASKED`. Never claim that a native questionnaire appeared when it did not. `USER_ACTION` requires an explicit checklist, not a fake choice. Background tasks can continue independently.

**Pre-send check:** Is the ASCII bar on its own line inside a separate code block? Are the counts verified and current? Can an outsider tell what functionality was delivered and who is doing what? If you mention an owner question count, can you list the actual pending questions/actions and point to the queue? If no, repair the report before sending. Only report material milestones; no per-tool narration, clock-refresh chores or artificial polling.

**Separate control from presentation.** Project artifacts retain exact machine states. Normally show only the human report. When a technical root handoff must be visible, place it **before** `---` and finish with the complete human summary. Do not overwrite working task records to make the status look better.

## Converge decisions; don't wait hours in agent sessions

Resolve a factual uncertainty once with authoritative evidence. After a decision, act or return a concrete blocker; do not reiterate the same alternatives without new information. On future external dependencies (cron tick, reply, provider availability), do **not** sleep for hours, keep a specialist session open, or repeatedly poll. Save `WAITING_EXTERNAL`, `NOT_BEFORE_UTC`, outstanding acceptance, evidence, exact re-entry condition and owner; continue independent ready work. If no real timed resume scheduler exists, be explicit that a later user/system invocation is needed: native background completion is not a wall-clock alarm. Future checks never count as VERIFIED or a Human Gate.

## Memory and safety

Use standard `memory_*` MCP only for stable accepted decisions/root causes, not source-code locations, current progress, raw logs or secrets. Keep `plans/MEMORY.md` compact; current source/code is authoritative. Missing optional memory must not stop safe product work. Reviewer remains an independent blocker for missing requirements, security/data integrity failures and unverified critical behavior. DevOps must bind actual deployed version to approved candidate and preserve rollback readiness; never fabricate evidence or bypass access controls.

## Tool paths

`<OPENCODE_CONFIG>` refers to the actual global config directory, typically `%USERPROFILE%\.config\opencode` on Windows (`$env:USERPROFILE\.config\opencode` in PowerShell) or `~/.config/opencode` elsewhere. Run the global Python tools with `py -3` (Windows) or `python3` (Unix) from the **project Git root**; resolve placeholders to real paths. Gate/tool errors are not success and must not be suppressed.
