from pathlib import Path

import pytest

from harness.utils import atomic


def test_write_bytes_fsync_option(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(atomic.os, "fsync", calls.append)
    path = tmp_path / "result.json"

    atomic.write_bytes(path, b"first", fsync=False)
    assert path.read_bytes() == b"first"
    assert calls == []

    atomic.write_bytes(path, b"second")
    assert path.read_bytes() == b"second"
    assert len(calls) == 1
