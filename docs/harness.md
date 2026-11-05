# v1 harness

The v1 harness runs a coding task through a fixed workflow. A person approves the spec and the
plan. An agent writes the code. A second agent reviews it. The harness runs the tests and compares
the real file changes with the approved plan.

The harness is part of this repository. Its code, tests, skills, and tool hooks are reviewed with
the application. You need no separate install, `init` step, or harness version.

## What you need

- macOS. This is the system that the v1 harness is tested on.
- Node and pnpm, set up as in `AGENTS.md`. The harness runs the app's tests and checks with pnpm.
- Python 3.12 and [uv](https://docs.astral.sh/uv/). The harness is Python code, so it needs its
  own environment. This is the only Python that the app itself does not need.
- Codex or Claude Code. The same workflow runs in both.

Prepare both environments once, from the repository root:

```bash
pnpm install
uv sync
```

`uv sync` installs only the harness tools (pydantic, pytest, and ruff) from `pyproject.toml`. It
does not touch the Forecast API's `requirements.txt`.

If `uv` fails with `invalid peer certificate: UnknownIssuer`, your network re-signs HTTPS traffic.
Tell `uv` to use the system certificates:

```bash
UV_SYSTEM_CERTS=1 uv sync
```

## Run a task

Ask your coding tool to run the `harness-run` skill with your request. The skill drives the
workflow in a loop. It calls the repo-local command below, from the repository root:

```bash
uv run python -m harness.execute <operation> [payload-file|-]
```

Each operation reads one JSON payload from a file, or from standard input when you give no file or
`-`. It prints one JSON value.

| Operation     | Payload                                                   | What it does                                 |
| ------------- | --------------------------------------------------------- | -------------------------------------------- |
| `start`       | `repo_root`, `original_request`, optional `context_paths` | Starts a run and returns its first action.   |
| `next-action` | `repo_root`, `task_id`                                    | Returns the pending action of a run.         |
| `submit`      | `repo_root`, `task_id`, `result`                          | Submits the result of the pending action.    |
| `status`      | `repo_root`, optional `task_id`                           | Reports where a run stands. Changes nothing. |
| `resume`      | `repo_root`, optional `task_id`                           | Continues a saved run after a restart.       |

Run `uv run python -m harness.execute --help` to list the operations.

`start` needs `uv` on `PATH` and an existing repository root. It can create a run without
`src/`, but the default spec-author and planner read context still uses that directory. The optional `context_paths` changes only the first spec-author action's
read context.

## The workflow

1. **Spec.** An agent writes `planning/<task-id>/SPEC.md`. It may ask you questions first. You
   approve or reject the spec.
2. **Plan.** An agent writes `planning/<task-id>/plan.json`. The harness renders `PLAN.md` for you
   to read. You approve or reject the plan. At approval, the harness saves an exact copy as
   `runs/<task-id>/changes/approved-plan.json`. The rest of the run uses only this copy.
3. **Implementation.** A maker agent changes the files that the plan names. The harness compares
   the real changes with the plan.
4. **Review.** A separate checker agent reviews the candidate. It can not edit files.
5. **Tests and checks.** The harness runs the app tests, then the mandatory checks and any
   check that the approved plan selected (see [Checks](#checks)):

   ```bash
   pnpm test --maxWorkers=1
   ```

   When the files that the round checks hold a protected file, the harness also runs the
   harness tests with `uv run python -m pytest -q tests/harness`. Round 1 checks the files after the candidate. A later round checks the
   files after a change during an approval pause.

6. **Repair.** If the review, the tests, or a required check fail, the maker gets one repair. After the repair, the
   harness completes or blocks the run. It never starts a third candidate.
7. **Completion.** The harness compares the file changes again. If protected files changed, you
   approve their exact paths. After your approval, the harness scans the files again. If a file
   changed during the pause, the harness runs the tests and checks again on the current files,
   and then asks for your approval again. The run then completes and reports how each acceptance
   criterion was verified (see [The result](#the-result)).

Only the harness chooses the next step. The skill and the agents never decide it.

## Project guidance

`AGENTS.md` is the source of repository instructions for every role. `rules.md` holds the hard
rules, and `design.md` holds the design standard for UI work. Each role reads them before it starts.
`CLAUDE.md` points Claude Code to `AGENTS.md`. Action `required_input_paths` list task files
and context paths, without a separate guidance prefix.

The harness adds no coding standard of its own. It enforces the standards in these files.

## Checks

The tests, `ruff-lint`, `eslint`, and `typecheck` always run. The plan's `task_type` adds more
required checks, and a plan can select the optional checks by name. The harness owns all commands.
A plan names no command.

| Name          | What it runs                                                                                                    |
| ------------- | --------------------------------------------------------------------------------------------------------------- |
| `ruff-lint`   | Required. `ruff check` on added or changed Python files in the current round.                                   |
| `eslint`      | Required. `eslint` on added or changed `.ts`, `.tsx`, `.js`, `.jsx`, `.mjs`, and `.cjs` files.                  |
| `typecheck`   | Required. `pnpm typecheck` (`tsc --noEmit`) on the whole repository.                                            |
| `ruff-format` | Optional. `ruff format --check` on the same Python files as `ruff-lint`.                                        |
| `prettier`    | Optional. `prettier --check` on the same files as `eslint`.                                                     |
| `red-tests`   | Runs added or changed `*.test.ts(x)` files on a clean checkout of `HEAD`. Passes only if they fail there.       |
| `browser`     | Builds the app and loads every static page in Chromium. Fails on an error, a console error, or an HTTP error.   |
| `a11y`        | Builds the app and runs an axe WCAG 2.2 AA scan on every static page. Fails on a serious or critical violation. |

| `task_type`                 | Required checks it adds       |
| --------------------------- | ----------------------------- |
| `feature`                   | `red-tests`, `browser`        |
| `bug-fix`                   | `red-tests`                   |
| `ui-change`                 | `browser`, `a11y`             |
| `refactor`, `docs`, `other` | None. `other` is the default. |

`red-tests` fails when no test file changed, and when the baseline run breaks before it reports
test results, so a broken tool never counts as a failing test. `browser` and `a11y` use the
`smoke` and `a11y` projects in `playwright.config.ts`, with the specs in `e2e/harness/`.

Each check has a time limit, runs from the repository root, and saves bounded output.
The supervisor always runs `ruff-lint`, `eslint`, and `typecheck` as required checks, plus the
checks the task type adds. Plans may select any other check, and mark each `"required": true`
(the default) or `"required": false`.

- A changed-file check with no matching added or changed file records `skipped`. A skipped
  required check satisfies validation. `typecheck` still runs on the whole repository.
- A failed **required** check fails the candidate in the same way as a failed test. The maker gets
  the one repair, and the saved results show which check failed.
- A failed **report-only** check stays in the results and in the final report. It does not block.

```json
"checks": [{"name": "prettier", "required": false}]
```

`PLAN.md` lists the required and selected checks, so you see them when you approve the plan. Git
ignores caches that the tools write, such as `.next`, `.ruff_cache`, and `.pytest_cache`. See
[The changed-file check](#the-changed-file-check).

## Manual review

Some criteria can not be tested, for example a documentation change. For these, the plan adds a
`manual_reviews` entry with three short lines:

```json
"manual_reviews": [
  {
    "acceptance_id": "AC-02",
    "inspect": "docs/harness.md",
    "how": "Read the new section.",
    "pass_if": "It names every check."
  }
]
```

`PLAN.md` shows each entry at plan approval. The checker is read-only. It records `pass` or `fail`
for an entry when it can decide from the files, with one line about what it saw. When only a person
can decide, it records `pending`. A `fail` requires a `revise` verdict. A criterion with no
observation is `pending`. The harness never reports a review that nobody did as passed. There is
no extra approval step for manual reviews.

## The result

When a run completes, the result lists each acceptance criterion with its method, result, and a
note, and lists every saved check, including mandatory and skipped checks:

| Method          | Result                       | Meaning                                                                                                                                                                            |
| --------------- | ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `tests`         | `pass` or `fail`             | The result of the whole test suite. The harness does not map tests to criteria. The note shows the planned test intent. A criterion that has a manual review has no `tests` entry. |
| `manual-review` | `pass`, `fail`, or `pending` | What the checker saw. `pending` means that nobody has verified the criterion yet.                                                                                                  |

A passing suite shows that the suite passed. It does not prove that a criterion is met. Read
`pending` entries and the checker review for what was not verified.

## Approvals

You approve in these places. The agent can not approve for you.
At each approval pause, the agent gives a short summary of the decision and links the full review
files. The summary highlights the proposed behavior or steps, scope, and checks. Review the files
before you decide. The approval applies to the current artifact revision or listed actual changes.

- **Spec approval.** You review `SPEC.md`.
- **Plan approval.** You review `PLAN.md` and `plan.json`. The approval shows every planned file
  and folder, and flags every protected file.
- **Whole-repository confirmation.** If the plan uses the `.` scope, you must also set
  `confirm_whole_repository` to `true` in your approval. The harness refuses the approval without
  it.
- **Protected changes.** If a protected file changed, the run pauses before
  completion. The approval shows each changed path next to the purpose that the approved plan gave
  it. You decide whether the task needs the change. You approve the exact paths, or you reject
  them and the run blocks. The run also pauses here when files changed during an earlier approval
  pause. The approval then lists each such file in `drift`, with its old and new digest.

Each spec and plan approval is bound to the exact content shown. The approval action records a
digest of each file in `review_paths`. If a file changes before you approve, the harness does not
accept the approval. It returns a new approval for the current content, with a `notice` that names
the changed files. If the current content is now invalid, the harness refuses the approval and
keeps it pending. Restore the file, or reject the approval with feedback. A rejection never checks
the digests.

Plan approval also checks that `SPEC.md` still has the content that you approved. If it changed,
the run goes back to spec approval, and the plan is not approved. If the changed `SPEC.md` is
invalid, the harness refuses the approval and keeps it pending. Restore `planning/<id>/SPEC.md` to
the approved content. A rejection does not help here, because the planner can not change the spec.

An approval is a result that you submit for the pending action:

```json
{
  "kind": "approval-response",
  "task_id": "01",
  "action_id": "<action_id of the pending approval>",
  "artifact": "plan",
  "revision": 1,
  "approved": true,
  "confirm_whole_repository": false
}
```

A rejection needs `"approved": false` and a `feedback` text that tells the author what to change.

## File scopes

The repository root is the outer work boundary. The plan names what an agent may change. Each work
item is one of:

- a `file` scope: one named file. The file may not exist yet.
- a `folder` scope: everything below an existing folder, including new files.
- the `.` folder scope: the whole repository. It needs your explicit confirmation.

```json
{ "path": "src/utils/format-dates.ts", "scope": "file", "intent": "Add the helper." }
```

These rules apply to every scope, including `.`:

- The harness owns `.git`, `runs/`, and `planning/`. A plan can not name them, and no folder scope
  covers them.
- Protected files need a named `file` scope. A folder scope never covers them. This includes
  `harness/`, `tests/harness/`, `.agents/`, `.claude/`, `.codex/`, `.husky/`, `.ruff.toml`,
  `pyproject.toml`, `uv.lock`, `package.json`, `pnpm-lock.yaml`, `tsconfig.json`,
  `eslint.config.mjs`, `vitest.config.ts`, `vitest.setup.ts`, `next.config.ts`, `.prettierrc`, and
  every `conftest.py`, `CLAUDE.md`, or `AGENTS.md`.
- Paths must stay inside the repository. The harness rejects `..` and any path that goes through a
  symbolic link.

The edit hooks for Codex and Claude Code reject an edit outside the scopes before it happens. In
Claude Code, the hook applies to the four harness role agents only, so other sessions can keep
working during a run. The changed-file check still blocks a run if any edit lands outside its
scopes.

## The changed-file check

Edit hooks only see edits that the coding tool reports. A shell command can change files too. The
harness therefore compares the real files with the approved plan.

- **Before coding.** At plan approval, the harness saves the approved plan. It then saves the
  baseline separately: digests of Git-listed files in `runs/<task-id>/changes/baseline.json`. It
  uses a filesystem scan if Git is unavailable. Every later scan uses the scan method of the
  baseline. If that method fails, the run stops.
- **After each candidate, and after the tests.** The harness lists the added, changed, and deleted
  files. It blocks the run, and names the exact paths, when:
  - a change is outside the approved scopes.
  - a change is missing from the candidate's `files_changed` report.
  - a symbolic link points outside the repository.
  - a file differs after the tests from the validated state.
  - the file state can not be read or the saved baseline is missing.
- **Before completion.** When you approve the changes, the harness scans the files again. It
  compares them with the state that the checks passed. If a file changed, it saves
  `runs/<task-id>/changes/drift-<candidate>.json` and starts a new validation round. The round
  runs the tests and checks again, and compares the files with the drift record. The changed
  files count as reported. A change outside the approved scopes still blocks the run. If the
  round passes, you approve again, with the changed files listed. If the tests or a required
  check fail, candidate 1 gets the one repair, and candidate 2 blocks.
- **Kept apart.** The record lists files that were already changed in Git before the run. Git
  ignores check outputs such as `__pycache__`, `.next`, `.pytest_cache`, and `.ruff_cache`. The filesystem
  fallback records these outputs separately. They never block a run.

A run may change protected code that the same run then uses. If you approve such a change, the next
call to the harness can already run the changed code. The check shows you the change, but nothing
holds the running harness at a fixed version. Review every changed protected path with care.

The check detects changes. It does not prevent them. The Git scan excludes ignored files and root
`runs/`. The filesystem fallback skips `.git`, `.venv`, `node_modules`, and root `runs/`. It can
not tell which process changed a file, so it stops for your review whenever it can not explain a
change.

## Status and recovery

Run `status` to see where a run stands without changing it:

```bash
echo '{"repo_root": "."}' | uv run python -m harness.execute status
```

The report shows the stage, the pending action, the approvals, whether the baseline exists, each
changed-file record with its blocking paths, and the next step. Without a `task_id`, it uses the
active run, then the newest run.

| Situation                                     | What to do                                                                                                                  |
| --------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| The process stopped or you restarted the tool | Run `resume`. It selects the run, finishes any interrupted tests, and returns the pending action again.                     |
| An agent stopped without a result             | Run `next-action`. The same action returns until its result is accepted.                                                    |
| The run is `blocked`                          | Read the reason in `status`. It names the exact paths. Fix the cause, then start a new run. A blocked run does not restart. |
| `resume` says the file baseline is missing    | The run can not compare its changes any more. Start a new run.                                                              |
| `submit` is refused                           | The result does not match the pending action. Nothing changed. Fix the result and submit it again.                          |
| A role agent's edit is denied                 | The edit is outside the action's scopes, or the run is waiting for a person. Read the reason, then run `status`.            |

## Where things live

| Path                          | Purpose                                                                  |
| ----------------------------- | ------------------------------------------------------------------------ |
| `harness/`                    | The supervisor, contracts, gates, hooks, and checks.                     |
| `harness/contracts/checks.py` | The mandatory checks, allowed check names, commands, limits, and scopes. |
| `tests/harness/`              | Tests for the harness. They run with `pnpm harness:test`.                |
| `.agents/skills/`             | The `harness-run` skill and the role skills.                             |
| `.agents/agents/`             | The role personas.                                                       |
| `.claude/agents/`             | Links that let Claude Code find each role persona as an agent.           |
| `.claude/settings.json`       | The Claude Code edit hook.                                               |
| `.claude/skills/`             | Links that let Claude Code find `harness-run` and each role skill.       |
| `.codex/hooks.json`           | The Codex edit hook.                                                     |
| `planning/<task-id>/`         | The spec and plan for review.                                            |
| `runs/<task-id>/`             | Run state, reviews, test results, and changed-file records.              |
