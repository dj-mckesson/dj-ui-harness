import pytest
from pydantic import ValidationError

from harness.contracts.actions import ApprovalAction
from harness.contracts.scopes import (
    is_protected_path,
    is_within,
    requires_exact_file_scope,
    scope_allows,
)


@pytest.mark.parametrize(
    ("path", "root", "expected"),
    [
        ("a/b.py", "a", True),
        ("a", "a", True),
        ("ab/c.py", "a", False),
        ("a/b.py", ".", True),
    ],
)
def test_is_within_matches_whole_path_segments(
    path: str, root: str, expected: bool
) -> None:
    assert is_within(path, root) is expected


@pytest.mark.parametrize(
    "path", [".git", ".git/config", "runs/01/state.json", "planning/01/SPEC.md"]
)
def test_supervisor_paths_are_protected(path: str) -> None:
    assert is_protected_path(path)


@pytest.mark.parametrize("path", ["runs_extra/x.py", "planning_notes.md", "src/x.py"])
def test_ordinary_paths_are_not_protected(path: str) -> None:
    assert not is_protected_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "package.json",
        ".husky/pre-commit",
        ".ruff.toml",
        "pyproject.toml",
        "eslint.config.mjs",
        "conftest.py",
        "tests/unit/conftest.py",
        "CLAUDE.md",
        "docs/AGENTS.md",
        "harness/gates.py",
        "tests/harness/test_state.py",
        ".agents/skills/maker/SKILL.md",
        ".claude/settings.json",
        ".codex/hooks.json",
    ],
)
def test_protected_files_require_exact_scopes(path: str) -> None:
    assert requires_exact_file_scope(path)
    assert not scope_allows(path, (), (".",))
    assert scope_allows(path, (path,), (".",))


def test_similar_names_do_not_require_exact_scopes() -> None:
    for path in (
        "package.json.bak",
        ".huskyx/a.py",
        "docs/AGENTS.md.txt",
        "harness_notes.md",
        "tests/test_other.py",
        "src/a.py",
    ):
        assert not requires_exact_file_scope(path)


def test_scope_allows_named_files_and_folder_contents_only() -> None:
    files = ["README.md", "harness/gates.py"]
    folders = ["scripts"]

    assert scope_allows("README.md", files, folders)
    assert scope_allows("harness/gates.py", files, folders)
    assert scope_allows("scripts/new/tool.py", files, folders)
    assert not scope_allows("scripts", files, folders)
    assert not scope_allows("docs/a.md", files, folders)
    assert not scope_allows("harness/state.py", files, folders)


def test_root_scope_covers_everything_except_supervisor_and_harness_paths() -> None:
    assert scope_allows("docs/a.md", [], ["."])
    for path in ("runs/x", "planning/01/plan.json", ".git/HEAD", "harness/x.py"):
        assert not scope_allows(path, [], ["."])


def _approval(**fields: object) -> dict[str, object]:
    data: dict[str, object] = {
        "action_id": "a" * 32,
        "task_id": "01",
        "artifact": "plan",
        "revision": 1,
        "review_paths": ("planning/01/plan.json", "planning/01/PLAN.md"),
        **fields,
    }
    if data["artifact"] != "changes":
        data.setdefault("review_digests", dict.fromkeys(data["review_paths"], "0" * 64))  # type: ignore[arg-type]
    return data


def test_approval_action_rejects_inconsistent_scope_details() -> None:
    with pytest.raises(ValidationError, match="whole_repository must match"):
        ApprovalAction(**_approval(whole_repository=True))
    with pytest.raises(ValidationError, match="protected paths must be planned files"):
        ApprovalAction(**_approval(harness_paths=("harness/gates.py",)))
    with pytest.raises(ValidationError, match="spec approval must not carry"):
        ApprovalAction(
            **_approval(
                artifact="spec",
                review_paths=("planning/01/SPEC.md",),
                planned_files=("a.py",),
            )
        )


def test_changes_approval_needs_a_planned_intent_for_each_harness_path() -> None:
    fields = {
        "artifact": "changes",
        "revision": 1,
        "review_paths": ("runs/01/changes/checks-1.json",),
        "harness_paths": ("harness/gates.py",),
    }

    approval = ApprovalAction(
        **_approval(**fields, harness_intents={"harness/gates.py": "Adjust a gate."})
    )
    assert approval.harness_intents == {"harness/gates.py": "Adjust a gate."}
    with pytest.raises(ValidationError, match="planned intent for each protected path"):
        ApprovalAction(**_approval(**fields))
    with pytest.raises(ValidationError, match="only changes approval carries"):
        ApprovalAction(**_approval(harness_intents={"harness/gates.py": "x"}))
