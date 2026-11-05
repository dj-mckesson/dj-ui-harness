import json
import re
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from harness.contracts.actions import AgentAction, ApprovalAction
from harness.contracts.changes import Baseline
from harness.contracts.results import (
    ApprovalResponse,
    AuthorResult,
    CheckerResult,
    MakerResult,
)
from harness.contracts.state import RunState
from harness.contracts.validation import ValidationResultsDocument

ACTION_ID = "a" * 32
DIGEST = "0" * 64
SKILL_ROOT = Path(__file__).resolve().parents[2] / ".agents" / "skills"


def _agent_action(role: str) -> dict[str, object]:
    """Return one valid action for the requested workflow role."""

    details = {
        "spec-author": {
            "persona_id": "spec-author",
            "skill_id": "spec",
            "candidate_attempt": None,
            "writable_paths": ["planning/01/SPEC.md"],
            "context": {
                "context_type": "spec-author",
                "original_request": "Add one behavior.",
            },
        },
        "planner": {
            "persona_id": "planner",
            "skill_id": "planner",
            "candidate_attempt": None,
            "writable_paths": ["planning/01/plan.json"],
            "context": {
                "context_type": "planner",
                "spec_revision": 1,
                "permitted_work_paths": ["src"],
            },
        },
        "maker": {
            "persona_id": "maker",
            "skill_id": "maker",
            "candidate_attempt": 1,
            "writable_paths": ["src/feature.py"],
            "context": {
                "context_type": "candidate",
                "spec_revision": 1,
                "plan_revision": 1,
            },
        },
        "repair": {
            "persona_id": "maker",
            "skill_id": "maker",
            "candidate_attempt": 2,
            "writable_paths": ["src/feature.py"],
            "context": {
                "context_type": "candidate",
                "spec_revision": 1,
                "plan_revision": 1,
            },
        },
        "checker": {
            "persona_id": "checker",
            "skill_id": "checker",
            "candidate_attempt": 1,
            "writable_paths": [],
            "context": {
                "context_type": "checker",
                "spec_revision": 1,
                "plan_revision": 1,
                "candidate_summary": "Implemented the approved behavior.",
            },
        },
    }[role]
    return {
        "kind": "agent",
        "action_id": ACTION_ID,
        "task_id": "01",
        "role": role,
        "persona_id": details["persona_id"],
        "skill_id": details["skill_id"],
        "candidate_attempt": details["candidate_attempt"],
        "required_input_paths": ["src/feature.py"],
        "writable_paths": details["writable_paths"],
        "context": details["context"],
    }


def _state_for_role(role: str) -> dict[str, object]:
    """Return valid workflow state with one pending action for a role."""

    stage = {
        "spec-author": "spec-authoring",
        "planner": "plan-authoring",
        "maker": "implementation",
        "repair": "repair",
        "checker": "review",
    }[role]
    plan_started = role not in {"spec-author", "planner"}
    return {
        "task_id": "01",
        "original_request": "Add one behavior.",
        "stage": stage,
        "spec_revision": 0 if role == "spec-author" else 1,
        "spec_approved": role != "spec-author",
        "spec_digest": None if role == "spec-author" else DIGEST,
        "plan_revision": 1 if plan_started else 0,
        "plan_approved": plan_started,
        "resolved_spec_answers": [],
        "resolved_plan_answers": [],
        "rejection_feedback": None,
        "candidate_attempt": _agent_action(role)["candidate_attempt"],
        "pending_action": _agent_action(role),
        "blocked_reason": None,
        "completion_evidence": None,
    }


@pytest.mark.parametrize(
    ("role", "field", "value"),
    [
        ("spec-author", "persona_id", "checker"),
        ("spec-author", "writable_paths", []),
        ("maker", "candidate_attempt", 2),
        ("repair", "candidate_attempt", 1),
        ("checker", "writable_paths", ["src/feature.py"]),
    ],
)
def test_agent_identity_and_write_scope_must_match_its_role(
    role: str,
    field: str,
    value: object,
) -> None:
    """Reject actions that claim a conflicting identity or permission scope."""

    action = _agent_action(role)
    action[field] = value

    with pytest.raises(ValidationError):
        AgentAction.model_validate(action)


@pytest.mark.parametrize(
    ("role", "field", "value"),
    [
        ("spec-author", "original_request", "A different request."),
        ("planner", "spec_revision", 2),
        ("maker", "spec_revision", 2),
        ("maker", "plan_revision", 2),
        ("repair", "spec_revision", 2),
        ("repair", "plan_revision", 2),
        ("checker", "spec_revision", 2),
        ("checker", "plan_revision", 2),
    ],
)
def test_saved_action_context_must_match_current_state(
    role: str,
    field: str,
    value: object,
) -> None:
    """Reject an action built from an old request, spec, or plan revision."""

    state = _state_for_role(role)
    state["pending_action"]["context"][field] = value  # type: ignore[index]

    with pytest.raises(ValidationError):
        RunState.model_validate(state)


def test_approval_requires_a_boolean_and_feedback_when_rejected() -> None:
    """Keep human approval explicit and make rejection feedback actionable."""

    response = {
        "kind": "approval-response",
        "task_id": "01",
        "action_id": ACTION_ID,
        "artifact": "spec",
        "revision": 1,
        "approved": 1,
        "feedback": None,
    }
    with pytest.raises(ValidationError):
        ApprovalResponse.model_validate(response)

    response["approved"] = False
    with pytest.raises(ValidationError, match="requires feedback"):
        ApprovalResponse.model_validate(response)


def test_stage_and_terminal_evidence_must_describe_one_consistent_run() -> None:
    """Reject state that mixes stages, approvals, candidates, or evidence."""

    wrong_stage = _state_for_role("spec-author")
    wrong_stage.update(
        {
            "stage": "plan-authoring",
            "spec_revision": 1,
            "spec_approved": True,
            "spec_digest": DIGEST,
        }
    )
    with pytest.raises(ValidationError, match="pending action"):
        RunState.model_validate(wrong_stage)

    complete = _state_for_role("repair")
    complete.update(
        {
            "stage": "complete",
            "pending_action": None,
            "completion_evidence": {
                "candidate_attempt": 2,
                "checker_evidence_path": "runs/01/reviews/checker-2.json",
                "validation_evidence_path": "runs/01/validation/results.json",
            },
        }
    )
    RunState.model_validate(complete)

    mismatched = deepcopy(complete)
    mismatched["completion_evidence"]["checker_evidence_path"] = (  # type: ignore[index]
        "runs/01/reviews/checker-1.json"
    )
    with pytest.raises(ValidationError, match="current checker evidence"):
        RunState.model_validate(mismatched)


def test_copyable_skill_results_match_runtime_contracts() -> None:
    """Keep JSON examples that learners copy compatible with the supervisor."""

    cases = (
        ("spec", AuthorResult),
        ("planner", AuthorResult),
        ("maker", MakerResult),
        ("checker", CheckerResult),
    )
    for skill_name, model in cases:
        content = (SKILL_ROOT / skill_name / "SKILL.md").read_text(encoding="utf-8")
        examples = re.findall(r"```json\n(.*?)\n```", content, flags=re.DOTALL)
        result_examples = [
            json.loads(example)
            for example in examples
            if '"status"' in example or '"result"' in example
        ]
        assert result_examples
        for example in result_examples:
            serialized = json.dumps(example)
            serialized = serialized.replace("<supervisor-supplied task ID>", "01")
            serialized = serialized.replace("<supervisor-supplied action ID>", ACTION_ID)
            serialized = serialized.replace("<task_id>", "01")
            model.model_validate_json(serialized)


def test_claude_code_finds_the_harness_run_skill_through_a_link() -> None:
    link = SKILL_ROOT.parents[1] / ".claude" / "skills" / "harness-run"

    assert link.is_symlink()
    assert (link / "SKILL.md").read_text(encoding="utf-8") == (
        SKILL_ROOT / "harness-run" / "SKILL.md"
    ).read_text(encoding="utf-8")


def test_baseline_requires_a_known_scan_method() -> None:
    fields = {
        "task_id": "01",
        "files": {},
        "preexisting_changes": None,
        "declared_outputs": [],
    }

    assert Baseline.model_validate({**fields, "inventory": "git"}).inventory == "git"
    with pytest.raises(ValidationError, match="inventory"):
        Baseline.model_validate(fields)
    with pytest.raises(ValidationError, match="inventory"):
        Baseline.model_validate({**fields, "inventory": "svn"})


def test_spec_and_plan_approvals_need_one_digest_for_each_review_path() -> None:
    spec = {
        "action_id": ACTION_ID,
        "task_id": "01",
        "artifact": "spec",
        "revision": 1,
        "review_paths": ["planning/01/SPEC.md"],
    }
    plan = {
        **spec,
        "artifact": "plan",
        "review_paths": ["planning/01/plan.json", "planning/01/PLAN.md"],
    }

    ApprovalAction.model_validate(
        {**spec, "review_digests": {"planning/01/SPEC.md": DIGEST}}
    )
    ApprovalAction.model_validate(
        {**plan, "review_digests": dict.fromkeys(plan["review_paths"], DIGEST)}
    )
    for action, digests in (
        (spec, {}),
        (plan, {"planning/01/plan.json": DIGEST}),
        (plan, {**dict.fromkeys(plan["review_paths"], DIGEST), "other.md": DIGEST}),
    ):
        with pytest.raises(ValidationError, match="one review digest"):
            ApprovalAction.model_validate({**action, "review_digests": digests})
    for bad_digest in ("A" * 64, "0" * 63, 7):
        with pytest.raises(ValidationError):
            ApprovalAction.model_validate(
                {**spec, "review_digests": {"planning/01/SPEC.md": bad_digest}}
            )


def test_changes_approval_carries_no_review_digests() -> None:
    changes = {
        "action_id": ACTION_ID,
        "task_id": "01",
        "artifact": "changes",
        "revision": 1,
        "review_paths": ["runs/01/changes/checks-1.json"],
        "harness_paths": ["harness/gates.py"],
        "harness_intents": {"harness/gates.py": "Adjust a gate."},
    }

    ApprovalAction.model_validate(changes)
    with pytest.raises(ValidationError, match="must not carry review digests"):
        ApprovalAction.model_validate(
            {
                **changes,
                "review_digests": {"runs/01/changes/checks-1.json": DIGEST},
            }
        )


def test_spec_digest_is_set_exactly_when_the_spec_is_approved() -> None:
    approved = _state_for_role("planner")
    RunState.model_validate(approved)

    with pytest.raises(ValidationError, match="spec_digest must be set"):
        RunState.model_validate({**approved, "spec_digest": None})
    unapproved = _state_for_role("spec-author")
    with pytest.raises(ValidationError, match="spec_digest must be set"):
        RunState.model_validate({**unapproved, "spec_digest": DIGEST})


@pytest.mark.parametrize(
    ("keys", "valid"),
    [
        ([(1, 1), (1, 2), (1, 3), (2, 1), (2, 2)], True),
        ([(1, 1), (2, 1)], True),
        ([(1, 2)], False),
        ([(1, 1), (1, 1)], False),
        ([(1, 1), (1, 3)], False),
        ([(1, 1), (2, 2)], False),
        ([(1, 1), (2, 1), (1, 2)], False),
    ],
)
def test_validation_results_add_one_round_or_the_next_candidate_at_a_time(
    keys: list[tuple[int, int]], valid: bool
) -> None:
    result = {
        "status": "pass",
        "exit_code": 0,
        "stdout": "",
        "stderr": "",
        "timed_out": False,
    }
    runs = [
        {
            "candidate_attempt": attempt,
            "validation_round": validation_round,
            "timestamp": datetime(2026, 8, 11, tzinfo=UTC),
            "result": result,
            "checks": [
                {"name": "ruff-lint", "required": True, "result": result},
                {"name": "eslint", "required": True, "result": result},
                {"name": "typecheck", "required": True, "result": result},
            ],
        }
        for attempt, validation_round in keys
    ]

    if valid:
        ValidationResultsDocument.model_validate({"task_id": "01", "runs": runs})
    else:
        with pytest.raises(ValidationError, match="must start at candidate 1, round 1"):
            ValidationResultsDocument.model_validate({"task_id": "01", "runs": runs})


def test_changes_approval_needs_protected_paths_or_drift() -> None:
    changes = {
        "action_id": ACTION_ID,
        "task_id": "01",
        "artifact": "changes",
        "revision": 1,
        "review_paths": ["runs/01/changes/checks-1.json"],
    }
    drift = [{"path": "src/feature.py", "old_digest": DIGEST, "new_digest": None}]

    assert ApprovalAction.model_validate({**changes, "drift": drift}).harness_paths == ()
    with pytest.raises(ValidationError, match="protected paths or drift"):
        ApprovalAction.model_validate(changes)
