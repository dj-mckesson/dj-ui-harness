# dj-ui-harness

A copy of the v1 coding harness used in the Pricewave UI repo. A Python supervisor runs a coding task through fixed steps: spec, plan, implement, review, then tests and checks. A person approves the spec and the plan.

## Origin

- Copied from `mckesson/scm-pricinglabs-ui-pricewave` PR 44 (`feat/harness`, commit `8c82dd2`).
- That PR ports the harness from `mckesson/pricing-lab` (PR 8, Saanvi Kandala), which grew out of `mckesson/pricing-lab-harness` (Pranav Kumar).
- The browser, a11y, red-tests, task-type, and role-agent hook changes match the review suggestions on PR 44 (review `5788971735`).
- This is McKesson internal code. Keep this repository private.

## What is here

| Path | Purpose |
| --- | --- |
| `harness/` | Supervisor, contracts, gates, hooks, and checks |
| `tests/harness/` | Harness tests |
| `.agents/skills/`, `.agents/agents/` | The `harness-run` skill, the role skills, and the role personas |
| `.claude/`, `.codex/` | Edit hooks and the links that let Claude Code find the skills and personas |
| `playwright.config.ts`, `e2e/harness/` | The `smoke` and `a11y` Playwright projects behind the `browser` and `a11y` checks |
| `docs/harness.md` | Full workflow, approvals, checks, task types, and recovery |
| `docs/harness-backlog.md` | Known risks and open work |

## Use

```bash
uv sync
uv run pytest -q
```

The checks in `harness/contracts/checks.py` call `pnpm test`, `pnpm exec eslint`, `pnpm typecheck`, `ruff`, `node_modules/.bin/vitest` (`red-tests`), and `pnpm exec playwright test` (`browser` and `a11y`). They expect a Next.js app in the same repository.

The plan's `task_type` adds required checks:

| `task_type` | Added required checks |
| --- | --- |
| `feature` | `red-tests`, `browser` |
| `bug-fix` | `red-tests` |
| `ui-change` | `browser`, `a11y` |
| `refactor`, `docs`, `other` | None |

To use the browser checks in an app repository:

```bash
pnpm add -D @playwright/test @axe-core/playwright
pnpm exec playwright install chromium
```

Add `"test:e2e": "playwright test"` to the app's `package.json` scripts. The specs check every static page under `src/app`. The roles read `AGENTS.md`, `rules.md`, and `design.md` from the host repository. Copy the harness into an app repository to run a full task.

On its own, this repository passes 436 of 438 tests. Two tests check for files that the host app owns, so they fail here until the harness sits in an app repository:

- `test_actions.py::test_repository_guidance_pointers_exist` needs `CLAUDE.md` and `AGENTS.md`.
- `test_checks.py::test_registry_config_exists_in_the_repository` needs the tool configs, such as `eslint.config.mjs`.
