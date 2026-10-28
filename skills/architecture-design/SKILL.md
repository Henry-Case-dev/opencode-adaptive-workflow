---
name: architecture-design
description: Repository-grounded architecture decisions for software changes across languages and platforms. Use for new or altered module boundaries, shared APIs, persistence, concurrency, lifecycle, security, migrations, integrations, or architectural trade-offs; skip small changes using stable contracts.
compatibility: opencode
metadata:
  scope: universal
  discipline: pragmatic-software-architecture
---

# Architecture design — fit the real system, not a named pattern

## When this skill earns its cost

Load when the assigned work has a concrete architecture/contract decision, material coupling or lifecycle risk, repeated root-cause regression, or an explicit architecture review. Prefer the existing approved design for small changes. A large or beautifully detailed feature request does **not** prove that the repository's current contracts support it; check. Conversely, a multi-file edit does **not** automatically require redesign.

This is guidance **inside the current Architect/Reviewer task**, not another agent, gate, meeting, ADR, or deliverable. Do not ask the owner for a choice already settled. Respect the existing architecture preflight, explicit product requirements and the existing acceptance/release gates.

## Establish the actual system before suggesting structure

1. Read the requested observable behavior, invariants, non-goals and scope. Separate *required outcome* from *suggested implementation* when the user permits design latitude.
2. Inspect the current interfaces, dependency direction, data ownership, important call paths and runtime entrypoints in the **current repository**. Check relevant versions, target platforms and build/runtime limits; treat symbol caches and prior docs as navigation hints.
3. Identify whether this is a local extension, an existing-contract reuse, or an actual cross-boundary design change. Find prior decisions before inventing alternatives.
4. Check external SDK/protocol/engine facts against the installed version and authoritative documentation **only when** they materially affect the decision; never browse for ceremony or present speculative compatibility as fact.
5. State material unknowns explicitly. Do not manufacture certainty or ask for owner approval when a safe evidence-based technical choice is available.

## Decision rubric (use the relevant questions, not a ritual checklist)

- **Boundaries and ownership:** Which component owns each invariant, state transition and failure? Are responsibilities cohesive and dependencies directional? Avoid cycles and two independent sources of truth.
- **Contracts:** Which types/DTOs/events/API schemas are public, shared or persisted? Who consumes them? Versioning, compatibility, ordering, nullability and units matter when observable.
- **Data and failure semantics:** When is data committed, retried, rolled back or deduplicated? What happens on partial failure, cancellation, crash, provider outage or recovery? Fail closed where integrity/security demands it; avoid silent-success fallbacks.
- **Concurrency and lifecycle:** Consider races, reentrancy, idempotence, shutdown, timeouts, thread/process/actor ownership, event-loop/main-thread rules and resource disposal **where the stack needs them**.
- **Change cost:** Prefer the smallest design that localizes the next plausible change, not speculative extensibility. Would a proposed abstraction reduce or increase coupling and cognitive load?
- **Compatibility and deployment:** Check database/schema migrations, persisted formats, wire contracts, external APIs, config defaults, platform/toolchain version, rollback and old-data behavior when affected.
- **Security and trust boundaries:** Authority, secrets, injection, untrusted input, privileges and data exposure at the relevant seam; threat-model proportionately.
- **Performance:** Identify actual hotspots and scale assumptions before adding caches, queues, batching or optimizations. Assess failure modes of any new cache.
- **User-visible behavior:** Preserve explicitly required UI/UX, accessibility, responsiveness and platform-native rules. Architecture must not silently redesign the product.

## Patterns are tools, not requirements

- Apply dependency inversion only across a real stability or testing boundary. Interfaces/abstractions require at least one concrete reason, not a one-implementation reflex.
- Separate domain logic from I/O when it clarifies invariants or testing; do not impose Clean/Hexagonal/DDD/CQRS/event sourcing/microservices as default templates.
- Extract duplication only if the *meaning and change cadence* are genuinely shared. Sometimes two explicit flows are safer than one configurable abstraction.
- Keep module boundaries consistent with the language and runtime: idiomatic Go packages/interfaces, Python modules/async lifecycles, Rust ownership, JVM plugin classloaders/event buses, web UI state and server/client boundaries, engine or mod-loader threading/version constraints, etc. These are **examples**, not a fixed supported-stack list. First detect the actual project and use its conventions.
- In plugins, games and mods, host application contracts and versioned extension APIs outrank generic backend conventions. Do not assume server process semantics in a host-managed lifecycle.
- Avoid introducing a framework, cache, background worker, migration, or dependency unless the problem warrants it and the user/project constraints permit it.

## Choose and hand off the smallest safe decision

For a substantive decision, compare only viable options against actual constraints (correctness, reversibility, maintainability, risk and implementation cost). Then pick one recommendation with a testable reason; do not create a matrix of theoretical alternatives where no real choice exists.

Deliver a **compact, implementation-ready** answer in the existing feature artifact/handoff when appropriate:

1. Existing contracts reused and their concrete references, or the changed boundary and contract.
2. One chosen approach and why it beats the nearest reasonable alternative.
3. Who owns state/side effects, failure/cancellation behavior and any compatibility or rollback requirement.
4. Risks that need targeted Builder tests / Reviewer verification.
5. Files or surfaces **likely** affected, labeling inference vs inspection.

If no genuine architecture change remains, return that finding promptly (`ARCHITECT_NOT_NEEDED`/reuse) with evidence. Do **not** create an ADR, rewrite the owner's specification, or widen Builder scope just to demonstrate architectural work.

## Stop signs

- Invariants are not tied to a concrete component or owner.
- New abstraction has no current consumer/benefit and makes the flow harder to follow.
- Two independent owners can update the same state without a convergence rule.
- Interface compatibility is guessed rather than checked.
- A fallback masks a broken primary path or silently discards data.
- An architecture proposal demands large unrelated refactors to deliver a small requested outcome.

When one applies, identify the smallest tractable fix and its evidence. Style preference alone is not a blocker.
