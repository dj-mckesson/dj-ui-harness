# Coding harness

The v1 harness runs a coding task through a fixed workflow. A person approves the spec and the
plan. An agent writes the code. A second agent reviews it. The harness runs the tests and compares
the real file changes with the approved plan.

The harness works with any stack: a web UI, a Python service, or a full-stack repository with
both. Everything stack-specific is in one file, `harness.toml`, at the repository root. Once
installed, the harness's code, tests, skills, and tool hooks live in the project repository and are
reviewed with the application.

## What you need

- macOS or Linux. The harness is tested on macOS.
- Python 3.12 and [uv](https://docs.astral.sh/uv/). The harness is Python code with its own
  environment in `.harness/`. It never uses or changes the project's own Python environment,
  `pyproject.toml`, or ruff and pytest settings.
- The project's own tools, such as pnpm, npm, or uv, for the commands in `harness.toml`.
- Codex or Claude Code. The same workflow runs in both.

## Install

From a checkout of dj-ui-harness, install it into the project:

```bash
uv sync --project .harness
uv run --project .harness --no-sync python -m harness.install ../my-project --preset auto
```

`--preset auto` picks a preset from the project's files. You can also name one:

| Preset           | For                                                                          |
| ---------------- | ---------------------------------------------------------------------------- |
| `nextjs-pnpm`    | Next.js App Router with pnpm, Vitest, ESLint, and Playwright.                |
| `vite-react-npm` | Vite and React with npm, Vitest, ESLint, and Playwright.                     |
| `python-uv`      | A Python project on uv with pytest, ruff, and pyright.                       |
| `fastapi-react`  | FastAPI in `backend/` (uv, pytest) and React in `frontend/` (pnpm, Vitest).  |

The install copies the harness files, writes `harness.toml` from the preset, merges the edit hooks
into any existing `.claude/settings.json` and `.codex/hooks.json`, and adds the `## Harness`
section to `AGENTS.md` and the run folders to `.gitignore`. It keeps an existing `harness.toml`
and `playwright.config.ts` unless you pass `--force`. Run it again to update the harness. Then, in
the project:

```bash
uv sync --project .harness
uv run --project .harness --no-sync python -m harness.doctor
```

`doctor` checks `harness.toml`, that each command's program and folder exist, the context and
guidance paths, the browser check files, the hooks, and the skill links. Fix every `fail` before
the first run.

If `uv` fails with `invalid peer certificate: UnknownIssuer`, your network re-signs HTTPS traffic.
Tell `uv` to use the system certificates:

```bash
UV_SYSTEM_CERTS=1 uv sync --project .harness
```

## `harness.toml`

The presets are commented starting points. Edit the copy in the project until `doctor` passes and
the commands match how the team runs its tests.

```toml
context = ["src"]          # what the spec and plan agents read by default
guidance = ["AGENTS.md"]   # the coding and design rules every role agent follows

[[tests]]                  # every candidate must pass these, in order
name = "pytest"
command = ["uv", "run", "--no-sync", "pytest", "-q"]
cwd = "."                  # optional: run in a subfolder of a monorepo
timeout = 600

[checks.ruff-lint]
command = ["uv", "run", "--no-sync", "ruff", "check"]
files = ["*.py"]           # runs on the changed files that match; omit to run once
mandatory = true           # runs for every candidate

[checks.red-tests]
kind = "red-tests"         # the changed tests must fail on a clean HEAD
command = ["uv", "run", "--frozen", "pytest", "-q"]
files = ["test_*.py"]
separator = ""             # pass paths with no "--" before them
failure_pattern = '\d+ (failed|errors?)\b'

[task_types]               # required checks each task type adds
feature = ["red-tests"]
bug-fix = ["red-tests"]

[protected]                # added to the harness's own protected paths
files = ["pyproject.toml", "uv.lock"]
dirs = ["migrations"]

[changes]                  # added to the outputs a filesystem scan ignores
output_dirs = ["dist"]
```

- A pattern without `/` matches the file name at any depth. A pattern with `/` matches the whole
  path: `*` stays inside one folder and `**` crosses folders, as in `backend/**/*.py`.
- A check with `cwd` receives only the changed paths inside that folder, relative to it.
- A check's `env` table sets environment variables for its command. The browser checks use it to
  say how to start the app.
- The task types are fixed: `feature`, `bug-fix`, `ui-change`, `refactor`, `docs`, and `other`.

## Run a task

Ask your coding tool to run the `harness-run` skill with your request. The skill drives the
workflow in a loop. It calls the repo-local command below, from the repository root:

```bash
uv run --project .harness --no-sync python -m harness.execute <operation> [payload-file|-]
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

Run `uv run --project .harness --no-sync python -m harness.execute --help` to list the operations.

`start` needs `uv` on `PATH` and an existing repository root. The default spec-author and planner
read context is `context` in `harness.toml`. The optional `context_paths` changes only the first
spec-author action's read context.

## The workflow

1. **Spec.** An agent writes `planning/<task-id>/SPEC.md`. It may ask you questions first. You
   approve or reject the spec.
2. **Plan.** An agent writes `planning/<task-id>/plan.json`. The harness renders `PLAN.md` for you
   to read. You approve or reject the plan. At approval, the harness saves an exact copy as
   `runs/<task-id>/changes/approved-plan.json`. The rest of the run uses only this copy.
3. **Implementation.** A maker agent changes the files that the plan names. The harness compares
   the real changes with the plan.
4. **Review.** A separate checker agent reviews the candidate. It can not edit files.
5. **Tests and checks.** The harness runs every `[[tests]]` command in `harness.toml`, in order,
   then the mandatory checks, the checks the task type adds, and any check that the approved plan
   selected (see [Checks](#checks)). When the files that the round checks hold a protected file,
   the harness also runs its own tests. Round 1 checks the files after the candidate. A later
   round checks the files after a change during an approval pause.

6. **Repair.** If the review, the tests, or a required check fail, the maker gets one repair. After the repair, the
   harness completes or blocks the run. It never starts a third candidate.
7. **Completion.** The harness compares the file changes again. If protected files changed, you
   approve their exact paths. After your approval, the harness scans the files again. If a file
   changed during the pause, the harness runs the tests and checks again on the current files,
   and then asks for your approval again. The run then completes and reports how each acceptance
   criterion was verified (see [The result](#the-result)).

Only the harness chooses the next step. The skill and the agents never decide it.

## Project guidance

The `guidance` list in `harness.toml` names the project's instruction files, such as `AGENTS.md`,
a rules file, or a design standard for UI work. Each role reads them, and `harness.toml`, before it
starts. `CLAUDE.md` points Claude Code to `AGENTS.md`. Action `required_input_paths` list task
files and context paths, without a separate guidance prefix.

The harness adds no coding standard of its own. It enforces the standards in these files.

## Checks

`harness.toml` defines every check. The harness owns all commands: a plan names checks, never a
command. A check with `mandatory = true` always runs, `[task_types]` adds required checks for the
plan's `task_type`, and a plan can select any other check by name. This is what the presets set:

| Check                    | Presets                  | What it runs                                                              |
| ------------------------ | ------------------------ | ------------------------------------------------------------------------- |
| `ruff-lint`              | all                      | Mandatory. `ruff check` on the changed Python files.                      |
| `eslint`                 | web, full stack          | Mandatory. `eslint` on the changed JavaScript and TypeScript files.       |
| `typecheck`, `typecheck-*` | all                    | Mandatory. `tsc --noEmit` or `pyright` on the whole project.              |
| `ruff-format`, `prettier`| all                      | Optional format checks on the same files.                                 |
| `red-tests`, `red-tests-*` | all                    | The changed test files on a clean `HEAD`. Passes only if they fail there. |
| `browser`                | web, full stack          | Starts the app and loads each route in Chromium. Fails on any error.      |
| `a11y`                   | web, full stack          | An axe WCAG 2.2 AA scan of each route. Fails on a serious violation.      |

| `task_type`                 | Required checks it adds in the web presets | In `python-uv`  |
| --------------------------- | ------------------------------------------ | --------------- |
| `feature`                   | `red-tests`, `browser`                     | `red-tests`     |
| `bug-fix`                   | `red-tests`                                | `red-tests`     |
| `ui-change`                 | `browser`, `a11y`                          | None            |
| `refactor`, `docs`, `other` | None. `other` is the default.              | None            |

A `red-tests` check fails when no matching test file changed, and when the baseline run breaks
before its output matches `failure_pattern`, so a broken tool never counts as a failing test. The
clean checkout links the folders in `link`, such as `node_modules`. With several `red-tests`
checks, as in `fastapi-react`, one changed test on either side is enough, and the other side
records `skipped`.

`browser` and `a11y` use the `smoke` and `a11y` projects in `playwright.config.ts`, with the specs
in `e2e/harness/`. The check's `env` tells Playwright how to start the app:

| Variable         | Meaning                                                                       |
| ---------------- | ----------------------------------------------------------------------------- |
| `E2E_WEB_SERVER` | Shell command that serves the built app. `{port}` becomes the port.           |
| `E2E_PORT`       | Port for the app. Default 3100.                                               |
| `E2E_BASE_URL`   | Use an app that is already running instead of starting one.                   |
| `E2E_API_SERVER` | Also start a backend with this command, and wait for `E2E_API_URL`.           |
| `E2E_ROUTES`     | Comma-separated routes to visit.                                              |

The routes come from `E2E_ROUTES`, then `e2e/harness/routes.json` (a JSON array), then every static
page of a Next.js App Router app, and otherwise `/` alone. List the routes in `routes.json` for any
router that is not file-based. Mark an element `data-a11y-ignore` only when WCAG exempts it, such
as a logo wordmark.

Each check has a time limit, runs in its `cwd` (the repository root by default), and saves bounded
output. Plans mark each selected check `"required": true` (the default) or `"required": false`.

- A changed-file check with no matching added or changed file records `skipped`. A skipped
  required check satisfies validation. A check without `files` still runs on the whole project.
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
- Protected files need a named `file` scope. A folder scope never covers them. The harness always
  protects `harness/`, `tests/harness/`, `e2e/harness/`, `.harness/`, `.agents/`, `.claude/`,
  `.codex/`, `.github/`, `.husky/`, `harness.toml`, and every `conftest.py`, `CLAUDE.md`, or
  `AGENTS.md`. The `[protected]` table in `harness.toml` adds the project's tool configs and
  lockfiles, such as `package.json` or `pyproject.toml`.
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
`runs/`. The filesystem fallback skips `.git`, `.venv`, `venv`, `node_modules`, root `runs/`, and
any `ignore_dirs` in `[changes]`. It can
not tell which process changed a file, so it stops for your review whenever it can not explain a
change.

## Status and recovery

Run `status` to see where a run stands without changing it:

```bash
echo '{"repo_root": "."}' | uv run --project .harness --no-sync python -m harness.execute status
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
| `harness.toml`                | The project's tests, checks, task types, and protected files.            |
| `harness/presets/`            | Starting `harness.toml` files for common stacks.                         |
| `harness/config.py`           | Reads and checks `harness.toml`.                                         |
| `.harness/`                   | The harness's own Python environment (`pyproject.toml`, `uv.lock`).      |
| `tests/harness/`              | Tests for the harness. See the `AGENTS.md` Harness section.              |
| `.agents/skills/`             | The `harness-run` skill and the role skills.                             |
| `.agents/agents/`             | The role personas.                                                       |
| `.claude/agents/`             | Links that let Claude Code find each role persona as an agent.           |
| `.claude/settings.json`       | The Claude Code edit hook.                                               |
| `.claude/skills/`             | Links that let Claude Code find `harness-run` and each role skill.       |
| `.codex/hooks.json`           | The Codex edit hook.                                                     |
| `planning/<task-id>/`         | The spec and plan for review.                                            |
| `runs/<task-id>/`             | Run state, reviews, test results, and changed-file records.              |
