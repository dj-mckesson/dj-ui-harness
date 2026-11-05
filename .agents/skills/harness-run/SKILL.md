---
name: harness-run
description: Run or resume the guided coding harness from its saved Python supervisor state. Use when the user asks to start, continue, execute, or resume the spec, plan, implementation, review, and validation workflow, including when it is waiting for agent work, human questions, or artifact approval.
---

# Harness Run

Run the saved workflow as a thin transport loop. Treat every JSON value returned by Python as
authoritative. Never choose the next role, stage, repair, validation command, or writable path.

## Start or resume

Work from the repository root. Put one JSON payload in a temporary file, then use one of these
commands:

```text
uv run python -m harness.execute start <payload-file>
uv run python -m harness.execute next-action <payload-file>
uv run python -m harness.execute submit <payload-file>
```

For `start`, provide `repo_root`, `original_request`, and optional `context_paths`. For
`next-action`, provide `repo_root` and the saved `task_id`. For `submit`, provide `repo_root`, the
saved `task_id`, and one `result` object for the current action. Omit the file argument to read the
same JSON payload from standard input.

Two more commands help after an interruption. Both take `repo_root` and an optional `task_id`,
and default to the active or newest run:

```text
uv run python -m harness.execute status <payload-file>
uv run python -m harness.execute resume <payload-file>
```

Use `status` to read where a run stands without changing it. Use `resume` after a restart. It
finishes checks that a stopped process left behind and returns the pending action again.

Use `start` once for a new request. Use `next-action` for an existing task. Repeated
`next-action` calls return the same durable pending action until its matching result is accepted.

## Carry out one returned action

1. Read the returned JSON `kind`.
2. Carry out or present exactly that one action.
3. Submit the exact result for that action.
4. Request the next action and repeat until human input or a terminal result stops the loop.

For an `agent` action:

- Start a fresh agent with the exact `persona_id` and `skill_id` supplied by Python.
- Pass the complete action, including its exact `task_id`, `action_id`, `candidate_attempt`,
  `required_input_paths`, `writable_paths`, and context.
- Do not infer, widen, remove, or replace any supplied identity or path.
- Tell each role agent to read `AGENTS.md` and `rules.md` before work, and `design.md` for any UI
  change. Tell the agent to read every path in `required_input_paths` before it starts.
- Submit the agent's structured result unchanged inside the CLI `result` field.
- If the agent stops without a result, leave the action pending. A later run may retry the same
  action ID.

For a `question` action, state what decision is needed in plain language, then show the exact
questions. Keep the artifact, task ID, and action ID in a short review-details line. Stop and wait
for the user's answers. Submit a `question-response` for the same identity and include one answer
for each question in the supplied order. Never answer for the user.

For an `approval` action, read every listed `review_paths` artifact before writing the prompt.
Lead with the decision in plain language. Give a brief **At a glance** summary based on the
current artifacts, then link every exact `review_paths` entry for full review. A summary helps the
user scan the proposal; it does not replace the artifacts. Avoid raw JSON fields, long IDs, and
workflow jargon in the opening explanation. Put the exact artifact, task ID, action ID, and
revision in a compact **Review details** line after the explanation. If `notice` is present, show
it first and explain that the user must review the current revision again.

- For a spec, summarize the goal, main behavior, important boundaries, and how success will be
  checked. Base each point on `SPEC.md`. Ask directly whether the user approves that specification.
- For a plan, summarize the implementation steps, affected areas, and planned tests and checks.
  Base each point on `PLAN.md` and `plan.json`. Show every `planned_files` and `planned_folders`
  path, and every `harness_paths` entry under **Protected files**. Explain why each protected path
  is planned. Ask the user to confirm that the task needs each one. Explain that each actual
  protected-file change needs a separate approval later. If `whole_repository` is
  true, say clearly that the plan may change the whole repository and ask the user to confirm that
  scope. Ask directly whether the user approves the plan and its stated scope.
- For `changes`, say that the plan was approved earlier and this decision concerns the actual
  protected-file changes. Show every exact `harness_paths` entry under **Protected files** with its
  `harness_intents` text from the approved plan. Explain each change's purpose in plain language,
  without claiming that planned intent proves the actual edit. Link the `review_paths` record that
  lists all actual changes. `harness_paths` can be empty when `drift` is not. Show every `drift`
  entry under **Changed during the pause** with its `path`, `old_digest`, and `new_digest`. Show a
  `null` digest as "absent". Explain that these files changed after checks passed and were checked
  again. Ask the user to confirm that the task needs each change. Ask directly whether the user
  approves the listed actual changes.

Keep the prompt concise, but do not omit required paths, scope, or drift details. Say that the user
can reject with feedback. Stop and wait for an explicit approval or rejection. Submit an
`approval-response` with the same identity and revision, a Boolean `approved` value, and the user's
feedback when rejected. When `whole_repository` is true, set `confirm_whole_repository` to true
only after the user explicitly confirms it. Never manufacture approval.

If `submit-result` refuses an approval with a "Restore …" message, relay that message to the user
as written. Do not resubmit. An approval can return a new approval action, for example after a
file changed. The user's earlier approval does not apply to it. Present the new action and ask
again.

For `blocked`, report the exact reason and stop. For `complete`, report the supplied evidence and
stop. Show `evidence.conditions` as a short table of acceptance ID, method, result, and note, and
list `evidence.checks`. Say plainly that a `pending` condition is not verified. Do not call a
passing test suite proof of a criterion.

## Boundaries

- Let Python choose every action and terminal outcome.
- Do not reconstruct routing rules or derive paths from repository contents.
- Run only the `harness.execute` commands above and read-only inspection needed for human prompts.
  Do not run gates, tests, validation, or caller-supplied command strings.
- Do not treat an agent suggestion as permission to change the action.
- Do not continue past a human question or approval within the same turn.
