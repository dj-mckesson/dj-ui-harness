import hashlib
import os
from pathlib import Path

import pytest

from harness.utils.digests import sha256_artifact, sha256_bytes


def test_artifact_digest_matches_the_file_bytes(tmp_path: Path) -> None:
    (tmp_path / "plan.json").write_bytes(b"{}\r\n")

    digest = sha256_artifact(tmp_path, tmp_path / "plan.json", label="plan")

    assert digest == hashlib.sha256(b"{}\r\n").hexdigest() == sha256_bytes(b"{}\r\n")


def test_artifact_digest_rejects_a_symlink_before_reading_it(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.json"
    outside.write_text("{}", encoding="utf-8")
    (tmp_path / "plan.json").symlink_to(outside)

    with pytest.raises(ValueError, match="must not traverse a symlink"):
        sha256_artifact(tmp_path, tmp_path / "plan.json", label="plan")


@pytest.mark.parametrize("kind", ["fifo", "folder", "missing"])
def test_artifact_digest_rejects_a_path_that_is_not_a_regular_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    path = tmp_path / "plan.json"
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "folder":
        path.mkdir()

    def no_open(*args: object, **kwargs: object) -> None:
        raise AssertionError("the guard must reject the path before it is opened")

    # Opening a FIFO blocks. This keeps a broken guard from hanging the test.
    monkeypatch.setattr(Path, "open", no_open)

    with pytest.raises(ValueError, match="not a regular file"):
        sha256_artifact(tmp_path, path, label="plan")
