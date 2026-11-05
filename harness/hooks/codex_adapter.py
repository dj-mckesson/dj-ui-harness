"""Codex ``PreToolUse`` adapter for the shared protected-path policy."""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path, PureWindowsPath
from typing import Literal

from pydantic import ValidationError

from harness import NonEmptyStr, RuntimeEventModel, repository_relative_path
from harness.contracts.models import DenyResponse, HookOutput
from harness.hooks.protect_paths import evaluate_paths, load_active_run_state


class CodexToolInput(RuntimeEventModel):
    """Describe the part of a Codex event that contains an edit patch.

    :ivar command: Complete ``apply_patch`` document supplied by Codex.
    """

    command: NonEmptyStr


class CodexPreToolUseEvent(RuntimeEventModel):
    """Validate one supported Codex edit event before the patch is applied.

    :ivar hook_event_name: Runtime event handled by this adapter.
    :ivar tool_name: Only the registered ``apply_patch`` tool is supported.
    :ivar tool_input: Patch command whose file targets must be checked.
    :ivar cwd: Working directory used to resolve relative patch paths.
    """

    hook_event_name: Literal["PreToolUse"]
    tool_name: Literal["apply_patch"]
    tool_input: CodexToolInput
    cwd: NonEmptyStr


class PatchParseError(ValueError):
    """Raised when an apply_patch command is malformed or ambiguous."""


def _patch_path(header: str, prefix: str) -> str:
    """Read and validate one repository path from a patch header.

    :param header: Complete add, update, delete, or move header.
    :param prefix: Header prefix that appears before the path.
    :return: Normalized repository-relative path.
    :raises PatchParseError: If the path is empty, absolute, or malformed.
    """

    path = header.removeprefix(prefix)
    if not path:
        raise PatchParseError(f"{prefix.rstrip(': ')} requires a path")
    if PureWindowsPath(path).drive:
        raise PatchParseError("absolute patch paths are not allowed")
    try:
        return repository_relative_path(path)
    except ValueError as exc:
        raise PatchParseError(f"invalid patch path {path!r}: {exc}") from exc


def extract_patch_targets(
    command: str,
    *,
    cwd: str | Path,
    repo_root: str | Path,
) -> list[Path]:
    """Extract every affected path from one exact ``apply_patch`` document.

    The Codex adapter calls this before the shared path policy. Move operations return both the old
    and new path because both files are affected by the edit.

    :param command: Complete patch document supplied by the runtime.
    :param cwd: Working directory used for relative patch paths.
    :param repo_root: Repository that owns the current workflow.
    :return: Absolute paths affected by the patch.
    :raises PatchParseError: If the patch envelope or an operation is unsafe or unclear.
    """

    lines = command.splitlines()
    if len(lines) < 3 or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise PatchParseError(
            "apply_patch command must use one exact Begin Patch/End Patch envelope"
        )

    root = Path(repo_root)
    working_directory = Path(cwd)
    if not working_directory.is_absolute():
        raise PatchParseError("event cwd must be an absolute path")

    try:
        resolved_root = root.resolve()
        resolved_cwd = working_directory.resolve()
        resolved_cwd.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PatchParseError("event cwd must resolve inside the repository") from exc

    targets: list[Path] = []
    operation_count = 0
    line_index = 1
    while line_index < len(lines) - 1:
        line = lines[line_index]
        operation: Literal["Add", "Delete", "Update"] | None = None
        for candidate in ("Add", "Delete", "Update"):
            prefix = f"*** {candidate} File: "
            if line.startswith(prefix):
                operation = candidate
                targets.append(resolved_cwd / _patch_path(line, prefix))
                operation_count += 1
                break

        if operation == "Update" and line_index + 1 < len(lines) - 1:
            move_line = lines[line_index + 1]
            move_prefix = "*** Move to: "
            if move_line.startswith(move_prefix):
                targets.append(resolved_cwd / _patch_path(move_line, move_prefix))
                line_index += 1
        elif operation is None and line.startswith("*** "):
            raise PatchParseError(f"unknown or misplaced patch operation: {line}")

        line_index += 1

    if operation_count == 0:
        raise PatchParseError(
            "apply_patch command must contain at least one file operation"
        )
    return targets


def _deny(reason: str) -> DenyResponse:
    """Build the denial shape expected by the Codex hook runtime.

    :param reason: Plain-language reason that the patch was rejected.
    :return: Validated Codex denial response.
    """

    return DenyResponse(hookSpecificOutput=HookOutput(permissionDecisionReason=reason))


def handle_event(
    repo_root: str | Path,
    event_input: object,
) -> DenyResponse | None:
    """Check one Codex patch against the current saved agent action.

    Codex calls this before applying a registered patch. The adapter extracts every target, loads
    the complete workflow state, and asks the shared policy whether all targets are writable.

    :param repo_root: Repository that owns the workflow run.
    :param event_input: Raw Codex event payload.
    :return: ``None`` when allowed, or a denial response when the event is unsafe.
    """

    try:
        run_state = load_active_run_state(Path(repo_root))
        if run_state is None:
            return None
        event = CodexPreToolUseEvent.model_validate(event_input)
        targets = extract_patch_targets(
            event.tool_input.command,
            cwd=event.cwd,
            repo_root=repo_root,
        )
        decision = evaluate_paths(Path(repo_root), targets, run_state)
    except (OSError, RuntimeError, TypeError, ValueError, ValidationError) as exc:
        return _deny(f"protected-path hook rejected invalid input: {exc}")

    if decision.decision == "deny":
        return _deny(decision.reason)
    return None


def _parser() -> argparse.ArgumentParser:
    """Build the small command-line parser used by the Codex hook process.

    :return: Parser for the repository argument.
    """

    parser = argparse.ArgumentParser(
        description="Guard one Codex apply_patch event with the protected-path policy."
    )
    parser.add_argument("--repo-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Read one Codex event from standard input and print a denial when needed.

    The runtime starts this command for each registered patch event. Allowed events stay silent;
    denied or malformed events print the validated response expected by Codex.

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
