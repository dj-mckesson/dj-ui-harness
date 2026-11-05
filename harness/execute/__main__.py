"""Provide the small JSON command line interface for the harness workflow."""

import json
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, TextIO, cast

from pydantic import BaseModel, ValidationError

from harness.contracts.core import NonEmptyStr, RepoPath, StrictModel, TaskId
from harness.contracts.results import ErrorResponse
from harness.execute.state import StateStorageError
from harness.execute.status import resume_run, run_status
from harness.execute.supervisor import (
    SupervisorError,
    create_run,
    next_action,
    submit_result,
)

Operation = Literal["start", "next-action", "submit", "status", "resume"]

OPERATIONS: dict[str, str] = {
    "start": "start a run: repo_root, original_request, optional context_paths",
    "next-action": "return the pending action of a run: repo_root, task_id",
    "submit": "submit the result of the pending action: repo_root, task_id, result",
    "status": "report where a run stands: repo_root, optional task_id",
    "resume": "continue a saved run after a restart: repo_root, optional task_id",
}
_HELP_FLAGS = {"-h", "--help", "help"}


class _StartPayload(StrictModel):
    """Describe the JSON needed to start one workflow run."""

    repo_root: Path
    original_request: NonEmptyStr
    context_paths: tuple[RepoPath, ...] | None = None


class _TaskPayload(StrictModel):
    """Describe the JSON needed to read one saved workflow run."""

    repo_root: Path
    task_id: TaskId


class _OptionalTaskPayload(StrictModel):
    """Describe the JSON for an operation that defaults to the active or newest run."""

    repo_root: Path
    task_id: TaskId | None = None


class _UsageResponse(StrictModel):
    """List the operations when a person asks for help."""

    kind: Literal["usage"] = "usage"
    command: str = "uv run python -m harness.execute <operation> [payload-file|-]"
    operations: dict[str, str]


class _SubmitPayload(_TaskPayload):
    """Describe the JSON needed to submit one pending-action result."""

    result: dict[str, object]


class _CliError(ValueError):
    """Carry one expected CLI error code and plain message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _read_payload(source: str | None) -> object:
    """Read one JSON value from standard input or a named file.

    Every CLI operation uses this helper before calling the Python supervisor. Passing no file, or
    passing ``-``, reads standard input so another process can send one structured payload without
    creating a permanent repository file.

    :param source: JSON file path, ``-``, or ``None`` for standard input.
    :return: Decoded JSON value ready for operation-specific validation.
    :raises _CliError: If the input file is not valid UTF-8 text.
    :raises json.JSONDecodeError: If the input is not valid JSON.
    :raises OSError: If a named input file cannot be read.
    """

    try:
        text = (
            sys.stdin.read()
            if source in {None, "-"}
            else Path(source).read_text(encoding="utf-8")
        )
    except UnicodeError as exc:
        raise _CliError("invalid_json", "input is not valid UTF-8 JSON") from exc
    return json.loads(text)


def _run_operation(operation: Operation, payload: object) -> BaseModel:
    """Call the public supervisor API for one CLI operation.

    This function only translates checked JSON into existing Python calls. The supervisor and its
    routing module remain responsible for choosing actions, approvals, repairs, validation, and
    terminal results.

    :param operation: One of the three public CLI operations.
    :param payload: Decoded JSON input for that operation.
    :return: Checked pending action or terminal result from the supervisor.
    :raises pydantic.ValidationError: If the payload does not match the selected operation.
    :raises StateStorageError: If saved workflow state is missing or invalid.
    :raises SupervisorError: If a submission does not match the pending action.
    """

    if operation == "status":
        status_data = _OptionalTaskPayload.model_validate(payload)
        return run_status(status_data.repo_root, status_data.task_id)
    if operation == "resume":
        resume_data = _OptionalTaskPayload.model_validate(payload)
        return resume_run(resume_data.repo_root, resume_data.task_id)
    if operation == "start":
        if shutil.which("uv") is None:
            raise _CliError(
                "missing_dependency",
                "the test command runs through uv, but uv is not on PATH; install uv first",
            )
        data = _StartPayload.model_validate(payload)
        state = create_run(
            data.repo_root,
            data.original_request,
            context_paths=data.context_paths,
        )
        return next_action(data.repo_root, state.task_id)
    if operation == "next-action":
        data = _TaskPayload.model_validate(payload)
        return next_action(data.repo_root, data.task_id)

    data = _SubmitPayload.model_validate(payload)
    return submit_result(data.repo_root, data.task_id, data.result)


def _validation_message(error: ValidationError) -> str:
    """Return one short explanation for an invalid CLI payload.

    The command line boundary uses only the first Pydantic problem so expected user mistakes stay
    readable and do not produce a Python traceback.

    :param error: Payload validation error raised by Pydantic.
    :return: Plain message naming the invalid field when available.
    """

    first = error.errors(include_url=False)[0]
    location = ".".join(str(part) for part in first["loc"])
    prefix = f"{location}: " if location else ""
    return prefix + str(first["msg"])


def _write_json(stream: TextIO, value: BaseModel) -> None:
    """Write exactly one checked JSON value to a CLI stream.

    Success and error paths share this helper so the harness runner always receives one complete
    JSON value with a trailing newline.

    :param stream: Standard output or standard error text stream.
    :param value: Checked contract to serialize.
    """

    stream.write(value.model_dump_json() + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    """Run one JSON CLI operation and return its process exit code.

    ``python -m harness.execute`` calls this function. It validates the operation and payload,
    delegates to the public supervisor, and keeps expected errors on standard error as
    ``ErrorResponse`` JSON instead of displaying a traceback.

    :param argv: Arguments after the module name; defaults to ``sys.argv[1:]``.
    :return: Zero for success or a non-zero code for expected input and workflow errors.
    """

    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) == 1 and arguments[0] in _HELP_FLAGS:
        _write_json(sys.stdout, _UsageResponse(operations=OPERATIONS))
        return 0
    try:
        if len(arguments) not in {1, 2}:
            raise _CliError(
                "invalid_arguments",
                "usage: python -m harness.execute <"
                + "|".join(OPERATIONS)
                + "> [payload-file|-]",
            )
        operation = arguments[0]
        if operation not in OPERATIONS:
            raise _CliError(
                "invalid_arguments",
                f"unknown operation {operation!r}; use " + ", ".join(OPERATIONS),
            )
        payload = _read_payload(arguments[1] if len(arguments) == 2 else None)
        output = _run_operation(cast(Operation, operation), payload)
    except _CliError as exc:
        error = ErrorResponse(code=exc.code, message=str(exc))
    except json.JSONDecodeError as exc:
        error = ErrorResponse(
            code="invalid_json", message=f"input is not valid JSON: {exc.msg}"
        )
    except ValidationError as exc:
        error = ErrorResponse(code="invalid_input", message=_validation_message(exc))
    except StateStorageError as exc:
        error = ErrorResponse(code="invalid_state", message=str(exc))
    except SupervisorError as exc:
        error = ErrorResponse(code="invalid_submission", message=str(exc))
    except OSError as exc:
        error = ErrorResponse(code="io_error", message=str(exc))
    else:
        _write_json(sys.stdout, output)
        return 0

    _write_json(sys.stderr, error)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
