import io
import json
from pathlib import Path

import pytest

import harness.hooks.claude_adapter as adapter
from harness.contracts.actions import AgentAction, CandidateContext
from harness.contracts.models import DenyResponse, HookDecision
from harness.contracts.state import RunState
from harness.execute.state import save_active_task
from harness.hooks.claude_adapter import handle_event, main

TASK_ID = "01"


def _make_repo(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    state_path = repo_root / "runs" / TASK_ID / "state.json"
    state_path.parent.mkdir(parents=True)
    action = AgentAction(
        action_id="a" * 32,
        task_id=TASK_ID,
        role="maker",
        persona_id="maker",
        skill_id="maker",
        candidate_attempt=1,
        required_input_paths=(),
        writable_paths=("src/module.py",),
        context=CandidateContext(spec_revision=1, plan_revision=1),
    )
    state = RunState(
        task_id=TASK_ID,
        original_request="Add due dates",
        stage="implementation",
        spec_revision=1,
        spec_approved=True,
        spec_digest="0" * 64,
        plan_revision=1,
        plan_approved=True,
        candidate_attempt=1,
        pending_action=action,
    )
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    save_active_task(repo_root, TASK_ID)
    return repo_root


def _event(target: Path | str, *, tool_name: str = "Edit") -> dict[str, object]:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {"file_path": str(target), "runtime_metadata": "ignored"},
        "session_id": "ignored",
    }


@pytest.mark.parametrize("tool_name", ["Edit", "Write"])
def test_allowed_edit_tools_accept_runtime_metadata(
    tmp_path: Path, tool_name: str
) -> None:
    repo_root = _make_repo(tmp_path)
    event_input = _event(repo_root / "src" / "module.py", tool_name=tool_name)

    assert handle_event(repo_root, event_input) is None


def test_no_selected_task_produces_no_hook_response(tmp_path: Path) -> None:
    repo_root = _make_repo(tmp_path)
    (repo_root / "runs" / "active.json").unlink()

    assert handle_event(repo_root, {"not": "a supported event"}) is None


def test_cli_with_no_selected_task_is_silent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = _make_repo(tmp_path)
    (repo_root / "runs" / "active.json").unlink()
    event_input = _event(repo_root / "outside.py")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event_input)))

    assert main(["--repo-root", str(repo_root)]) == 0
    assert capsys.readouterr().out == ""


def test_invalid_selected_task_data_is_denied(tmp_path: Path) -> None:
    repo_root = _make_repo(tmp_path)
    (repo_root / "runs" / "active.json").write_text("{not json", encoding="utf-8")

    response = handle_event(repo_root, {"not": "a supported event"})

    assert response is not None
    assert response.hookSpecificOutput.permissionDecision == "deny"
    assert "active task marker" in response.hookSpecificOutput.permissionDecisionReason


def test_cross_scope_target_is_denied(tmp_path: Path) -> None:
    repo_root = _make_repo(tmp_path)

    response = handle_event(
        repo_root,
        _event(repo_root / "checks" / TASK_ID / "check_acceptance.py"),
    )

    assert response is not None
    assert "outside scope" in response.hookSpecificOutput.permissionDecisionReason


@pytest.mark.parametrize(
    "event_input",
    [
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Edit",
            "tool_input": {"file_path": "src/module.py"},
        },
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Edit",
            "tool_input": {},
        },
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Edit",
            "tool_input": {"file_path": 7},
        },
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "/src/module.py"},
        },
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Edit",
            "tool_input": {"file_path": "/src/module.py"},
        },
    ],
)
def test_malformed_event_is_denied(
    tmp_path: Path, event_input: dict[str, object]
) -> None:
    repo_root = _make_repo(tmp_path)

    response = handle_event(repo_root, event_input)

    assert response is not None
    assert response.hookSpecificOutput.permissionDecision == "deny"


def test_one_absolute_target_is_passed_to_policy_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _make_repo(tmp_path)
    target = repo_root / "src" / "module.py"
    calls: list[list[Path]] = []

    def record_evaluation(
        supplied_root: Path, targets: list[Path], run_state: object
    ) -> HookDecision:
        assert supplied_root == repo_root
        assert isinstance(run_state, RunState)
        calls.append(targets)
        return HookDecision(
            decision="allow",
            reason="target is permitted",
            targets=["src/module.py"],
        )

    monkeypatch.setattr(adapter, "evaluate_paths", record_evaluation)

    assert handle_event(repo_root, _event(target)) is None
    assert calls == [[target]]


def test_missing_active_state_is_translated_to_denial_before_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _make_repo(tmp_path)
    (repo_root / "runs" / TASK_ID / "state.json").unlink()
    monkeypatch.setattr(
        adapter,
        "evaluate_paths",
        lambda *args: pytest.fail("policy must not run without active state"),
    )

    response = handle_event(
        repo_root,
        _event(repo_root / "src" / "module.py"),
    )

    assert response is not None
    assert response.hookSpecificOutput.permissionDecision == "deny"


def test_cli_allow_is_silent_and_deny_emits_validated_wire_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = _make_repo(tmp_path)
    nested_cwd = repo_root / "src" / "nested"
    nested_cwd.mkdir(parents=True)
    monkeypatch.chdir(nested_cwd)
    argv = ["--repo-root", str(repo_root)]

    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps(_event(repo_root / "src" / "module.py"))),
    )
    assert main(argv) == 0
    assert capsys.readouterr().out == ""

    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps(_event(repo_root / "planning" / TASK_ID / "PLAN.md"))),
    )
    assert main(argv) == 0
    output = capsys.readouterr().out
    response = DenyResponse.model_validate_json(output)
    assert response.hookSpecificOutput.hookEventName == "PreToolUse"
    assert response.hookSpecificOutput.permissionDecision == "deny"
    assert "outside scope" in response.hookSpecificOutput.permissionDecisionReason


def test_cli_malformed_json_denies_with_exit_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = _make_repo(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO("not JSON"))

    exit_code = main(["--repo-root", str(repo_root)])
    output = capsys.readouterr().out

    assert exit_code == 0
    response = DenyResponse.model_validate_json(output)
    assert "malformed JSON" in response.hookSpecificOutput.permissionDecisionReason
