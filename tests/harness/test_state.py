import json
from pathlib import Path

import pytest

from harness.contracts.actions import AgentAction
from harness.execute.state import (
    StateStorageError,
    clear_active_task,
    load_active_task,
    load_state,
    save_active_task,
    save_state,
)
from harness.execute.supervisor import create_run
from harness.utils import atomic as atomic_module


def _repo(tmp_path: Path) -> Path:
    """Create the minimum repository used by state tests."""

    (tmp_path / "src").mkdir(parents=True)
    return tmp_path


def test_create_run_allocates_sequential_ids_and_initial_spec_actions(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)

    first = create_run(repo, "Add the first behavior.")
    second = create_run(repo, "Add the second behavior.")

    assert (first.task_id, second.task_id) == ("01", "02")
    assert first.stage == "spec-authoring"
    assert isinstance(first.pending_action, AgentAction)
    assert first.pending_action.role == "spec-author"
    assert first.pending_action.writable_paths == ("planning/01/SPEC.md",)
    for relative in (
        "planning/01",
        "runs/01/reviews",
        "runs/01/validation",
        "runs/01/state.json",
    ):
        assert (repo / relative).exists()


def test_state_round_trip_keeps_the_saved_pending_action(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    created = create_run(repo, "Add a restart-safe behavior.")

    loaded = load_state(repo, created.task_id)

    assert loaded == created
    assert loaded.pending_action is not None
    assert loaded.pending_action.action_id == created.pending_action.action_id  # type: ignore[union-attr]


def test_save_state_uses_a_sibling_temporary_file_and_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    state = create_run(repo, "Save state atomically.")
    calls: list[tuple[Path, Path]] = []
    original_replace = atomic_module.os.replace

    def record_replace(source: str | Path, destination: str | Path) -> None:
        """Record the atomic replacement while preserving its real behavior."""

        calls.append((Path(source), Path(destination)))
        original_replace(source, destination)

    monkeypatch.setattr(atomic_module.os, "replace", record_replace)
    saved_path = save_state(repo, state)

    assert len(calls) == 1
    temporary, destination = calls[0]
    assert destination == saved_path
    assert temporary.parent == destination.parent
    assert temporary != destination
    assert not temporary.exists()


def test_active_task_marker_round_trip_and_matching_cleanup(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    create_run(repo, "Create the runs directory.")

    assert load_active_task(repo) is None
    marker_path = save_active_task(repo, "01")
    assert marker_path == repo / "runs" / "active.json"
    assert json.loads(marker_path.read_text(encoding="utf-8")) == {"task_id": "01"}
    assert load_active_task(repo) == "01"

    clear_active_task(repo, "02")
    assert load_active_task(repo) == "01"
    clear_active_task(repo, "01")
    assert load_active_task(repo) is None
    clear_active_task(repo, "01")


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "{}",
        '{"task_id": "01", "stage": "implementation"}',
        '{"task_id": "one"}',
        '[{"task_id": "01"}]',
    ],
)
def test_active_task_marker_rejects_malformed_content(
    tmp_path: Path,
    content: str,
) -> None:
    repo = _repo(tmp_path)
    create_run(repo, "Create the runs directory.")
    marker_path = repo / "runs" / "active.json"
    marker_path.write_text(content, encoding="utf-8")

    with pytest.raises(StateStorageError, match="active task marker"):
        load_active_task(repo)
    with pytest.raises(StateStorageError, match="active task marker"):
        save_active_task(repo, "01")
    with pytest.raises(StateStorageError, match="active task marker"):
        clear_active_task(repo, "01")

    assert marker_path.read_text(encoding="utf-8") == content


def test_active_task_marker_rejects_a_symlink(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    create_run(repo, "Create the runs directory.")
    outside = tmp_path / "outside-active.json"
    outside.write_text('{"task_id": "01"}', encoding="utf-8")
    marker_path = repo / "runs" / "active.json"
    marker_path.symlink_to(outside)

    with pytest.raises(StateStorageError, match="symlink"):
        load_active_task(repo)
    with pytest.raises(StateStorageError, match="symlink"):
        save_active_task(repo, "01")
    assert outside.read_text(encoding="utf-8") == '{"task_id": "01"}'


def test_active_task_marker_reports_a_nonregular_file(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    create_run(repo, "Create the runs directory.")
    (repo / "runs" / "active.json").mkdir()

    with pytest.raises(
        StateStorageError, match="active task marker is not a regular file"
    ):
        load_active_task(repo)


def test_failed_active_task_replace_keeps_the_complete_old_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    create_run(repo, "Create the runs directory.")
    marker_path = save_active_task(repo, "01")
    before = marker_path.read_bytes()

    def fail_replace(_source: str | Path, _destination: str | Path) -> None:
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(atomic_module.os, "replace", fail_replace)
    with pytest.raises(StateStorageError, match="could not save active task marker"):
        save_active_task(repo, "02")

    assert marker_path.read_bytes() == before
    assert list(marker_path.parent.glob(".active.json.*.tmp")) == []


def test_load_state_rejects_missing_malformed_and_mismatched_state(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(StateStorageError, match="missing"):
        load_state(repo, "01")

    state = create_run(repo, "Create state that will be damaged.")
    state_path = repo / "runs" / state.task_id / "state.json"
    state_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(StateStorageError, match="invalid"):
        load_state(repo, state.task_id)

    save_state(repo, state)
    data = json.loads(state_path.read_text(encoding="utf-8"))
    data["task_id"] = "02"
    data["pending_action"]["task_id"] = "02"
    data["pending_action"]["writable_paths"] = ["planning/02/SPEC.md"]
    state_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StateStorageError, match="does not match directory"):
        load_state(repo, "01")


def test_task_allocation_uses_only_immediate_numeric_run_directories(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    (repo / "runs" / "09").mkdir(parents=True)
    (repo / "runs" / "notes").mkdir()
    (repo / "runs" / "09" / "99").mkdir()

    state = create_run(repo, "Allocate the next immediate run ID.")

    assert state.task_id == "10"


def test_invalid_first_action_does_not_consume_a_task_id(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside-context"
    outside.mkdir()
    (repo / "linked-context").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        create_run(
            repo,
            "This request has unsafe context.",
            context_paths=("linked-context",),
        )

    assert not (repo / "planning" / "01").exists()
    assert not (repo / "runs" / "01").exists()
    state = create_run(repo, "This request has safe context.")
    assert state.task_id == "01"


def test_create_run_requires_an_existing_repository(
    tmp_path: Path,
) -> None:
    missing_repo = tmp_path / "missing-repository"
    with pytest.raises(StateStorageError, match="repository root must already exist"):
        create_run(missing_repo, "Do not create a repository for a typo.")
    assert not missing_repo.exists()

    repo = tmp_path / "without-application"
    repo.mkdir()
    state = create_run(repo, "Start without an application directory.")
    assert state.task_id == "01"
    assert (repo / "runs" / "01" / "state.json").is_file()


def test_create_run_rejects_a_symlinked_application_before_writing(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repository"
    repo.mkdir()
    outside_app = tmp_path / "outside-application"
    outside_app.mkdir()
    (repo / "src").symlink_to(outside_app, target_is_directory=True)

    with pytest.raises(StateStorageError, match=r"repository layout is unsafe.*symlink"):
        create_run(repo, "Do not follow the application symlink.")

    assert not (repo / "planning").exists()
    assert not (repo / "runs").exists()
