# Harness backlog and known risks

This document lists open harness work and known risks. Each risk gives the problem, a failure case, and a fix.

## Backlog

- Add a `harness clear-active` CLI operation to remove a bad active-task marker.
- Cover `NotebookEdit` in the edit hook.
- Refactor `_handle_approval_response` separately.
- Reject a repeated question in one author result with `SupervisorError`.
  - Today `AuthorResult` accepts the same question twice. `build_question_action` then raises a raw pydantic `ValidationError`, because `QuestionAction` rejects duplicates.
  - Fix: reject duplicates in the `AuthorResult` contract, or wrap the error in `_handle_author_result`. The contract is the better place, because the result is then refused at validation.

## Risks

### Agent changes to supervisor evidence

- Harden supervisor evidence against agent changes through shell commands. The edit hook cannot see those writes, and the file scan excludes `runs/`. This includes `runs/<id>/changes/approved-plan.json`. For example, an agent could write `runs/<id>/validation/results.json` with a passing result. `_current_validation_record` can reuse a saved result without proof that the supervisor wrote it, so tests may not run.
- Fix: hash every file under `runs/<id>/` except `state.json` and `active.json` when the supervisor hands work to an agent. Save one digest in run state. Hash again before routing the agent result. The supervisor is idle during the agent turn, so a difference identifies a change during that turn.
- Open decision: an evidence digest mismatch can pause for a human or block the run. Other digest mismatches, such as a changed spec or plan, pause for a human. This case is not decided.
- This evidence digest cannot stop an agent that also rewrites the saved digest. Preventing that needs an operating-system write restriction on `runs/`, such as a sandbox rule. That is a separate decision.

### Approval digests

- Check the spec digest first at plan approval. Today the plan files are checked first.
  - If `SPEC.md` and `plan.json` both changed, the person reviews the plan again. Only the next approval goes back to spec approval. The plan review is wasted, because a spec change voids it.
  - If the spec change breaks the plan, the refusal names `PLAN.md` or `plan.json` as the changed file, not `SPEC.md`.
  - Fix: in the plan branch of `_handle_approval_response`, compare `SPEC.md` with `spec_digest` before `_changed_review_paths`. Add a test where both files changed.
- Bind `PLAN.md` to the same `plan.json` content as the approval digest.
  - `_request_plan_approval` validates `plan.json` and renders `PLAN.md`. Only after that does `build_approval_action` hash the two files.
  - If `plan.json` changes between the render and the hash, the approval binds the new `plan.json` with the old `PLAN.md`. Both digests match at approval, so the new plan becomes the approved plan. The person did not see it in `PLAN.md`. The list of planned files does show it.
  - The race is unlikely, but the comment in `build_approval_action` says the order closes it.
  - Fix: hash `plan.json` before validation and rendering, and hash `PLAN.md` after rendering. Pass the digests to the builder. A change between the steps then causes a new approval request.
- Runs saved before approval digests existed cannot load.
  - A saved spec or plan approval has no `review_digests`, and a saved approved spec has no `spec_digest`. `next_action` raises a raw `StateStorageError` validation message.
  - The same happens to a run after plan approval, although nothing reads `spec_digest` at that stage.
  - Fix: state in the harness recovery guidance that these runs must restart. Optionally make the load error say "restart the run".

### Drift validation

- Drift outside the approved scopes runs in the tests before the run blocks.
  - Round 1 checks scopes in `verify_candidate`, before any test runs. A later round checks scopes only in `verify_checks`, after the tests.
  - Example: during a change-approval pause, someone adds `tests/unit/conftest.py`, which the plan does not name. Round 2 runs the tests with that file loaded, so its code runs. Only then does the run block with "changes outside the approved scopes".
  - Fix: in `_handle_changes_approval`, after `find_drift`, check the drift paths against the approved plan with `scope_allows`. Block before the run goes back to validation.
- The repair agent does not see the drift.
  - `build_repair_action` gives the repair agent `checker-1.json` and `results.json` only.
  - Example: a person edits a file during the pause, and round 2 fails. The repair agent sees that round 2 failed, but it cannot tell which file changed or that the maker did not change it.
  - Fix: add `drift-1.json` to the repair inputs when it exists.
- The completion result does not show the drift.
  - `_completion_evidence` lists the checks record and `results.json` only.
  - Example: a run completes in round 2. The result does not say that files changed during a pause and a person approved them.
  - Fix: add the drift record path to the completion evidence when the round is greater than 1.

### Project settings

- A change to `harness.toml` applies to the run that made it.
  - The supervisor reads `harness.toml` on each call. `harness.toml` is protected, so a person approves the change, but the next call already uses the new checks.
  - Fix: save a digest of `harness.toml` at plan approval, and block the run when it changes before completion.
- A `red-tests` check can import the changed code instead of `HEAD`.
  - The clean checkout links the folders in `link`. A package that an editable install or a workspace link points at the main checkout loads from there.
  - The `python-uv` and `fastapi-react` presets avoid it for Python: `uv run --frozen` builds a fresh environment in the clean checkout. A pnpm or npm workspace with linked local packages still has the risk.
  - Fix: install the dependencies in the clean checkout, or link each workspace package to its clean copy.
- The presets assume a layout.
  - `fastapi-react` assumes `backend/` and `frontend/`, and `app/main.py` for the API server in the browser checks. `nextjs-pnpm` assumes pnpm.
  - `doctor` reports a missing folder or program, but it cannot tell that a command is the wrong one.
