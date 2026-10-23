"""Shared fixtures. Every test runs with HOME and XDG directories inside a temporary path,
so nothing touches the developer's real state, logs, benches or system."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from frappe_setup.commands import CommandResult, PromptResponse, Runner
from frappe_setup.compatibility import load_matrix
from frappe_setup.redaction import REDACTOR
from frappe_setup.system import Interpreter, NodeRuntime, SystemFacts, ToolInfo


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var, sub in (
        ("XDG_STATE_HOME", ".local/state"),
        ("XDG_DATA_HOME", ".local/share"),
        ("XDG_CACHE_HOME", ".cache"),
    ):
        monkeypatch.setenv(var, str(home / sub))
    for var in ("FRAPPE_SETUP_ADMIN_PASSWORD", "FRAPPE_SETUP_DB_ROOT_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    REDACTOR.clear()
    yield home
    REDACTOR.clear()


Handler = CommandResult | Callable[[list[str], dict], CommandResult]


class FakeRunner(Runner):
    """Scripted command runner. Rules are (pattern, handler); pattern is a regex matched against
    the space-joined argv with argv[0] reduced to its basename. First match wins."""

    def __init__(self, rules: Sequence[tuple[str, Handler]] = ()) -> None:
        super().__init__()
        self.rules: list[tuple[re.Pattern[str], Handler]] = [(re.compile(p), h) for p, h in rules]
        self.calls: list[list[str]] = []
        self.prompt_calls: list[tuple[list[str], list[PromptResponse]]] = []

    def add(self, pattern: str, handler: Handler) -> None:
        self.rules.insert(0, (re.compile(pattern), handler))

    def _key(self, argv: Sequence[str]) -> str:
        argv = [str(a) for a in argv]
        return " ".join([os.path.basename(argv[0]), *argv[1:]]) if argv else ""

    def _dispatch(self, argv: Sequence[str], kwargs: dict) -> CommandResult:
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        key = self._key(argv)
        for pattern, handler in self.rules:
            if pattern.search(key):
                if callable(handler):
                    return handler(argv, kwargs)
                return CommandResult(
                    argv, handler.returncode, handler.output, handler.stderr, timed_out=handler.timed_out
                )
        return CommandResult(argv, 127, f"{argv[0]}: command not found (no fake rule)")

    def run(self, argv, **kwargs):  # type: ignore[override]
        return self._dispatch(argv, kwargs)

    def capture(self, argv, **kwargs):  # type: ignore[override]
        return self._dispatch(argv, kwargs)

    def run_with_prompts(self, argv, responses, **kwargs):  # type: ignore[override]
        self.prompt_calls.append(([str(a) for a in argv], list(responses)))
        return self._dispatch(argv, kwargs)

    def run_on_terminal(self, argv, **kwargs):  # type: ignore[override]
        return self._dispatch(argv, kwargs).returncode

    def called(self, pattern: str) -> list[list[str]]:
        rx = re.compile(pattern)
        return [c for c in self.calls if rx.search(self._key(c))]


def ok(output: str = "") -> CommandResult:
    return CommandResult([], 0, output)


def fail(output: str = "", code: int = 1) -> CommandResult:
    return CommandResult([], code, output)


@pytest.fixture
def matrix():
    return load_matrix()


def make_facts(**overrides) -> SystemFacts:
    """A fully provisioned Ubuntu 24.04 x86_64 machine; override fields to create gaps."""
    facts = SystemFacts(
        os_id="ubuntu",
        os_version="24.04",
        os_codename="noble",
        os_pretty="Ubuntu 24.04.1 LTS",
        arch="x86_64",
        cpu_count=4,
        cpu_flags=["cx16", "lahf_lm", "popcnt", "sse4_1", "sse4_2", "ssse3"],
        mem_total_mb=8192,
        mem_available_mb=6000,
        swap_total_mb=2048,
        user="dev",
        uid=1000,
        home="/home/dev",
        systemd=True,
        sudo_available=True,
        sudo_nopasswd=False,
    )
    tools = {
        "git": "2.43.0",
        "curl": "8.5.0",
        "python3": "3.12.3",
        "pip": "24.0",
        "node": "20.11.1",
        "npm": "10.2.4",
        "yarn": "1.22.22",
        "uv": "0.9.0",
        "bench": "5.31.0",
        "mariadb": "10.11.8",
        "mariadbd": "10.11.8",
        "redis-server": "7.0.15",
        "redis-cli": "7.0.15",
        "gcc": "13.2.0",
        "make": "4.3",
        "pkg-config": "1.8.1",
        "wkhtmltopdf": "0.12.6",
        "crontab": "present",
        "sudo": "1.9.15",
    }
    facts.tools = {k: ToolInfo(k, f"/usr/bin/{k}", v, v) for k, v in tools.items()}
    facts.pythons = [Interpreter("/usr/bin/python3.12", "3.12.3", "system")]
    facts.nodes = [NodeRuntime("/usr/bin", "20.11.1", "path")]
    facts.services = {"mariadb": "active", "postgresql": "inactive", "redis-server": "active"}
    facts.ports_in_use = {8000: False, 9000: False, 11000: False, 13000: False}
    facts.apt_installed = {
        p: "1.0"
        for p in [
            "git",
            "curl",
            "ca-certificates",
            "gnupg",
            "build-essential",
            "pkg-config",
            "python3-dev",
            "python3-venv",
            "libffi-dev",
            "libssl-dev",
            "libmariadb-dev",
            "mariadb-server",
            "mariadb-client",
            "redis-server",
            "python3.12-dev",
            "python3.12-venv",
        ]
    }
    for key, value in overrides.items():
        if key == "tools":
            for name, ver in value.items():
                if ver is None:
                    facts.tools.pop(name, None)
                else:
                    facts.tools[name] = ToolInfo(name, f"/usr/bin/{name}", ver, ver)
        else:
            setattr(facts, key, value)
    return facts


@pytest.fixture
def facts_factory():
    return make_facts
