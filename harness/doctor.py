"""Check that the harness is set up correctly in a project repository.

Run it from the project root::

    uv run --project .harness --no-sync python -m harness.doctor

Each finding is ``ok``, ``warn``, or ``fail``. The command exits with status 1 when any finding
fails. A warning is something the harness can run without, but probably should not.
"""

import argparse
import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from harness.config import CONFIG_FILE, ConfigError, ProjectConfig, load_config

Level = Literal["ok", "warn", "fail"]
ROLE_SKILLS = ("harness-run", "spec", "planner", "maker", "checker")
ROLE_AGENTS = ("spec-author", "planner", "maker", "checker")


@dataclass(frozen=True)
class Finding:
    """Hold one doctor result."""

    level: Level
    message: str


def _program(repo_root: Path, cwd: str, command: tuple[str, ...]) -> Finding:
    program = command[0]
    folder = repo_root if cwd == "." else repo_root / cwd
    if "/" in program:
        if (folder / program).exists():
            return Finding("ok", f"{program} exists in {cwd}")
        return Finding(
            "warn", f"{program} is missing in {cwd}; install the project dependencies"
        )
    if shutil.which(program) is None:
        return Finding("fail", f"`{program}` is not on PATH (used in {cwd})")
    return Finding("ok", f"`{program}` is on PATH")


_UV_VALUE_FLAGS = {
    "--project",
    "--directory",
    "--python",
    "-p",
    "--with",
    "--group",
    "--extra",
    "--package",
}


def _uv_tool(command: tuple[str, ...]) -> tuple[str | None, str | None]:
    """Return the tool that ``uv run`` starts and the ``--project`` it uses, if any."""

    if len(command) < 2 or command[1] != "run":
        return None, None
    project = None
    index = 2
    while index < len(command):
        part = command[index]
        if part in _UV_VALUE_FLAGS:
            if part == "--project" and index + 1 < len(command):
                project = command[index + 1]
            index += 2
        elif part.startswith("-"):
            index += 1
        else:
            break
    if index >= len(command):
        return None, project
    tool = command[index]
    if tool == "python" and command[index + 1 : index + 2] == ("-m",):
        return None, project
    return tool, project


def _node_tool(command: tuple[str, ...]) -> str | None:
    """Return the package binary that ``pnpm exec``, ``npm exec``, or ``npx`` starts."""

    if command[0] in {"pnpm", "npm"} and command[1:2] == ("exec",):
        rest = command[2:]
    elif command[0] == "npx":
        rest = command[1:]
    else:
        return None
    rest = tuple(part for part in rest if part != "--" and not part.startswith("-"))
    return rest[0] if rest else None


def _package_script(folder: Path, command: tuple[str, ...]) -> str | None:
    """Return the ``package.json`` script that ``pnpm <script>`` or ``npm run`` names."""

    builtins = {"exec", "install", "add", "dlx", "run", "i", "ci", "x"}
    if command[0] == "pnpm" and len(command) > 1 and command[1] not in builtins:
        return command[1]
    if command[0] in {"pnpm", "npm"} and command[1:2] == ("run",) and len(command) > 2:
        return command[2]
    if command[0] == "npm" and command[1:2] == ("test",):
        return "test"
    return None


def _tool_findings(repo_root: Path, cwd: str, command: tuple[str, ...]) -> list[Finding]:
    """Check the tool behind a package-manager command, such as ``uv run pyright``."""

    folder = repo_root if cwd == "." else repo_root / cwd
    if command[0] == "uv":
        tool, project = _uv_tool(command)
        if tool is None:
            return []
        venv = (folder / project if project else folder) / ".venv"
        if not venv.is_dir():
            return [
                Finding(
                    "warn",
                    f"{venv.relative_to(repo_root)} is missing; run `uv sync` so "
                    f"`{tool}` can run",
                )
            ]
        if not (venv / "bin" / tool).exists():
            return [
                Finding(
                    "warn",
                    f"`{tool}` is not installed in {venv.relative_to(repo_root)}; add it to "
                    "the project's dev dependencies or change the command",
                )
            ]
        return [Finding("ok", f"`{tool}` is installed in {venv.relative_to(repo_root)}")]
    tool = _node_tool(command)
    if tool is not None:
        if (folder / "node_modules" / ".bin" / tool).exists():
            return [Finding("ok", f"`{tool}` is installed in {cwd}/node_modules")]
        return [
            Finding(
                "warn",
                f"`{tool}` is not in {cwd}/node_modules/.bin; install the dependencies "
                "or add it",
            )
        ]
    script = _package_script(folder, command)
    if script is not None:
        package = folder / "package.json"
        try:
            scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts", {})
        except (OSError, json.JSONDecodeError, AttributeError):
            scripts = {}
        if script not in scripts:
            return [Finding("fail", f"{cwd}/package.json has no `{script}` script")]
        return [Finding("ok", f"{cwd}/package.json has a `{script}` script")]
    return []


def _check_commands(repo_root: Path, config: ProjectConfig) -> list[Finding]:
    findings: list[Finding] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    commands = [(f"test {test.name}", test.cwd, test.command) for test in config.tests]
    commands.extend(
        (f"check {name}", spec.cwd, spec.command) for name, spec in config.checks.items()
    )
    for label, cwd, command in commands:
        folder = repo_root if cwd == "." else repo_root / cwd
        if not folder.is_dir():
            findings.append(
                Finding("fail", f"{label} runs in {cwd}/, which does not exist")
            )
            continue
        if (cwd, command[:1]) not in seen:
            seen.add((cwd, command[:1]))
            findings.append(_program(repo_root, cwd, command))
        if (cwd, command) not in seen:
            seen.add((cwd, command))
            findings.extend(
                Finding(f.level, f"{label}: {f.message}")
                for f in _tool_findings(repo_root, cwd, command)
            )
    return findings


def _check_browser(repo_root: Path, config: ProjectConfig) -> list[Finding]:
    findings: list[Finding] = []
    dirs = sorted(
        {
            spec.cwd
            for spec in config.checks.values()
            if "playwright" in " ".join(spec.command)
        }
    )
    for cwd in dirs:
        base = repo_root if cwd == "." else repo_root / cwd
        for name in (
            "playwright.config.ts",
            "e2e/harness/smoke.spec.ts",
            "e2e/harness/a11y.spec.ts",
        ):
            if not (base / name).is_file():
                findings.append(
                    Finding(
                        "fail", f"{cwd}/{name} is missing; the browser checks need it"
                    )
                )
        package = base / "package.json"
        if package.is_file():
            try:
                data = json.loads(package.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
            deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
            for dependency in ("@playwright/test", "@axe-core/playwright"):
                if dependency not in deps:
                    findings.append(
                        Finding(
                            "warn", f"{cwd}/package.json does not depend on {dependency}"
                        )
                    )
        else:
            findings.append(Finding("warn", f"{cwd}/package.json is missing"))
    return findings


def _check_wiring(repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    if not (repo_root / ".harness" / ".venv").is_dir():
        findings.append(
            Finding(
                "fail",
                "the harness environment is missing; run `uv sync --project .harness`",
            )
        )
    settings = repo_root / ".claude" / "settings.json"
    text = settings.read_text(encoding="utf-8") if settings.is_file() else ""
    if "harness.hooks.claude_adapter" not in text:
        findings.append(
            Finding(
                "fail",
                ".claude/settings.json has no harness edit hook; rerun the install",
            )
        )
    elif "--project .harness" not in text:
        findings.append(
            Finding(
                "fail",
                ".claude/settings.json runs the hook outside .harness; rerun the install",
            )
        )
    codex = repo_root / ".codex" / "hooks.json"
    if "harness.hooks.codex_adapter" not in (
        codex.read_text(encoding="utf-8") if codex.is_file() else ""
    ):
        findings.append(Finding("warn", ".codex/hooks.json has no harness edit hook"))
    for skill in ROLE_SKILLS:
        if not (repo_root / ".claude" / "skills" / skill / "SKILL.md").is_file():
            findings.append(Finding("fail", f"Claude Code cannot find the {skill} skill"))
    for agent in ROLE_AGENTS:
        if not (repo_root / ".claude" / "agents" / f"{agent}.md").is_file():
            findings.append(Finding("fail", f"Claude Code cannot find the {agent} agent"))
    ignore = repo_root / ".gitignore"
    ignored = ignore.read_text(encoding="utf-8").splitlines() if ignore.is_file() else []
    for line in ("/runs/", "/planning/"):
        if line not in ignored:
            findings.append(Finding("warn", f".gitignore does not ignore {line}"))
    agents = repo_root / "AGENTS.md"
    if not agents.is_file() or "## Harness" not in agents.read_text(encoding="utf-8"):
        findings.append(Finding("warn", "AGENTS.md has no Harness section"))
    return findings


def diagnose(repo_root: Path) -> list[Finding]:
    """Return every finding for one repository."""

    try:
        config = load_config(repo_root / CONFIG_FILE)
    except ConfigError as exc:
        return [Finding("fail", str(exc)), *_check_wiring(repo_root)]
    findings = [
        Finding(
            "ok",
            f"{CONFIG_FILE}: {len(config.tests)} test command(s), {len(config.checks)} check(s), "
            f"mandatory: {', '.join(config.mandatory) or 'none'}",
        )
    ]
    for path in config.context:
        if not (repo_root / path).exists():
            findings.append(
                Finding("fail", f"context path {path} does not exist; fix `context`")
            )
    for path in config.guidance:
        if not (repo_root / path).is_file():
            findings.append(
                Finding("warn", f"guidance file {path} does not exist; fix `guidance`")
            )
    findings.extend(_check_commands(repo_root, config))
    findings.extend(_check_browser(repo_root, config))
    findings.extend(_check_wiring(repo_root))
    return findings


def print_findings(findings: list[Finding], out: Callable[[str], None]) -> None:
    """Print findings, worst first, with a closing summary line."""

    order = {"fail": 0, "warn": 1, "ok": 2}
    for finding in sorted(findings, key=lambda item: order[item.level]):
        out(f"{finding.level:<5}{finding.message}")
    failed = sum(finding.level == "fail" for finding in findings)
    warned = sum(finding.level == "warn" for finding in findings)
    out(f"doctor: {failed} failed, {warned} warning(s)")


def main(argv: list[str] | None = None) -> int:
    """Check the setup from the command line."""

    parser = argparse.ArgumentParser(
        prog="python -m harness.doctor",
        description="Check that the coding harness is set up correctly in a repository.",
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    findings = diagnose(args.repo_root.resolve())
    print_findings(findings, print)
    return 1 if any(finding.level == "fail" for finding in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
