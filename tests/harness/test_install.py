"""Installing the harness into a project of any stack, and checking the setup."""

import json
from pathlib import Path

import pytest

from harness.config import ConfigError, load_config
from harness.doctor import diagnose
from harness.install import detect_preset, install


def _project(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "project"
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    root.mkdir(exist_ok=True)
    return root


@pytest.mark.parametrize(
    ("files", "preset"),
    [
        ({"next.config.ts": "", "package.json": "{}"}, "nextjs-pnpm"),
        ({"vite.config.ts": "", "package.json": "{}"}, "vite-react-npm"),
        ({"pyproject.toml": "[project]\nname='x'\n"}, "python-uv"),
        ({"backend/pyproject.toml": "", "frontend/package.json": "{}"}, "fastapi-react"),
    ],
)
def test_the_preset_is_detected_from_the_project_files(
    tmp_path: Path, files: dict[str, str], preset: str
) -> None:
    assert detect_preset(_project(tmp_path, files)) == preset


def test_an_unknown_project_asks_for_a_preset(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="--preset"):
        detect_preset(_project(tmp_path, {"README.md": "hi"}))


def test_a_python_project_install_keeps_its_own_pyproject_and_ruff_settings(
    tmp_path: Path,
) -> None:
    pyproject = "[project]\nname = 'api'\n\n[tool.ruff]\nline-length = 120\n"
    root = _project(
        tmp_path,
        {"pyproject.toml": pyproject, "src/api/__init__.py": "", ".gitignore": "dist/\n"},
    )

    install(root, "auto")

    assert (root / "pyproject.toml").read_text(encoding="utf-8") == pyproject
    assert not (root / ".ruff.toml").exists()
    assert (root / ".harness" / "pyproject.toml").is_file()
    assert (root / "harness" / "gates.py").is_file()
    assert not list(root.rglob("__pycache__"))
    config = load_config(root / "harness.toml")
    assert config.tests[0].command[:4] == ("uv", "run", "--no-sync", "pytest")
    assert not (root / "playwright.config.ts").exists()
    assert not (root / "e2e").exists()
    ignore = (root / ".gitignore").read_text(encoding="utf-8")
    assert (
        ignore.startswith("dist/\n")
        and "/runs/" in ignore
        and ".harness/.venv/" in ignore
    )
    assert "## Harness" in (root / "AGENTS.md").read_text(encoding="utf-8")
    assert (root / "CLAUDE.md").read_text(encoding="utf-8") == "@AGENTS.md\n"
    for skill in ("harness-run", "spec", "planner", "maker", "checker"):
        assert (root / ".claude" / "skills" / skill / "SKILL.md").is_file()


def test_install_merges_the_hook_into_existing_claude_settings(tmp_path: Path) -> None:
    settings = {
        "permissions": {"allow": ["Bash(make test)"]},
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": []}]},
    }
    root = _project(
        tmp_path,
        {"pyproject.toml": "", ".claude/settings.json": json.dumps(settings)},
    )

    install(root, "python-uv")
    install(root, "python-uv")

    merged = json.loads((root / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert merged["permissions"] == settings["permissions"]
    hooks = merged["hooks"]["PreToolUse"]
    assert hooks[0] == {"matcher": "Bash", "hooks": []}
    assert sum("harness.hooks.claude_adapter" in json.dumps(hook) for hook in hooks) == 1
    assert "--project .harness" in json.dumps(hooks)


def test_a_full_stack_install_puts_the_browser_files_in_the_frontend(
    tmp_path: Path,
) -> None:
    root = _project(
        tmp_path,
        {"backend/pyproject.toml": "", "frontend/package.json": "{}"},
    )

    install(root, "auto")

    for name in (
        "playwright.config.ts",
        "e2e/harness/routes.ts",
        "e2e/harness/a11y.spec.ts",
    ):
        assert (root / "frontend" / name).is_file(), name
    assert not (root / "e2e").exists()


def test_install_keeps_an_existing_config_and_playwright_config_without_force(
    tmp_path: Path,
) -> None:
    root = _project(
        tmp_path,
        {
            "next.config.ts": "",
            "playwright.config.ts": "// mine\n",
            "harness.toml": '[[tests]]\ncommand = ["make", "test"]\n',
        },
    )

    report = install(root, "nextjs-pnpm")

    assert "harness.toml" in report.kept
    assert load_config(root / "harness.toml").tests[0].command == ("make", "test")
    assert (root / "playwright.config.ts").read_text(encoding="utf-8") == "// mine\n"
    assert any("--force" in note for note in report.notes)


def test_the_doctor_reports_a_missing_config_and_missing_folders(tmp_path: Path) -> None:
    root = _project(tmp_path, {"README.md": ""})

    findings = diagnose(root)

    assert findings[0].level == "fail" and "harness.toml" in findings[0].message


def test_the_doctor_fails_a_test_folder_that_does_not_exist(tmp_path: Path) -> None:
    root = _project(
        tmp_path,
        {
            "harness.toml": '[[tests]]\ncommand = ["true"]\ncwd = "backend"\n',
            "src/x.py": "",
        },
    )

    messages = [(f.level, f.message) for f in diagnose(root)]

    assert ("fail", "test tests-1 runs in backend/, which does not exist") in messages
    assert any(level == "fail" and "harness environment" in m for level, m in messages)


def test_the_doctor_looks_inside_package_manager_commands(tmp_path: Path) -> None:
    root = _project(
        tmp_path,
        {
            "harness.toml": """
context = ["."]
[[tests]]
command = ["uv", "run", "--no-sync", "pytest"]
[checks.pyright]
command = ["uv", "run", "--no-sync", "pyright"]
[checks.harness-lint]
command = ["uv", "run", "--project", ".harness", "--no-sync", "ruff", "check"]
[checks.eslint]
command = ["pnpm", "exec", "eslint", "--no-warn-ignored"]
[checks.typecheck]
command = ["pnpm", "typecheck"]
""",
            "package.json": '{"scripts": {"test": "vitest"}}',
            ".venv/bin/pytest": "",
            ".harness/.venv/bin/ruff": "",
        },
    )

    messages = {f.message: f.level for f in diagnose(root)}

    assert messages["test tests-1: `pytest` is installed in .venv"] == "ok"
    assert messages["check harness-lint: `ruff` is installed in .harness/.venv"] == "ok"
    assert (
        messages[
            "check pyright: `pyright` is not installed in .venv; add it to the "
            "project's dev dependencies or change the command"
        ]
        == "warn"
    )
    assert (
        messages[
            "check eslint: `eslint` is not in ./node_modules/.bin; install the "
            "dependencies or add it"
        ]
        == "warn"
    )
    assert messages["check typecheck: ./package.json has no `typecheck` script"] == "fail"


def test_install_from_an_installed_project_copies_only_the_hook(tmp_path: Path) -> None:
    from harness.install import InstallReport, _merge_hooks

    template = tmp_path / "source-settings.json"
    template.write_text(
        json.dumps(
            {
                "permissions": {"allow": ["Bash(rm -rf /)"]},
                "hooks": {
                    "PreToolUse": [
                        {"matcher": "Bash", "hooks": []},
                        {
                            "matcher": "Edit|Write",
                            "hooks": [{"command": "x harness.hooks.claude_adapter"}],
                        },
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    target = tmp_path / "settings.json"

    _merge_hooks(
        target, template, "harness.hooks.claude_adapter", InstallReport(), "settings"
    )

    written = json.loads(target.read_text(encoding="utf-8"))
    assert "permissions" not in written
    assert written["hooks"]["PreToolUse"] == [
        {
            "matcher": "Edit|Write",
            "hooks": [{"command": "x harness.hooks.claude_adapter"}],
        }
    ]
