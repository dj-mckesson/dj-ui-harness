"""Compare repository files with the baseline and approved plan."""

import os
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from harness.contracts.changes import (
    Baseline,
    ChangeRecord,
    ChangeSet,
    DriftRecord,
    FileDrift,
    Inventory,
)
from harness.contracts.core import (
    MAX_CANDIDATE_ATTEMPTS,
    CandidateAttempt,
    TaskPaths,
    ValidationRound,
)
from harness.contracts.plan import PlanDocument
from harness.contracts.scopes import requires_exact_file_scope, scope_allows
from harness.utils.artifact_paths import (
    require_artifact_file,
    require_safe_artifact_path,
)
from harness.utils.atomic import write_bytes
from harness.utils.digests import sha256_bytes, sha256_file

# Folders that hold the environment or supervisor evidence, not repository content.
SKIPPED_ROOTS = (".git", ".venv", "node_modules", ".next", "runs")

# Check outputs that the file state ignores. Pytest, ruff, and Next.js write them.
DECLARED_OUTPUT_DIRECTORIES = (
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
)
DECLARED_OUTPUT_FILES = (".DS_Store", "next-env.d.ts", "tsconfig.tsbuildinfo")
DECLARED_OUTPUTS = (*DECLARED_OUTPUT_DIRECTORIES, *DECLARED_OUTPUT_FILES)


class FileStateError(ValueError):
    """Report that the file state cannot be read or compared."""


def _digest(path: Path) -> str:
    """Return a stable digest of one file, or ``link:<target>`` for a symlink."""

    if path.is_symlink():
        return f"link:{os.readlink(path)}"
    return sha256_file(path)


def _scan_filesystem(repo_root: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Return digests of the repository files and of the declared check outputs.

    Keep check outputs separate so a test run does not appear as a file change.

    :raises FileStateError: If a file cannot be read.
    """

    root = repo_root.resolve()
    files: dict[str, str] = {}
    outputs: dict[str, str] = {}

    def walk_error(error: OSError) -> None:
        raise error

    try:
        for current, directories, names in os.walk(root, onerror=walk_error):
            current_path = Path(current)
            relative_parent = current_path.relative_to(root)
            inside_output = any(
                part in DECLARED_OUTPUT_DIRECTORIES for part in relative_parent.parts
            )
            kept: list[str] = []
            for name in directories:
                if relative_parent == Path(".") and name in SKIPPED_ROOTS:
                    continue
                if name in {".venv", "node_modules"}:
                    continue
                if (current_path / name).is_symlink():
                    target = outputs if inside_output else files
                    target[(relative_parent / name).as_posix()] = _digest(
                        current_path / name
                    )
                    continue
                kept.append(name)
            directories[:] = kept
            for name in names:
                path = current_path / name
                if not (path.is_symlink() or path.is_file()):
                    continue
                relative = (relative_parent / name).as_posix()
                is_output = inside_output or name in DECLARED_OUTPUT_FILES
                (outputs if is_output else files)[relative] = _digest(path)
    except OSError as exc:
        raise FileStateError(f"file state could not be read: {exc}") from exc
    return files, outputs


def _git_listing(root: Path) -> bytes | None:
    """Return the NUL-separated Git file list, or ``None`` when Git cannot list the files."""

    try:
        completed = subprocess.run(
            ("git", "ls-files", "-co", "--exclude-standard", "-z"),
            cwd=root,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


def _scan(
    repo_root: Path, inventory: Inventory | None = None
) -> tuple[dict[str, str], dict[str, str], tuple[str, ...], Inventory]:
    """Scan files with the baseline method to avoid false changes.

    :param inventory: Scan method of the baseline, or ``None`` for the baseline scan.
    :return: File digests, check-output digests, check-output names, and the method.
    :raises FileStateError: If files cannot be read with the selected method.
    """

    root = repo_root.resolve()
    listing = None if inventory == "filesystem" else _git_listing(root)
    if listing is None:
        if inventory == "git":
            raise FileStateError(
                "file state could not be read: Git could not list the files, "
                "and the baseline used Git"
            )
        files, outputs = _scan_filesystem(root)
        return files, outputs, DECLARED_OUTPUTS, "filesystem"

    files: dict[str, str] = {}
    try:
        for entry in listing.split(b"\0"):
            if not entry:
                continue
            relative = os.fsdecode(entry)
            if relative == "runs" or relative.startswith("runs/"):
                continue
            if any(
                (root / parent).is_symlink()
                for parent in Path(relative).parents
                if parent != Path(".")
            ):
                continue
            path = root / relative
            if path.is_symlink() or path.is_file():
                files[relative] = _digest(path)
    except OSError as exc:
        raise FileStateError(f"file state could not be read: {exc}") from exc
    return files, {}, (), "git"


def diff_snapshots(before: dict[str, str], after: dict[str, str]) -> ChangeSet:
    """Return the files added, changed, and deleted between two snapshots."""

    return ChangeSet(
        added=tuple(sorted(set(after) - set(before))),
        changed=tuple(
            sorted(p for p in set(before) & set(after) if before[p] != after[p])
        ),
        deleted=tuple(sorted(set(before) - set(after))),
    )


def preexisting_changes(repo_root: Path) -> tuple[str, ...] | None:
    """Return the files that already differ from the last commit, or ``None`` without Git."""

    try:
        completed = subprocess.run(
            ("git", "status", "--porcelain=v1", "-z", "--untracked-files=all"),
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    paths: list[str] = []
    entries = completed.stdout.split("\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            index += 1  # the original path follows a rename or copy
        paths.append(path)
    return tuple(sorted(paths))


def _write_atomically(repo_root: Path, path: Path, content: bytes, label: str) -> None:
    """Write one evidence file without leaving a partial file."""

    safe_path = require_safe_artifact_path(repo_root, path, label=label)
    safe_path.parent.mkdir(parents=True, exist_ok=True)
    write_bytes(safe_path, content)


def save_approved_plan(
    repo_root: Path, task_paths: TaskPaths, digest: str
) -> PlanDocument:
    """Save the exact ``plan.json`` content that a person approved.

    Prevent later draft edits from widening approved scopes.

    :param digest: SHA-256 digest of the approved ``plan.json``.
    :return: The saved plan.
    :raises FileStateError: If the plan does not match the digest or cannot be saved.
    """

    resolved_root = repo_root.resolve()
    try:
        # Read once, so the saved bytes are the bytes that match the digest.
        content = require_artifact_file(
            resolved_root, task_paths.canonical_plan, label="canonical plan"
        ).read_bytes()
        if sha256_bytes(content) != digest:
            raise ValueError("plan.json differs from the approved content")
        plan_text = content.decode("utf-8")
        plan = PlanDocument.model_validate_json(plan_text)
        if plan.task_id != task_paths.task_id:
            raise ValueError("the plan belongs to another task")
        _write_atomically(
            resolved_root, task_paths.approved_plan, content, "approved plan"
        )
    except (OSError, ValueError) as exc:
        raise FileStateError(f"the approved plan could not be saved: {exc}") from exc
    return plan


def save_baseline(repo_root: Path, task_paths: TaskPaths) -> Baseline:
    """Save the file baseline before coding starts.

    :param repo_root: Repository containing the run.
    :param task_paths: Paths for the current task.
    :return: The saved baseline.
    :raises FileStateError: If the file state cannot be read or saved.
    """

    resolved_root = repo_root.resolve()
    try:
        files, output_files, declared_outputs, inventory = _scan(resolved_root)
        baseline = Baseline(
            task_id=task_paths.task_id,
            files=files,
            output_files=output_files,
            preexisting_changes=preexisting_changes(resolved_root),
            declared_outputs=declared_outputs,
            inventory=inventory,
        )
        _write_atomically(
            resolved_root,
            task_paths.baseline,
            (baseline.model_dump_json(indent=2) + "\n").encode("utf-8"),
            "file baseline",
        )
    except (OSError, ValueError) as exc:
        raise FileStateError(f"the file baseline could not be saved: {exc}") from exc
    return baseline


def load_approved_plan(repo_root: Path, task_paths: TaskPaths) -> PlanDocument:
    """Load the plan copy that was saved when a person approved the plan.

    :param repo_root: Repository containing the run.
    :param task_paths: Paths for the current task.
    :return: The approved plan.
    :raises FileStateError: If the copy is missing, invalid, or belongs to another task.
    """

    try:
        plan_path = require_safe_artifact_path(
            repo_root, task_paths.approved_plan, label="approved plan"
        )
        plan = PlanDocument.model_validate_json(plan_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FileStateError(f"the approved plan could not be read: {exc}") from exc
    if plan.task_id != task_paths.task_id:
        raise FileStateError("the approved plan belongs to another task")
    return plan


def _load_baseline(repo_root: Path, task_paths: TaskPaths) -> Baseline:
    try:
        baseline_path = require_safe_artifact_path(
            repo_root, task_paths.baseline, label="file baseline"
        )
        baseline = Baseline.model_validate_json(baseline_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FileStateError(f"the saved file baseline could not be read: {exc}") from exc
    if baseline.task_id != task_paths.task_id:
        raise FileStateError("the saved file baseline belongs to another task")
    return baseline


def _load_saved(repo_root: Path, task_paths: TaskPaths) -> tuple[Baseline, PlanDocument]:
    """Load the saved baseline and approved plan."""

    baseline = _load_baseline(repo_root, task_paths)
    return baseline, load_approved_plan(repo_root, task_paths)


def _scopes(plan: PlanDocument) -> tuple[tuple[str, ...], tuple[str, ...]]:
    items = [item for step in plan.steps for item in step.work_items]
    return (
        tuple(item.path for item in items if item.scope == "file"),
        tuple(item.path for item in items if item.scope == "folder"),
    )


def _load_record[M: BaseModel](
    repo_root: Path, path: Path, label: str, model: type[M]
) -> M:
    try:
        safe_path = require_safe_artifact_path(repo_root, path, label=label)
        return model.model_validate_json(safe_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FileStateError(f"the saved {label} could not be read: {exc}") from exc


def _load_matching_record(
    repo_root: Path,
    task_paths: TaskPaths,
    attempt: CandidateAttempt,
    phase: Literal["candidate", "checks"],
) -> ChangeRecord:
    """Load the candidate or checks record of one candidate and check that it belongs to it.

    :raises FileStateError: If the record is missing, invalid, or belongs elsewhere.
    """

    path = (
        task_paths.candidate_changes(attempt)
        if phase == "candidate"
        else task_paths.check_changes(attempt)
    )
    label = f"{phase} change record"
    record = _load_record(repo_root, path, label, ChangeRecord)
    if (record.task_id, record.candidate_attempt, record.phase) != (
        task_paths.task_id,
        attempt,
        phase,
    ):
        raise FileStateError(f"the saved {label} does not match this validation")
    return record


def reference_state(
    repo_root: Path,
    task_paths: TaskPaths,
    attempt: CandidateAttempt,
    validation_round: ValidationRound,
) -> ChangeRecord | DriftRecord:
    """Return the candidate or drift record for one validation round.

    :return: The candidate record in round 1, otherwise the drift record.
    :raises FileStateError: If the record is missing, invalid, or belongs elsewhere.
    """

    resolved_root = repo_root.resolve()
    if validation_round == 1:
        return _load_matching_record(resolved_root, task_paths, attempt, "candidate")
    record = _load_record(
        resolved_root, task_paths.drift_changes(attempt), "drift record", DriftRecord
    )
    if (record.task_id, record.candidate_attempt, record.validation_round) != (
        task_paths.task_id,
        attempt,
        validation_round,
    ):
        raise FileStateError("the saved drift record does not match this validation")
    return record


def planned_intents(
    repo_root: Path,
    task_paths: TaskPaths,
    paths: tuple[str, ...],
) -> dict[str, str]:
    """Return the approved purpose of each path, or a note for an unplanned path.

    :raises FileStateError: If the approved plan cannot be read.
    """

    _, plan = _load_saved(repo_root.resolve(), task_paths)
    items = [
        item for step in plan.steps for item in step.work_items if item.scope == "file"
    ]
    return {
        path: "; ".join(item.intent for item in items if item.path == path)
        or "The approved plan does not name this file."
        for path in paths
    }


def _link_escapes(
    repo_root: Path, observed: tuple[str, ...], digests: dict[str, str]
) -> tuple[str, ...]:
    """Return the differing symlinks whose target resolves outside the repository."""

    escapes = []
    for path in observed:
        if not digests.get(path, "").startswith("link:"):
            continue
        try:
            (repo_root / path).resolve(strict=False).relative_to(repo_root)
        except (OSError, RuntimeError, ValueError):
            escapes.append(path)
    return tuple(escapes)


def _describe(label: str, paths: tuple[str, ...]) -> str:
    return f"{label}: {', '.join(paths)}"


def _build_change_record(
    repo_root: Path,
    task_paths: TaskPaths,
    baseline: Baseline,
    plan: PlanDocument,
    attempt: CandidateAttempt,
    phase: Literal["candidate", "checks"],
    current: dict[str, str],
    current_outputs: dict[str, str],
    reported_paths: tuple[str, ...],
    changed_by_checks: tuple[str, ...] = (),
) -> ChangeRecord:
    """Build a change record from one scan and the phase-specific paths."""

    changes = diff_snapshots(baseline.files, current)
    observed = changes.paths
    digests = {path: current.get(path, "deleted") for path in observed}
    files, folders = _scopes(plan)
    out_of_scope = tuple(p for p in observed if not scope_allows(p, files, folders))
    unreported = tuple(p for p in observed if p not in reported_paths)
    link_escapes = _link_escapes(repo_root, observed, digests)
    problems = []
    if out_of_scope:
        problems.append(_describe("changes outside the approved scopes", out_of_scope))
    if unreported:
        problems.append(_describe("changes the candidate did not report", unreported))
    if link_escapes:
        problems.append(_describe("links that leave the repository", link_escapes))
    if changed_by_checks:
        problems.append(
            _describe(
                "files that differ from the validated state after the checks",
                changed_by_checks,
            )
        )
    return ChangeRecord(
        task_id=task_paths.task_id,
        candidate_attempt=attempt,
        phase=phase,
        link_escapes=link_escapes,
        changes=changes,
        digests=digests,
        reported_paths=reported_paths,
        out_of_scope=out_of_scope,
        unreported=unreported,
        changed_by_checks=changed_by_checks,
        harness_changes=tuple(p for p in observed if requires_exact_file_scope(p)),
        preexisting_changes=baseline.preexisting_changes,
        declared_outputs=baseline.declared_outputs,
        output_changes=diff_snapshots(baseline.output_files, current_outputs),
        status="blocked" if problems else "pass",
        problems=tuple(problems),
    )


def verify_candidate(
    repo_root: Path,
    task_paths: TaskPaths,
    attempt: CandidateAttempt,
    reported_files: tuple[str, ...],
) -> ChangeRecord:
    """Save the candidate's file changes against the baseline and approved scopes.

    Save blocked records so the changed paths remain available for review.

    :param reported_files: Files the maker or repair reported as changed.
    :return: The saved change record.
    :raises FileStateError: If the file state or the saved baseline cannot be used.
    """

    resolved_root = repo_root.resolve()
    baseline, plan = _load_saved(resolved_root, task_paths)
    current, current_outputs, _, _ = _scan(resolved_root, baseline.inventory)
    reported = set(reported_files)
    if attempt == MAX_CANDIDATE_ATTEMPTS:
        # The checks record also holds the drift paths of a later validation round.
        reported |= set(
            _load_matching_record(resolved_root, task_paths, 1, "checks").reported_paths
        )
    record = _build_change_record(
        resolved_root,
        task_paths,
        baseline,
        plan,
        attempt,
        "candidate",
        current,
        current_outputs,
        tuple(sorted(reported)),
    )
    _save_record(resolved_root, task_paths.candidate_changes(attempt), record)
    return record


def verify_checks(
    repo_root: Path,
    task_paths: TaskPaths,
    attempt: CandidateAttempt,
    validation_round: ValidationRound,
) -> tuple[ChangeRecord, DriftRecord | None]:
    """Save file changes after checks against the baseline and round reference.

    Ignore declared check outputs when comparing files.

    :return: The saved change record and any drift record checked in the round.
    :raises FileStateError: If the file state or a saved record cannot be used.
    """

    resolved_root = repo_root.resolve()
    baseline, plan = _load_saved(resolved_root, task_paths)
    reference = reference_state(resolved_root, task_paths, attempt, validation_round)
    current, current_outputs, _, _ = _scan(resolved_root, baseline.inventory)
    changes = diff_snapshots(baseline.files, current)
    digests = {p: current.get(p, "deleted") for p in changes.paths}
    expected = reference.digests
    changed_by_checks = tuple(
        sorted(
            p for p in set(digests) | set(expected) if digests.get(p) != expected.get(p)
        )
    )
    record = _build_change_record(
        resolved_root,
        task_paths,
        baseline,
        plan,
        attempt,
        "checks",
        current,
        current_outputs,
        reference.reported_paths,
        changed_by_checks,
    )
    _save_record(resolved_root, task_paths.check_changes(attempt), record)
    return record, reference if isinstance(reference, DriftRecord) else None


def find_drift(
    repo_root: Path,
    task_paths: TaskPaths,
    attempt: CandidateAttempt,
    next_round: ValidationRound,
) -> DriftRecord | None:
    """Compare the current files with the state that the checks validated.

    Save drift so the next validation round checks files changed during approval.

    :return: The saved drift record, or ``None`` when no file changed.
    :raises FileStateError: If the file state or a saved record cannot be used.
    """

    resolved_root = repo_root.resolve()
    baseline = _load_baseline(resolved_root, task_paths)
    checks = _load_matching_record(resolved_root, task_paths, attempt, "checks")
    validated = dict(baseline.files)
    for path, digest in checks.digests.items():
        if digest == "deleted":
            validated.pop(path, None)
        else:
            validated[path] = digest
    current, _, _, _ = _scan(resolved_root, baseline.inventory)
    drift_paths = diff_snapshots(validated, current).paths
    if not drift_paths:
        return None
    record = DriftRecord(
        task_id=task_paths.task_id,
        candidate_attempt=attempt,
        validation_round=next_round,
        drift=tuple(
            FileDrift(path=p, old_digest=validated.get(p), new_digest=current.get(p))
            for p in drift_paths
        ),
        digests={
            p: current.get(p, "deleted")
            for p in diff_snapshots(baseline.files, current).paths
        },
        reported_paths=tuple(sorted({*checks.reported_paths, *drift_paths})),
    )
    _save_record(resolved_root, task_paths.drift_changes(attempt), record)
    return record


def _save_record(repo_root: Path, path: Path, record: ChangeRecord | DriftRecord) -> None:
    try:
        _write_atomically(
            repo_root,
            path,
            (record.model_dump_json(indent=2) + "\n").encode("utf-8"),
            "change record",
        )
    except (OSError, ValueError) as exc:
        raise FileStateError(f"the change record could not be saved: {exc}") from exc
