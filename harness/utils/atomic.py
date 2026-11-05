"""Replace complete files with sibling temporary files."""

import os
import tempfile
from pathlib import Path


def write_bytes(path: Path, content: bytes, *, fsync: bool = True) -> None:
    """Replace a file with complete bytes after its parent has been checked.

    :param path: Destination in an existing, checked parent directory.
    :param content: Complete file content.
    :param fsync: Flush the temporary file to storage before replacement.
    :raises OSError: If the temporary file or replacement fails.
    """

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(content)
            if fsync:
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
