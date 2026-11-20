"""Load the per-project harness settings from ``harness.toml``.

The harness itself knows nothing about a stack. ``harness.toml`` names the test commands, the
checks, the checks a task type adds, the files a plan must name one by one, and the outputs a
change scan ignores. Presets for common stacks live in ``harness/presets``.
"""

import os
import re
import tomllib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Literal

CONFIG_FILE = "harness.toml"
CONFIG_ENV = "HARNESS_CONFIG"
PRESETS_DIRECTORY = Path(__file__).resolve().parent / "presets"
# The harness package sits at the repository root of the project that uses it.
DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[1]

TaskType = Literal["feature", "bug-fix", "ui-change", "refactor", "docs", "other"]
TASK_TYPES: tuple[TaskType, ...] = (
    "feature",
    "bug-fix",
    "ui-change",
    "refactor",
    "docs",
    "other",
)
CheckKind = Literal["command", "red-tests"]
_CHECK_NAME = re.compile(r"^[a-z][a-z0-9-]*$")

# Paths the harness owns in every project. A plan names each one as a file, and a person
# approves the change. ``harness.toml`` adds the project's own tool configs and lockfiles.
BUILTIN_PROTECTED_DIRS = (
    "harness",
    "tests/harness",
    "e2e/harness",
    ".harness",
    ".agents",
    ".claude",
    ".codex",
    ".github",
    ".husky",
)
BUILTIN_PROTECTED_FILES = (CONFIG_FILE,)
BUILTIN_PROTECTED_NAMES = ("conftest.py", "CLAUDE.md", "AGENTS.md")

# Folders a filesystem scan never reads, and check outputs it never reports as changes. A Git
# scan uses the project's .gitignore instead.
BUILTIN_IGNORED_DIRS = (
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "runs",
)
BUILTIN_OUTPUT_DIRS = (
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".tox",
    ".nox",
    ".next",
    ".turbo",
    ".vite",
    "coverage",
    "htmlcov",
    "playwright-report",
    "test-results",
)
BUILTIN_OUTPUT_FILES = (
    ".DS_Store",
    ".coverage",
    "next-env.d.ts",
    "tsconfig.tsbuildinfo",
)

HARNESS_TEST_COMMAND = (
    "uv",
    "run",
    "--project",
    ".harness",
    "--no-sync",
    "python",
    "-m",
    "pytest",
    "-c",
    ".harness/pyproject.toml",
    "--rootdir",
    ".",
    "-q",
    "tests/harness",
)


class ConfigError(ValueError):
    """Report a missing or invalid ``harness.toml``."""


@dataclass(frozen=True)
class TestCommand:
    """Hold one project test command that every candidate must pass.

    :ivar name: Label shown in the saved output.
    :ivar command: Program and arguments, run without a shell.
    :ivar timeout: Seconds before the command is stopped.
    :ivar cwd: Repository-relative folder the command runs in.
    :ivar env: Extra environment variables.
    """

    name: str
    command: tuple[str, ...]
    timeout: int = 300
    cwd: str = "."
    env: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True)
class CheckSpec:
    """Hold one approved check.

    :ivar command: Program and arguments, run without a shell.
    :ivar timeout: Seconds before the command is stopped.
    :ivar files: Patterns of the changed files the check receives. ``None`` runs the check once
        on the whole repository. A pattern without ``/`` matches a file name at any depth; see
        :func:`matches`.
    :ivar cwd: Repository-relative folder the command runs in. Changed paths are passed relative
        to it, and only paths inside it are passed.
    :ivar env: Extra environment variables.
    :ivar separator: Argument placed before the changed paths, or ``None`` for none.
    :ivar kind: ``red-tests`` runs the changed test files on a clean checkout of ``HEAD`` and
        passes only when they fail there.
    :ivar failure_pattern: For ``red-tests``, output that proves tests ran and failed, as
        opposed to the runner crashing.
    :ivar link: For ``red-tests``, repository-relative folders linked into the clean checkout,
        such as ``node_modules``.
    """

    command: tuple[str, ...]
    timeout: int = 300
    files: tuple[str, ...] | None = None
    cwd: str = "."
    env: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    separator: str | None = "--"
    kind: CheckKind = "command"
    failure_pattern: str | None = None
    link: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Reject invalid check policy values."""

        if (
            not isinstance(self.command, tuple)
            or not self.command
            or not all(isinstance(part, str) and part for part in self.command)
        ):
            raise ValueError("a check command must be a non-empty tuple of strings")
        if self.timeout <= 0:
            raise ValueError("a check timeout must be positive")
        if self.files is not None and not self.files:
            raise ValueError("a check file pattern list cannot be empty")
        if self.kind == "red-tests":
            if self.files is None:
                raise ValueError("a red-tests check must name test file patterns")
            if not self.failure_pattern:
                raise ValueError("a red-tests check must set failure_pattern")
        if self.failure_pattern:
            re.compile(self.failure_pattern)

    @property
    def scope(self) -> Literal["repository", "changed-files"]:
        """Return whether the check runs once or on the changed files."""

        return "repository" if self.files is None else "changed-files"


@dataclass(frozen=True)
class ProjectConfig:
    """Hold every project-specific harness setting."""

    tests: tuple[TestCommand, ...]
    checks: Mapping[str, CheckSpec]
    mandatory: tuple[str, ...]
    task_types: Mapping[TaskType, tuple[str, ...]]
    context: tuple[str, ...] = ("src",)
    guidance: tuple[str, ...] = ("AGENTS.md",)
    protected_dirs: tuple[str, ...] = BUILTIN_PROTECTED_DIRS
    protected_files: frozenset[str] = frozenset(BUILTIN_PROTECTED_FILES)
    protected_names: frozenset[str] = frozenset(BUILTIN_PROTECTED_NAMES)
    ignored_dirs: tuple[str, ...] = BUILTIN_IGNORED_DIRS
    output_dirs: tuple[str, ...] = BUILTIN_OUTPUT_DIRS
    output_files: tuple[str, ...] = BUILTIN_OUTPUT_FILES
    source: str = "<memory>"

    def check(self, name: str) -> CheckSpec:
        """Return one approved check.

        :raises KeyError: If ``harness.toml`` does not define the check.
        """

        return self.checks[name]


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Translate a glob where ``*`` stays inside one folder and ``**`` crosses folders."""

    parts: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            parts.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("**", index):
            parts.append(".*")
            index += 2
        elif pattern[index] == "*":
            parts.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            parts.append("[^/]")
            index += 1
        else:
            parts.append(re.escape(pattern[index]))
            index += 1
    return re.compile("".join(parts) + r"\Z")


def matches(path: str, patterns: tuple[str, ...]) -> bool:
    """Return whether a repository path matches any pattern.

    A pattern without ``/`` matches the file name at any depth, like ``.gitignore``. A pattern
    with ``/`` matches the whole path: ``*`` stays inside one folder and ``**`` crosses folders,
    so ``backend/**/test_*.py`` matches ``backend/tests/test_api.py``.
    """

    name = path.rsplit("/", 1)[-1]
    return any(
        _glob_regex(pattern).match(path if "/" in pattern else name) is not None
        for pattern in patterns
    )


def _relative_dir(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{label} must be a non-empty string")
    if value == ".":
        return value
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or "\\" in value
        or str(path) != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ConfigError(f"{label} must be a normalized relative POSIX path: {value!r}")
    return value


def _strings(value: object, label: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ConfigError(f"{label} must be a list of non-empty strings")
    if not allow_empty and not value:
        raise ConfigError(f"{label} cannot be empty")
    return tuple(value)


def _env(value: object, label: str) -> Mapping[str, str]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise ConfigError(f"{label} must be a table of string values")
    return MappingProxyType(dict(value))


def _timeout(value: object, label: str, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{label} must be a positive integer")
    return value


def _only(table: dict[str, object], allowed: set[str], label: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(f"{label} has unknown keys: {', '.join(unknown)}")


def _parse_tests(raw: object) -> tuple[TestCommand, ...]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError("[[tests]] must define at least one test command")
    tests = []
    for index, entry in enumerate(raw):
        label = f"tests[{index}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{label} must be a table")
        _only(entry, {"name", "command", "timeout", "cwd", "env"}, label)
        name = entry.get("name", f"tests-{index + 1}")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"{label}.name must be a non-empty string")
        tests.append(
            TestCommand(
                name=name,
                command=_strings(
                    entry.get("command"), f"{label}.command", allow_empty=False
                ),
                timeout=_timeout(entry.get("timeout"), f"{label}.timeout", 300),
                cwd=_relative_dir(entry.get("cwd", "."), f"{label}.cwd"),
                env=_env(entry.get("env"), f"{label}.env"),
            )
        )
    names = [test.name for test in tests]
    if len(names) != len(set(names)):
        raise ConfigError("test command names must be unique")
    return tuple(tests)


def _parse_check(name: str, entry: object) -> tuple[CheckSpec, bool]:
    label = f"checks.{name}"
    if not _CHECK_NAME.match(name):
        raise ConfigError(
            f"{label}: a check name uses lowercase letters, digits, and '-'"
        )
    if not isinstance(entry, dict):
        raise ConfigError(f"{label} must be a table")
    _only(
        entry,
        {
            "command",
            "timeout",
            "files",
            "cwd",
            "env",
            "separator",
            "kind",
            "failure_pattern",
            "link",
            "mandatory",
        },
        label,
    )
    kind = entry.get("kind", "command")
    if kind not in ("command", "red-tests"):
        raise ConfigError(f"{label}.kind must be 'command' or 'red-tests'")
    files = entry.get("files")
    # TOML has no null, so an empty separator means "pass the paths with no separator".
    separator = entry.get("separator", "--")
    if not isinstance(separator, str):
        raise ConfigError(f"{label}.separator must be a string")
    separator = separator or None
    failure_pattern = entry.get("failure_pattern")
    if failure_pattern is not None and not isinstance(failure_pattern, str):
        raise ConfigError(f"{label}.failure_pattern must be a string")
    mandatory = entry.get("mandatory", False)
    if not isinstance(mandatory, bool):
        raise ConfigError(f"{label}.mandatory must be true or false")
    try:
        spec = CheckSpec(
            command=_strings(entry.get("command"), f"{label}.command", allow_empty=False),
            timeout=_timeout(entry.get("timeout"), f"{label}.timeout", 300),
            files=None
            if files is None
            else _strings(files, f"{label}.files", allow_empty=False),
            cwd=_relative_dir(entry.get("cwd", "."), f"{label}.cwd"),
            env=_env(entry.get("env"), f"{label}.env"),
            separator=separator,
            kind=kind,
            failure_pattern=failure_pattern,
            link=tuple(
                _relative_dir(item, f"{label}.link")
                for item in _strings(entry.get("link", []), f"{label}.link")
            ),
        )
    except (ValueError, re.error) as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError(f"{label}: {exc}") from exc
    return spec, mandatory


def parse_config(data: Mapping[str, object], source: str = "<memory>") -> ProjectConfig:
    """Build and check the settings from parsed TOML data.

    :raises ConfigError: If a setting is missing or invalid.
    """

    table = dict(data)
    _only(
        table,
        {"tests", "checks", "task_types", "context", "guidance", "protected", "changes"},
        source,
    )
    tests = _parse_tests(table.get("tests"))

    raw_checks = table.get("checks", {})
    if not isinstance(raw_checks, dict):
        raise ConfigError("[checks] must be a table of check tables")
    checks: dict[str, CheckSpec] = {}
    mandatory: list[str] = []
    for name, entry in raw_checks.items():
        spec, is_mandatory = _parse_check(name, entry)
        checks[name] = spec
        if is_mandatory:
            mandatory.append(name)

    raw_types = table.get("task_types", {})
    if not isinstance(raw_types, dict):
        raise ConfigError("[task_types] must be a table")
    unknown_types = sorted(set(raw_types) - set(TASK_TYPES))
    if unknown_types:
        raise ConfigError(
            f"[task_types] has unknown task types: {', '.join(unknown_types)}; "
            f"use {', '.join(TASK_TYPES)}"
        )
    task_types: dict[TaskType, tuple[str, ...]] = {}
    for task_type in TASK_TYPES:
        names = _strings(raw_types.get(task_type, []), f"task_types.{task_type}")
        for name in names:
            if name not in checks:
                raise ConfigError(
                    f"task_types.{task_type} names an undefined check: {name}"
                )
            if name in mandatory:
                raise ConfigError(
                    f"task_types.{task_type} names {name}, which is already mandatory"
                )
        if len(names) != len(set(names)):
            raise ConfigError(f"task_types.{task_type} repeats a check")
        task_types[task_type] = names

    context = tuple(
        _relative_dir(item, "context")
        for item in _strings(table.get("context", ["src"]), "context", allow_empty=False)
    )
    guidance = tuple(
        _relative_dir(item, "guidance")
        for item in _strings(table.get("guidance", ["AGENTS.md"]), "guidance")
    )

    protected = table.get("protected", {})
    if not isinstance(protected, dict):
        raise ConfigError("[protected] must be a table")
    _only(protected, {"files", "dirs", "names"}, "[protected]")
    changes = table.get("changes", {})
    if not isinstance(changes, dict):
        raise ConfigError("[changes] must be a table")
    _only(changes, {"ignore_dirs", "output_dirs", "output_files"}, "[changes]")

    def merged(base: tuple[str, ...], extra: object, label: str) -> tuple[str, ...]:
        values = [*base]
        for item in _strings(extra if extra is not None else [], label):
            if item not in values:
                values.append(item)
        return tuple(values)

    return ProjectConfig(
        tests=tests,
        checks=MappingProxyType(checks),
        mandatory=tuple(mandatory),
        task_types=MappingProxyType(task_types),
        context=context,
        guidance=guidance,
        protected_dirs=tuple(
            _relative_dir(item, "protected.dirs")
            for item in merged(
                BUILTIN_PROTECTED_DIRS, protected.get("dirs"), "protected.dirs"
            )
        ),
        protected_files=frozenset(
            _relative_dir(item, "protected.files")
            for item in merged(
                BUILTIN_PROTECTED_FILES, protected.get("files"), "protected.files"
            )
        ),
        protected_names=frozenset(
            merged(BUILTIN_PROTECTED_NAMES, protected.get("names"), "protected.names")
        ),
        ignored_dirs=merged(
            BUILTIN_IGNORED_DIRS, changes.get("ignore_dirs"), "changes.ignore_dirs"
        ),
        output_dirs=merged(
            BUILTIN_OUTPUT_DIRS, changes.get("output_dirs"), "changes.output_dirs"
        ),
        output_files=merged(
            BUILTIN_OUTPUT_FILES, changes.get("output_files"), "changes.output_files"
        ),
        source=source,
    )


def load_config(path: Path) -> ProjectConfig:
    """Read and check one ``harness.toml``.

    :raises ConfigError: If the file is missing, unreadable, or invalid.
    """

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(
            f"{path} is missing. Create it with "
            "`uv run --project .harness python -m harness.install . --preset <name>`, "
            "or copy one from harness/presets."
        ) from exc
    except OSError as exc:
        raise ConfigError(f"{path} could not be read: {exc}") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    return parse_config(data, str(path))


def load_preset(name: str) -> ProjectConfig:
    """Read one preset that ships with the harness.

    :raises ConfigError: If the preset does not exist or is invalid.
    """

    path = PRESETS_DIRECTORY / f"{name}.toml"
    if not path.is_file():
        raise ConfigError(
            f"unknown preset {name!r}; choose one of {', '.join(preset_names())}"
        )
    return load_config(path)


def preset_names() -> tuple[str, ...]:
    """Return the names of the presets that ship with the harness."""

    return tuple(sorted(path.stem for path in PRESETS_DIRECTORY.glob("*.toml")))


_override: ProjectConfig | None = None
_cache: dict[Path, ProjectConfig] = {}


def config_path() -> Path:
    """Return the ``harness.toml`` the harness uses."""

    configured = os.environ.get(CONFIG_ENV)
    return Path(configured) if configured else DEFAULT_REPO_ROOT / CONFIG_FILE


def project_config() -> ProjectConfig:
    """Return the settings of the project the harness is installed in.

    :raises ConfigError: If ``harness.toml`` is missing or invalid.
    """

    if _override is not None:
        return _override
    path = config_path().resolve()
    if path not in _cache:
        _cache[path] = load_config(path)
    return _cache[path]


@contextmanager
def use_config(config: ProjectConfig) -> Iterator[ProjectConfig]:
    """Use other settings inside a block, mainly for tests."""

    global _override
    previous = _override
    _override = config
    try:
        yield config
    finally:
        _override = previous
