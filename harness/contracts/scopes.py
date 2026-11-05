"""Rules that decide which repository paths a write scope can cover.

Plan validation, agent actions, and edit hooks all use these rules. One shared module keeps the
three checks from drifting apart.
"""

from collections.abc import Iterable

REPOSITORY_ROOT_SCOPE = "."

# The supervisor owns these paths. A folder scope never covers them, and a plan never names them.
PROTECTED_ROOTS = (".git", "runs", "planning")

# Protected files need a named file scope. These include harness code, tests, skills, hook
# settings, and the project checks.
EXACT_FILE_ROOTS = (
    "harness",
    "tests/harness",
    ".agents",
    ".claude",
    ".codex",
    ".github",
    ".husky",
)
EXACT_FILES = {
    ".ruff.toml",
    "pyproject.toml",
    "uv.lock",
    "package.json",
    "pnpm-lock.yaml",
    "tsconfig.json",
    "eslint.config.mjs",
    "vitest.config.ts",
    "vitest.setup.ts",
    "next.config.ts",
    ".prettierrc",
}
EXACT_NAMES = {"conftest.py", "CLAUDE.md", "AGENTS.md"}


def is_within(path: str, root: str) -> bool:
    """Return whether ``path`` is ``root`` or lies below it.

    :param path: Normalized repository-relative path.
    :param root: Normalized repository-relative folder, or ``.`` for the repository root.
    :return: ``True`` when the path equals the folder or is inside it.
    """

    return root == REPOSITORY_ROOT_SCOPE or path == root or path.startswith(f"{root}/")


def is_protected_path(path: str) -> bool:
    """Return whether the supervisor owns this path."""

    return any(is_within(path, root) for root in PROTECTED_ROOTS)


def requires_exact_file_scope(path: str) -> bool:
    """Return whether a file needs an exact scope and human approval."""

    return (
        path in EXACT_FILES
        or path.rsplit("/", 1)[-1] in EXACT_NAMES
        or any(is_within(path, root) for root in EXACT_FILE_ROOTS)
    )


def scope_allows(
    target: str,
    files: Iterable[str],
    folders: Iterable[str],
) -> bool:
    """Return whether the write scopes allow an edit to ``target``.

    A named file always allows itself. A folder scope excludes supervisor paths and protected files.

    :param target: Normalized repository-relative path being edited.
    :param files: Normalized file scopes.
    :param folders: Normalized folder scopes.
    :return: ``True`` when the target is inside the scopes.
    """

    if target in set(files):
        return True
    if is_protected_path(target) or requires_exact_file_scope(target):
        return False
    return any(
        folder == REPOSITORY_ROOT_SCOPE or target.startswith(f"{folder}/")
        for folder in folders
    )
