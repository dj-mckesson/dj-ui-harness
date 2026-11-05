"""Claude Code ``PreToolUse`` adapter for the protected-path policy."""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from harness import NonEmptyStr, RuntimeEventModel
from harness.contracts.models import DenyResponse, HookOutput
from harness.hooks.protect_paths import evaluate_paths, load_active_run_state

# Claude Code sends the subagent name as ``agent_type``. Only the harness roles are held to a
# run's write scopes. Other sessions may edit; the changed-file scan still reports their edits.
HARNESS_AGENT_TYPES = frozenset({"spec-author", "planner", "maker", "checker"})


class ClaudeToolInput(RuntimeEventModel):
    """Describe the file path included in a Claude Code edit event.

    :ivar file_path: Absolute file path that Claude Code wants to change.
    """

    file_path: NonEmptyStr


class ClaudePreToolUseEvent(RuntimeEventModel):
    """Validate one supported Claude Code event before a file is changed.

    :ivar hook_event_name: Runtime event handled by this adapter.
    :ivar tool_name: Registered Claude Code edit tool.
    :ivar tool_input: File path that must pass the shared policy.
    """

    hook_event_name: Literal["PreToolUse"]
    tool_name: Literal["Edit", "Write"]
    tool_input: ClaudeToolInput


def _deny(reason: str) -> DenyResponse:
    """Build the denial shape expected by the Claude Code hook runtime.

    :param reason: Plain-language reason that the edit was rejected.
    :return: Validated Claude Code denial response.
    """

    return DenyResponse(hookSpecificOutput=HookOutput(permissionDecisionReason=reason))


def handle_event(
    repo_root: str | Path,
    event_input: object,
) -> DenyResponse | None:
    """Check one Claude Code edit against the current saved agent action.

    Claude Code calls this before an ``Edit`` or ``Write`` operation. The adapter validates the
    event, loads the complete workflow state, and asks the shared policy about the target file.

    :param repo_root: Repository that owns the workflow run.
    :param event_input: Raw Claude Code event payload.
    :return: ``None`` when allowed, or a denial response when the event is unsafe.
    """

    if not _is_harness_agent(event_input):
        return None
    try:
        run_state = load_active_run_state(Path(repo_root))
        if run_state is None:
            return None
        event = ClaudePreToolUseEvent.model_validate(event_input)
        target = Path(event.tool_input.file_path)
        if not target.is_absolute():
            raise ValueError("tool_input.file_path must be an absolute path")

        decision = evaluate_paths(Path(repo_root), [target], run_state)
    except (OSError, RuntimeError, TypeError, ValueError, ValidationError) as exc:
        return _deny(f"protected-path hook rejected invalid input: {exc}")

    if decision.decision == "deny":
        return _deny(decision.reason)
    return None


def _is_harness_agent(event_input: object) -> bool:
    """Return whether the event comes from one of the harness role agents."""

    return (
        isinstance(event_input, dict)
        and event_input.get("agent_type") in HARNESS_AGENT_TYPES
    )


def _parser() -> argparse.ArgumentParser:
    """Build the small command-line parser used by the Claude Code hook process.

    :return: Parser for the repository argument.
    """

    parser = argparse.ArgumentParser(
        description=(
            "Guard one Claude Code Edit or Write event with the protected-path policy."
        )
    )
    parser.add_argument("--repo-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Read one Claude Code event and print a denial when needed.

    The runtime starts this command for each registered edit event. Allowed events stay silent;
    denied or malformed events print the validated response expected by Claude Code.

    :param argv: Optional command-line arguments used by tests or the runtime.
    :return: Zero because hook decisions are carried in the response body.
    """

    args = _parser().parse_args(argv)

    try:
        event_input = json.load(sys.stdin)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        response = _deny(f"protected-path hook rejected malformed JSON: {exc}")
    else:
        response = handle_event(args.repo_root, event_input)

    if response is not None:
        sys.stdout.write(response.model_dump_json())
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
