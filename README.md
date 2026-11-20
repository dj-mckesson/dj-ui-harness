# dj-ui-harness

A coding harness for any repository: a web UI, a Python service, or a full-stack project with both. A Python supervisor runs a coding task through fixed steps: spec, plan, implement, review, then tests and checks. A person approves the spec and the plan, and the supervisor compares every real file change with the approved plan.

Everything stack-specific is in one file, `harness.toml`, at the root of the project. It lists the test commands, the checks, the checks each task type adds, and the files that need a person's approval to change. Presets cover common stacks.

## Install into a project

```bash
git clone https://github.com/dj-mckesson/dj-ui-harness.git
cd dj-ui-harness
uv sync --project .harness
uv run --project .harness --no-sync python -m harness.install ../my-project --preset auto
```

Then, in the project:

```bash
uv sync --project .harness
uv run --project .harness --no-sync python -m harness.doctor
```

Fix every `fail` that `doctor` reports, review `harness.toml`, and commit the result. Run the install again later to update the harness. It keeps the project's `harness.toml`. Behind a TLS proxy, add `UV_SYSTEM_CERTS=1` before `uv sync`.

| Preset | For |
| --- | --- |
| `nextjs-pnpm` | Next.js App Router with pnpm, Vitest, ESLint, and Playwright |
| `vite-react-npm` | Vite and React with npm, Vitest, ESLint, and Playwright |
| `python-uv` | A Python project on uv with pytest, ruff, and pyright |
| `fastapi-react` | FastAPI in `backend/` (uv, pytest, ruff, pyright) and React in `frontend/` (pnpm, Vitest, ESLint, Playwright) |

`--preset auto` picks one from the project's files. For another stack, start from the closest preset and edit `harness.toml`. The format is in [`docs/harness.md`](docs/harness.md#harnesstoml).

## Run a task

Ask Claude Code or Codex to run the `harness-run` skill with your request. The full workflow, the approvals, the checks, and recovery steps are in [`docs/harness.md`](docs/harness.md). Known risks are in [`docs/harness-backlog.md`](docs/harness-backlog.md).

## What is here

| Path | Purpose |
| --- | --- |
| `harness/` | The supervisor, contracts, gates, hooks, checks, installer (`install.py`), and doctor (`doctor.py`) |
| `harness/presets/` | Starting `harness.toml` files for common stacks |
| `harness/templates/agents-section.md` | The `## Harness` section the installer adds to `AGENTS.md` |
| `tests/harness/` | Harness tests |
| `.harness/` | The harness's own Python environment, kept apart from the project's |
| `.agents/skills/`, `.agents/agents/` | The `harness-run` skill, the role skills, and the role personas |
| `.claude/`, `.codex/` | Edit hooks, and the links that let Claude Code find the skills and personas |
| `harness/templates/browser/` | The Playwright config and `e2e/harness/` specs behind the `browser` and `a11y` checks. The installer copies them into the app folder without the `.template` suffix. |
| `docs/harness.md` | Full workflow, `harness.toml`, approvals, checks, task types, and recovery |
| `docs/harness-backlog.md` | Known risks and open work |

## Develop the harness

```bash
uv sync --project .harness
uv run --project .harness --no-sync python -m pytest -c .harness/pyproject.toml --rootdir . -q
uv run --project .harness --no-sync ruff check --config .harness/ruff.toml harness tests/harness
```

The tests use the `nextjs-pnpm` preset unless a test picks other settings, so they pass in this repository and in any project the harness is installed in.

## Origin

- Started as a copy of the v1 harness in `mckesson/scm-pricinglabs-ui-pricewave` PRs 44 and 46.
- That harness ports `mckesson/pricing-lab` PR 8 (Saanvi Kandala), which grew out of `mckesson/pricing-lab-harness` (Pranav Kumar).
- This is McKesson internal code. Keep this repository private.
