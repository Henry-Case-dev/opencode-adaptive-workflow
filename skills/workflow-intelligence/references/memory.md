# Durable historical memory — optional public procedure

The public workflow supports the configured Memory MCP when installed. It does not assume OpenViking, custom services, paid providers or particular credentials. Only the existing Memory role is responsible for updates unless a project's explicit permissions allow otherwise. Current tasks, durable assignments, Git commits, requirements maps and release evidence are authoritative; memory is historical assistance.

## When to use
- On recovery or a new session, only if previous decisions/root causes can materially change continuation, make one targeted project-scoped lookup after reading the canonical checkpoint.
- Before an important cross-feature contract decision or when a corrective failure repeats, inspect relevant accepted history if available.
- After a verified reusable root cause, accepted stable decision/owner constraint or completed feature with durable lessons, save the smallest source-backed change, deduplicated by stable project/subsystem/decision key. Routine test runs, temporary states, version bumps and raw logs are not durable knowledge.

## Read, verify and write
1. Discover the real configured memory tool names and schemas; standard Memory MCP installations can differ across versions. Do not assume search/read/write operations exist when they are absent.
2. Query project/subsystem/decision key with provenance/date and choose only material results. Expand the specific relevant record; verify against current sources, source links, current owner decision and Git. Empty retrieval is not proof of absence.
3. Before writing, search the stable key; skip identical entries, update or version an actually changed decision with predecessor/source reference, never append duplicates merely because a session changed. Do not store secrets, private data, transcripts, unconfirmed theories, session state or temporary artifacts.
4. On unavailable/timeout/permission error: one bounded diagnostic check, report `MEMORY_BACKEND_DEGRADED`, continue from local source/Git and preserve the exact pending delta in the existing project memory index if it matters. Retry only on a later real event; never block Builder/Reviewer/DevOps or fabricate a successful sync.

Do not install a second memory server, enable automatic capture/recall, alter credentials or create another tracking ledger for the sake of memory. The public `opencode.jsonc` supplies an optional standard Memory MCP entry; actual effective permissions still govern calls.
