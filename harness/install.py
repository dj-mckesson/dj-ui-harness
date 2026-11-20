"""Install the harness into a project repository, or write its ``harness.toml``.

Run it from a dj-ui-harness checkout::

    uv run --project .harness --no-sync python -m harness.install ../my-app --preset auto

It copies the harness files, writes ``harness.toml`` from a preset, adds the edit hooks to any
existing Claude Code and Codex settings, adds the ``AGENTS.md`` section and the ``.gitignore``
lines, then runs the doctor. It never overwrites a project file the harness does not own unless
``--force`` is given. Running it again updates the harness files and keeps the project's edits.
"""

import argparse
import json
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from harness.config import (
    CONFIG_FILE,
    DEFAULT_REPO_ROOT,
    PRESETS_DIRECTORY,
    ConfigError,
    load_config,
    preset_names,
)

SOURCE_ROOT = DEFAULT_REPO_ROOT
TEMPLATES = Path(__file__).resolve().parent / "templates"
ROLE_SKILLS = ("harness-run", "spec", "planner", "maker", "checker")
ROLE_AGENTS = ("spec-author", "planner", "maker", "checker")
# Files the harness owns. An install replaces them so that the project gets the current version.
OWNED_TREES = ("harness", "tests/harness")
OWNED_FILES = (
    ".harness/pyproject.toml",
    ".harness/uv.lock",
    ".harness/.python-version",
    ".harness/ruff.toml",
    "docs/harness.md",
    "docs/harness-backlog.md",
)
BROWSER_FILES = (
    "e2e/harness/smoke.spec.ts",
    "e2e/harness/a11y.spec.ts",
    "e2e/harness/routes.ts",
)
# The browser check files ship inside the harness package, so every install carries them. The
# ``.template`` suffix keeps a project's TypeScript, lint, and test tools from reading them there.
BROWSER_TEMPLATES = TEMPLATES / "browser"
TEMPLATE_SUFFIX = ".template"
GITIGNORE_LINES = (
    "# Coding harness: run artifacts and its own environment",
    "/runs/",
    "/planning/",
    "/.plans/",
    ".harness/.venv/",
)
CLAUDE_HOOK_MARKER = "harness.hooks.claude_adapter"
CODEX_HOOK_MARKER = "harness.hooks.codex_adapter"
_SKIPPED_NAMES = {"__pycache__", ".pytest_cache", ".ruff_cache", ".DS_Store"}


@dataclass
class InstallReport:
    """Collect what an install did, for the summary it prints."""

    written: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def detect_preset(target: Path) -> str:
    """Pick the preset that fits the files already in a project.

    :raises ConfigError: If no preset fits.
    """

    def exists(*names: str) -> bool:
        return any((target / name).exists() for name in names)

    if exists("backend", "api") and exists("frontend", "web"):
        return "fastapi-react"
    if exists("next.config.ts", "next.config.js", "next.config.mjs"):
        return "nextjs-pnpm"
    if exists("vite.config.ts", "vite.config.js", "vite.config.mjs"):
        return "vite-react-npm"
    if exists("pyproject.toml", "setup.py", "requirements.txt"):
        return "python-uv"
    raise ConfigError(
        f"no preset fits {target}; pass --preset with one of {', '.join(preset_names())}"
    )


def _copy_tree(source: Path, target: Path, report: InstallReport, label: str) -> None:
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(
        source,
        target,
        symlinks=True,
        ignore=lambda _dir, names: [name for name in names if name in _SKIPPED_NAMES],
    )
    report.written.append(label + "/")


def _copy_file(
    source: Path, target: Path, report: InstallReport, label: str, *, overwrite: bool
) -> None:
    if target.exists() and not overwrite:
        report.kept.append(label)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    report.written.append(label)


def _link(target: Path, points_to: str, report: InstallReport, label: str) -> None:
    if target.is_symlink() or target.exists():
        if target.is_symlink() and str(target.readlink()) == points_to:
            return
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(points_to)
    report.written.append(label)


def _merge_hooks(
    path: Path,
    template: Path,
    marker: str,
    report: InstallReport,
    label: str,
) -> None:
    """Add the harness hook to existing settings, keeping every other setting.

    Only the harness hook entries are taken from ``template``, so an install from a project that
    already has the harness never copies that project's own settings.
    """

    template_data = json.loads(template.read_text(encoding="utf-8"))
    wanted = [
        entry
        for entry in template_data["hooks"]["PreToolUse"]
        if marker in json.dumps(entry)
    ]
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{label} is not valid JSON: {exc}") from exc
        if not isinstance(current, dict):
            raise ConfigError(f"{label} must hold a JSON object")
        action = " (hook merged)"
    else:
        current = {}
        if "description" in template_data:
            current["description"] = template_data["description"]
        action = ""
    hooks = current.setdefault("hooks", {})
    entries = hooks.setdefault("PreToolUse", [])
    kept = [entry for entry in entries if marker not in json.dumps(entry)]
    hooks["PreToolUse"] = [*kept, *wanted]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    report.written.append(label + action)


def _append_lines(
    path: Path, lines: tuple[str, ...], report: InstallReport, label: str
) -> None:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    present = {line.strip() for line in existing.splitlines()}
    missing = [line for line in lines if line.strip() not in present]
    if not [line for line in missing if not line.startswith("#")]:
        return
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    spacer = "\n" if existing.strip() else ""
    path.write_text(
        existing + prefix + spacer + "\n".join(missing) + "\n", encoding="utf-8"
    )
    report.written.append(label)


def _ensure_agents_section(target: Path, report: InstallReport) -> None:
    section = (TEMPLATES / "agents-section.md").read_text(encoding="utf-8")
    agents = target / "AGENTS.md"
    if not agents.exists():
        agents.write_text("# Agent guide\n\n" + section, encoding="utf-8")
        report.written.append("AGENTS.md")
    elif "## Harness\n" not in agents.read_text(encoding="utf-8"):
        text = agents.read_text(encoding="utf-8").rstrip("\n")
        agents.write_text(text + "\n\n" + section, encoding="utf-8")
        report.written.append("AGENTS.md (Harness section added)")
    else:
        report.kept.append("AGENTS.md")
    claude = target / "CLAUDE.md"
    if not claude.exists():
        claude.write_text("@AGENTS.md\n", encoding="utf-8")
        report.written.append("CLAUDE.md")
    elif "AGENTS.md" not in claude.read_text(encoding="utf-8"):
        report.notes.append(
            "CLAUDE.md does not mention AGENTS.md. Add `@AGENTS.md` so Claude Code reads it."
        )


def install(
    target: Path,
    preset: str = "auto",
    *,
    force: bool = False,
    source: Path = SOURCE_ROOT,
) -> InstallReport:
    """Install or update the harness in ``target``.

    :param preset: Preset name, or ``auto`` to pick one from the project's files.
    :param force: Replace an existing ``harness.toml`` and Playwright config.
    :raises ConfigError: If no preset fits or a settings file cannot be merged.
    """

    target = target.resolve()
    source = source.resolve()
    if not target.is_dir():
        raise ConfigError(f"{target} is not a folder")
    report = InstallReport()
    in_place = target == source
    config_file = target / CONFIG_FILE

    if config_file.exists() and not force:
        report.kept.append(CONFIG_FILE)
        if preset != "auto":
            report.notes.append(
                f"{CONFIG_FILE} already exists, so --preset {preset} was not applied. "
                "Pass --force to replace it."
            )
    else:
        name = detect_preset(target) if preset == "auto" else preset
        preset_file = PRESETS_DIRECTORY / f"{name}.toml"
        if not preset_file.is_file():
            raise ConfigError(
                f"unknown preset {name!r}; choose one of {', '.join(preset_names())}"
            )
        shutil.copyfile(preset_file, config_file)
        report.written.append(f"{CONFIG_FILE} (preset {name})")
    config = load_config(config_file)

    if not in_place:
        for tree in OWNED_TREES:
            _copy_tree(source / tree, target / tree, report, tree)
        for name in OWNED_FILES:
            _copy_file(source / name, target / name, report, name, overwrite=True)
        for skill in ROLE_SKILLS:
            _copy_tree(
                source / ".agents" / "skills" / skill,
                target / ".agents" / "skills" / skill,
                report,
                f".agents/skills/{skill}",
            )
        for agent in ROLE_AGENTS:
            name = f".agents/agents/{agent}.md"
            _copy_file(source / name, target / name, report, name, overwrite=True)

    for skill in ROLE_SKILLS:
        _link(
            target / ".claude" / "skills" / skill,
            f"../../.agents/skills/{skill}",
            report,
            f".claude/skills/{skill}",
        )
    for agent in ROLE_AGENTS:
        _link(
            target / ".claude" / "agents" / f"{agent}.md",
            f"../../.agents/agents/{agent}.md",
            report,
            f".claude/agents/{agent}.md",
        )
    if not in_place:
        _merge_hooks(
            target / ".claude" / "settings.json",
            source / ".claude" / "settings.json",
            CLAUDE_HOOK_MARKER,
            report,
            ".claude/settings.json",
        )
        _merge_hooks(
            target / ".codex" / "hooks.json",
            source / ".codex" / "hooks.json",
            CODEX_HOOK_MARKER,
            report,
            ".codex/hooks.json",
        )

    browser_dirs = sorted(
        {
            spec.cwd
            for spec in config.checks.values()
            if "playwright" in " ".join(spec.command)
        }
    )
    if not in_place:
        for web_dir in browser_dirs:
            base = target if web_dir == "." else target / web_dir
            prefix = "" if web_dir == "." else f"{web_dir}/"
            for name in BROWSER_FILES:
                _copy_file(
                    BROWSER_TEMPLATES / f"{name}{TEMPLATE_SUFFIX}",
                    base / name,
                    report,
                    prefix + name,
                    overwrite=True,
                )
            _copy_file(
                BROWSER_TEMPLATES / f"playwright.config.ts{TEMPLATE_SUFFIX}",
                base / "playwright.config.ts",
                report,
                prefix + "playwright.config.ts",
                overwrite=force,
            )
            if prefix + "playwright.config.ts" in report.kept:
                report.notes.append(
                    f"{prefix}playwright.config.ts already exists. Add the `smoke` and `a11y` "
                    "projects and the E2E_* web server settings from the harness copy, or "
                    "pass --force to replace it."
                )
            report.notes.append(
                f"Browser checks: in {web_dir}, install @playwright/test and "
                "@axe-core/playwright as dev dependencies, then `playwright install chromium`."
            )

    _append_lines(target / ".gitignore", GITIGNORE_LINES, report, ".gitignore")
    _ensure_agents_section(target, report)
    report.notes.append(
        "Install the harness environment: `uv sync --project .harness` "
        "(add UV_SYSTEM_CERTS=1 behind a TLS proxy)."
    )
    report.notes.append(
        f"Review {CONFIG_FILE}: the test commands, checks, `context`, and `guidance` must "
        "match this project."
    )
    return report


def _print_report(report: InstallReport, out: Callable[[str], None]) -> None:
    for label in report.written:
        out(f"wrote  {label}")
    for label in report.kept:
        out(f"kept   {label}")
    for note in report.notes:
        out(f"next   {note}")


def main(argv: list[str] | None = None) -> int:
    """Install the harness from the command line."""

    parser = argparse.ArgumentParser(
        prog="python -m harness.install",
        description="Install or update the coding harness in a project repository.",
    )
    parser.add_argument("target", type=Path, help="project repository root")
    parser.add_argument(
        "--preset",
        default="auto",
        help=f"one of {', '.join(preset_names())}, or auto (default)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing harness.toml and playwright.config.ts",
    )
    args = parser.parse_args(argv)
    try:
        report = install(args.target, args.preset, force=args.force)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)  # noqa: T201 - command-line output
        return 2
    _print_report(report, print)

    from harness.doctor import diagnose, print_findings

    findings = diagnose(args.target.resolve())
    print()  # noqa: T201 - command-line output
    print_findings(findings, print)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
