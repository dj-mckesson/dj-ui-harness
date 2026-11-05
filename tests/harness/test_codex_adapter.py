import io
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

import harness.hooks.codex_adapter as adapter
from harness.contracts.actions import AgentAction, CandidateContext
from harness.contracts.models import DenyResponse, HookDecision
from harness.contracts.state import RunState
from harness.execute.state import save_active_task
from harness.hooks.codex_adapter import (
    CodexPreToolUseEvent,
    PatchParseError,
    extract_patch_targets,
    handle_event,
    main,
)

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
        writable_paths=(
            "src/module.py",
            "src/new.py",
            "src/old.py",
            "src/valid.py",
        ),
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


def _event(
    repo_root: Path, command: str, *, cwd: Path | None = None
) -> dict[str, object]:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "apply_patch",
        "tool_input": {"command": command, "runtime_metadata": "ignored"},
        "cwd": str(cwd or repo_root),
        "session_id": "ignored",
    }


def test_allowed_patch_accepts_runtime_metadata(tmp_path: Path) -> None:
    repo_root = _make_repo(tmp_path)
    command = """*** Begin Patch
*** Add File: src/new.py
+value = 1
*** End Patch"""

    assert handle_event(repo_root, _event(repo_root, command)) is None

    event = CodexPreToolUseEvent.model_validate(_event(repo_root, command))
    with pytest.raises(ValidationError):
        event.cwd = "/different"


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
    command = "*** Begin Patch\n*** Delete File: outside.py\n*** End Patch"
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_event(repo_root, command))))

    assert main(["--repo-root", str(repo_root)]) == 0
    assert capsys.readouterr().out == ""


def test_invalid_selected_task_data_is_denied(tmp_path: Path) -> None:
    repo_root = _make_repo(tmp_path)
    (repo_root / "runs" / "active.json").write_text("{not json", encoding="utf-8")

    response = handle_event(repo_root, _event(repo_root, "not a patch"))

    assert response is not None
    assert response.hookSpecificOutput.permissionDecision == "deny"
    assert "active task marker" in response.hookSpecificOutput.permissionDecisionReason


def test_cross_scope_and_mixed_scope_patches_are_denied(tmp_path: Path) -> None:
    repo_root = _make_repo(tmp_path)
    cross_scope = """*** Begin Patch
*** Update File: checks/01/check_acceptance.py
@@
-old
+new
*** End Patch"""
    mixed_scope = """*** Begin Patch
*** Update File: src/valid.py
@@
-old
+new
*** Delete File: planning/01/PLAN.md
*** End Patch"""

    cross_response = handle_event(repo_root, _event(repo_root, cross_scope))
    mixed_response = handle_event(repo_root, _event(repo_root, mixed_scope))

    assert cross_response is not None
    assert "outside scope" in cross_response.hookSpecificOutput.permissionDecisionReason
    assert mixed_response is not None
    assert "outside scope" in mixed_response.hookSpecificOutput.permissionDecisionReason


@pytest.mark.parametrize(
    "command",
    [
        "*** Add File: src/new.py\n+value = 1",
        "*** Begin Patch\n*** Rename File: src/a.py\n*** End Patch",
        "*** Begin Patch\n*** Add File: /tmp/new.py\n+value = 1\n*** End Patch",
        "*** Begin Patch\n*** Add File: ../new.py\n+value = 1\n*** End Patch",
        "*** Begin Patch\n*** Add File: \n*** End Patch",
    ],
)
def test_malformed_or_ambiguous_patch_is_denied(tmp_path: Path, command: str) -> None:
    repo_root = _make_repo(tmp_path)

    response = handle_event(repo_root, _event(repo_root, command))

    assert response is not None
    assert response.hookSpecificOutput.permissionDecision == "deny"


def test_move_passes_source_and_destination_to_policy_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _make_repo(tmp_path)
    command = """*** Begin Patch
*** Update File: src/old.py
*** Move to: src/new.py
@@
-old
+new
*** End Patch"""
    calls: list[list[Path]] = []

    def record_evaluation(
        supplied_root: Path, targets: list[Path], run_state: object
    ) -> HookDecision:
        assert supplied_root == repo_root
        assert isinstance(run_state, RunState)
        calls.append(targets)
        return HookDecision(
            decision="allow",
            reason="all targets are permitted",
            targets=["src/old.py", "src/new.py"],
        )

    monkeypatch.setattr(adapter, "evaluate_paths", record_evaluation)

    assert handle_event(repo_root, _event(repo_root, command)) is None
    assert calls == [[repo_root / "src/old.py", repo_root / "src/new.py"]]


def test_nested_cwd_resolves_targets_while_explicit_root_anchors_policy(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo(tmp_path)
    nested_cwd = repo_root / "src"
    nested_cwd.mkdir(parents=True)
    command = """*** Begin Patch
*** Update File: module.py
@@
-old
+new
*** End Patch"""

    assert (
        handle_event(
            repo_root,
            _event(repo_root, command, cwd=nested_cwd),
        )
        is None
    )


def test_extract_patch_targets_rejects_cwd_outside_explicit_root(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo(tmp_path)

    with pytest.raises(PatchParseError, match="inside the repository"):
        extract_patch_targets(
            "*** Begin Patch\n*** Delete File: file.py\n*** End Patch",
            cwd=tmp_path,
            repo_root=repo_root,
        )


def test_cli_allow_is_silent_and_deny_emits_validated_wire_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_root = _make_repo(tmp_path)
    nested_cwd = repo_root / "src" / "nested"
    nested_cwd.mkdir(parents=True)
    monkeypatch.chdir(nested_cwd)
    allowed = """*** Begin Patch
*** Add File: src/new.py
+value = 1
*** End Patch"""
    denied = """*** Begin Patch
*** Delete File: checks/01/check_acceptance.py
*** End Patch"""
    argv = ["--repo-root", str(repo_root)]

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_event(repo_root, allowed))))
    assert main(argv) == 0
    assert capsys.readouterr().out == ""

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_event(repo_root, denied))))
    assert main(argv) == 0
    output = capsys.readouterr().out
    response = DenyResponse.model_validate_json(output)
    assert response.hookSpecificOutput.permissionDecision == "deny"
    assert response.hookSpecificOutput.permissionDecisionReason


def test_cli_malformed_json_denies_with_exit_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_root = _make_repo(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO("not JSON"))

    exit_code = main(["--repo-root", str(repo_root)])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "malformed JSON" in output["hookSpecificOutput"]["permissionDecisionReason"]
