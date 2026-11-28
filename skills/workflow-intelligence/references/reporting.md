# Reporter and native continuation

At each primary status/release/wait, dispatch safe engineering work first, persist queue and compact checkpoint, call native `workflow_report` with `{"canonical_root":"<absolute canonical project root>"}` (no action parameter). Publish its complete returned Russian Markdown unchanged, every line, measured result, table row, prefix, time, progress and publication marker. A previous publication never covers a later wait; do not call again in the same execution when a report is already prepared and no material change followed. Never replace with abbreviated technical delta. Reporter is tool-free transient model (no extra tabs), uses background generation/cached current prose; program renders counts and queue, does not await LLM prose. If unavailable, give grounded full degraded status with unknown values, no raw technical logs. Report verification is observational; a missed/flattened report is logged and corrected on next normal turn, not an extra model-only recovery loop.

Primary progress counts the registered canonical task lifecycle. Separate recorded new-format delivery and historically closed tasks; neither is a fresh server probe. Existing map-stage and requirement counts are diagnostic coverage only, may be partial/unknown, not execution gates. Show actual blocked reasons, externally pending proof separately for delivered tasks, optional observations outside engineering. No mass list of closed tasks to re-certify. Unknown intake/queue/map issues are shown without deleting work or blocking independent tasks. No artificial progress reset after document formatting. Use actual root, not release snapshot. Technical evidence stays in Git/project materials. Native Guard owns continuation, child liveness and telemetry; respect actual permissions and user stops. Telemetry measures observable events, not proof; do not sum overlapping durations into wall-clock. No model/reasoning routing or additional watchdog. Native GUI may be explored separately.

Use the existing compact checkpoint header, not another document. Optional `Recent results:` accepts short individual Russian bullets, one observed outcome each, e.g.

```text
Recent results:
- Удалены 9 завершённых рабочих копий; занятое место уменьшилось с 18 до 10,7 ГБ.
- Сохранены активные рабочие копии и резервные копии.
- workflow_state.md уменьшен с 462 до 10,5 КБ.
```

Examples are illustrative, never copy their numbers into a real report. Capture actual meaningful changes, units, retained resources and unresolved limits from existing results of the latest material work; keep until superseded. No separate measurement run, mandatory inventory or evidence ledger. The renderer copies accepted lines without model rewriting; historical sections and technical diagnostics are excluded. If facts are missing, do not invent metrics. Markdown table rows preserve queue order, status, original code/ID, priority and dependency identifiers. A missing publication yields a reminder on the next ordinary model context, never a reporting-only wake.
