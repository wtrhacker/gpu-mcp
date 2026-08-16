# ADR 0006: Agent Guidance Boundaries for Managed GPU Work

## Status

Accepted and implemented on 2026-08-16.

## Principle

GPU MCP supports scientific judgment by making machine state legible and by
enforcing the few conditions that must hold for safe shared-GPU operation. It
does not prescribe the research workflow that should follow from that state.

> Freedom is structural: if state blocks an action, prose cannot restore it;
> if state leaves judgment open, prompts need not enumerate choices.

This principle separates three kinds of information:

1. **Event facts** are short-lived observations such as a local outcome or a
   scheduled status check becoming due.
2. **Stable semantics** explain durable tool concepts such as smoke evidence,
   polling cadence, provisional outputs, and targeting.
3. **Hard invariants** are conditions enforced by server state, validation, and
   fail-closed refusals.

Each kind belongs at the narrowest surface that can express it correctly.

## Decision

### Event facts belong in hooks

The managed-job hooks report only:

- the job identifier;
- whether a local outcome was detected or a scheduled status check is due;
- the exact targeted `manage_gpu_job(action="status", ...)` call; and
- a terse handoff to continue from the returned lifecycle.

Hooks do not interpret outcomes, restate the full polling model, list possible
research actions, or recommend how scientific work should proceed. A
`PreToolUse` reminder suppresses a job already covered by the in-flight
targeted status call. If other jobs have events ready, their reminders remain
visible.

The Stop hook waits for the same local readiness facts and requests the same
targeted status reconciliation. It does not turn a cadence event into a general
instruction to stay busy or to avoid intentional waiting.

### Stable semantics belong in tool descriptions

The registered `run_python_on_gpu` description is the planning-time source for
job roles, smoke evidence, smoke skip reasons, cadence hints, cadence defaults,
and descriptive duration metadata. These semantics appear once in the stable
tool surface and do not need reactive launch reminders.

The registered `manage_gpu_job` description explains the compact not-due
response and the reason-bearing overrides for early inspection or cadence
updates. Stable documentation and ADRs carry fuller operational detail.

### Current evidence belongs in tool results

Tool results say what happened, where the evidence came from, and what is still
uncertain. A running or compact not-due status identifies closed or atomically
published artifacts as provisional evidence. Provisional does not mean
unusable; it means the evidence
may still change. It may still show that a live run should stop. What it cannot
establish by itself is that the job completed or that an artifact is its final
result. An unknown outcome remains nonterminal. A terminal result says so
directly.

A successful smoke result exposes neutral evidence metadata: the
`smoke_job_id` and the fact that it is positive viability evidence. It does not
preselect a main launch. Smoke establishes evidence that another decision may
use; it is not a workflow transition.

## Evidence, Not Workflow

Smoke checks and polling cadence organize evidence without deciding the
science. A smoke run can establish that a setup is viable, but it cannot decide
whether the next useful action is production, parameter revision, analysis, or
something else. A scheduled status time makes a lifecycle observation due, but
it does not determine how the agent should spend all intervening research time.

Structured fields preserve the facts needed for judgment. Model-facing prose
stays local to the state being reported and does not enumerate an action menu.

## Hard Invariants and Escape Hatches

Hard gates protect real invariants:

- stale policy requires the approval protocol;
- reservation and process identity must be proven before destructive lifecycle
  changes or retry;
- a recorded outcome is reconciled through `status` before it is called
  terminal, and a reservation is released only after a terminal outcome or
  separate proof that the process is gone; and
- a not-due status avoids remote inspection unless the call carries a reason.

Smoke and early-poll defaults admit explicit, auditable judgment. An explicitly
staged main launch may use successful same-repo smoke evidence or a nonempty
`smoke_skip_reason`. A justified early inspection may use
`early_poll_reason`. These fields are escape hatches in server state, not prose
assurances. They preserve autonomy while recording why the default was not
appropriate.

## Prompt Locality and Churn Control

One transition should normally produce one relevant message. The hook does not
repeat a status request while that targeted request is already in flight. Due
and outcome reminders remain scoped to the current repository and active
attempt. Reminder throttling uses the job's polling interval, and Stop delivery
deduplicates the same event within a turn.

The system remains quiet when no event or invariant requires model attention.
This protects context for scientific reasoning and avoids making repeated
phrasing look more authoritative than the underlying state.

## Final Architecture

| Layer | Model-visible responsibility | Enforced responsibility |
|---|---|---|
| Tool description | Stable planning semantics | None |
| PreToolUse hook | Ready event fact and exact status target | Policy-staleness block only |
| Stop hook | Ready event fact and exact status target | Continue the turn for reconciliation |
| Tool result | Current lifecycle evidence and epistemic boundary | Report the server decision |
| Server state machine | Concise refusals and structured provenance | Policy, ownership, identity, reservation, and lifecycle invariants |

The hook notices, the status tool interprets, the server enforces, and the agent
judges what the evidence means for the research.

## Verification

Focused contract tests cover reminder suppression for in-flight targeted status
calls, preservation of reminders for other ready jobs, terse Stop continuation,
compact provisional/final guidance, and neutral successful-smoke evidence.
