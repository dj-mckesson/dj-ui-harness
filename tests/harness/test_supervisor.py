import json
from pathlib import Path

import pytest

import harness.execute.routing as routing_module
import harness.execute.supervisor as supervisor_module
from harness.contracts.actions import AgentAction, ApprovalAction, QuestionAction
from harness.contracts.results import BlockedOutput, CompleteOutput
from harness.execute.state import (
    StateStorageError,
    load_active_task,
    load_state,
    save_active_task,
)
from harness.execute.status import resume_run, run_status
from harness.execute.supervisor import (
    SupervisorError,
    create_run,
    next_action,
    submit_result,
)
from harness.utils.digests import sha256_bytes
from tests.harness.support import (
    SPEC_TEXT,
    _approval_response,
    _author_result,
    _blocked_reason,
    _checker_result,
    _edit_spec,
    _patch_validation,
    _reach_checker,
    _reach_harness_approval,
    _reach_maker,
    _reach_plan_approval,
    _reach_spec_approval,
    _repo,
    _submit_maker,
    _validation_record,
    plan_data,
)


@pytest.mark.parametrize("artifact", ["spec", "plan"])
def test_question_and_rejection_loops_stay_in_authoring_stages(
    tmp_path: Path, artifact: str
) -> None:
    repo = _repo(tmp_path)
    if artifact == "spec":
        author = create_run(repo, "Add one behavior.").pending_action
    else:
        spec_approval = _reach_spec_approval(repo)
        author = submit_result(repo, "01", _approval_response(spec_approval, True))
    assert isinstance(author, AgentAction)

    question_text = "Should invalid input return an error?"
    question = submit_result(
        repo,
        "01",
        _author_result(author, status="questions", questions=[question_text]),
    )
    assert isinstance(question, QuestionAction)
    assert load_state(repo, "01").stage == f"{artifact}-authoring"
    assert load_active_task(repo) == "01"

    author = submit_result(
        repo,
        "01",
        {
            "kind": "question-response",
            "task_id": "01",
            "action_id": question.action_id,
            "artifact": artifact,
            "answers": [{"question": question_text, "answer": "Yes."}],
        },
    )
    assert isinstance(author, AgentAction)
    assert len(author.context.resolved_answers) == 1  # type: ignore[union-attr]

    draft = repo / "planning" / "01" / ("SPEC.md" if artifact == "spec" else "plan.json")
    draft.write_text(
        SPEC_TEXT if artifact == "spec" else json.dumps(plan_data()), encoding="utf-8"
    )
    approval = submit_result(
        repo,
        "01",
        _author_result(
            author, status="ready_for_approval", files_written=list(author.writable_paths)
        ),
    )
    assert isinstance(approval, ApprovalAction)
    feedback = "Clarify the error response."
    author = submit_result(repo, "01", _approval_response(approval, False, feedback))
    assert isinstance(author, AgentAction)
    assert author.role == ("spec-author" if artifact == "spec" else "planner")
    state = load_state(repo, "01")
    assert state.stage == f"{artifact}-authoring"
    assert not (state.spec_approved if artifact == "spec" else state.plan_approved)
    assert author.context.rejection_feedback == feedback  # type: ignore[union-attr]

    second_approval = submit_result(
        repo,
        "01",
        _author_result(
            author, status="ready_for_approval", files_written=list(author.writable_paths)
        ),
    )
    assert isinstance(second_approval, ApprovalAction)
    assert second_approval.revision == 2
    state = load_state(repo, "01")
    assert (state.spec_revision if artifact == "spec" else state.plan_revision) == 2
    assert not state.plan_approved


def test_author_cannot_repeat_an_already_resolved_question(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    author = create_run(repo, "Add one behavior.").pending_action
    assert isinstance(author, AgentAction)
    question_text = "Should invalid input return an error?"
    question = submit_result(
        repo,
        "01",
        _author_result(
            author,
            status="questions",
            questions=[question_text],
        ),
    )
    assert isinstance(question, QuestionAction)
    author = submit_result(
        repo,
        "01",
        {
            "kind": "question-response",
            "task_id": "01",
            "action_id": question.action_id,
            "artifact": "spec",
            "answers": [{"question": question_text, "answer": "Yes."}],
        },
    )
    assert isinstance(author, AgentAction)
    state_path = repo / "runs" / "01" / "state.json"
    before = state_path.read_bytes()

    with pytest.raises(SupervisorError, match="already-resolved"):
        submit_result(
            repo,
            "01",
            _author_result(
                author,
                status="questions",
                questions=[question_text],
            ),
        )

    assert state_path.read_bytes() == before
    saved = load_state(repo, "01")
    answers = saved.resolved_spec_answers
    assert tuple((answer.question, answer.answer) for answer in answers) == (
        (question_text, "Yes."),
    )


def test_stale_approval_revision_does_not_change_state(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    approval = _reach_spec_approval(repo)
    before = (repo / "runs" / "01" / "state.json").read_bytes()
    response = _approval_response(approval, True)
    response["revision"] = 2

    with pytest.raises(SupervisorError, match="revision"):
        submit_result(repo, "01", response)

    assert (repo / "runs" / "01" / "state.json").read_bytes() == before


def test_spec_edited_while_approval_is_pending_returns_a_new_spec_approval(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    approval = _reach_spec_approval(repo)
    _edit_spec(repo)

    output = submit_result(repo, "01", _approval_response(approval, True))

    assert isinstance(output, ApprovalAction)
    assert output.artifact == "spec" and output.revision == 2
    assert output.notice is not None and "planning/01/SPEC.md" in output.notice
    state = load_state(repo, "01")
    assert state.spec_revision == 2
    assert not state.spec_approved and state.spec_digest is None

    planner = submit_result(repo, "01", _approval_response(output, True))
    assert isinstance(planner, AgentAction) and planner.role == "planner"
    assert (
        load_state(repo, "01").spec_digest == output.review_digests["planning/01/SPEC.md"]
    )


@pytest.mark.parametrize("edited", ["plan.json", "PLAN.md"])
def test_plan_edited_while_approval_is_pending_returns_a_new_plan_approval(
    tmp_path: Path, edited: str
) -> None:
    repo = _repo(tmp_path)
    approval = _reach_plan_approval(repo)
    planning = repo / "planning" / "01"
    if edited == "plan.json":
        plan = plan_data()
        plan["steps"][0]["intent"] = "A changed intent."  # type: ignore[index]
        (planning / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    else:
        (planning / "PLAN.md").write_text("# Edited by hand\n", encoding="utf-8")

    output = submit_result(repo, "01", _approval_response(approval, True))

    assert isinstance(output, ApprovalAction)
    assert output.artifact == "plan" and output.revision == 2
    assert output.notice is not None and f"planning/01/{edited}" in output.notice
    rendered = (planning / "PLAN.md").read_text(encoding="utf-8")
    assert rendered.startswith("# ") and "Edited by hand" not in rendered
    if edited == "plan.json":
        assert "A changed intent." in rendered
    assert not (repo / "runs" / "01" / "changes" / "approved-plan.json").exists()
    assert not load_state(repo, "01").plan_approved

    maker = submit_result(repo, "01", _approval_response(output, True))
    assert isinstance(maker, AgentAction) and maker.role == "maker"


def test_spec_edited_after_its_approval_returns_to_spec_approval_at_plan_approval(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    approval = _reach_plan_approval(repo)
    _edit_spec(repo)

    output = submit_result(repo, "01", _approval_response(approval, True))

    assert isinstance(output, ApprovalAction)
    assert output.artifact == "spec" and output.revision == 2
    assert output.notice is not None and "planning/01/SPEC.md" in output.notice
    state = load_state(repo, "01")
    assert state.stage == "spec-authoring"
    assert not state.spec_approved and not state.plan_approved
    assert state.spec_digest is None
    assert not (repo / "runs" / "01" / "changes" / "approved-plan.json").exists()

    planner = submit_result(repo, "01", _approval_response(output, True))
    assert isinstance(planner, AgentAction) and planner.role == "planner"


def test_an_invalid_edited_draft_keeps_the_approval_pending_and_can_be_rejected(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    approval = _reach_plan_approval(repo)
    (repo / "planning" / "01" / "plan.json").write_text("{not JSON", encoding="utf-8")
    state_path = repo / "runs" / "01" / "state.json"
    before = state_path.read_bytes()

    with pytest.raises(SupervisorError, match="Restore the file") as error:
        submit_result(repo, "01", _approval_response(approval, True))

    assert "planning/01/plan.json changed" in str(error.value)
    assert state_path.read_bytes() == before
    assert next_action(repo, "01") == approval

    planner = submit_result(
        repo, "01", _approval_response(approval, False, "Fix the plan.")
    )
    assert isinstance(planner, AgentAction) and planner.role == "planner"


def test_an_invalid_spec_at_plan_approval_asks_to_restore_the_spec(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    approval = _reach_plan_approval(repo)
    (repo / "planning" / "01" / "SPEC.md").write_text(
        SPEC_TEXT.split("## Requirements")[0], encoding="utf-8"
    )
    state_path = repo / "runs" / "01" / "state.json"
    before = state_path.read_bytes()

    with pytest.raises(SupervisorError) as error:
        submit_result(repo, "01", _approval_response(approval, True))

    message = str(error.value)
    assert message.endswith("Restore planning/01/SPEC.md to the approved content.")
    assert "reject" not in message
    assert state_path.read_bytes() == before


def test_a_rejection_ignores_changed_spec_content(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    approval = _reach_spec_approval(repo)
    _edit_spec(repo)

    author = submit_result(repo, "01", _approval_response(approval, False, "Say more."))

    assert isinstance(author, AgentAction) and author.role == "spec-author"
    state = load_state(repo, "01")
    assert state.spec_revision == 1 and state.spec_digest is None


def test_stale_wrong_type_and_out_of_scope_results_do_not_change_state(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    state = create_run(repo, "Add one behavior.")
    action = state.pending_action
    assert isinstance(action, AgentAction)
    before = (repo / "runs" / "01" / "state.json").read_bytes()

    bad_results = [
        _author_result(
            action.model_copy(update={"action_id": "b" * 32}),
            status="questions",
            questions=["A question?"],
        ),
        {
            "kind": "approval-response",
            "task_id": "01",
            "action_id": action.action_id,
            "artifact": "spec",
            "revision": 1,
            "approved": True,
            "feedback": None,
        },
    ]
    for result in bad_results:
        with pytest.raises(SupervisorError):
            submit_result(repo, "01", result)
        assert (repo / "runs" / "01" / "state.json").read_bytes() == before

    (repo / "planning" / "01" / "SPEC.md").write_text(SPEC_TEXT, encoding="utf-8")
    with pytest.raises(SupervisorError, match="exact"):
        submit_result(
            repo,
            "01",
            _author_result(
                action,
                status="ready_for_approval",
                files_written=["src/feature.py"],
            ),
        )
    assert (repo / "runs" / "01" / "state.json").read_bytes() == before


def test_happy_path_saves_checker_and_validation_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    calls = _patch_validation(monkeypatch, {1: _validation_record(1, "pass")})
    maker = _reach_maker(repo)
    (repo / "src" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
    checker = submit_result(
        repo,
        "01",
        {
            "task_id": "01",
            "action_id": maker.action_id,
            "status": "implemented",
            "files_changed": ["src/feature.py"],
            "summary": "Implemented the approved behavior.",
            "blockers": [],
        },
    )
    assert isinstance(checker, AgentAction) and checker.role == "checker"
    assert load_active_task(repo) == "01"

    cleared_stages: list[str] = []
    original_clear = supervisor_module.clear_active_task

    def record_clear(repo_root: Path, task_id: str) -> None:
        cleared_stages.append(load_state(repo_root, task_id).stage)
        original_clear(repo_root, task_id)

    monkeypatch.setattr(supervisor_module, "clear_active_task", record_clear)
    save_active_task(repo, "02")

    output = submit_result(repo, "01", _checker_result(checker))

    assert isinstance(output, CompleteOutput)
    assert calls == [(1, 1, False)]
    assert load_state(repo, "01").stage == "complete"
    assert {(c.method, c.result) for c in output.evidence.conditions} == {
        ("tests", "pass")
    }
    assert [(c.name, c.result) for c in output.evidence.checks] == [
        ("ruff-lint", "skipped"),
        ("eslint", "skipped"),
        ("typecheck", "pass"),
    ]
    assert (repo / "runs" / "01" / "reviews" / "checker-1.json").is_file()
    assert (repo / "runs" / "01" / "validation" / "results.json").is_file()
    changes_path = repo / "runs" / "01" / "changes" / "checks-1.json"
    changes = json.loads(changes_path.read_text(encoding="utf-8"))
    assert changes["status"] == "pass"
    assert changes["changes"] == {
        "added": [],
        "changed": ["src/feature.py"],
        "deleted": [],
    }
    assert output.evidence.changes_evidence_path == "runs/01/changes/checks-1.json"
    assert cleared_stages == ["complete"]
    assert load_active_task(repo) == "02"
    assert next_action(repo, "01") == output
    assert load_active_task(repo) == "02"


def test_checker_result_replay_after_state_save_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    calls = _patch_validation(monkeypatch, {1: _validation_record(1, "pass")})
    checker = _reach_checker(repo, ["src/feature.py"])
    state_path = repo / "runs" / "01" / "state.json"
    review_path = repo / "runs" / "01" / "reviews" / "checker-1.json"
    review_state = state_path.read_bytes()
    original_save_state = supervisor_module.save_state

    def interrupt_state_save(_repo_root: Path, _state: object) -> None:
        raise RuntimeError("simulated interruption before validation state save")

    monkeypatch.setattr(supervisor_module, "save_state", interrupt_state_save)
    passing_result = _checker_result(checker)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        submit_result(repo, "01", passing_result)

    assert state_path.read_bytes() == review_state
    assert load_state(repo, "01").pending_action == checker
    original_evidence = review_path.read_bytes()

    monkeypatch.setattr(supervisor_module, "save_state", original_save_state)
    conflicting_result = _checker_result(checker, "revise")
    with pytest.raises(
        ValueError,
        match="already been recorded with different content",
    ):
        submit_result(repo, "01", conflicting_result)

    assert state_path.read_bytes() == review_state
    assert review_path.read_bytes() == original_evidence

    output = submit_result(repo, "01", passing_result)

    assert isinstance(output, CompleteOutput)
    assert calls == [(1, 1, False)]
    assert load_state(repo, "01").stage == "complete"
    assert review_path.read_bytes() == original_evidence


@pytest.mark.parametrize(
    ("verdict", "validation"),
    [
        ("revise", "pass"),
        ("pass", "fail"),
    ],
)
def test_candidate_one_checker_or_test_failure_creates_one_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verdict: str,
    validation: str,
) -> None:
    repo = _repo(tmp_path)
    _patch_validation(
        monkeypatch,
        {1: _validation_record(1, validation)},
    )
    checker = _reach_checker(repo)

    repair = submit_result(repo, "01", _checker_result(checker, verdict))

    assert isinstance(repair, AgentAction)
    assert repair.role == "repair" and repair.candidate_attempt == 2
    assert load_state(repo, "01").stage == "repair"


@pytest.mark.parametrize(
    ("candidate_two_verdict", "candidate_two_validation", "expected_type"),
    [
        ("pass", "pass", CompleteOutput),
        ("revise", "pass", BlockedOutput),
        ("pass", "fail", BlockedOutput),
    ],
)
def test_candidate_two_completes_or_blocks_without_a_third_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate_two_verdict: str,
    candidate_two_validation: str,
    expected_type: type[CompleteOutput] | type[BlockedOutput],
) -> None:
    repo = _repo(tmp_path)
    _patch_validation(
        monkeypatch,
        {
            1: _validation_record(1, "fail"),
            2: _validation_record(2, candidate_two_validation),
        },
    )
    checker_one = _reach_checker(repo)
    repair = submit_result(repo, "01", _checker_result(checker_one))
    assert isinstance(repair, AgentAction)
    checker_two = submit_result(
        repo,
        "01",
        {
            "task_id": "01",
            "action_id": repair.action_id,
            "status": "implemented",
            "files_changed": [],
            "summary": "Prepared candidate two.",
            "blockers": [],
        },
    )
    assert isinstance(checker_two, AgentAction)

    output = submit_result(
        repo,
        "01",
        _checker_result(checker_two, candidate_two_verdict),
    )

    assert isinstance(output, expected_type)
    assert load_state(repo, "01").candidate_attempt == 2
    assert load_active_task(repo) is None
    assert next_action(repo, "01") == output


@pytest.mark.parametrize("failure", ["checker", "launch"])
def test_checker_blocker_and_validation_launch_failure_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    repo = _repo(tmp_path)
    calls = _patch_validation(
        monkeypatch,
        {1: _validation_record(1, "fail", launch_error="pytest was not found")},
    )
    checker = _reach_checker(repo)
    if failure == "checker":
        result = {
            "task_id": "01",
            "action_id": checker.action_id,
            "result": {
                "status": "blocked",
                "verdict": None,
                "findings": [],
                "summary": "",
                "blockers": ["Candidate files could not be read."],
            },
        }
        expected = "Checker gate failed"
    else:
        result = _checker_result(checker)
        expected = "could not start"

    blocked = submit_result(repo, "01", result)

    assert isinstance(blocked, BlockedOutput)
    assert expected in blocked.reason
    assert calls == ([] if failure == "checker" else [(1, 1, False)])
    assert load_active_task(repo) is None


def test_next_action_reuses_saved_validation_without_running_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    calls = _patch_validation(monkeypatch, {1: _validation_record(1, "pass")})
    checker = _reach_checker(repo)

    original_continue = supervisor_module.continue_validation
    monkeypatch.setattr(
        supervisor_module, "continue_validation", lambda _repo, state: state
    )
    with pytest.raises(SupervisorError, match="no action"):
        submit_result(repo, "01", _checker_result(checker))
    monkeypatch.setattr(supervisor_module, "continue_validation", original_continue)
    assert load_state(repo, "01").stage == "validation"
    record = _validation_record(1, "pass")
    path = repo / "runs" / "01" / "validation" / "results.json"
    path.write_text(
        json.dumps({"task_id": "01", "runs": [record.model_dump(mode="json")]}),
        encoding="utf-8",
    )

    output = next_action(repo, "01")

    assert isinstance(output, CompleteOutput)
    assert calls == []


def test_stale_validation_history_blocks_without_running_tests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    calls = _patch_validation(monkeypatch, {1: _validation_record(1, "pass")})
    checker = _reach_checker(repo)

    original_continue = supervisor_module.continue_validation

    def simulate_crash(_repo: Path, _state: object) -> object:
        """Stop after the supervisor saves its internal validation stage."""

        raise RuntimeError("simulated process crash")

    monkeypatch.setattr(supervisor_module, "continue_validation", simulate_crash)
    with pytest.raises(RuntimeError, match="simulated"):
        submit_result(repo, "01", _checker_result(checker))
    monkeypatch.setattr(supervisor_module, "continue_validation", original_continue)
    assert load_state(repo, "01").stage == "validation"

    records = [_validation_record(1, "pass"), _validation_record(2, "pass")]
    path = repo / "runs" / "01" / "validation" / "results.json"
    path.write_text(
        json.dumps(
            {
                "task_id": "01",
                "runs": [record.model_dump(mode="json") for record in records],
            }
        ),
        encoding="utf-8",
    )

    output = next_action(repo, "01")

    assert isinstance(output, BlockedOutput)
    assert "stale" in output.reason
    assert calls == []


def test_whole_repository_scope_needs_explicit_confirmation(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    approval = _reach_plan_approval(
        repo, [{"path": ".", "scope": "folder", "intent": "Change anything."}]
    )
    assert approval.whole_repository
    before = (repo / "runs" / "01" / "state.json").read_bytes()

    with pytest.raises(SupervisorError, match="confirm_whole_repository"):
        submit_result(repo, "01", _approval_response(approval, True))
    assert (repo / "runs" / "01" / "state.json").read_bytes() == before

    confirmed = {**_approval_response(approval, True), "confirm_whole_repository": True}
    maker = submit_result(repo, "01", confirmed)

    assert isinstance(maker, AgentAction)
    assert maker.writable_folders == (".",)


def test_shell_write_outside_the_scope_blocks_with_the_exact_path(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    maker = _reach_maker(repo)
    (repo / "src" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
    (repo / "README.md").write_text("written by a shell command\n", encoding="utf-8")

    output = _submit_maker(repo, maker, ["src/feature.py"])

    reason = _blocked_reason(output)
    assert "outside the approved scopes: README.md" in reason
    assert load_state(repo, "01").stage == "blocked"


def test_a_plan_symlinked_before_approval_is_not_read_or_saved(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    approval = _reach_plan_approval(repo)
    plan_path = repo / "planning" / "01" / "plan.json"
    target = tmp_path.parent / f"{tmp_path.name}-plan.json"
    target.write_bytes(plan_path.read_bytes())
    plan_path.unlink()
    plan_path.symlink_to(target)
    state_before = (repo / "runs" / "01" / "state.json").read_bytes()

    with pytest.raises(SupervisorError) as error:
        submit_result(repo, "01", _approval_response(approval, True))

    assert "plan artifact is invalid: canonical plan must not traverse a symlink" in str(
        error.value
    )
    assert not (repo / "runs" / "01" / "changes" / "approved-plan.json").exists()
    assert (repo / "runs" / "01" / "state.json").read_bytes() == state_before


def test_another_process_changing_a_file_during_the_checks_blocks_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    maker = _reach_maker(repo)
    (repo / "src" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
    checker = _submit_maker(repo, maker, ["src/feature.py"])
    assert isinstance(checker, AgentAction)
    records = {1: _validation_record(1, "pass")}
    _patch_validation(monkeypatch, records)
    real_run = routing_module.run_tests

    def run_then_interfere(repo_root: Path, *args: object, **kwargs: object):
        record = real_run(repo_root, *args, **kwargs)
        (repo_root / "src" / "feature.py").write_text("VALUE = 3\n", encoding="utf-8")
        return record

    monkeypatch.setattr(routing_module, "run_tests", run_then_interfere)

    reason = _blocked_reason(submit_result(repo, "01", _checker_result(checker)))

    assert "differ from the validated state after the checks: src/feature.py" in reason


def test_a_missing_baseline_blocks_because_file_state_cannot_be_checked(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    maker = _reach_maker(repo)
    (repo / "runs" / "01" / "changes" / "baseline.json").unlink()

    reason = _blocked_reason(_submit_maker(repo, maker, []))

    assert "could not be checked" in reason


def test_changed_harness_files_pause_for_human_approval_of_their_exact_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    path = "harness/gates.py"

    pause = _reach_harness_approval(repo, monkeypatch, path)

    assert pause.artifact == "changes"
    assert pause.harness_paths == (path,)
    assert pause.review_paths == ("runs/01/changes/checks-1.json",)
    assert pause.harness_intents == {path: "Adjust a gate."}
    assert load_state(repo, "01").stage == "change-approval"
    assert next_action(repo, "01") == pause

    output = submit_result(repo, "01", _approval_response(pause, True))

    assert isinstance(output, CompleteOutput)
    assert output.evidence.changes_evidence_path == "runs/01/changes/checks-1.json"


def test_rejecting_changed_harness_files_blocks_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    pause = _reach_harness_approval(repo, monkeypatch)

    output = submit_result(
        repo, "01", _approval_response(pause, False, "The gate change is not needed.")
    )

    assert "not approved" in _blocked_reason(output)
    assert "The gate change is not needed." in _blocked_reason(output)


@pytest.mark.parametrize(
    ("path", "text", "protected"),
    [
        ("src/feature.py", "VALUE = 2\n", ("harness/gates.py",)),
        ("harness/gates.py", "GATE = 1\n", ()),
    ],
)
def test_a_file_changed_during_the_change_approval_is_validated_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    text: str,
    protected: tuple[str, ...],
) -> None:
    repo = _repo(tmp_path)
    pause = _reach_harness_approval(repo, monkeypatch)
    calls = _patch_validation(monkeypatch, {1: _validation_record(1, "pass")})
    old_digest = sha256_bytes((repo / path).read_bytes())
    (repo / path).write_text(text, encoding="utf-8")

    again = submit_result(repo, "01", _approval_response(pause, True))

    assert isinstance(again, ApprovalAction) and again.artifact == "changes"
    assert again.harness_paths == protected
    assert [(d.path, d.old_digest, d.new_digest) for d in again.drift] == [
        (path, old_digest, sha256_bytes(text.encode("utf-8")))
    ]
    results = json.loads(
        (repo / "runs" / "01" / "validation" / "results.json").read_text(encoding="utf-8")
    )
    assert [run["validation_round"] for run in results["runs"]] == [1, 2]
    assert calls == [(1, 2, True)]
    output = submit_result(repo, "01", _approval_response(again, True))
    assert isinstance(output, CompleteOutput)


@pytest.mark.parametrize("attempt", [1, 2])
def test_a_change_that_fails_validation_again_repairs_or_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, attempt: int
) -> None:
    repo = _repo(tmp_path)
    pause = _reach_harness_approval(repo, monkeypatch, attempt=attempt)
    _patch_validation(monkeypatch, {attempt: _validation_record(attempt, "fail")})
    (repo / "src" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")

    output = submit_result(repo, "01", _approval_response(pause, True))

    if attempt == 1:
        assert isinstance(output, AgentAction) and output.role == "repair"
        state = load_state(repo, "01")
        assert (state.candidate_attempt, state.validation_round) == (2, 1)
        checker = _submit_maker(repo, output, [])
        assert isinstance(checker, AgentAction) and checker.role == "checker"
    else:
        assert "repair is exhausted" in _blocked_reason(output)


def test_a_missing_candidate_record_blocks_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    calls = _patch_validation(monkeypatch, {1: _validation_record(1, "pass")})
    checker = _reach_checker(repo)
    (repo / "runs" / "01" / "changes" / "candidate-1.json").unlink()

    reason = _blocked_reason(submit_result(repo, "01", _checker_result(checker)))

    assert "Validation evidence is invalid" in reason
    assert calls == []


def test_status_reports_the_stage_and_pending_action_without_changing_the_run(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    create_run(repo, "Add one behavior.")
    state_path = repo / "runs" / "01" / "state.json"
    before = state_path.read_bytes()

    report = run_status(repo)

    assert report.task_id == "01"
    assert report.stage == "spec-authoring"
    assert report.pending is not None
    assert (report.pending.kind, report.pending.role) == ("agent", "spec-author")
    assert not report.baseline_saved
    assert "spec-author" in report.next_step
    assert state_path.read_bytes() == before


def test_status_shows_the_baseline_and_blocking_change_records(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    maker = _reach_maker(repo)
    assert run_status(repo, "01").baseline_saved
    (repo / "README.md").write_text("shell write\n", encoding="utf-8")
    _submit_maker(repo, maker, [])

    report = run_status(repo, "01")

    assert report.stage == "blocked"
    assert report.blocked_reason is not None and "README.md" in report.blocked_reason
    assert "run is blocked" in report.next_step


def test_status_defaults_to_the_active_run_then_the_newest_run(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(StateStorageError, match="no saved runs"):
        run_status(repo)
    create_run(repo, "First.")
    create_run(repo, "Second.")
    save_active_task(repo, "01")

    assert run_status(repo).task_id == "01"
    (repo / "runs" / "active.json").unlink()
    assert run_status(repo).task_id == "02"


def test_resume_returns_the_same_pending_action_and_selects_the_run(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    maker = _reach_maker(repo)
    save_active_task(repo, "02")

    assert resume_run(repo, "01") == maker
    assert load_active_task(repo) == "01"
    assert resume_run(repo, "01") == maker


def test_resume_finishes_checks_that_a_stopped_process_left_behind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    calls = _patch_validation(monkeypatch, {1: _validation_record(1, "pass")})
    checker = _reach_checker(repo)
    original = supervisor_module.continue_validation

    def stop(*_args: object) -> None:
        raise RuntimeError("process stopped during the checks")

    monkeypatch.setattr(supervisor_module, "continue_validation", stop)
    with pytest.raises(RuntimeError, match="process stopped"):
        submit_result(repo, "01", _checker_result(checker))
    assert load_state(repo, "01").stage == "validation"
    assert "Run resume" in run_status(repo, "01").next_step
    assert calls == []

    monkeypatch.setattr(supervisor_module, "continue_validation", original)
    output = resume_run(repo)

    assert isinstance(output, CompleteOutput)
    assert calls == [(1, 1, False)]


def test_resume_waits_at_the_harness_change_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    pause = _reach_harness_approval(repo, monkeypatch)

    assert resume_run(repo, "01") == pause
    report = run_status(repo, "01")
    assert report.stage == "change-approval"
    assert report.pending is not None and report.pending.artifact == "changes"
    assert report.change_records[-1].harness_changes == ("harness/gates.py",)


def test_resume_refuses_a_run_that_lost_its_file_baseline(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _reach_maker(repo)
    (repo / "runs" / "01" / "changes" / "baseline.json").unlink()

    with pytest.raises(SupervisorError, match="file baseline is missing"):
        resume_run(repo, "01")
