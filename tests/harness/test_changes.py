import json
import subprocess
from pathlib import Path

import pytest

from harness.contracts.changes import Baseline, FileDrift
from harness.contracts.core import TaskPaths, load_task_paths
from harness.feedback.changes import (
    FileStateError,
    diff_snapshots,
    find_drift,
    load_approved_plan,
    preexisting_changes,
    reference_state,
    save_approved_plan,
    save_baseline,
    verify_candidate,
    verify_checks,
)
from harness.utils.digests import sha256_bytes
from tests.harness.support import TASK_ID, approve_draft_plan, plan_data


def _write(root: Path, relative: str, text: str = "x\n") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _baseline(root: Path) -> Baseline:
    return save_baseline(root, load_task_paths(root, TASK_ID))


def _git_unavailable(*args: object, **kwargs: object) -> None:
    raise FileNotFoundError("git unavailable")


def test_snapshot_covers_repository_files_and_skips_environment_and_outputs(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "src/a.py")
    _write(tmp_path, "planning/01/plan.json")
    _write(tmp_path, "runs/01/state.json")
    _write(tmp_path, ".git/config")
    _write(tmp_path, ".venv/lib/x.py")
    _write(tmp_path, "src/__pycache__/a.pyc")
    _write(tmp_path, ".pytest_cache/v/cache")
    _write(tmp_path, ".DS_Store")

    assert set(_baseline(tmp_path).files) == {"src/a.py", "planning/01/plan.json"}


def test_a_runs_folder_below_the_root_is_still_covered(tmp_path: Path) -> None:
    _write(tmp_path, "src/runs/data.py")

    assert "src/runs/data.py" in _baseline(tmp_path).files


def test_symlinks_are_recorded_by_target_and_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    _write(outside, "secret.txt")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "linked").symlink_to(outside, target_is_directory=True)
    (repo / "file-link").symlink_to(outside / "secret.txt")

    snapshot = _baseline(repo).files

    assert snapshot["linked"] == f"link:{outside}"
    assert snapshot["file-link"].startswith("link:")
    assert "linked/secret.txt" not in snapshot


def test_diff_lists_added_changed_and_deleted_files(tmp_path: Path) -> None:
    before = {"a": "1", "b": "1", "c": "1"}
    after = {"a": "1", "b": "2", "d": "1"}

    changes = diff_snapshots(before, after)

    assert changes.added == ("d",)
    assert changes.changed == ("b",)
    assert changes.deleted == ("c",)
    assert changes.paths == ("b", "c", "d")


def test_content_change_with_the_same_size_is_detected(tmp_path: Path) -> None:
    _write(tmp_path, "a.py", "VALUE = 1\n")
    before = _baseline(tmp_path).files
    _write(tmp_path, "a.py", "VALUE = 2\n")

    assert diff_snapshots(before, _baseline(tmp_path).files).changed == ("a.py",)


def test_preexisting_changes_come_from_git_and_are_unavailable_outside_git(
    tmp_path: Path,
) -> None:
    assert preexisting_changes(tmp_path) is None

    git = ("git", "-c", "user.name=t", "-c", "user.email=t@example.com")
    subprocess.run((*git, "init", "-q"), cwd=tmp_path, check=True)
    _write(tmp_path, "tracked.py")
    subprocess.run((*git, "add", "."), cwd=tmp_path, check=True)
    subprocess.run((*git, "commit", "-qm", "init"), cwd=tmp_path, check=True)
    _write(tmp_path, "tracked.py", "edited\n")
    _write(tmp_path, "new dir/untracked.py")

    assert preexisting_changes(tmp_path) == ("new dir/untracked.py", "tracked.py")


def test_declared_outputs_are_listed_apart_from_the_compared_files(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "src/a.py")
    _write(tmp_path, "src/__pycache__/a.pyc")
    _write(tmp_path, ".pytest_cache/v/cache")
    _write(tmp_path, ".DS_Store")
    _write(tmp_path, ".venv/lib/x.pyc")
    _write(tmp_path, ".git/__pycache__/x")

    baseline = _baseline(tmp_path)

    assert set(baseline.files) == {"src/a.py"}
    assert set(baseline.output_files) == {
        "src/__pycache__/a.pyc",
        ".pytest_cache/v/cache",
        ".DS_Store",
    }


def test_git_inventory_tracks_normal_files_and_omits_ignored_or_missing_files(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(("git", "init", "-q", str(repo)), check=True)
    _write(repo, ".gitignore", "ignored/\n__pycache__/\n")
    _write(repo, "tracked.py")
    _write(repo, "missing.py")
    _write(repo, "new name\nfile.py")
    _write(repo, "ignored/data.py")
    _write(repo, "runs/01/state.json")
    _write(repo, "nested/runs/data.py")
    _write(repo, "__pycache__/output.pyc")
    subprocess.run(
        ("git", "add", ".gitignore", "tracked.py", "missing.py", "runs/01/state.json"),
        cwd=repo,
        check=True,
    )
    (repo / "missing.py").unlink()
    (repo / "link").symlink_to("tracked.py")
    (repo / "linked-dir").symlink_to("nested", target_is_directory=True)

    baseline = _baseline(repo)
    snapshot = baseline.files

    assert baseline.inventory == "git"
    assert set(snapshot) == {
        ".gitignore",
        "tracked.py",
        "new name\nfile.py",
        "nested/runs/data.py",
        "link",
        "linked-dir",
    }
    assert snapshot["link"] == "link:tracked.py"
    assert snapshot["linked-dir"] == "link:nested"
    assert baseline.output_files == {}


def test_git_inventory_reports_deleted_tracked_file(tmp_path: Path) -> None:
    subprocess.run(("git", "init", "-q", str(tmp_path)), check=True)
    _write(tmp_path, "tracked.py")
    subprocess.run(("git", "add", "tracked.py"), cwd=tmp_path, check=True)
    before = _baseline(tmp_path).files
    (tmp_path / "tracked.py").unlink()

    assert diff_snapshots(before, _baseline(tmp_path).files).deleted == ("tracked.py",)


def test_git_inventory_does_not_follow_a_symlinked_parent(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(("git", "init", "-q", str(repo)), check=True)
    _write(repo, "nested/tracked.txt", "original\n")
    subprocess.run(("git", "add", "nested/tracked.txt"), cwd=repo, check=True)
    before = _baseline(repo).files

    outside = tmp_path / "outside"
    _write(outside, "tracked.txt", "outside\n")
    (repo / "nested" / "tracked.txt").unlink()
    (repo / "nested").rmdir()
    (repo / "nested").symlink_to(outside, target_is_directory=True)

    after = _baseline(repo).files

    assert after == {"nested": f"link:{outside}"}
    assert diff_snapshots(before, after).deleted == ("nested/tracked.txt",)


def test_git_inventory_falls_back_when_git_cannot_list_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "source.py")
    _write(tmp_path, "__pycache__/source.pyc")

    monkeypatch.setattr("harness.feedback.changes.subprocess.run", _git_unavailable)

    baseline = _baseline(tmp_path)

    assert baseline.inventory == "filesystem"
    assert set(baseline.files) == {"source.py"}
    assert set(baseline.output_files) == {"__pycache__/source.pyc"}


def _approve_plan(repo: Path, text: str) -> bytes:
    """Write ``plan.json``, save its approved copy, and return the draft bytes."""

    _write(repo, "planning/01/plan.json", text)
    approve_draft_plan(repo)
    return (repo / "planning/01/plan.json").read_bytes()


def test_save_approved_plan_copies_the_exact_bytes(tmp_path: Path) -> None:
    # Indentation, CRLF, and a non-ASCII character must survive the copy.
    text = json.dumps(plan_data(), indent=3, ensure_ascii=False)
    content = _approve_plan(
        tmp_path, text.replace("Implement the behavior.", "Add é.").replace("\n", "\r\n")
    )
    task_paths = load_task_paths(tmp_path, TASK_ID)

    assert task_paths.approved_plan.read_bytes() == content
    assert load_approved_plan(tmp_path, task_paths).steps[0].work_items[0].intent == (
        "Add é."
    )


def test_save_approved_plan_rejects_a_digest_mismatch(tmp_path: Path) -> None:
    _write(tmp_path, "planning/01/plan.json", json.dumps(plan_data()))
    task_paths = load_task_paths(tmp_path, TASK_ID)

    with pytest.raises(
        FileStateError,
        match=r"^the approved plan could not be saved: plan\.json differs from the approved",
    ):
        save_approved_plan(tmp_path, task_paths, "0" * 64)

    assert not task_paths.approved_plan.exists()


def test_save_approved_plan_rejects_a_plan_for_another_task(tmp_path: Path) -> None:
    with pytest.raises(FileStateError, match="another task"):
        _approve_plan(tmp_path, json.dumps({**plan_data(), "task_id": "02"}))

    assert not load_task_paths(tmp_path, TASK_ID).approved_plan.exists()


def test_a_git_baseline_stops_when_git_fails_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subprocess.run(("git", "init", "-q", str(tmp_path)), check=True)
    _write(tmp_path, "src/a.py")
    _approve_plan(tmp_path, json.dumps(plan_data()))
    task_paths = load_task_paths(tmp_path, TASK_ID)
    assert save_baseline(tmp_path, task_paths).inventory == "git"
    monkeypatch.setattr("harness.feedback.changes.subprocess.run", _git_unavailable)

    with pytest.raises(FileStateError, match="baseline used Git"):
        verify_candidate(tmp_path, task_paths, 1, ())

    assert not task_paths.candidate_changes(1).exists()


def _validated_candidate(repo: Path) -> TaskPaths:
    """Run candidate 1 and its round-1 checks, so that a later change is drift."""

    _write(repo, "src/feature.py", "VALUE = 1\n")
    _write(repo, "src/test_feature.py", "def test(): pass\n")
    _approve_plan(repo, json.dumps(plan_data()))
    task_paths = load_task_paths(repo, TASK_ID)
    save_baseline(repo, task_paths)
    _write(repo, "src/feature.py", "VALUE = 2\n")
    verify_candidate(repo, task_paths, 1, ("src/feature.py",))
    assert verify_checks(repo, task_paths, 1, 1)[0].status == "pass"
    return task_paths


def _sha(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def test_find_drift_lists_added_changed_and_deleted_files(tmp_path: Path) -> None:
    task_paths = _validated_candidate(tmp_path)
    assert find_drift(tmp_path, task_paths, 1, 2) is None

    _write(tmp_path, "src/feature.py", "VALUE = 3\n")
    (tmp_path / "src/test_feature.py").unlink()
    _write(tmp_path, "src/new.py", "NEW = 1\n")
    record = find_drift(tmp_path, task_paths, 1, 2)

    assert record is not None
    assert record.drift == (
        FileDrift(
            path="src/feature.py",
            old_digest=_sha("VALUE = 2\n"),
            new_digest=_sha("VALUE = 3\n"),
        ),
        FileDrift(path="src/new.py", old_digest=None, new_digest=_sha("NEW = 1\n")),
        FileDrift(
            path="src/test_feature.py",
            old_digest=_sha("def test(): pass\n"),
            new_digest=None,
        ),
    )
    assert record.validation_round == 2
    assert record.digests["src/test_feature.py"] == "deleted"
    assert task_paths.drift_changes(1).is_file()


def test_reference_state_reads_the_candidate_then_the_drift_record(
    tmp_path: Path,
) -> None:
    task_paths = _validated_candidate(tmp_path)
    _write(tmp_path, "src/test_feature.py", "def test(): assert True\n")
    drift = find_drift(tmp_path, task_paths, 1, 2)
    assert drift is not None

    candidate = reference_state(tmp_path, task_paths, 1, 1)
    assert (candidate.digests, candidate.reported_paths) == (
        {"src/feature.py": _sha("VALUE = 2\n")},
        ("src/feature.py",),
    )
    assert reference_state(tmp_path, task_paths, 1, 2) == drift
    assert drift.reported_paths == (
        "src/feature.py",
        "src/test_feature.py",
    )


@pytest.mark.parametrize(
    ("attempt", "validation_round", "message"),
    [
        (1, 2, "could not be read"),
        (1, 3, "does not match"),
    ],
)
def test_reference_state_rejects_a_missing_or_mismatched_record(
    tmp_path: Path, attempt: int, validation_round: int, message: str
) -> None:
    task_paths = _validated_candidate(tmp_path)
    if validation_round == 3:
        _write(tmp_path, "src/feature.py", "VALUE = 3\n")
        find_drift(tmp_path, task_paths, 1, 2)

    with pytest.raises(FileStateError, match=message):
        reference_state(tmp_path, task_paths, attempt, validation_round)
