"""Resolve branches and read metadata from upstream Git repositories without cloning them fully.

`git ls-remote` proves a branch/tag exists. File contents are read from a shallow, blob-less,
no-checkout clone (`git show HEAD:<file>`), which downloads only the requested blobs, works
when raw.githubusercontent.com is unreachable, and is not subject to GitHub API rate limits.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .commands import Runner, command_error
from .errors import SetupError
from .paths import cache_dir, ensure_private_dir
from .validation import repo_basename, validate_app_name, validate_branch, validate_git_url

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on Python 3.10 only
    import tomli as tomllib

GIT_TIMEOUT = 120
GIT_RETRIES = 2


def git_env() -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"  # never hang on a credential prompt
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=20")
    return env


@dataclass
class RefInfo:
    url: str
    ref: str
    kind: str  # "branch" | "tag"
    sha: str


@dataclass
class AppMetadata:
    url: str
    ref: str
    sha: str
    name: str
    title: str | None = None
    required_apps: list[str] = field(default_factory=list)
    frappe_dependencies: dict[str, str] = field(default_factory=dict)
    requires_python: str | None = None


@dataclass
class FrappeMetadata:
    ref: str
    sha: str
    version: str | None
    requires_python: str | None
    node_engine: str | None


def list_refs(runner: Runner, url: str, *, phase: str = "resolve") -> dict[str, dict[str, str]]:
    """{'branch': {name: sha}, 'tag': {name: sha}} for a repository."""
    validate_git_url(url)
    result = runner.run(
        ["git", "ls-remote", "--heads", "--tags", "--", url],
        env=git_env(),
        timeout=GIT_TIMEOUT,
        retries=GIT_RETRIES,
        phase=phase,
        quiet=True,
    )
    if not result.ok:
        raise command_error(result, phase=phase, message=f"Cannot list branches of {url}")
    refs: dict[str, dict[str, str]] = {"branch": {}, "tag": {}}
    for line in result.output.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        sha, name = parts
        if name.startswith("refs/heads/"):
            refs["branch"][name.removeprefix("refs/heads/")] = sha
        elif name.startswith("refs/tags/"):
            tag = name.removeprefix("refs/tags/")
            if tag.endswith("^{}"):
                refs["tag"][tag[:-3]] = sha  # peeled commit wins over the tag object
            else:
                refs["tag"].setdefault(tag, sha)
    return refs


def resolve_ref(runner: Runner, url: str, ref: str, *, phase: str = "resolve") -> RefInfo:
    """Verify that `ref` exists as a branch or tag. Never substitutes another ref."""
    validate_branch(ref)
    refs = list_refs(runner, url, phase=phase)
    if ref in refs["branch"]:
        return RefInfo(url, ref, "branch", refs["branch"][ref])
    if ref in refs["tag"]:
        return RefInfo(url, ref, "tag", refs["tag"][ref])
    similar = sorted(
        b for b in refs["branch"] if b.startswith(("version-", "develop", "main", "master"))
    )
    raise SetupError(
        f"Branch or tag {ref!r} does not exist in {url}.",
        phase=phase,
        cause="The requested ref was not found by `git ls-remote`.",
        remedy=("Available branches include: " + ", ".join(similar[:12])) if similar else
        "Check the branch name with `git ls-remote --heads --tags <url>`.",
        retry_safe=False,
    )


class RepoSnapshot:
    """A shallow, blob-less clone used only for reading individual files."""

    def __init__(self, runner: Runner, url: str, ref: str, *, phase: str = "resolve") -> None:
        self.runner = runner
        self.url = url
        self.ref = ref
        self.phase = phase
        self._dir: Path | None = None

    def __enter__(self) -> RepoSnapshot:
        base = ensure_private_dir(cache_dir() / "probe")
        self._dir = Path(tempfile.mkdtemp(prefix="repo-", dir=base))
        result = self.runner.run(
            [
                "git", "clone", "--quiet", "--depth", "1", "--filter=blob:none", "--no-checkout",
                "--single-branch", "--branch", self.ref, "--", self.url, str(self._dir / "r"),
            ],
            env=git_env(),
            timeout=GIT_TIMEOUT,
            retries=GIT_RETRIES,
            phase=self.phase,
            quiet=True,
        )
        if not result.ok:
            self.__exit__(None, None, None)
            raise command_error(
                result, phase=self.phase, message=f"Cannot read {self.url} at {self.ref}"
            )
        return self

    def __exit__(self, *exc: object) -> None:
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)  # our own temporary directory
            self._dir = None

    @property
    def path(self) -> Path:
        assert self._dir is not None
        return self._dir / "r"

    def read(self, file: str) -> str | None:
        result = self.runner.run(
            ["git", "-C", str(self.path), "show", f"HEAD:{file}"],
            env=git_env(),
            timeout=GIT_TIMEOUT,
            retries=GIT_RETRIES,
            phase=self.phase,
            quiet=True,
        )
        return result.output if result.ok else None

    def head(self) -> str:
        result = self.runner.run(
            ["git", "-C", str(self.path), "rev-parse", "HEAD"], timeout=30, phase=self.phase, quiet=True
        )
        return result.output.strip() if result.ok else ""


# ------------------------------------------------------------------- parsers
def parse_pyproject(text: str | None) -> dict[str, object]:
    if not text:
        return {}
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {}


def parse_required_apps(hooks_py: str | None) -> list[str]:
    """Read `required_apps = [...]` from hooks.py without executing it."""
    if not hooks_py:
        return []
    try:
        tree = ast.parse(hooks_py)
    except SyntaxError:
        return []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "required_apps" for t in node.targets
        ):
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                return []
            if isinstance(value, (list, tuple)):
                return [str(x) for x in value if isinstance(x, str)]
    return []


def required_app_name(entry: str) -> str:
    """'frappe/erpnext' or 'https://github.com/frappe/erpnext' or 'erpnext' -> 'erpnext'."""
    entry = entry.strip().rstrip("/")
    if entry.endswith(".git"):
        entry = entry[:-4]
    return entry.rsplit("/", 1)[-1].replace("-", "_")


def _setup_py_name(text: str | None) -> str | None:
    if not text:
        return None
    m = re.search(r"""name\s*=\s*["']([A-Za-z0-9_.-]+)["']""", text)
    return m.group(1) if m else None


def _hooks_title(text: str | None) -> str | None:
    if not text:
        return None
    m = re.search(r"""^app_title\s*=\s*["']([^"']+)["']""", text, re.M)
    return m.group(1) if m else None


# ------------------------------------------------------------------- probes
def probe_frappe(runner: Runner, url: str, ref: RefInfo, *, phase: str = "resolve") -> FrappeMetadata:
    with RepoSnapshot(runner, url, ref.ref, phase=phase) as snap:
        pyproject = parse_pyproject(snap.read("pyproject.toml"))
        package_json_text = snap.read("package.json")
        init_py = snap.read("frappe/__init__.py") or ""
    requires_python = (pyproject.get("project") or {}).get("requires-python")  # type: ignore[union-attr]
    node_engine = None
    if package_json_text:
        try:
            node_engine = (json.loads(package_json_text).get("engines") or {}).get("node")
        except json.JSONDecodeError:
            node_engine = None
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)', init_py, re.M)
    return FrappeMetadata(
        ref=ref.ref,
        sha=ref.sha,
        version=m.group(1) if m else None,
        requires_python=str(requires_python) if requires_python else None,
        node_engine=str(node_engine) if node_engine else None,
    )


def probe_app(runner: Runner, url: str, ref: RefInfo, *, phase: str = "resolve") -> AppMetadata:
    with RepoSnapshot(runner, url, ref.ref, phase=phase) as snap:
        pyproject = parse_pyproject(snap.read("pyproject.toml"))
        project = pyproject.get("project") or {}
        name = project.get("name") if isinstance(project, dict) else None  # type: ignore[assignment]
        if not name:
            name = _setup_py_name(snap.read("setup.py"))
        if not name:
            name = repo_basename(url)
        name = str(name).replace("-", "_").lower()
        hooks = snap.read(f"{name}/hooks.py")
        if hooks is None:
            raise SetupError(
                f"{url} ({ref.ref}) does not look like a Frappe app: {name}/hooks.py is missing.",
                phase=phase,
                cause="Frappe apps must contain <app_name>/hooks.py.",
                remedy="Check that the URL points to a Frappe application repository.",
                retry_safe=False,
            )
    validate_app_name(name)
    bench_tool = ((pyproject.get("tool") or {}).get("bench") or {})  # type: ignore[union-attr]
    deps = bench_tool.get("frappe-dependencies") or {}
    return AppMetadata(
        url=url,
        ref=ref.ref,
        sha=ref.sha,
        name=name,
        title=_hooks_title(hooks),
        required_apps=[required_app_name(x) for x in parse_required_apps(hooks)],
        frappe_dependencies={str(k).replace("-", "_"): str(v) for k, v in deps.items()},
        requires_python=str(project.get("requires-python")) if isinstance(project, dict) and project.get("requires-python") else None,
    )
