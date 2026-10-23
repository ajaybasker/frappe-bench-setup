"""Application resolution (before installation) and fetching into the bench."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .commands import Runner, command_error
from .compatibility import parse_version, satisfies, spec
from .config import AppSpec
from .dependencies import ToolEnv
from .errors import SetupError
from .remote import AppMetadata, list_refs, probe_app
from .ui import UI
from .validation import normalize_url, validate_branch

PHASE_RESOLVE = "resolve"
PHASE = "apps"
GET_APP_TIMEOUT = 2 * 3600


@dataclass
class ResolvedApp:
    spec: AppSpec
    meta: AppMetadata

    @property
    def name(self) -> str:
        return self.meta.name


def default_branch_for(refs: dict[str, dict[str, str]], frappe_branch: str) -> str | None:
    """Same-named branch as the framework (e.g. version-16), if the app has one."""
    return frappe_branch if frappe_branch in refs["branch"] else None


def notable_branches(refs: dict[str, dict[str, str]], limit: int = 10) -> list[str]:
    """main/master/develop and version-NN branches first: the ones users normally want."""
    branches = refs["branch"]
    first = [b for b in ("main", "master", "develop") if b in branches]
    versions = sorted(
        (b for b in branches if re.fullmatch(r"version-\d+", b)), key=lambda b: int(b.split("-")[1]), reverse=True
    )
    rest = sorted(b for b in branches if b not in first and b not in versions)
    return (first + versions + rest)[:limit]


def choose_branch(ui: UI, app: AppSpec, refs: dict[str, dict[str, str]], frappe_branch: str) -> str:
    """Pick the app branch. Explicit values are verified; nothing is substituted silently."""
    if app.branch:
        validate_branch(app.branch)
        if app.branch not in refs["branch"] and app.branch not in refs["tag"]:
            raise SetupError(
                f"Branch or tag {app.branch!r} does not exist in {app.url}.",
                phase=PHASE_RESOLVE,
                remedy="Available branches include: " + ", ".join(notable_branches(refs)),
                retry_safe=False,
            )
        return app.branch
    same = default_branch_for(refs, frappe_branch)
    if same:
        return same
    notable = notable_branches(refs, limit=20)
    preferred = [b for b in notable if b in ("main", "master", "develop")]
    if not ui.interactive:
        raise SetupError(
            f"{app.url} has no {frappe_branch!r} branch; choose one explicitly.",
            phase=PHASE_RESOLVE,
            remedy=(
                f"Pass the branch, e.g. {'--app ' + app.key if app.source == 'catalog' else '--app-url ' + app.url}"
                f"@{notable[0]} (available include: {', '.join(notable[:10])})."
            ),
            retry_safe=False,
        )
    ui.warn(f"{app.key}: no {frappe_branch!r} branch. Choose the branch to use:")
    options = [(b, b) for b in notable]
    return ui.choose(f"Branch for {app.key}", options, default=preferred[0] if preferred else notable[0])


def _check_framework_constraint(meta: AppMetadata, frappe_version: str | None, frappe_branch: str) -> str | None:
    constraint = meta.frappe_dependencies.get("frappe")
    if not constraint or not frappe_version:
        return None
    version = parse_version(frappe_version)
    try:
        ok = satisfies(version, spec(constraint))
    except SetupError:
        return None
    if ok:
        return None
    return f"{meta.name} ({meta.ref}) declares frappe {constraint}, but {frappe_branch} is frappe {frappe_version}."


def topo_order(apps: list[ResolvedApp], available: set[str]) -> list[ResolvedApp]:
    """Order apps so required_apps come first; keeps user order where possible."""
    by_name = {a.name: a for a in apps}
    ordered: list[ResolvedApp] = []
    state: dict[str, str] = {}

    def visit(app: ResolvedApp, chain: list[str]) -> None:
        s = state.get(app.name)
        if s == "done":
            return
        if s == "visiting":
            raise SetupError(
                "Circular app dependency: " + " -> ".join([*chain, app.name]),
                phase=PHASE_RESOLVE,
                retry_safe=False,
            )
        state[app.name] = "visiting"
        for dep in app.meta.required_apps:
            if dep == "frappe" or dep in available:
                continue
            if dep not in by_name:
                raise SetupError(
                    f"{app.name} requires the app {dep!r}, which is not selected.",
                    phase=PHASE_RESOLVE,
                    remedy=f"Add {dep} to the application list (it must be installed before {app.name}).",
                    retry_safe=False,
                )
            visit(by_name[dep], [*chain, app.name])
        state[app.name] = "done"
        ordered.append(app)

    for app in apps:
        visit(app, [])
    return ordered


def resolve_apps(
    runner: Runner,
    ui: UI,
    apps: list[AppSpec],
    *,
    frappe_branch: str,
    frappe_version: str | None,
    db_engine: str,
    catalog: list[dict[str, object]],
    existing_apps: set[str] | None = None,
    on_progress: object | None = None,
) -> list[ResolvedApp]:
    existing_apps = existing_apps or set()
    resolved: list[ResolvedApp] = []
    problems: list[str] = []
    catalog_by_url = {normalize_url(str(c["repository"])): c for c in catalog}
    for app in apps:
        refs = list_refs(runner, app.url, phase=PHASE_RESOLVE)
        app.branch = choose_branch(ui, app, refs, frappe_branch)
        kind = "branch" if app.branch in refs["branch"] else "tag"
        from .remote import RefInfo

        ref = RefInfo(app.url, app.branch, kind, refs[kind][app.branch])
        meta = probe_app(runner, app.url, ref, phase=PHASE_RESOLVE)
        if app.name and app.name != meta.name:
            problems.append(f"{app.url} declares app name {meta.name!r}, not {app.name!r}.")
        app.name = meta.name
        entry = catalog_by_url.get(normalize_url(app.url))
        if db_engine == "postgres" and entry and entry.get("mariadb_only"):
            problems.append(f"{meta.name} supports MariaDB only; choose MariaDB or drop this app.")
        problem = _check_framework_constraint(meta, frappe_version, frappe_branch)
        if problem:
            problems.append(problem)
        resolved.append(ResolvedApp(app, meta))
    names = [r.name for r in resolved]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        problems.append(f"More than one repository provides app(s): {', '.join(sorted(dupes))}.")
    if problems:
        raise SetupError(
            "The selected applications are not compatible with this setup.",
            phase=PHASE_RESOLVE,
            cause="; ".join(problems),
            remedy="Pick app branches that match the Frappe branch (e.g. the same version-NN branch).",
            retry_safe=False,
            details=problems,
        )
    return topo_order(resolved, available=existing_apps)


# ===================================================================== fetching
def _git_out(runner: Runner, repo: Path, *args: str) -> str | None:
    result = runner.run(["git", "-C", str(repo), *args], timeout=60, phase=PHASE, quiet=True)
    return result.output.strip() if result.ok else None


def existing_app_matches(runner: Runner, bench: Path, app: ResolvedApp) -> tuple[bool, str]:
    repo = bench / "apps" / app.name
    if not repo.exists():
        return False, "absent"
    remote = _git_out(runner, repo, "remote", "get-url", "upstream") or _git_out(
        runner, repo, "remote", "get-url", "origin"
    )
    branch = _git_out(runner, repo, "rev-parse", "--abbrev-ref", "HEAD")
    if remote and normalize_url(remote) != normalize_url(app.spec.url):
        return False, f"apps/{app.name} exists with a different remote ({remote})"
    if app.meta.ref and branch and branch != "HEAD" and branch != app.meta.ref:
        return False, f"apps/{app.name} exists on branch {branch!r}, not {app.meta.ref!r}"
    return True, "present"


def apps_txt(bench: Path) -> list[str]:
    path = bench / "sites" / "apps.txt"
    if not path.is_file():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def get_app(runner: Runner, ui: UI, toolenv: ToolEnv, bench: Path, app: ResolvedApp, bench_cmd: str) -> bool:
    """Fetch one app into the bench. Returns True if it was fetched now, False if already there."""
    matches, why = existing_app_matches(runner, bench, app)
    if (bench / "apps" / app.name).exists():
        if not matches:
            raise SetupError(
                f"Cannot add {app.name}: {why}.",
                phase=PHASE,
                remedy="frappe-setup never overwrites existing apps. Remove or rename that app directory "
                "yourself, or choose the matching repository/branch.",
                retry_safe=False,
            )
        if app.name in apps_txt(bench):
            ui.success(f"{app.name} is already in the bench ({app.meta.ref})")
            return False
    argv = [bench_cmd, "get-app", "--branch", app.meta.ref, app.spec.url]
    with ui.progress(f"Fetching {app.name} ({app.meta.ref}) and building its assets") as on_line:
        result = runner.run(argv, cwd=bench, env=toolenv.env(), timeout=GET_APP_TIMEOUT, on_line=on_line, phase=PHASE)
    if not result.ok:
        raise command_error(result, phase=PHASE, message=f"bench get-app {app.name} failed")
    if not (bench / "apps" / app.name).is_dir() or app.name not in apps_txt(bench):
        raise SetupError(
            f"bench get-app reported success but {app.name} is not registered in sites/apps.txt.",
            phase=PHASE,
            retry_safe=True,
        )
    check = runner.run(
        [str(bench / "env" / "bin" / "python"), "-c", f"import {app.name}"],
        cwd=bench / "sites",
        timeout=120,
        phase=PHASE,
        quiet=True,
    )
    if not check.ok:
        raise command_error(
            check, phase=PHASE, message=f"{app.name} was fetched but cannot be imported from the bench env"
        )
    return True
