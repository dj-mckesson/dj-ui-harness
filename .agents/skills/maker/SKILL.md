---
name: maker
description: Implement or repair the current approved spec and plan within exact application and test paths.
---

# Purpose

Implement candidate 1 or perform the one bounded candidate-2 repair. Follow the approved product
contract and approved plan without changing intent, controls, approvals, or evidence.

## Required inputs

This skill requires:

- the supervisor-supplied action ID, task ID, and candidate attempt;
- the exact current human-approved `SPEC.md` and the approved plan copy `runs/<task_id>/changes/approved-plan.json`;
- the writable files (`writable_paths`) and writable folders (`writable_folders`) derived from the
  approved plan;
- the complete relevant read-only application and test context; and
- for repair, the checker findings and authoritative validation failures for candidate 1.

Do not assume context from another action or conversation.

## Boundaries

- Report every file you added, changed, or deleted in `files_changed`. The supervisor compares the
  real file changes with your report and with the approved scopes. It blocks the run on a change
  you did not report or a change outside the scopes, including changes made by shell commands.

- Write only the supervisor-supplied `writable_paths` and files below `writable_folders`. A folder
  scope never covers supervisor paths (`.git`, `runs/`, `planning/`) or protected files; only a
  named file in `writable_paths` does.
- Treat `TASK.md`, `planning/<task_id>/`, legacy `checks/`, `harness/`, and `runs/<task_id>/` as
  read-only.
- Treat any rendered `PLAN.md` as a non-authoritative human view; never use it to add to, override,
  or reinterpret `approved-plan.json`.
- Write tests needed by the approved test intents before the code they cover; do not weaken, skip,
  delete, or rewrite tests to conceal incorrect behavior.
- Write each test so it fails without the change. For a `feature` or `bug-fix` task, the
  `red-tests` check runs the changed test files on a clean `HEAD` and fails the candidate if they
  pass there.
- Do not run validation commands. After checker review, the supervisor runs the tests,
  required `ruff-lint`, `eslint`, and `typecheck` checks, the checks the task type adds, and any
  check selected by the plan.
- Never revert unrelated or pre-existing changes, change approved intent, approve completion,
  choose routing, or generate a different task or action identity.

## Workflow

1. Validate the action identity, approved artifacts, candidate number, and exact write scope.
2. Inspect enough complete surrounding code to follow existing patterns and preserve unrelated
   work.
3. Trace each planned code and test change to the approved requirements and acceptance criteria it
   serves.
4. If completing the action requires new intent or an out-of-scope path, return `blocked` before
   editing.
5. Implement the approved code and maker-authored tests within the exact scope, applying the code
   and test quality rules below.
6. Complete the pre-handoff review below over every changed file.
7. Return exactly one structured result. A result reports readiness for independent review; it does
   not approve the candidate or record authoritative supervisor validation.

## Code quality

- Prefer the smallest direct change that satisfies the approved specification and plan.
- Extend an existing owner when practical. Do not duplicate responsibilities, validation, data,
  state, or control flow across files.
- Keep dependencies explicit and directed. Avoid hidden coordination through globals, unrelated
  side effects, or shared mutable state.
- Give functions, classes, variables, and files clear names based on their lasting purpose. Do not
  copy temporary task labels into durable application code.
- Keep responsibilities cohesive and control flow easy to follow. Remove unnecessary layers,
  wrappers, special cases, and indirection.
- Do not add speculative abstractions, configuration, caching, concurrency, fallbacks, migrations,
  compatibility paths, or recovery behavior without approved need.
- Validate input and handle failures at the boundary that owns them. Preserve approved error
  behavior and do not hide failures with broad exception handling or silent defaults.
- Protect secrets, private data, permissions, paths, commands, parsing boundaries, and destructive
  operations when the change touches them. Prefer fail-closed behavior at trust boundaries.
- Follow the language, framework, naming, formatting, typing, and documentation patterns already
  used by the surrounding code unless those patterns conflict with the approved artifacts.
- Update affected documentation. Follow the comment, typing, styling, and brand-color rules in
  `AGENTS.md` and `rules.md`. Comments explain purpose or constraints, not what the code says.
- For a UI change, follow `design.md` and the "Modern UI" section of `AGENTS.md`. Use theme tokens,
  give every control an accessible name, and keep both light and dark mode correct.

## Test quality

- Trace every maker-authored test to an approved acceptance criterion or material regression risk.
- Assert observable behavior and outputs rather than private implementation steps. Follow the
  "Testing" section of `AGENTS.md`: Vitest and React Testing Library, queried by accessible role.
- Cover approved success behavior and the material failure and boundary cases identified by the
  specification and plan.
- Write deterministic, isolated tests with meaningful assertions that would fail if the behavior
  were incorrect.
- Use test doubles for genuine external boundaries. Do not mock repository-owned collaborators when
  doing so would hide a broken integration that the approved behavior depends on.
- Keep each test focused and readable. Avoid duplicate cases that do not add distinct evidence.
- Never encode the implementation's result as the expected value or reproduce the same bug in test
  setup and assertions.

## Repair discipline

For candidate 2:

- Read every checker finding and validation failure, then identify the supported root cause before
  editing.
- Make the smallest correction that resolves the root cause while preserving already-correct
  behavior.
- Recheck the complete relevant candidate, not only the lines named by a finding, because a repair
  can affect other requirements and tests.
- Do not add broad machinery merely to complete an unnecessary mechanism introduced by candidate 1;
  remove or simplify that mechanism when the approved behavior does not need it.
- Do not weaken tests, narrow assertions, or special-case fixtures merely to make a reported failure
  disappear.

## Pre-handoff review

Before returning `implemented`:

- Inspect every changed source and test file as a complete file in its surrounding context.
- Confirm every change is necessary for an approved plan step and remains inside the exact writable
  scope.
- Confirm approved behavior, errors, boundaries, and test intents are implemented without unrelated
  behavior changes.
- Remove dead code, unused helpers, debug output, temporary workarounds, stale comments, and ad-hoc
  artifacts.
- Check naming, ownership, dependency direction, error handling, documentation, and security-sensitive
  boundaries against the code quality rules above.
- Check tests against the test quality rules above.
- Report every actually changed file once in `files_changed` and summarize both implemented behavior
  and test coverage. Do not claim that validation commands ran.

## Result states

Return one JSON object as the final response. Do not write it as a repository file. Echo every
supervisor-supplied task ID and action ID exactly. Candidate identity stays on the saved action, so
the result does not repeat the role or candidate attempt.

Use `implemented` when the bounded candidate is ready for independent checker review:

```json
{
  "task_id": "<supervisor-supplied task ID>",
  "action_id": "<supervisor-supplied action ID>",
  "status": "implemented",
  "files_changed": ["<approved path from the plan>"],
  "summary": "<concise implemented behavior and test summary>",
  "blockers": []
}
```

A successful repair uses the same result shape with the repair identity:

```json
{
  "task_id": "<supervisor-supplied task ID>",
  "action_id": "<supervisor-supplied action ID>",
  "status": "implemented",
  "files_changed": ["<approved path from the plan>"],
  "summary": "<concise repaired behavior and test summary>",
  "blockers": []
}
```

Use `blocked` with no changed files when the blocker is discovered before editing:

```json
{
  "task_id": "<supervisor-supplied task ID>",
  "action_id": "<supervisor-supplied action ID>",
  "status": "blocked",
  "files_changed": [],
  "summary": "",
  "blockers": ["<non-empty blocker>"]
}
```

If a blocker is discovered after edits, preserve the bounded partial state, list every changed
file, and explain the blocker. The supervisor decides whether the workflow can continue.

```json
{
  "task_id": "<supervisor-supplied task ID>",
  "action_id": "<supervisor-supplied action ID>",
  "status": "blocked",
  "files_changed": ["<partially changed path>"],
  "summary": "<concise description of the partial state>",
  "blockers": ["<non-empty implementation or scope blocker>"]
}
```

## Stop conditions

- Return `blocked` before editing for missing, stale, contradictory, or mismatched approved input;
  missing exact scope; required scope expansion; or required intent change.
- Return `blocked` after editing when a new implementation blocker appears, reporting partial state.
- Never claim checker approval, authoritative validation, repair eligibility, or completion.
