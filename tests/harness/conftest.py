"""Keep the harness tests apart from the project's own test run.

The harness tests run only with the harness's pytest settings
(``-c .harness/pyproject.toml``). A project's own ``pytest`` run, in its own environment, skips
this folder. This file imports nothing from ``harness`` at import time, because the project's
environment may not be able to import it.

The tests use the Next.js preset unless a test picks other settings. ``HARNESS_CONFIG`` also
reaches the hook adapters that tests start as subprocesses.
"""

import os
from pathlib import Path

import pytest

DEFAULT_TEST_PRESET = (
    Path(__file__).resolve().parents[2] / "harness" / "presets" / "nextjs-pnpm.toml"
)


def _is_harness_session(config: pytest.Config) -> bool:
    inipath = config.inipath
    return inipath is not None and inipath.parent.name == ".harness"


def pytest_configure(config: pytest.Config) -> None:
    if _is_harness_session(config):
        os.environ.setdefault("HARNESS_CONFIG", str(DEFAULT_TEST_PRESET))


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    return True if not _is_harness_session(config) else None
