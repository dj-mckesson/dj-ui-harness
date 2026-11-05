"""Normalize repository paths before the harness reads or writes them."""

import os
from pathlib import Path

from harness.contracts.core import repository_relative_path


def normalize_repository_path(
    repo_root: Path,
    path: object,
    *,
    label: str,
) -> tuple[Path, str]:
    """Return safe absolute and repository-relative forms of one path.

    Artifact helpers and edit hooks use this function so they apply the same basic path rules. It
    accepts existing and future files, but rejects empty values, parent traversal, repository
    escapes, and paths that pass through symlinks.

    :param repo_root: Repository that must contain the path.
    :param path: Absolute or repository-relative path to normalize.
    :param label: Short name used in error messages.
    :return: Resolved absolute path and normalized repository-relative POSIX path.
    :raises ValueError: If the path is invalid or unsafe.
    """

    if not isinstance(path, (str, os.PathLike)) or isinstance(path, bytes):
        raise ValueError(f"{label} must be a string or path-like value")
    raw_path = os.fspath(path)
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError(f"{label} must be a non-empty path")

    supplied_path = Path(raw_path)
    if ".." in supplied_path.parts:
        raise ValueError(f"{label} must not use parent traversal")

    resolved_root = repo_root.resolve()
    absolute_path = (
        supplied_path if supplied_path.is_absolute() else resolved_root / supplied_path
    )
    try:
        relative_path = absolute_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"{label} must be inside the repository") from exc
    if relative_path == Path("."):
        raise ValueError(f"{label} must identify a path below the repository root")

    normalized_path = repository_relative_path(relative_path.as_posix())
    current = resolved_root
    for part in relative_path.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"{label} must not traverse a symlink: {current}")

    try:
        resolved_path = absolute_path.resolve(strict=False)
        resolved_path.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"{label} must resolve inside the repository") from exc
    return resolved_path, normalized_path


def normalize_scope_path(
    repo_root: Path,
    path: object,
    *,
    label: str,
    allow_root: bool,
) -> tuple[Path, str]:
    """Return safe forms of a write-scope path, which may be the repository root.

    Write scopes use the same rules as other paths, except that a folder scope may be ``.``.

    :param repo_root: Repository that must contain the path.
    :param path: Absolute or repository-relative scope path.
    :param label: Short name used in error messages.
    :param allow_root: Whether ``.`` may name the repository root.
    :return: Resolved absolute path and normalized repository-relative POSIX path.
    :raises ValueError: If the path is invalid or unsafe.
    """

    if allow_root and path == ".":
        return repo_root.resolve(), "."
    return normalize_repository_path(repo_root, path, label=label)


def require_safe_artifact_path(
    repo_root: Path,
    artifact_path: Path,
    *,
    label: str,
) -> Path:
    """Return a safe repository path for a harness-owned artifact.

    The harness calls this before it reads or writes specifications, plans, rendered plans, and
    test results. The check blocks symlinks from redirecting those files outside the repository.
    The final file does not need to exist yet.

    :param repo_root: Repository that owns the artifact.
    :param artifact_path: Artifact path to check.
    :param label: Name used in error messages.
    :return: The absolute path after safety checks.
    :raises ValueError: If the path leaves the repository or uses a symlink.
    """

    absolute_path, _ = normalize_repository_path(
        repo_root,
        artifact_path,
        label=label,
    )
    return absolute_path


def require_artifact_file(
    repo_root: Path,
    artifact_path: Path,
    *,
    label: str,
) -> Path:
    """Return a safe repository path for a harness artifact that must be a regular file.

    Call this before a read. A FIFO or a device can block a read, so the check rejects them first.

    :param repo_root: Repository that owns the artifact.
    :param artifact_path: Artifact path to check.
    :param label: Name used in error messages.
    :return: The absolute path after safety checks.
    :raises ValueError: If the path is unsafe, or the file is missing or is not a regular file.
    """

    safe_path = require_safe_artifact_path(repo_root, artifact_path, label=label)
    if not safe_path.is_file():
        raise ValueError(f"{label} is missing or is not a regular file: {safe_path}")
    return safe_path
