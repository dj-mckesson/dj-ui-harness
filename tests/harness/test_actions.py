import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from harness.design import render_plan_markdown, validate_plan, validate_spec
from harness.execute.actions import (
    build_approval_action,
    build_checker_action,
    build_maker_action,
    build_planner_action,
    build_question_action,
    build_repair_action,
    build_spec_author_action,
)
from harness.utils.digests import sha256_bytes
from tests.harness.support import SPEC_TEXT, approve_draft_plan, plan_data

ACTION_ID = "a" * 32


def _write_repo(tmp_path: Path) -> Path:
    """Create one task with a valid spec, a valid plan, and its approved copy."""

    app = tmp_path / "src"
    app.mkdir(parents=True)
    (app / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    (app / "test_feature.py").write_text(
        "def test_value():\n    assert True\n",
        encoding="utf-8",
    )
    planning = tmp_path / "planning" / "01"
    planning.mkdir(parents=True)
    (planning / "SPEC.md").write_text(SPEC_TEXT, encoding="utf-8")
    (planning / "plan.json").write_text(json.dumps(plan_data()), encoding="utf-8")
    identifiers = validate_spec(tmp_path, "01")
    plan = validate_plan(tmp_path, "01", identifiers)
    render_plan_markdown(tmp_path, "01", plan)
    approve_draft_plan(tmp_path)
    (tmp_path / "runs" / "01" / "reviews").mkdir(parents=True)
    (tmp_path / "runs" / "01" / "validation").mkdir()
    return tmp_path


def test_spec_and_planner_actions_have_exact_author_outputs(tmp_path: Path) -> None:
    repo = _write_repo(tmp_path)
    spec = build_spec_author_action(
        repo,
        "01",
        "Add the approved behavior.",
        action_id=ACTION_ID,
    )
    planner = build_planner_action(
        repo,
        "01",
        1,
        spec_approved=True,
        action_id="b" * 32,
    )

    assert spec.writable_paths == ("planning/01/SPEC.md",)
    assert spec.required_input_paths == ("src",)
    assert planner.writable_paths == ("planning/01/plan.json",)
    assert planner.required_input_paths == (
        "planning/01/SPEC.md",
        "src",
    )
    assert planner.context.permitted_work_paths == (".",)  # type: ignore[union-attr]


def test_planner_requires_spec_approval_and_has_one_fixed_application_scope(
    tmp_path: Path,
) -> None:
    repo = _write_repo(tmp_path)
    with pytest.raises(ValueError, match="must be approved"):
        build_planner_action(repo, "01", 1, spec_approved=False)

    with pytest.raises(ValidationError, match="Unexpected keyword argument"):
        build_planner_action(
            repo,
            "01",
            1,
            spec_approved=True,
            permitted_work_paths=("src/feature.py",),  # type: ignore[call-arg]
        )


def test_maker_checker_and_repair_paths_come_from_the_approved_plan(
    tmp_path: Path,
) -> None:
    repo = _write_repo(tmp_path)
    draft = repo / "planning" / "01" / "plan.json"
    edited = json.loads(draft.read_text(encoding="utf-8"))
    edited["steps"][0]["work_items"].append(
        {"path": "src/extra.py", "intent": "Widen the scope."}
    )
    draft.write_text(json.dumps(edited), encoding="utf-8")
    common = {
        "repo_root": repo,
        "task_id": "01",
        "spec_revision": 1,
        "plan_revision": 1,
        "spec_approved": True,
        "plan_approved": True,
    }
    maker = build_maker_action(**common, action_id=ACTION_ID)
    checker = build_checker_action(
        **common,
        candidate_attempt=1,
        candidate_summary="Implemented the planned behavior and test.",
        action_id="b" * 32,
    )
    repair = build_repair_action(**common, action_id="c" * 32)

    work_paths = (
        "src/feature.py",
        "src/test_feature.py",
    )
    assert maker.candidate_attempt == 1
    assert maker.writable_paths == work_paths
    assert maker.required_input_paths[:2] == (
        "planning/01/SPEC.md",
        "runs/01/changes/approved-plan.json",
    )
    assert checker.candidate_attempt == 1
    assert checker.writable_paths == ()
    assert checker.required_input_paths[:2] == maker.required_input_paths[:2]
    assert all(path in checker.required_input_paths for path in work_paths)
    assert checker.required_input_paths[-1] == "src/test_feature.py"
    assert "runs/01/validation/results.json" not in checker.required_input_paths
    assert repair.candidate_attempt == 2
    assert repair.writable_paths == work_paths
    assert repair.required_input_paths[:2] == maker.required_input_paths[:2]
    assert repair.required_input_paths[-2:] == (
        "runs/01/reviews/checker-1.json",
        "runs/01/validation/results.json",
    )

    for action in (maker, checker, repair):
        assert "src/extra.py" not in action.required_input_paths
        assert "runs/01/changes/approved-plan.json" in action.required_input_paths
        assert "planning/01/plan.json" not in action.required_input_paths
        serialized = action.model_dump(mode="json")
        assert "command" not in json.dumps(serialized).lower()


def test_candidate_actions_require_the_approved_plan(tmp_path: Path) -> None:
    repo = _write_repo(tmp_path)
    (repo / "runs" / "01" / "changes" / "approved-plan.json").unlink()

    with pytest.raises(ValueError, match="approved plan could not be read"):
        build_maker_action(repo, "01", 1, 1, spec_approved=True, plan_approved=True)


@pytest.mark.parametrize(
    ("spec_approved", "plan_approved"), [(False, True), (True, False)]
)
def test_candidate_actions_require_both_current_approvals(
    tmp_path: Path,
    spec_approved: bool,
    plan_approved: bool,
) -> None:
    repo = _write_repo(tmp_path)
    with pytest.raises(ValueError, match="must be approved"):
        build_maker_action(
            repo,
            "01",
            1,
            1,
            spec_approved=spec_approved,
            plan_approved=plan_approved,
        )


def test_question_and_approval_builders_include_human_review_context(
    tmp_path: Path,
) -> None:
    repo = _write_repo(tmp_path)
    question = build_question_action(
        "01",
        "plan",
        ("Should the test cover missing input?",),
        action_id=ACTION_ID,
    )
    spec_approval = build_approval_action(
        repo,
        "01",
        "spec",
        1,
        action_id="b" * 32,
    )
    plan_approval = build_approval_action(
        repo,
        "01",
        "plan",
        1,
        notice="planning/01/plan.json changed.",
        action_id="c" * 32,
    )

    assert question.artifact == "plan"
    assert spec_approval.review_paths == ("planning/01/SPEC.md",)
    assert spec_approval.review_digests == {
        "planning/01/SPEC.md": sha256_bytes(SPEC_TEXT.encode())
    }
    assert spec_approval.notice is None
    assert plan_approval.notice == "planning/01/plan.json changed."
    assert plan_approval.review_digests == {
        path: sha256_bytes((repo / path).read_bytes())
        for path in plan_approval.review_paths
    }
    assert plan_approval.review_paths == (
        "planning/01/plan.json",
        "planning/01/PLAN.md",
    )
    assert set(plan_approval.model_dump()) == {
        "kind",
        "action_id",
        "task_id",
        "artifact",
        "revision",
        "review_paths",
        "review_digests",
        "notice",
        "planned_files",
        "planned_folders",
        "harness_paths",
        "whole_repository",
        "harness_intents",
        "drift",
    }


def test_actions_reject_invalid_candidate_and_symlink_context(
    tmp_path: Path,
) -> None:
    repo = _write_repo(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (repo / "linked-context").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        build_spec_author_action(
            repo,
            "01",
            "Add behavior.",
            context_paths=("linked-context",),
        )

    with pytest.raises(ValidationError):
        build_checker_action(
            repo,
            "01",
            1,
            1,
            3,
            "Review candidate 3.",
            spec_approved=True,
            plan_approved=True,
        )


def test_the_agents_section_template_points_to_docs_that_ship_with_the_harness() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    text = (repo_root / "harness" / "templates" / "agents-section.md").read_text(
        encoding="utf-8"
    )
    assert text.startswith("## Harness\n")
    pointers = re.findall(r"\]\(([^)]+)\)", text)
    assert pointers == ["docs/harness.md", "docs/harness-backlog.md"]
    for pointer in pointers:
        assert (repo_root / pointer).is_file(), (
            f"the template points to a missing file: {pointer}"
        )


@pytest.mark.parametrize("persona", ["spec-author", "planner", "maker", "checker"])
def test_claude_code_finds_each_persona(persona: str) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    link = repo_root / ".claude" / "agents" / f"{persona}.md"
    assert link.is_symlink()
    assert (
        link.resolve() == (repo_root / ".agents" / "agents" / f"{persona}.md").resolve()
    )
