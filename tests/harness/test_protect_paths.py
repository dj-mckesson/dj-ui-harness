import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from harness.contracts.actions import (
    AgentAction,
    ApprovalAction,
    CandidateContext,
    CheckerContext,
    PlannerContext,
    QuestionAction,
    SpecAuthorContext,
)
from harness.contracts.state import RunState
from harness.execute.state import StateStorageError, save_active_task
from harness.hooks.protect_paths import (
    evaluate_paths,
    load_active_run_state,
    load_run_state,
)

TASK_ID = "01"
ACTION_ID = "a" * 32


def _make_repo(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    return repo_root


def _agent_state(
    role: str,
    *,
    task_id: str = TASK_ID,
    writable_paths: tuple[str, ...] | None = None,
    writable_folders: tuple[str, ...] = (),
) -> RunState:
    common = {
        "kind": "agent",
        "action_id": ACTION_ID,
        "task_id": task_id,
        "required_input_paths": (),
    }
    if role == "spec-author":
        action = AgentAction(
            **common,
            role=role,
            persona_id="spec-author",
            skill_id="spec",
            candidate_attempt=None,
            writable_paths=(f"planning/{task_id}/SPEC.md",),
            context=SpecAuthorContext(original_request="Add due dates"),
        )
        return RunState(
            task_id=task_id,
            original_request="Add due dates",
            stage="spec-authoring",
            pending_action=action,
        )
    if role == "planner":
        action = AgentAction(
            **common,
            role=role,
            persona_id="planner",
            skill_id="planner",
            candidate_attempt=None,
            writable_paths=(f"planning/{task_id}/plan.json",),
            context=PlannerContext(
                spec_revision=1,
                permitted_work_paths=("src",),
            ),
        )
        return RunState(
            task_id=task_id,
            original_request="Add due dates",
            stage="plan-authoring",
            spec_revision=1,
            spec_approved=True,
            spec_digest="0" * 64,
            pending_action=action,
        )

    candidate_attempt = 1 if role in {"maker", "checker"} else 2
    if role == "checker":
        action = AgentAction(
            **common,
            role=role,
            persona_id="checker",
            skill_id="checker",
            candidate_attempt=candidate_attempt,
            writable_paths=(),
            context=CheckerContext(
                spec_revision=1,
                plan_revision=1,
                candidate_summary="Implemented due dates",
            ),
        )
        stage = "review"
    else:
        action = AgentAction(
            **common,
            role=role,
            persona_id="maker",
            skill_id="maker",
            candidate_attempt=candidate_attempt,
            writable_paths=(
                writable_paths
                if writable_paths is not None
                else (() if writable_folders else ("src/source.py",))
            ),
            writable_folders=writable_folders,
            context=CandidateContext(spec_revision=1, plan_revision=1),
        )
        stage = "implementation" if role == "maker" else "repair"
    return RunState(
        task_id=task_id,
        original_request="Add due dates",
        stage=stage,
        spec_revision=1,
        spec_approved=True,
        spec_digest="0" * 64,
        plan_revision=1,
        plan_approved=True,
        candidate_attempt=candidate_attempt,
        pending_action=action,
    )


@pytest.mark.parametrize(
    ("role", "target"),
    [
        ("spec-author", "planning/01/SPEC.md"),
        ("planner", "planning/01/plan.json"),
        ("maker", "src/source.py"),
        ("repair", "src/source.py"),
    ],
)
def test_each_writable_role_allows_its_exact_saved_path(
    tmp_path: Path, role: str, target: str
) -> None:
    repo_root = _make_repo(tmp_path)

    decision = evaluate_paths(repo_root, [target], _agent_state(role))

    assert decision.decision == "allow"
    assert decision.targets == [target]


def test_absolute_target_normalizes_to_repo_path(tmp_path: Path) -> None:
    repo_root = _make_repo(tmp_path)
    relative_target = Path("src/source.py")

    decision = evaluate_paths(
        repo_root,
        [repo_root / relative_target],
        _agent_state("maker"),
    )

    assert decision.decision == "allow"
    assert decision.targets == [relative_target.as_posix()]


@pytest.mark.parametrize(
    ("role", "target"),
    [
        ("spec-author", "planning/01/plan.json"),
        ("planner", "planning/01/PLAN.md"),
        ("planner", "planning/01/other.json"),
        ("maker", "src/unlisted.py"),
        ("repair", "planning/01/SPEC.md"),
    ],
)
def test_actions_deny_cross_artifact_and_unlisted_edits(
    tmp_path: Path, role: str, target: str
) -> None:
    repo_root = _make_repo(tmp_path)

    decision = evaluate_paths(repo_root, [target], _agent_state(role))

    assert decision.decision == "deny"
    assert decision.targets == [target]
    assert "writable scopes" in decision.reason


def test_checker_denies_every_edit(tmp_path: Path) -> None:
    repo_root = _make_repo(tmp_path)

    decision = evaluate_paths(
        repo_root,
        ["src/source.py"],
        _agent_state("checker"),
    )

    assert decision.decision == "deny"
    assert decision.targets == []
    assert "read-only" in decision.reason


@pytest.mark.parametrize(
    "protected_path",
    [
        "planning/01/SPEC.md",
        "planning/01/plan.json",
        "planning/01/PLAN.md",
        "runs/01/state.json",
        "runs/01/reviews/checker-1.json",
        "runs/01/validation/results.json",
    ],
)
def test_candidate_action_cannot_make_control_paths_writable(
    tmp_path: Path, protected_path: str
) -> None:
    repo_root = _make_repo(tmp_path)
    state = _agent_state("maker", writable_paths=(protected_path,))

    decision = evaluate_paths(repo_root, [protected_path], state)

    assert decision.decision == "deny"
    assert decision.targets == []
    assert "supervisor paths" in decision.reason


def test_parent_traversal_is_denied_even_when_it_resolves_to_a_saved_path(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo(tmp_path)

    decision = evaluate_paths(
        repo_root,
        ["src/sub/../source.py"],
        _agent_state("maker"),
    )

    assert decision.decision == "deny"
    assert decision.targets == []
    assert "parent traversal" in decision.reason


def test_symlink_in_a_saved_write_path_fails_closed(tmp_path: Path) -> None:
    repo_root = _make_repo(tmp_path)
    app_root = repo_root / "src"
    outside = repo_root / "outside"
    app_root.mkdir(parents=True)
    outside.mkdir()
    (app_root / "linked").symlink_to(outside, target_is_directory=True)
    state = _agent_state(
        "maker",
        writable_paths=("src/linked/escaped.py",),
    )

    decision = evaluate_paths(
        repo_root,
        ["src/linked/escaped.py"],
        state,
    )

    assert decision.decision == "deny"
    assert decision.targets == []
    assert "symlink" in decision.reason


def test_symlink_target_outside_repository_is_denied(tmp_path: Path) -> None:
    repo_root = _make_repo(tmp_path)
    app_root = repo_root / "src"
    outside = tmp_path / "outside"
    app_root.mkdir(parents=True)
    outside.mkdir()
    (app_root / "linked").symlink_to(outside, target_is_directory=True)
    state = _agent_state(
        "maker",
        writable_paths=("src/linked/escaped.py",),
    )

    decision = evaluate_paths(
        repo_root,
        ["src/linked/escaped.py"],
        state,
    )

    assert decision.decision == "deny"
    assert "symlink" in decision.reason


def test_multi_target_request_allows_only_when_every_target_is_listed(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo(tmp_path)
    state = _agent_state(
        "maker",
        writable_paths=("src/first.py", "src/second.py"),
    )

    allowed = evaluate_paths(
        repo_root,
        ["src/first.py", repo_root / "src/second.py"],
        state,
    )
    denied = evaluate_paths(
        repo_root,
        ["src/first.py", "src/unlisted.py"],
        state,
    )

    assert allowed.decision == "allow"
    assert allowed.targets == ["src/first.py", "src/second.py"]
    assert denied.decision == "deny"
    assert denied.targets == ["src/first.py", "src/unlisted.py"]


def test_multi_target_request_reports_valid_targets_on_normalization_failure(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo(tmp_path)
    state = _agent_state(
        "maker",
        writable_paths=("src/valid.py", "src/other.py"),
    )

    decision = evaluate_paths(
        repo_root,
        ["src/valid.py", r"src/foo\bar.py"],
        state,
    )

    assert decision.decision == "deny"
    assert decision.targets == ["src/valid.py"]
    assert "target 1 is invalid" in decision.reason


def test_missing_targets_fail_closed(tmp_path: Path) -> None:
    repo_root = _make_repo(tmp_path)
    state = _agent_state("maker")

    assert evaluate_paths(repo_root, [], state).decision == "deny"
    assert evaluate_paths(repo_root, None, state).decision == "deny"


def test_human_internal_and_terminal_states_fail_closed(tmp_path: Path) -> None:
    repo_root = _make_repo(tmp_path)
    question_state = RunState(
        task_id=TASK_ID,
        original_request="Add due dates",
        stage="spec-authoring",
        pending_action=QuestionAction(
            action_id=ACTION_ID,
            task_id=TASK_ID,
            artifact="spec",
            questions=("Should dates be optional?",),
        ),
    )
    approval_state = RunState(
        task_id=TASK_ID,
        original_request="Add due dates",
        stage="spec-authoring",
        spec_revision=1,
        pending_action=ApprovalAction(
            action_id=ACTION_ID,
            task_id=TASK_ID,
            artifact="spec",
            revision=1,
            review_paths=("planning/01/SPEC.md",),
            review_digests={"planning/01/SPEC.md": "0" * 64},
        ),
    )
    validation_state = RunState(
        task_id=TASK_ID,
        original_request="Add due dates",
        stage="validation",
        spec_revision=1,
        spec_approved=True,
        spec_digest="0" * 64,
        plan_revision=1,
        plan_approved=True,
        candidate_attempt=1,
        pending_action=None,
    )
    blocked_state = RunState(
        task_id=TASK_ID,
        original_request="Add due dates",
        stage="blocked",
        pending_action=None,
        blocked_reason="The workflow cannot continue",
    )

    for state in (question_state, approval_state, validation_state, blocked_state):
        decision = evaluate_paths(repo_root, ["src/source.py"], state)
        assert decision.decision == "deny"
        assert decision.targets == []


def test_malformed_state_and_unknown_role_fail_closed(tmp_path: Path) -> None:
    repo_root = _make_repo(tmp_path)
    malformed = _agent_state("maker").model_dump(mode="json")
    assert isinstance(malformed["pending_action"], dict)
    malformed["pending_action"]["role"] = "supervisor"

    decision = evaluate_paths(repo_root, ["src/source.py"], malformed)

    assert decision.decision == "deny"
    assert decision.targets == []
    assert "invalid run state" in decision.reason


def test_load_run_state_reads_and_validates_the_complete_state(tmp_path: Path) -> None:
    repo_root = _make_repo(tmp_path)
    state = _agent_state("planner")
    state_path = repo_root / "runs" / TASK_ID / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(state.model_dump_json(), encoding="utf-8")

    assert load_run_state(repo_root, TASK_ID) == state


def test_load_run_state_rejects_missing_malformed_and_mismatched_state(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo(tmp_path)

    with pytest.raises(StateStorageError, match="missing"):
        load_run_state(repo_root, TASK_ID)

    state_path = repo_root / "runs" / TASK_ID / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("not json", encoding="utf-8")
    with pytest.raises(StateStorageError, match="invalid"):
        load_run_state(repo_root, TASK_ID)

    state_path.write_text(_agent_state("spec-author", task_id="02").model_dump_json())
    with pytest.raises(StateStorageError, match="does not match"):
        load_run_state(repo_root, TASK_ID)


def test_invalid_supplied_task_id_is_rejected_before_path_loading(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError):
        load_run_state(tmp_path / "not-a-repository", "../01")


def test_load_active_run_state_returns_none_without_a_marker(tmp_path: Path) -> None:
    repo_root = _make_repo(tmp_path)

    assert load_active_run_state(repo_root) is None


def test_load_active_run_state_reads_the_selected_authoritative_state(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo(tmp_path)
    state = _agent_state("planner")
    state_path = repo_root / "runs" / TASK_ID / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    save_active_task(repo_root, TASK_ID)

    assert load_active_run_state(repo_root) == state


def test_load_active_run_state_rejects_invalid_marker_or_selected_state(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo(tmp_path)
    runs_dir = repo_root / "runs"
    runs_dir.mkdir()
    marker_path = runs_dir / "active.json"
    marker_path.write_text("{not json", encoding="utf-8")

    with pytest.raises(StateStorageError, match="active task marker"):
        load_active_run_state(repo_root)

    marker_path.write_text(json.dumps({"task_id": TASK_ID}), encoding="utf-8")
    with pytest.raises(StateStorageError, match="missing"):
        load_active_run_state(repo_root)

    state_path = runs_dir / TASK_ID / "state.json"
    state_path.parent.mkdir()
    state_path.write_text(_agent_state("spec-author", task_id="02").model_dump_json())
    with pytest.raises(StateStorageError, match="does not match"):
        load_active_run_state(repo_root)


def test_folder_scope_allows_new_files_below_it(tmp_path: Path) -> None:
    repo_root = _make_repo(tmp_path)
    state = _agent_state("maker", writable_folders=("scripts",))

    decision = evaluate_paths(repo_root, ["scripts/new/tool.py"], state)

    assert decision.decision == "allow"
    assert decision.targets == ["scripts/new/tool.py"]


def test_folder_scope_does_not_allow_siblings(tmp_path: Path) -> None:
    repo_root = _make_repo(tmp_path)
    state = _agent_state("maker", writable_folders=("scripts",))

    decision = evaluate_paths(repo_root, ["scripts_extra/tool.py"], state)

    assert decision.decision == "deny"
    assert "writable scopes" in decision.reason


@pytest.mark.parametrize(
    "target",
    [
        ".git/config",
        "runs/01/state.json",
        "runs/active.json",
        "planning/01/SPEC.md",
        "planning/01/plan.json",
        "harness/gates.py",
        "tests/harness/test_state.py",
        ".agents/skills/maker/SKILL.md",
        ".claude/settings.json",
        ".codex/hooks.json",
        "package.json",
        ".husky/pre-commit",
        "tests/unit/conftest.py",
        "docs/AGENTS.md",
        ".ruff.toml",
    ],
)
def test_whole_repository_scope_excludes_supervisor_and_harness_paths(
    tmp_path: Path, target: str
) -> None:
    repo_root = _make_repo(tmp_path)
    state = _agent_state("maker", writable_folders=(".",))

    decision = evaluate_paths(repo_root, [target], state)

    assert decision.decision == "deny"
    assert decision.targets == [target]


def test_whole_repository_scope_allows_ordinary_files(tmp_path: Path) -> None:
    repo_root = _make_repo(tmp_path)
    state = _agent_state("maker", writable_folders=(".",))

    decision = evaluate_paths(repo_root, ["README.md", "docs/new.md"], state)

    assert decision.decision == "allow"


def test_named_harness_file_is_writable_but_its_siblings_are_not(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo(tmp_path)
    state = _agent_state(
        "maker", writable_paths=("harness/gates.py",), writable_folders=(".",)
    )

    allowed = evaluate_paths(repo_root, ["harness/gates.py"], state)
    denied = evaluate_paths(repo_root, ["harness/state.py"], state)

    assert allowed.decision == "allow"
    assert denied.decision == "deny"


def test_named_guidance_file_is_writable_under_a_folder_scope(tmp_path: Path) -> None:
    repo_root = _make_repo(tmp_path)
    state = _agent_state(
        "maker", writable_paths=("docs/AGENTS.md",), writable_folders=(".",)
    )

    assert evaluate_paths(repo_root, ["docs/AGENTS.md"], state).decision == "allow"
    assert evaluate_paths(repo_root, ["docs/CLAUDE.md"], state).decision == "deny"


@pytest.mark.parametrize(
    "folder", ["harness", ".agents/skills", ".husky", "runs", "planning/01"]
)
def test_candidate_folder_cannot_name_supervisor_or_harness_paths(
    tmp_path: Path, folder: str
) -> None:
    repo_root = _make_repo(tmp_path)
    state = _agent_state("maker", writable_folders=(folder,))

    decision = evaluate_paths(repo_root, [f"{folder}/x.py"], state)

    assert decision.decision == "deny"
    assert "invalid run state" in decision.reason


def test_target_through_a_symlink_is_denied_under_a_folder_scope(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo_root / "scripts").symlink_to(outside, target_is_directory=True)
    state = _agent_state("maker", writable_folders=(".",))

    decision = evaluate_paths(repo_root, ["scripts/evil.py"], state)

    assert decision.decision == "deny"
    assert "symlink" in decision.reason
