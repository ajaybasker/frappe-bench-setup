"""Bench CLI validation, bench initialization and structural verification."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .commands import Runner, command_error
from .compatibility import parse_version, satisfies
from .dependencies import ToolEnv
from .errors import SetupError
from .system import inspect_bench
from .ui import UI

PHASE = "bench"
INIT_TIMEOUT = 3 * 3600


@dataclass
class BenchCheck:
    ok: bool
    problems: list[str] = field(default_factory=list)
    frappe_branch: str | None = None
    frappe_version: str | None = None
    python_version: str | None = None
    webserver_port: int | None = None


def bench_exe(toolenv: ToolEnv) -> str:
    exe = toolenv.which("bench")
    if not exe:
        raise SetupError(
            "The Bench CLI is not on PATH.",
            phase=PHASE,
            remedy="Rerun frappe-setup so it can install Bench, or `uv tool install frappe-bench`.",
            retry_safe=True,
        )
    return exe


def bench_cli_version(runner: Runner, toolenv: ToolEnv) -> str | None:
    result = runner.run([bench_exe(toolenv), "--version"], env=toolenv.env(), timeout=60, phase=PHASE, quiet=True)
    if not result.ok:
        return None
    for line in reversed(result.output.splitlines()):
        v = parse_version(line)
        if v and not line.startswith("INFO"):
            return str(v)
    return None


def supported_init_options(runner: Runner, toolenv: ToolEnv) -> set[str]:
    """Options accepted by the installed `bench init` (verified, not assumed)."""
    result = runner.run([bench_exe(toolenv), "init", "--help"], env=toolenv.env(), timeout=60, phase=PHASE, quiet=True)
    return set(re.findall(r"(--[a-z][a-z0-9_-]+)", result.output))


def classify_target(path: Path) -> str:
    """'absent' | 'empty' | 'bench' | 'other' (a file or a non-bench, non-empty directory)."""
    if not path.exists():
        return "absent"
    if not path.is_dir():
        return "other"
    if inspect_bench(path):
        return "bench"
    try:
        next(path.iterdir())
    except StopIteration:
        return "empty"
    return "other"


def init_bench(
    runner: Runner,
    ui: UI,
    toolenv: ToolEnv,
    *,
    parent: Path,
    name: str,
    branch: str,
    python: str,
    enable_backups: bool,
    verbose: bool = False,
) -> None:
    options = supported_init_options(runner, toolenv)
    argv = [bench_exe(toolenv), "init", name, "--frappe-branch", branch, "--python", python]
    for opt in ("--frappe-branch", "--python"):
        if opt not in options:
            raise SetupError(
                f"The installed Bench CLI does not support `bench init {opt}`.",
                phase=PHASE,
                remedy="Upgrade it: `uv tool install --upgrade frappe-bench`.",
                retry_safe=True,
            )
    if not enable_backups and "--no-backups" in options:
        argv.append("--no-backups")
    if "--verbose" in options:
        argv.append("--verbose")
    with ui.progress(f"Initializing bench {name} (Frappe {branch}); this usually takes 5-20 minutes") as on_line:
        result = runner.run(argv, cwd=parent, env=toolenv.env(), timeout=INIT_TIMEOUT, on_line=on_line, phase=PHASE)
    if not result.ok:
        raise command_error(
            result,
            phase=PHASE,
            message="bench init failed",
            remedy=None,
        )


def read_common_config(bench: Path) -> dict[str, object]:
    path = bench / "sites" / "common_site_config.json"
    try:
        return dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


def validate_bench(
    runner: Runner,
    toolenv: ToolEnv,
    bench: Path,
    *,
    branch: str | None,
    python_constraint: object | None = None,
    expect_version: str | None = None,
    check_assets: bool = True,
) -> BenchCheck:
    check = BenchCheck(ok=True)
    problems = check.problems
    for rel in (
        "apps/frappe",
        "sites",
        "env/bin/python",
        "config",
        "Procfile",
        "sites/apps.txt",
        "sites/common_site_config.json",
    ):
        if not (bench / rel).exists():
            problems.append(f"missing {rel}")
    info = inspect_bench(bench)
    if info:
        check.frappe_branch = info.frappe_branch
        if info.apps and info.apps[0] != "frappe" and "frappe" not in info.apps:
            problems.append("sites/apps.txt does not list frappe")
    if branch and check.frappe_branch and check.frappe_branch != branch:
        problems.append(f"apps/frappe is on branch {check.frappe_branch!r}, expected {branch!r}")
    py = bench / "env" / "bin" / "python"
    if py.exists():
        result = runner.run(
            [str(py), "-c", "import sys, frappe; print('%d.%d.%d' % sys.version_info[:3]); print(frappe.__version__)"],
            cwd=bench / "sites",
            timeout=120,
            phase=PHASE,
            quiet=True,
        )
        if result.ok and len(result.output.strip().splitlines()) >= 2:
            lines = result.output.strip().splitlines()
            check.python_version, check.frappe_version = lines[-2].strip(), lines[-1].strip()
            if python_constraint is not None and not satisfies(check.python_version, python_constraint):  # type: ignore[arg-type]
                problems.append(
                    f"bench env uses Python {check.python_version}, which does not satisfy {python_constraint}"
                )
            if expect_version:
                want, got = parse_version(expect_version), parse_version(check.frappe_version)
                if want and got and want.major != got.major:
                    problems.append(f"installed frappe {got} does not match the requested branch ({want})")
        else:
            problems.append("frappe cannot be imported from the bench virtualenv")
    if check_assets:
        if not (bench / "apps" / "frappe" / "node_modules").is_dir():
            problems.append("frontend dependencies (apps/frappe/node_modules) are missing")
        assets = bench / "sites" / "assets"
        if not ((assets / "assets.json").exists() or (assets / "frappe" / "dist").is_dir()):
            problems.append("frontend assets were not built (sites/assets)")
    port = read_common_config(bench).get("webserver_port")
    check.webserver_port = int(port) if isinstance(port, (int, str)) and str(port).isdigit() else None
    check.ok = not problems
    return check
