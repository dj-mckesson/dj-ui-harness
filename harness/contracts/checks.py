"""Define the supervisor's validation check policy."""

from dataclasses import dataclass
from typing import Literal

CheckName = Literal["ruff-lint", "ruff-format", "eslint", "prettier", "typecheck"]
CheckScope = Literal["changed-python-files", "changed-web-files", "repository"]
CHECK_SCOPES: tuple[CheckScope, ...] = (
    "changed-python-files",
    "changed-web-files",
    "repository",
)
MANDATORY_CHECKS: tuple[CheckName, ...] = ("ruff-lint", "eslint", "typecheck")
PYTHON_SUFFIXES = (".py",)
WEB_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


@dataclass(frozen=True)
class CheckSpec:
    """Hold a check command, timeout, and file scope."""

    command: tuple[str, ...]
    timeout: int
    scope: CheckScope

    def __post_init__(self) -> None:
        """Reject invalid check policy values."""

        if not isinstance(self.command, tuple) or not self.command:
            raise ValueError("a check command must be a non-empty tuple")
        if self.timeout <= 0:
            raise ValueError("a check timeout must be positive")
        if self.scope not in CHECK_SCOPES:
            raise ValueError("a check scope is invalid")


APPROVED_CHECKS: dict[CheckName, CheckSpec] = {
    "ruff-lint": CheckSpec(
        ("uv", "run", "--no-sync", "ruff", "check", "--config=.ruff.toml"),
        120,
        "changed-python-files",
    ),
    "ruff-format": CheckSpec(
        ("uv", "run", "--no-sync", "ruff", "format", "--check", "--config=.ruff.toml"),
        120,
        "changed-python-files",
    ),
    "eslint": CheckSpec(
        ("pnpm", "exec", "eslint", "--no-warn-ignored"),
        180,
        "changed-web-files",
    ),
    "prettier": CheckSpec(
        ("pnpm", "exec", "prettier", "--check", "--ignore-unknown"),
        120,
        "changed-web-files",
    ),
    "typecheck": CheckSpec(("pnpm", "typecheck"), 300, "repository"),
}
