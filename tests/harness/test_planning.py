import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from harness.contracts.plan import SpecIdentifiers
from harness.design import render_plan_markdown, validate_plan, validate_spec

TASK_ID = "01"


def _spec_text() -> str:
    return """# Specification

## Objective and observable behavior
Provide the requested observable behavior.

## Boundaries, errors, and non-goals
Keep the change inside the application scope.

## Resolved decisions
The human selected the simple behavior.

## Requirements
- REQ-01: Implement the public behavior.
- REQ-02: Preserve the documented failure behavior.

## Acceptance criteria
- AC-01
  - Criterion: The public behavior is observable.
  - Evidence: A focused test passes.
- AC-02
  - Criterion: The failure is observable.
  - Evidence: A focused failure-path test passes.
"""


def _plan_data() -> dict[str, object]:
    return {
        "schema_version": 1,
        "task_id": TASK_ID,
        "steps": [
            {
                "id": "STEP-01",
                "title": "Implement behavior",
                "intent": "Implement the bounded public and failure behavior.",
                "work_items": [
                    {
                        "path": "src/feature.py",
                        "intent": "Implement the behavior.",
                    },
                    {
                        "path": "src/test_feature.py",
                        "intent": "Test the behavior.",
                    },
                ],
                "requirement_ids": ["REQ-01", "REQ-02"],
                "acceptance_ids": ["AC-01", "AC-02"],
            }
        ],
        "test_intents": {
            "AC-02": "Exercise and assert the failure path.",
            "AC-01": "Exercise and assert the public behavior.",
        },
    }


def _make_repo(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    planning_dir = repo_root / "planning" / TASK_ID
    app_dir = repo_root / "src"
    planning_dir.mkdir(parents=True)
    app_dir.mkdir(parents=True)
    (planning_dir / "SPEC.md").write_text(_spec_text(), encoding="utf-8")
    (planning_dir / "plan.json").write_text(json.dumps(_plan_data()), encoding="utf-8")
    return repo_root


def _identifiers(repo_root: Path) -> SpecIdentifiers:
    return validate_spec(repo_root, TASK_ID)


def test_validate_spec_extracts_ids_only_from_required_definition_sections(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo(tmp_path)
    spec_path = repo_root / "planning" / TASK_ID / "SPEC.md"
    spec_path.write_text(
        _spec_text().replace(
            "Provide the requested observable behavior.",
            "Provide behavior described by REQ-01 and observed by AC-01.",
        ),
        encoding="utf-8",
    )

    identifiers = validate_spec(repo_root, TASK_ID)

    assert identifiers.requirement_ids == ("REQ-01", "REQ-02")
    assert identifiers.acceptance_criterion_ids == ("AC-01", "AC-02")


@pytest.mark.parametrize("scenario", ["missing", "empty", "missing-heading"])
def test_validate_spec_rejects_missing_empty_or_incomplete_artifact(
    tmp_path: Path, scenario: str
) -> None:
    repo_root = _make_repo(tmp_path)
    spec_path = repo_root / "planning" / TASK_ID / "SPEC.md"
    if scenario == "missing":
        spec_path.unlink()
    elif scenario == "empty":
        spec_path.write_text(" \n", encoding="utf-8")
    else:
        spec_path.write_text(
            _spec_text().replace("## Resolved decisions\n", ""), encoding="utf-8"
        )

    with pytest.raises(ValueError):
        validate_spec(repo_root, TASK_ID)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("REQ-01: Implement", "REQ-1: Implement", "malformed requirement ID"),
        ("AC-01\n", "AC-one\n", "malformed acceptance-criterion ID"),
        (
            "- REQ-02: Preserve",
            "- REQ-01: Preserve",
            "duplicate requirement ID",
        ),
        ("- AC-02\n", "- AC-01\n", "duplicate acceptance-criterion ID"),
    ],
)
def test_validate_spec_rejects_malformed_and_duplicate_ids(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    repo_root = _make_repo(tmp_path)
    spec_path = repo_root / "planning" / TASK_ID / "SPEC.md"
    spec_path.write_text(_spec_text().replace(old, new), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        validate_spec(repo_root, TASK_ID)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("- REQ-02: Preserve", "- REQ 02: Preserve"),
        ("- REQ-02: Preserve", "- REQ_02: Preserve"),
        ("- REQ-02: Preserve", "- REQ-02 - Preserve"),
        ("- AC-02\n", "- AC 02\n"),
        ("- AC-02\n", "- AC:02\n"),
        ("- AC-02\n", "- AC_02\n"),
    ],
)
def test_validate_spec_rejects_every_malformed_top_level_definition(
    tmp_path: Path, old: str, new: str
) -> None:
    repo_root = _make_repo(tmp_path)
    spec_path = repo_root / "planning" / TASK_ID / "SPEC.md"
    spec_path.write_text(_spec_text().replace(old, new), encoding="utf-8")

    with pytest.raises(ValueError, match="malformed"):
        validate_spec(repo_root, TASK_ID)


@pytest.mark.parametrize("artifact", ["SPEC.md", "plan.json"])
def test_planning_reads_reject_task_parent_and_final_artifact_symlinks(
    tmp_path: Path, artifact: str
) -> None:
    repo_root = _make_repo(tmp_path)
    task_dir = repo_root / "planning" / TASK_ID
    outside = repo_root.parent / f"outside-{artifact}"
    outside.write_text(
        _spec_text() if artifact == "SPEC.md" else json.dumps(_plan_data()),
        encoding="utf-8",
    )
    artifact_path = task_dir / artifact
    artifact_path.unlink()
    artifact_path.symlink_to(outside)

    with pytest.raises(ValueError, match="must not traverse a symlink"):
        if artifact == "SPEC.md":
            validate_spec(repo_root, TASK_ID)
        else:
            validate_plan(
                repo_root,
                TASK_ID,
                SpecIdentifiers(
                    requirement_ids=("REQ-01", "REQ-02"),
                    acceptance_criterion_ids=("AC-01", "AC-02"),
                ),
            )

    external_task = repo_root.parent / f"external-task-{artifact}"
    external_task.mkdir()
    task_dir.rename(external_task / "content")
    task_dir.symlink_to(external_task / "content", target_is_directory=True)
    with pytest.raises(ValueError, match="must not traverse a symlink"):
        validate_spec(repo_root, TASK_ID)


def test_validate_plan_parses_authoritative_json_and_ignores_projection(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo(tmp_path)
    projection = repo_root / "planning" / TASK_ID / "PLAN.md"
    projection.write_text("out-of-band text that is never parsed\n", encoding="utf-8")

    plan = validate_plan(repo_root, TASK_ID, _identifiers(repo_root))

    assert plan.task_id == TASK_ID
    assert plan.steps[0].work_items[0].path == "src/feature.py"
    assert projection.read_text(encoding="utf-8").startswith("out-of-band")


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ("task", "does not match current task"),
        ("unknown-requirement", "unknown requirement IDs"),
        ("unknown-acceptance", "unknown acceptance-criterion IDs"),
        ("missing-requirement-coverage", "do not cover requirements: REQ-02"),
        ("unknown-manual-review", "manual review for unknown acceptance-criterion IDs"),
        ("missing-acceptance-coverage", "do not cover acceptance criteria"),
        ("missing-test-intent", "missing test intent"),
        ("unknown-test-intent", "test intent for unknown"),
        ("outside-repo", "normalized, relative POSIX"),
        ("supervisor", "owned by the supervisor"),
    ],
)
def test_validate_plan_rejects_contextual_mismatches(
    tmp_path: Path, mutation: str, match: str
) -> None:
    repo_root = _make_repo(tmp_path)
    data = deepcopy(_plan_data())
    if mutation == "task":
        data["task_id"] = "02"
    elif mutation == "unknown-requirement":
        data["steps"][0]["requirement_ids"] = ["REQ-99"]  # type: ignore[index]
    elif mutation == "unknown-acceptance":
        data["steps"][0]["acceptance_ids"] = ["AC-99"]  # type: ignore[index]
    elif mutation == "missing-requirement-coverage":
        data["steps"][0]["requirement_ids"] = ["REQ-01"]  # type: ignore[index]
    elif mutation == "unknown-manual-review":
        data["manual_reviews"] = [  # type: ignore[index]
            {"acceptance_id": "AC-99", "inspect": "x", "how": "y", "pass_if": "z"}
        ]
    elif mutation == "missing-acceptance-coverage":
        data["steps"][0]["acceptance_ids"] = ["AC-01"]  # type: ignore[index]
    elif mutation == "missing-test-intent":
        del data["test_intents"]["AC-02"]  # type: ignore[index]
    elif mutation == "unknown-test-intent":
        data["test_intents"]["AC-99"] = "Unknown criterion."  # type: ignore[index]
    elif mutation == "outside-repo":
        data["steps"][0]["work_items"][0]["path"] = "../outside.py"  # type: ignore[index]
    else:
        data["steps"][0]["work_items"][0]["path"] = "runs/01/state.json"  # type: ignore[index]
    (repo_root / "planning" / TASK_ID / "plan.json").write_text(
        json.dumps(data), encoding="utf-8"
    )

    with pytest.raises(ValueError, match=match):
        validate_plan(repo_root, TASK_ID, _identifiers(repo_root))


def test_validate_plan_rejects_schema_errors_and_symlink_escape(tmp_path: Path) -> None:
    repo_root = _make_repo(tmp_path)
    data = _plan_data()
    data["unexpected"] = True
    plan_path = repo_root / "planning" / TASK_ID / "plan.json"
    plan_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValidationError):
        validate_plan(repo_root, TASK_ID, _identifiers(repo_root))

    outside = repo_root / "outside"
    outside.mkdir()
    link = repo_root / "src" / "linked"
    link.symlink_to(outside, target_is_directory=True)
    escaped_data = _plan_data()
    escaped_data["steps"][0]["work_items"][0]["path"] = (  # type: ignore[index]
        "src/linked/escape.py"
    )
    plan_path.write_text(json.dumps(escaped_data), encoding="utf-8")
    with pytest.raises(ValueError, match="must not traverse a symlink"):
        validate_plan(repo_root, TASK_ID, _identifiers(repo_root))


def test_validate_plan_rejects_existing_directory_but_allows_future_file(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo(tmp_path)
    data = _plan_data()
    data["steps"][0]["work_items"][0]["path"] = "src/nested"  # type: ignore[index]
    (repo_root / "src" / "nested").mkdir()
    plan_path = repo_root / "planning" / TASK_ID / "plan.json"
    plan_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="regular file or a future file"):
        validate_plan(repo_root, TASK_ID, _identifiers(repo_root))

    data["steps"][0]["work_items"][0]["path"] = (  # type: ignore[index]
        "src/future/new_file.py"
    )
    plan_path.write_text(json.dumps(data), encoding="utf-8")
    assert validate_plan(repo_root, TASK_ID, _identifiers(repo_root)).steps


def test_render_plan_markdown_is_required_stable_projection(tmp_path: Path) -> None:
    repo_root = _make_repo(tmp_path)
    plan = validate_plan(repo_root, TASK_ID, _identifiers(repo_root))

    first_path = render_plan_markdown(repo_root, TASK_ID, plan)
    first = first_path.read_text(encoding="utf-8")
    second_path = render_plan_markdown(repo_root, TASK_ID, plan)

    assert second_path == first_path
    assert second_path.read_text(encoding="utf-8") == first
    assert first.index("`AC-01`") < first.index("`AC-02`")
    assert "## STEP-01: Implement behavior" in first
    assert "`src/feature.py`" in first


def test_render_rejects_symlinked_task_parent_and_final_projection(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo(tmp_path)
    plan = validate_plan(repo_root, TASK_ID, _identifiers(repo_root))
    projection = repo_root / "planning" / TASK_ID / "PLAN.md"
    outside = repo_root.parent / "outside-plan.md"
    outside.write_text("outside\n", encoding="utf-8")
    projection.symlink_to(outside)

    with pytest.raises(ValueError, match="must not traverse a symlink"):
        render_plan_markdown(repo_root, TASK_ID, plan)
    assert outside.read_text(encoding="utf-8") == "outside\n"


def _plan_with_items(repo_root: Path, *items: dict[str, str]) -> None:
    data = deepcopy(_plan_data())
    data["steps"][0]["work_items"] = list(items)  # type: ignore[index]
    (repo_root / "planning" / TASK_ID / "plan.json").write_text(
        json.dumps(data), encoding="utf-8"
    )


def test_validate_plan_accepts_files_outside_the_application_package(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo(tmp_path)
    _plan_with_items(
        repo_root,
        {"path": "README.md", "intent": "Document the change."},
        {"path": "tests/test_new.py", "intent": "Add a test."},
    )

    plan = validate_plan(repo_root, TASK_ID, _identifiers(repo_root))

    assert [item.path for item in plan.steps[0].work_items] == [
        "README.md",
        "tests/test_new.py",
    ]


def test_validate_plan_accepts_folder_and_whole_repository_scopes(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo(tmp_path)
    (repo_root / "scripts").mkdir()
    _plan_with_items(
        repo_root,
        {"path": "scripts", "scope": "folder", "intent": "Add a script."},
        {"path": ".", "scope": "folder", "intent": "Touch several areas."},
    )

    plan = validate_plan(repo_root, TASK_ID, _identifiers(repo_root))

    assert [item.path for item in plan.steps[0].work_items] == ["scripts", "."]


@pytest.mark.parametrize(
    ("item", "match"),
    [
        ({"path": ".", "intent": "x"}, "repository root can only be a folder"),
        ({"path": "missing", "scope": "folder", "intent": "x"}, "is not a folder"),
        ({"path": "src", "intent": "x"}, "use a folder scope"),
        ({"path": ".git", "scope": "folder", "intent": "x"}, "owned by the supervisor"),
        (
            {"path": "planning", "scope": "folder", "intent": "x"},
            "owned by the supervisor",
        ),
        ({"path": "runs/01/state.json", "intent": "x"}, "owned by the supervisor"),
        ({"path": "harness", "scope": "folder", "intent": "x"}, "protected path"),
        ({"path": ".agents/skills", "scope": "folder", "intent": "x"}, "protected path"),
        (
            {"path": ".husky", "scope": "folder", "intent": "x"},
            "protected path",
        ),
        ({"path": "/etc/passwd", "intent": "x"}, "normalized, relative"),
    ],
)
def test_validate_plan_rejects_unsafe_scopes(
    tmp_path: Path, item: dict[str, str], match: str
) -> None:
    repo_root = _make_repo(tmp_path)
    (repo_root / "harness").mkdir()
    (repo_root / ".agents" / "skills").mkdir(parents=True)
    (repo_root / "tests" / "architecture").mkdir(parents=True)
    item = {"intent": "x", **item}
    with pytest.raises((ValueError, ValidationError), match=match):
        _plan_with_items(repo_root, item)
        validate_plan(repo_root, TASK_ID, _identifiers(repo_root))


def test_validate_plan_accepts_a_named_harness_file(tmp_path: Path) -> None:
    repo_root = _make_repo(tmp_path)
    (repo_root / "harness").mkdir()
    _plan_with_items(
        repo_root, {"path": "harness/gates.py", "intent": "Adjust the gate."}
    )

    plan = validate_plan(repo_root, TASK_ID, _identifiers(repo_root))

    assert plan.steps[0].work_items[0].path == "harness/gates.py"


@pytest.mark.parametrize(
    "path", ["docs/AGENTS.md", "tests/unit/conftest.py", ".ruff.toml"]
)
def test_validate_plan_accepts_named_protected_file(tmp_path: Path, path: str) -> None:
    repo_root = _make_repo(tmp_path)
    _plan_with_items(repo_root, {"path": path, "intent": "Update the rule."})

    plan = validate_plan(repo_root, TASK_ID, _identifiers(repo_root))

    assert plan.steps[0].work_items[0].path == path


def test_validate_plan_rejects_a_scope_through_a_symlink(tmp_path: Path) -> None:
    repo_root = _make_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo_root / "linked").symlink_to(outside, target_is_directory=True)
    _plan_with_items(
        repo_root, {"path": "linked", "scope": "folder", "intent": "Follow a link."}
    )

    with pytest.raises(ValueError, match="symlink"):
        validate_plan(repo_root, TASK_ID, _identifiers(repo_root))


def test_render_shows_selected_checks_and_manual_reviews_for_approval(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo(tmp_path)
    data = deepcopy(_plan_data())
    data["checks"] = [{"name": "ruff-format", "required": False}]
    data["manual_reviews"] = [
        {
            "acceptance_id": "AC-02",
            "inspect": "docs/harness.md",
            "how": "Read the section.",
            "pass_if": "It names every check.",
        }
    ]
    (repo_root / "planning" / TASK_ID / "plan.json").write_text(
        json.dumps(data), encoding="utf-8"
    )
    plan = validate_plan(repo_root, TASK_ID, _identifiers(repo_root))

    text = render_plan_markdown(repo_root, TASK_ID, plan).read_text(encoding="utf-8")

    assert "`typecheck` — required" in text
    assert "`eslint` — required" in text
    assert "`ruff-lint` — required" in text
    assert "`ruff-format` — report only" in text
    assert "Pass if: It names every check." in text


def test_render_omits_check_and_review_sections_when_the_plan_has_none(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo(tmp_path)
    plan = validate_plan(repo_root, TASK_ID, _identifiers(repo_root))

    text = render_plan_markdown(repo_root, TASK_ID, plan).read_text(encoding="utf-8")

    assert "## Checks" in text
    assert "`ruff-lint` — required" in text
    assert "`eslint` — required" in text
    assert "`typecheck` — required" in text
    assert "## Manual reviews" not in text
