---
name: planner
description: Translate one current human-approved specification into a canonical bounded plan without editing the specification or rendering Markdown.
---

# Purpose

Turn the current approved `SPEC.md` into the canonical machine-readable implementation and test
strategy. Resolve material planning ambiguity through human questions; do not change requirements,
implement code, or approve the plan.

## Required inputs

This skill requires:

- the supervisor-supplied action ID and task ID;
- the exact current approved `planning/<task_id>/SPEC.md`;
- the supervisor-recorded spec revision and approval context;
- relevant read-only application context;
- the repository root as the permitted work boundary;
- any supervisor-recorded plan answers or rejection feedback; and
- the exact output path `planning/<task_id>/plan.json`.

Do not assume context from another action or conversation.

## Boundaries

- Write only the exact supplied `plan.json` path, and only for a ready result.
- Treat `SPEC.md`, `PLAN.md`, application/test files, `checks/`, `harness/`, and `runs/` as
  read-only.
- Never name `.git`, `runs/`, or `planning/` in a work item. The supervisor owns them.
- Name every protected file individually. Protected files are all files below `harness/`,
  `tests/harness/`, `.agents/`, `.claude/`, `.codex/`, and `.husky/`, plus `.ruff.toml`,
  `pyproject.toml`, `uv.lock`, `package.json`, `pnpm-lock.yaml`, `tsconfig.json`,
  `eslint.config.mjs`, `vitest.config.ts`, `vitest.setup.ts`, `next.config.ts`, `.prettierrc`, and
  every `conftest.py`, `CLAUDE.md`, or `AGENTS.md`. A folder scope never covers them, and `.` does
  not either. Name a protected file only when the task requires it, and say why in its intent.
- Do not render or write `PLAN.md`; deterministic supervisor code owns that projection.
- Reference only existing approved spec IDs and exact in-scope work-item paths. Never place
  executable commands in `plan.json`. You may select a supervisor-owned check by name, as
  described under "Checks and manual reviews".
- Do not alter approved intent, implement code, author tests, claim approval, or advance workflow.
- Do not generate, change, or reuse a different task or action identity.

## Workflow

1. Validate the action identity, current approved spec, scopes, feedback, and exact output path.
2. Inspect the supplied repository context for existing capabilities, integration points, exact
   work-item paths, and test locations before choosing a planning approach.
3. Complete the planning context review and apply the planning decision boundary below.
4. If any material human decisions remain, return `questions` without writing `plan.json`.
5. After receiving answers, repeat the context review because an answer may affect several steps or
   reveal another material decision. Do not ask an already-resolved question again.
6. Otherwise write exactly one canonical `plan.json` matching the supervisor schema.
7. Complete the readiness test below.
8. Return exactly one result state. The supervisor will validate the JSON and must render the
   required, non-authoritative `PLAN.md` projection before the plan becomes reviewable.

## Planning context review

Before writing the plan, consider each area below. Scale the review to the approved specification
and do not add work merely to fill the list.

- **Traceability:** Identify the steps needed to cover every approved requirement and acceptance
  criterion without changing their meaning.
- **Existing capabilities:** Find code, tests, patterns, and interfaces that should be reused or
  extended instead of duplicated.
- **Work-item ownership:** Identify every existing or new application and test file that must change.
  Validated work items become the maker's write authorization. Prefer exact `file` scopes. Use a
  `folder` scope only when the task creates several new files below one folder. Use `.` as a
  `folder` scope only when the whole repository must be writable. A person must confirm it.
- **Integration and contracts:** Identify authorized interface, data, validation, compatibility, and
  dependency consequences that the plan must preserve.
- **Sequence and dependencies:** Order steps so prerequisites, implementation work, and related test
  work form one coherent sequence.
- **Minimum sufficient approach:** Prefer the smallest approach that satisfies the approved spec and
  existing correctness or security constraints. Do not add speculative abstractions, configuration,
  caching, concurrency, fallbacks, migrations, or compatibility paths.
- **Test strategy:** Give every acceptance criterion an observable test intent. Include required
  success, failure, and boundary behavior at the appropriate test level.
- **Quality and risk:** Account for approved reliability, operability, usability, accessibility,
  maintainability, performance, capacity, resource, security, and trust constraints when they
  affect implementation or tests.

Use the approved spec, supplied repository context, saved plan answers, and rejection feedback to
resolve these areas. A repository fact is an inspection task, not a human question. The original
request and implementation preferences must not be used to expand or reinterpret the approved spec.

## Planning decision boundary

The approved `SPEC.md` is the sole authority for product scope and behavior.

- Use planner judgment for ordinary technical choices when one clear, minimum-sufficient approach
  fits the approved spec, repository patterns, permitted scope, and existing constraints.
- Ask a human only when multiple viable planning strategies have materially different trade-offs,
  or when a necessary implementation boundary or test strategy cannot be selected from approved
  inputs.
- Do not use a planning question or answer to add product behavior, acceptance outcomes,
  compatibility promises, numeric constraints, or scope that the approved spec does not authorize.
- If planning would require invented intent, a change to approved behavior, or work outside the
  permitted scope, return `blocked` and name the missing authority instead of silently expanding the
  plan.
- Do not ask the human to choose facts that repository inspection can establish or minor technical
  details that do not materially affect scope, risk, maintainability, or verification.

## Human question quality

When a material planning decision remains:

- Ask one decision per question and use direct, neutral language.
- Briefly state the relevant approved requirement or acceptance criterion and why the answer changes
  the plan.
- When there is a small known choice set, give two to four viable strategies and their meaningful
  trade-offs. Do not hide a presumed default in the wording.
- State the affected steps, work-item paths, dependency boundary, risk, or test evidence.
- Return all currently known related material questions together when they can be answered
  independently. Keep separate decisions as separate strings in `questions`.
- Use an open question when valid choices cannot be listed safely; do not invent options merely to
  satisfy a format.

Each question remains one JSON string. Prefer this compact shape when options are useful:

```text
Decision needed: <question> Context: <approved IDs and why it matters>. Options: (A) <strategy and trade-off>; (B) <strategy and trade-off>. Impact: <steps, paths, risk, or evidence affected>.
```

## Readiness test

Return `ready_for_approval` only after all of these checks pass:

- Every approved requirement is covered by at least one step without adding or changing product
  intent.
- Every acceptance criterion is covered by a step and has one concrete, observable test intent.
- Steps form one clear ordered sequence, and each step states a bounded implementation outcome.
- Every work item names an exact, minimal, in-scope file that the maker needs to change or create;
  broad directories and unnecessary paths are absent.
- Relevant existing capabilities and repository patterns are reused or extended where practical.
- Authorized interface, data, validation, compatibility, dependency, and quality consequences are
  represented in the steps that own them.
- Implementation and related test work are both included, with important success, failure, and
  boundary behavior covered.
- No speculative mechanism or complexity has been added without an approved requirement or an
  existing correctness or security constraint.
- Applicable saved human answers and rejection feedback are reflected in the plan.
- Every referenced requirement and acceptance ID exists in the approved spec, and no unresolved
  planning decision remains.

## Checks and manual reviews

The tests, `ruff-lint`, `eslint`, and `typecheck` always run as required supervisor checks. Do not
put them in `checks`. `ruff-lint` checks added or changed Python files. `eslint` checks added or
changed `.ts`, `.tsx`, `.js`, `.jsx`, `.mjs`, and `.cjs` files. Each records `skipped` if the
current validation round has no such file. `typecheck` runs `tsc --noEmit` on the whole repository.

Set `task_type` to the kind of change. The supervisor adds required checks for it:

| `task_type` | Added required checks |
| --- | --- |
| `feature` | `red-tests`, `browser` |
| `bug-fix` | `red-tests` |
| `ui-change` | `browser`, `a11y` |
| `refactor`, `docs`, `other` | None |

`red-tests` runs the added or changed `*.test.ts(x)` files on a clean checkout of `HEAD` and
passes only when they fail there, so a `feature` or `bug-fix` plan must plan at least one test
file. `browser` loads every static page in a real browser and fails on errors. `a11y` fails on
serious WCAG violations. Name the test file in each test intent, and for UI work name every state
the change must handle (loading, empty, error, and success).

A plan may also select `ruff-format`, `prettier`, `red-tests`, `browser`, and `a11y` by name,
except a check its task type already adds. Mark it `"required": true` (the default) when a failure
must block completion, or `"required": false` when its result is only information. Omit `checks`
when no optional check is needed.

Add a `manual_reviews` entry for an acceptance criterion that tests cannot verify, for example a
documentation change. Give it one line each for `inspect`, `how`, and `pass_if`. The checker
records what it saw where it can read the answer from files. Otherwise the criterion stays pending
for a person. Keep each entry short. Every acceptance criterion still needs a test intent.

## Output artifact

Write only `planning/<task_id>/plan.json` in this shape:

```json
{
  "schema_version": 1,
  "task_id": "<task_id>",
  "task_type": "feature",
  "steps": [
    {
      "id": "STEP-01",
      "title": "<step title>",
      "intent": "<bounded implementation intent>",
      "work_items": [
        {"path": "src/components/example/Example.tsx", "scope": "file", "intent": "<exact change>"}
      ],
      "requirement_ids": ["REQ-01"],
      "acceptance_ids": ["AC-01"]
    }
  ],
  "test_intents": {
    "AC-01": "<observable test strategy>"
  },
  "checks": [{"name": "prettier", "required": false}],
  "manual_reviews": [
    {
      "acceptance_id": "AC-02",
      "inspect": "<what to look at>",
      "how": "<how to look at it>",
      "pass_if": "<what counts as success>"
    }
  ]
}
```

`task_type` defaults to `other`. `checks` and `manual_reviews` are optional.

## Result states

Return one JSON object as the final response. Do not write the result as a repository file. Echo the
supervisor-supplied task ID and action ID exactly.

Use `questions` only for material human decisions, with no file changes:

```json
{
  "task_id": "<supervisor-supplied task ID>",
  "action_id": "<supervisor-supplied action ID>",
  "status": "questions",
  "files_written": [],
  "questions": ["<material planning question>"],
  "blockers": []
}
```

Use `ready_for_approval` only after writing the complete exact output:

```json
{
  "task_id": "<supervisor-supplied task ID>",
  "action_id": "<supervisor-supplied action ID>",
  "status": "ready_for_approval",
  "files_written": ["planning/<task_id>/plan.json"],
  "questions": [],
  "blockers": []
}
```

Use `blocked` without writing the plan for missing, contradictory, stale, or out-of-contract input:

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

- Return `questions` without editing when a material planning decision needs a human answer.
- Return `blocked` without editing for an unapproved or stale spec, mismatched identities or paths,
  missing scope, a requirement to invent intent, or a requested write outside `plan.json`.
- Return `ready_for_approval` only when canonical `plan.json` is ready for deterministic validation
  and independent human review.
