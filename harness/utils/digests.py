"""Calculate SHA-256 digests of files."""

import hashlib
from pathlib import Path

from harness.utils.artifact_paths import require_artifact_file


def sha256_bytes(data: bytes) -> str:
    """Return the SHA-256 digest of some bytes.

    :param data: Bytes to hash.
    :return: The lowercase hexadecimal digest.
    """

    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of one file, read in blocks.

    The file scan uses this after it has checked the path. Use ``sha256_artifact`` for any other
    path.

    :param path: Regular file to read. The function follows a symlink.
    :return: The lowercase hexadecimal digest.
    :raises OSError: If the file cannot be read.
    """

    sha256 = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            sha256.update(block)
    return sha256.hexdigest()


def sha256_artifact(repo_root: Path, path: Path, *, label: str) -> str:
    """Return the SHA-256 digest of one harness artifact after the path checks.

    :param repo_root: Repository that owns the artifact.
    :param path: Artifact to hash.
    :param label: Name used in error messages.
    :return: The lowercase hexadecimal digest.
    :raises ValueError: If the path is unsafe, or the file is missing or is not a regular file.
    :raises OSError: If the file cannot be read.
    """

    return sha256_file(require_artifact_file(repo_root, path, label=label))
