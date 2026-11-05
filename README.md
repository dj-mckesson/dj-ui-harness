# dj-ui-harness

A copy of the v1 coding harness used in the Pricewave UI repo. A Python supervisor runs a coding task through fixed steps: spec, plan, implement, review, then tests and checks. A person approves the spec and the plan.

## Origin

- Copied from `mckesson/scm-pricinglabs-ui-pricewave` PR 44 (`feat/harness`, commit `8c82dd2`).
- That PR ports the harness from `mckesson/pricing-lab` (PR 8, Saanvi Kandala), which grew out of `mckesson/pricing-lab-harness` (Pranav Kumar).
- This is McKesson internal code. Keep this repository private.

## What is here

| Path | Purpose |
| --- | --- |
| `harness/` | Supervisor, contracts, gates, hooks, and checks |
| `tests/harness/` | Harness tests |
| `.agents/skills/`, `.agents/agents/` | The `harness-run` skill, the role skills, and the role personas |
| `.claude/`, `.codex/` | Edit hooks and the links that let Claude Code find the skills and personas |
| `docs/harness.md` | Full workflow, approvals, and recovery |
| `docs/harness-backlog.md` | Known risks and open work |

## Use

```bash
uv sync
uv run pytest -q
```

The checks in `harness/contracts/checks.py` call `pnpm test`, `pnpm exec eslint`, `pnpm typecheck`, and `ruff`. They expect a Next.js app in the same repository. The roles read `AGENTS.md`, `rules.md`, and `design.md` from the host repository. Copy the harness into an app repository to run a full task.

On its own, this repository passes 416 of 418 tests. Two tests check for files that the host app owns, so they fail here until the harness sits in an app repository:

- `test_actions.py::test_repository_guidance_pointers_exist` needs `CLAUDE.md` and `AGENTS.md`.
- `test_checks.py::test_registry_config_exists_in_the_repository` needs the tool configs, such as `eslint.config.mjs`.
