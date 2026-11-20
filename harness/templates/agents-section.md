## Harness

Larger changes can run through a guided workflow: spec, plan, implement, review, then tests and checks, with a person approving the spec and the plan. Start it with the `harness-run` skill.

- The harness is Python code in `harness/`. It has its own environment in `.harness/` (`uv sync --project .harness`), separate from the project's own tooling.
- `harness.toml` lists the project's test commands, its checks, the checks each task type adds, and the protected files. The harness adds no coding standard of its own: its role agents follow the guidance files that `harness.toml` lists.
- A harness run uses only its four workflow personas (`spec-author`, `planner`, `maker`, `checker`).
- `harness/`, `tests/harness/`, `e2e/harness/`, `.harness/`, `.agents/`, `.claude/`, `.codex/`, `harness.toml`, and the files under `[protected]` in `harness.toml` are protected. A plan names each one as a file, and a person approves the change.
- Check the setup with `uv run --project .harness --no-sync python -m harness.doctor`.
- Run the harness's own tests with `uv run --project .harness --no-sync python -m pytest -c .harness/pyproject.toml --rootdir . -q`.

The full workflow, approvals, and recovery steps are in [`docs/harness.md`](docs/harness.md). Known risks are in [`docs/harness-backlog.md`](docs/harness-backlog.md).
