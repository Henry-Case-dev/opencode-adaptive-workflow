---
name: code-quality
description: Pragmatic implementation and review quality across any programming language, framework, plugin, game mod, service, UI, or script. Load for substantive code changes, complex fixes, or concrete maintainability risks; avoid ritual refactors and style-only release blocks.
compatibility: opencode
metadata:
  scope: universal
  discipline: maintainable-implementation
---

# Code quality — correct, understandable, changeable

## Purpose and activation

Use this skill **within** existing Builder or Reviewer work when a change is non-trivial or raises credible quality risks: intertwined logic, difficult state transitions, boundary/API edits, error handling, concurrency, testability or repeated regressions. For a one-line local correction with an obvious contract and regression test, skip loading. Avoid repeated reloads in one task.

It does not create a separate review pass, scoring rubric, new agent or automatic cleanup epic. The user's observable contract, current repository, language conventions and accepted architecture outrank generic advice. A new file, package or abstraction must pay for itself.

## Before writing or judging code

1. Read the smallest sufficient current code path, its callers and affected tests; confirm version/platform and current local conventions. Do not grade code from a summary or stale symbol cache.
2. Identify the required observable change and the invariants that must remain stable. Distinguish a genuine bug from a stylistic preference.
3. Prefer local, reversible changes that fix the root cause without moving unrelated code. Widen scope only for demonstrated shared-cause coupling or unavoidable compatibility constraints.
4. Determine what would prove success: behavior, edge/negative cases, contract integration and failures. Tests are evidence, not permission to alter a requirement.

## Practical quality lenses — apply selectively

- **Correctness & intent:** Does the code implement the requested semantics for normal, empty, boundary and error cases? Are implicit assumptions encoded or validated where needed?
- **Clarity & locality:** Can a maintainer follow the main path? Names should expose concepts and units; control flow should make branching/failure obvious. Prefer a cohesive function/module over arbitrary file-count, line-count or single-responsibility quotas.
- **Data & state:** Make ownership, mutation, persistence, caching and consistency rules discoverable. Beware aliasing, stale caches, unsafe shared mutable state, misleading defaults and duplicate sources of truth.
- **Interfaces & dependencies:** Keep API signatures, wire/persisted formats, semantic version and consumers compatible unless a deliberate migration is part of scope. Avoid dependency direction inversion, hidden global coupling and needless public surface.
- **Failure handling:** Distinguish expected failure from programming error; propagate meaningful context without swallowing exceptions, fabricating success or leaking secrets. Assess resource cleanup, bounded retries, timeout/cancellation and idempotency where relevant.
- **Concurrency/runtime:** Inspect races, async ordering, locks, lifecycle hooks, shutdown and host-specific thread ownership when the changed path uses them. Never blindly import one runtime's threading model into another.
- **Security:** Validate at trust boundaries; authorize at actual enforcement points; protect secrets, file/path/SQL/shell/network inputs and data exposure when touched.
- **Performance:** Avoid obvious unbounded loops/allocations/N+1 I/O/expensive work on hot paths, but demand evidence before complex optimization; assess behavior with realistic sizes when meaningful.
- **Testability:** Cover contracts and reproduced regressions at the cheapest reliable level. Prefer a focused integration test for a real boundary over piles of mocks or brittle tests tied to implementation detail.

## Refactoring decisions

- **DRY:** Deduplicate shared *knowledge*, not merely similar-looking syntax. Different future behavior may justify duplicate small code paths.
- **SOLID/DDD/Clean Architecture:** Use as questions about cohesion, extension and coupling, not mandatory layers or naming patterns.
- **KISS/YAGNI:** No service layer, interface, plugin system, wrapper, manager, generic helper or future-proof configuration without a demonstrated need.
- **Small functions:** Split by coherent responsibility and useful names, not arbitrary length limits. Preserve straightforward sequential code when it is clearest.
- **Dependency choice:** Prefer an already used, maintained project dependency when suitable. Add packages only when benefit outweighs compatibility, security, licensing and maintenance costs.
- **Public API:** Avoid changing widely used contracts just to prettify internals; if unavoidable, trace callers and describe migration.
- **Readable comments:** Explain non-obvious *why*, invariants or platform quirks; do not narrate syntax. Reuse established formatting and vocabulary.

## Use language/toolchain evidence, not generic rules

First detect the stack and existing CI/build tooling. Use native compiler, formatter, linter, type checker, tests and IDE/LSP diagnostics when available. Examples (choose only what exists or is already acceptable): `go test`/`go vet`; language type checking, `ruff`, `pytest`; TypeScript checks, ESLint/Biome; `cargo test`/Clippy; Gradle/Maven and the relevant Minecraft mod-loader mappings/version tests; Unity/Godot or other host validation. The examples are **not** an installation checklist. No surprise dependency installation, global config rewrites, heavyweight analysis, or bulk autoformatting of unrelated files.

Honor project-specific host constraints: e.g. game ticks/main-thread access, plugin unload/reload, data packs/resource mappings, distributed services, browser rendering, embedded resource limits. Do not assume Python, databases, HTTP endpoints or a web server exist.

## Builder mode — implement carefully

- State the relevant invariants to yourself, then implement the smallest complete change.
- Exercise success and important failure cases; choose focused checks proportionate to scope.
- When a new contract or architecture decision is discovered mid-build, return the concrete conflict for Architect/Orchestrator instead of privately redesigning the system.
- Hand off only meaningful decisions, affected paths, tests/evidence and known limits. No code-quality score or extra formal document.

## Reviewer mode — independent, evidence-based findings

- Verify the exact candidate, its changed interfaces and critical consumers. Do not inherit Builder's quality claim.
- Report a quality concern as a **specific mechanism**: location, triggering scenario, actual/predictable impact, severity, and smallest safe correction. Prefer reproducible evidence where possible.
- Block on correctness, security, data integrity, explicit contracts, material reliability regressions, or demonstrable maintainability faults likely to break the feature. A hypothetical future extension, personal aesthetic preference, file length alone or lack of a fashionable pattern is **not** a release blocker.
- Keep unrelated cleanup separate and nonblocking; do not expand a narrow fix into a rewrite. Do not downgrade an actual contract breach to mere style.
- Do not modify application code while reviewing; let the existing Builder → Reviewer correction loop handle fixes.

## Antipatterns to catch with evidence

- A test is weakened to conceal a defect; a fallback advertises success after failure.
- State is silently duplicated; cache invalidation or concurrent mutation is undefined.
- A tiny change creates a sprawling abstraction graph; an interface exists only to satisfy a pattern.
- Errors, cancellations, resource teardown or security boundaries are silently lost.
- New code works only for the happy path and skips the important boundary consumer.

If none of these applies, accept proportionate straightforward code. Good engineering is not maximal complexity or maximal ceremony.
