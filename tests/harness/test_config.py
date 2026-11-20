"""The harness reads every stack-specific setting from ``harness.toml``."""

import tomllib
from importlib import import_module
from pathlib import Path

import pytest
from pydantic import ValidationError

from harness.config import (
    BUILTIN_PROTECTED_DIRS,
    ConfigError,
    load_config,
    load_preset,
    matches,
    parse_config,
    use_config,
)
from harness.contracts.plan import PlanDocument
from harness.contracts.scopes import requires_exact_file_scope
from harness.contracts.validation import ValidationCommandResult
from harness.feedback.changes import _scan_filesystem
from harness.feedback.run_tests import run_tests
from tests.harness.support import TASK_ID, plan_data, write_approved_plan

runner = import_module("harness.feedback.run_tests")
PASS = ValidationCommandResult(
    status="pass", exit_code=0, stdout="ok", stderr="", timed_out=False
)
MINIMAL = {"tests": [{"command": ["make", "test"]}]}


def _config(text: str):
    return parse_config(tomllib.loads(text), "test.toml")


class _Recorder:
    def __init__(self, failing: str | None = None) -> None:
        self.calls: list[tuple[Path, tuple[str, ...], dict[str, str] | None]] = []
        self.failing = failing

    def __call__(
        self,
        cwd: Path,
        command: tuple[str, ...],
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> ValidationCommandResult:
        self.calls.append((cwd, command, env))
        if self.failing and self.failing in " ".join(command):
            return ValidationCommandResult(
                status="fail", exit_code=1, stdout="bad", stderr="", timed_out=False
            )
        return PASS


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config_text: str,
    digests: dict[str, str],
    *,
    task_type: str = "other",
    failing: str | None = None,
):
    repo_root = tmp_path / "repo"
    recorder = _Recorder(failing)
    monkeypatch.setattr(runner, "_execute", recorder)
    with use_config(_config(config_text)):
        write_approved_plan(repo_root, {**plan_data(), "task_type": task_type})
        record = run_tests(
            repo_root, TASK_ID, 1, include_harness_tests=False, reference_digests=digests
        )
    return repo_root.resolve(), record, recorder.calls


def test_a_minimal_config_needs_only_one_test_command() -> None:
    config = parse_config(MINIMAL)

    assert config.tests[0].command == ("make", "test")
    assert config.mandatory == ()
    assert config.context == ("src",)
    assert all(config.task_types[name] == () for name in config.task_types)
    assert set(BUILTIN_PROTECTED_DIRS) <= set(config.protected_dirs)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({}, "at least one test command"),
        ({"tests": [{"command": []}]}, "cannot be empty"),
        ({**MINIMAL, "unknown": 1}, "unknown keys"),
        ({**MINIMAL, "checks": {"Bad_Name": {"command": ["x"]}}}, "lowercase"),
        ({**MINIMAL, "checks": {"lint": {"command": ["x"], "timeout": 0}}}, "positive"),
        (
            {**MINIMAL, "checks": {"lint": {"command": ["x"], "files": []}}},
            "cannot be empty",
        ),
        (
            {**MINIMAL, "checks": {"red": {"command": ["x"], "kind": "red-tests"}}},
            "red-tests",
        ),
        ({**MINIMAL, "task_types": {"feature": ["missing"]}}, "undefined check"),
        ({**MINIMAL, "task_types": {"chore": []}}, "unknown task types"),
        ({**MINIMAL, "context": ["../outside"]}, "normalized relative"),
        (
            {**MINIMAL, "checks": {"lint": {"command": ["x"], "cwd": "/abs"}}},
            "normalized",
        ),
        (
            {
                **MINIMAL,
                "checks": {"lint": {"command": ["x"], "mandatory": True}},
                "task_types": {"feature": ["lint"]},
            },
            "already mandatory",
        ),
    ],
)
def test_invalid_settings_are_rejected_with_a_reason(
    data: dict[str, object], message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_config(data)


def test_a_missing_config_says_how_to_create_one(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"harness\.install"):
        load_config(tmp_path / "harness.toml")


def test_unknown_preset_lists_the_presets() -> None:
    with pytest.raises(ConfigError, match="python-uv"):
        load_preset("cobol")


@pytest.mark.parametrize(
    ("path", "patterns", "expected"),
    [
        ("src/app/page.tsx", ("*.tsx",), True),
        ("tests/test_api.py", ("test_*.py",), True),
        ("tests/api_test.py", ("test_*.py",), False),
        ("backend/app/main.py", ("backend/**/*.py",), True),
        ("backend/main.py", ("backend/**/*.py",), True),
        ("backend/app/main.py", ("backend/*.py",), False),
        ("backend/tests/test_api.py", ("backend/**/test_*.py",), True),
        ("frontend/src/a.py", ("backend/**/*.py",), False),
        ("a.py", ("**",), True),
        ("README.md", ("*.py", "*.ts"), False),
    ],
)
def test_patterns_without_a_slash_match_the_file_name_at_any_depth(
    path: str, patterns: tuple[str, ...], expected: bool
) -> None:
    assert matches(path, patterns) is expected


def test_the_plan_accepts_only_checks_the_project_defines() -> None:
    config = _config(
        """
[[tests]]
command = ["pytest"]
[checks.mypy]
command = ["mypy"]
"""
    )
    with use_config(config):
        assert PlanDocument.model_validate(plan_data([{"name": "mypy"}])).checks
        with pytest.raises(ValidationError, match="unknown check"):
            PlanDocument.model_validate(plan_data([{"name": "eslint"}]))


def test_project_protected_files_extend_the_builtin_ones() -> None:
    config = _config(
        """
[[tests]]
command = ["pytest"]
[protected]
files = ["poetry.lock"]
dirs = ["migrations"]
names = ["settings.py"]
"""
    )
    with use_config(config):
        for path in (
            "poetry.lock",
            "migrations/0001_init.py",
            "app/settings.py",
            "harness/gates.py",
            "harness.toml",
            "AGENTS.md",
        ):
            assert requires_exact_file_scope(path), path
        assert not requires_exact_file_scope("pnpm-lock.yaml")
        assert not requires_exact_file_scope("app/views.py")


def test_a_python_project_runs_pytest_and_ruff_without_any_node_tooling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = (Path("harness/presets/python-uv.toml")).read_text(encoding="utf-8")
    _, record, calls = _run(
        tmp_path,
        monkeypatch,
        text,
        {"src/app/api.py": "changed", "web/a.ts": "changed"},
    )

    commands = [command for _, command, _ in calls]
    assert commands == [
        ("uv", "run", "--no-sync", "pytest", "-q"),
        ("uv", "run", "--no-sync", "ruff", "check", "--", "src/app/api.py"),
        ("uv", "run", "--no-sync", "pyright", "src"),
    ]
    assert not any("pnpm" in part or "npx" in part for c in commands for part in c)
    assert record.status == "pass"


def test_every_test_command_runs_in_its_folder_and_the_output_names_each(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root, record, calls = _run(
        tmp_path,
        monkeypatch,
        """
[[tests]]
name = "api"
command = ["pytest"]
cwd = "backend"
[[tests]]
name = "web"
command = ["vitest", "run"]
cwd = "frontend"
env = { CI = "1" }
""",
        {},
    )

    assert calls == [
        (repo_root / "backend", ("pytest",), None),
        (repo_root / "frontend", ("vitest", "run"), {"CI": "1"}),
    ]
    assert "== api: pytest (pass) ==" in record.result.stdout
    assert "== web: vitest run (pass) ==" in record.result.stdout


def test_the_first_failing_test_command_stops_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, record, calls = _run(
        tmp_path,
        monkeypatch,
        """
[[tests]]
name = "api"
command = ["pytest"]
[[tests]]
name = "web"
command = ["vitest"]
""",
        {},
        failing="pytest",
    )

    assert [command for _, command, _ in calls] == [("pytest",)]
    assert record.result.status == "fail"
    assert record.status == "fail"


def test_a_check_in_a_subfolder_gets_only_its_paths_relative_to_that_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root, record, calls = _run(
        tmp_path,
        monkeypatch,
        """
[[tests]]
command = ["true"]
[checks.eslint]
command = ["pnpm", "exec", "eslint"]
files = ["frontend/**/*.ts"]
cwd = "frontend"
mandatory = true
[checks.biome]
command = ["biome", "check"]
files = ["*.ts"]
separator = ""
mandatory = true
""",
        {
            "frontend/src/a.ts": "changed",
            "scripts/b.ts": "added",
            "frontend/x.ts": "deleted",
        },
    )

    assert calls[1] == (
        repo_root / "frontend",
        ("pnpm", "exec", "eslint", "--", "src/a.ts"),
        None,
    )
    assert calls[2][1] == ("biome", "check", "frontend/src/a.ts", "scripts/b.ts")
    assert [c.result.status for c in record.checks] == ["pass", "pass"]


def test_a_check_env_reaches_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, calls = _run(
        tmp_path,
        monkeypatch,
        """
[[tests]]
command = ["true"]
[checks.browser]
command = ["playwright", "test"]
env = { E2E_WEB_SERVER = "make serve" }
[task_types]
ui-change = ["browser"]
""",
        {},
        task_type="ui-change",
    )

    assert calls[-1][2] == {"E2E_WEB_SERVER": "make serve"}


FULLSTACK_RED = """
[[tests]]
command = ["true"]
[checks.red-api]
kind = "red-tests"
command = ["pytest"]
files = ["backend/**/test_*.py"]
cwd = "backend"
separator = ""
failure_pattern = '\\d+ failed'
[checks.red-web]
kind = "red-tests"
command = ["vitest", "run"]
files = ["frontend/**/*.test.ts"]
cwd = "frontend"
link = ["frontend/node_modules"]
separator = ""
failure_pattern = 'Test Files\\s+\\d+ failed'
[task_types]
bug-fix = ["red-api", "red-web"]
"""


def test_a_red_tests_check_with_no_changed_test_is_skipped_when_another_side_has_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        runner, "_run_red_tests", lambda _root, _spec, paths: seen.append(paths) or PASS
    )

    _, record, _ = _run(
        tmp_path,
        monkeypatch,
        FULLSTACK_RED,
        {"backend/tests/test_api.py": "added", "frontend/src/a.ts": "changed"},
        task_type="bug-fix",
    )

    assert seen == [("tests/test_api.py",)]
    assert [(c.name, c.result.status) for c in record.checks] == [
        ("red-api", "pass"),
        ("red-web", "skipped"),
    ]
    assert record.status == "pass"


def test_red_tests_fail_when_no_side_has_a_changed_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, record, _ = _run(
        tmp_path,
        monkeypatch,
        FULLSTACK_RED,
        {"backend/app/api.py": "changed"},
        task_type="bug-fix",
    )

    assert [c.result.status for c in record.checks] == ["fail", "fail"]
    assert record.status == "fail"


def test_the_filesystem_scan_uses_the_configured_outputs_and_skips(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("a\n", encoding="utf-8")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "bundle.js").write_text("b\n", encoding="utf-8")
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "lib.js").write_text("c\n", encoding="utf-8")
    config = _config(
        """
[[tests]]
command = ["true"]
[changes]
ignore_dirs = ["vendor"]
output_dirs = ["dist"]
"""
    )

    with use_config(config):
        files, outputs = _scan_filesystem(tmp_path)

    assert set(files) == {"src/a.py"}
    assert set(outputs) == {"dist/bundle.js"}


def test_red_tests_in_a_subfolder_run_there_on_a_clean_head_with_linked_dependencies(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "frontend" / "node_modules" / "vitest").mkdir(parents=True)
    real_execute = runner._execute
    for command in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "harness@example.com"),
        ("git", "config", "user.name", "Harness"),
        ("git", "commit", "-q", "--allow-empty", "-m", "baseline"),
    ):
        assert real_execute(repo_root, command, 30).status == "pass"
    (repo_root / "frontend" / "src").mkdir()
    (repo_root / "frontend" / "src" / "a.test.ts").write_text("new\n", encoding="utf-8")
    spec = _config(FULLSTACK_RED).check("red-web")
    seen: list[tuple[Path, tuple[str, ...], bool, str]] = []

    def run(cwd: Path, command: tuple[str, ...], timeout: int, env=None):
        if command[0] == "git":
            return real_execute(cwd, command, timeout)
        seen.append(
            (
                cwd,
                command,
                (cwd / "node_modules").is_symlink(),
                (cwd / "src" / "a.test.ts").read_text(encoding="utf-8"),
            )
        )
        return ValidationCommandResult(
            status="fail",
            exit_code=1,
            stdout=" Test Files  1 failed (1)\n",
            stderr="",
            timed_out=False,
        )

    runner._execute = run
    try:
        result = runner._run_red_tests(repo_root, spec, ("src/a.test.ts",))
    finally:
        runner._execute = real_execute

    assert result.status == "pass", result
    [(cwd, command, linked, copied)] = seen
    assert cwd.name == "frontend" and cwd.parent != repo_root
    assert command == ("vitest", "run", "src/a.test.ts")
    assert linked and copied == "new\n"
