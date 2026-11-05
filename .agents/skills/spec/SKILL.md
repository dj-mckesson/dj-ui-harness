---
name: spec
description: Turn a coding request and resolved human answers into one bounded, reviewable product specification without planning or implementing the change.
---

# Purpose

Author the product contract that defines what the requested change must accomplish. Resolve
material ambiguity through explicit questions; do not design the implementation, write tests, or
approve the specification.

## Required inputs

This skill requires:

- the supervisor-supplied action ID and task ID;
- the original coding request;
- relevant read-only application context;
- any supervisor-recorded answers or rejection feedback for the current spec cycle; and
- the exact output path `planning/<task_id>/SPEC.md`.

Do not assume context from another action or conversation.

## Boundaries

- Write only the exact supplied `SPEC.md` path, and only for a ready result.
- Treat `TASK.md`, every other planning artifact, application and test files, `checks/`,
  `harness/`, and `runs/` as read-only.
- Do not write `plan.json` or `PLAN.md`, choose implementation paths or commands, implement code,
  author tests, claim approval, or advance the workflow.
- Do not guess when an unresolved decision would materially change observable behavior, errors,
  boundaries, scope, or acceptance evidence.
- Do not generate, change, or reuse a different task or action identity.

## Workflow

1. Validate the action identity, request, context, feedback, and exact output path.
2. Inspect the supplied repository context for facts that can be discovered without asking the
   human.
3. Complete the ambiguity review below and identify only material questions that require a human
   product decision.
4. If any remain, return `questions` without writing `SPEC.md`.
5. After receiving answers, repeat the context and ambiguity reviews because an answer may reveal
   another material decision. Do not ask an already-resolved question again.
6. Otherwise, write a complete `SPEC.md` with the required structure below.
7. Complete the readiness test below.
8. Return exactly one result state.

## Ambiguity review

Before writing the specification, consider each area below. Scale the review to the request and do
not invent requirements just to fill the list.

- **Users and outcomes:** Who uses the change, what starts it, and what observable result do they
  need?
- **In-scope behavior:** Which inputs, operations, outputs, state changes, and user-visible details
  are required?
- **Boundaries and non-goals:** What is included, excluded, preserved, or intentionally unchanged?
- **Errors and edge cases:** What should happen for missing, invalid, duplicate, empty, unavailable,
  or boundary inputs when those cases apply?
- **Data and lifecycle:** What information is created, read, changed, retained, removed, or exposed,
  and which lifecycle rules are product decisions?
- **Compatibility and dependencies:** Must existing behavior, stored data, interfaces, or external
  systems remain compatible?
- **Security and trust:** Are permissions, private data, destructive actions, or trust boundaries
  involved?
- **Quality constraints:** Are there required reliability, operability, usability, accessibility,
  maintainability, performance, capacity, or resource outcomes?
- **Acceptance evidence:** What observable evidence would let a reviewer decide that each required
  outcome works, including important failure behavior?

First use the request, supplied context, saved answers, and rejection feedback to resolve these
areas. A fact that can be learned from the supplied repository context is an inspection task, not a
question for the human. An implementation preference is not a human product question unless the
choice changes observable behavior, compatibility, risk, scope, or acceptance evidence.

Ask rather than assume when an unknown answer would materially change the specification. This
includes an unknown numeric target, compatibility promise, failure policy, data rule, security
boundary, or explicit exclusion. Do not ask speculative questions about hypothetical future needs.

## Human question quality

When human decisions remain:

- Ask one decision per question and use direct, neutral language.
- Briefly state the context and why the answer changes the specification.
- When there is a small known choice set, give two to four concrete options and their meaningful
  trade-offs. Do not hide a presumed default in the wording.
- State the affected behavior, boundary, risk, or acceptance evidence.
- Do not ask the human to choose file paths, libraries, algorithms, commands, or other implementation
  details unless the original request explicitly makes that choice a product constraint.
- Return all currently known related material questions together when they can be answered
  independently. Keep separate decisions as separate strings in `questions`.
- Use an open question when valid choices cannot be listed safely; do not invent options merely to
  satisfy a format.

Each question remains one JSON string. Prefer this compact shape when options are useful:

```text
Decision needed: <question> Context: <why it matters>. Options: (A) <choice and trade-off>; (B) <choice and trade-off>. Impact: <behavior, boundary, risk, or evidence affected>.
```

## Readiness test

Return `ready_for_approval` only after all of these checks pass:

- Every material ambiguity found in the review is resolved by repository evidence, a recorded human
  answer, an explicit requirement, or an explicit boundary or non-goal.
- No requirement depends on an assumed product decision or an invented constraint.
- The objective, success behavior, failure behavior, boundaries, and non-goals are clear enough for
  an independent planner and reviewer to reach the same scope.
- Every requirement and acceptance criterion has a unique stable ID.
- Every acceptance criterion names observable evidence, and the criteria cover each required
  outcome and material failure case.
- The `Resolved decisions` section records the decisions that materially shaped the specification,
  including applicable saved human answers.
- No unresolved blocking question remains.

## Output artifact

Write `SPEC.md` with these required headings:

```markdown
# Specification

## Objective and observable behavior
...

## Boundaries, errors, and non-goals
...

## Resolved decisions
...

## Requirements
- REQ-01: ...

## Acceptance criteria
- AC-01
  - Criterion: ...
  - Evidence: ...
```

Use `REQ-<two-or-more-digits>` and `AC-<two-or-more-digits>` IDs. Requirements and acceptance
criteria describe outcomes, not a preferred implementation. Preserve still-applicable IDs across a
rejected spec revision instead of renumbering them casually.

## Result states

Return one JSON object as the final response. Do not write the result as a repository file. Echo the
supervisor-supplied task ID and action ID exactly.

Use `questions` only when material human decisions remain, with no file changes:

```json
{
  "task_id": "<supervisor-supplied task ID>",
  "action_id": "<supervisor-supplied action ID>",
  "status": "questions",
  "files_written": [],
  "questions": ["<material question>"],
  "blockers": []
}
```

Use `ready_for_approval` only after writing the complete exact output:

```json
{
  "task_id": "<supervisor-supplied task ID>",
  "action_id": "<supervisor-supplied action ID>",
  "status": "ready_for_approval",
  "files_written": ["planning/<task_id>/SPEC.md"],
  "questions": [],
  "blockers": []
}
```

Use `blocked` without writing the artifact when required inputs are missing, contradictory, or
outside this contract:

```json
{
  "task_id": "<supervisor-supplied task ID>",
  "action_id": "<supervisor-supplied action ID>",
  "status": "blocked",
  "files_written": [],
  "questions": [],
  "blockers": ["<non-empty blocker>"]
}
```

## Stop conditions

- Return `questions` without editing when a material product decision is unresolved.
- Return `blocked` without editing for missing or contradictory inputs, mismatched identities or
  paths, requested writes outside `SPEC.md`, or a request to plan or implement.
- Return `ready_for_approval` only when the complete spec is ready for independent human review.
