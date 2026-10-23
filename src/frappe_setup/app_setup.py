"""Application resolution (before installation) and fetching into the bench."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .commands import Runner, command_error
from .compatibility import parse_version, satisfies, spec
from .config import AppSpec
from .dependencies import ToolEnv
from .errors import SetupError, UserAbort
from .remote import AppMetadata, list_refs, probe_app
from .ui import UI
from .validation import normalize_url, repo_basename, validate_branch, validate_git_url

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


OTHER_REF = "__other__"


def _ask_branch(
    ui: UI, app: AppSpec, refs: dict[str, dict[str, str]], frappe_branch: str, exclude: tuple[str, ...] = ()
) -> str:
    """Let the user pick a branch from the repository (or type any existing branch/tag)."""
    notable = [b for b in notable_branches(refs, limit=20) if b not in exclude]
    if not notable and not refs["tag"]:
        raise SetupError(f"{app.url} has no branches or tags.", phase=PHASE_RESOLVE, retry_safe=False)
    preferred = [b for b in notable if b in (frappe_branch, "main", "master", "develop")]
    options = [(b, b) for b in notable] + [(OTHER_REF, "Another branch or tag (type its name)")]
    choice = ui.choose(
        f"Branch for {app.key}", options, default=preferred[0] if preferred else (notable or [OTHER_REF])[0]
    )
    while choice == OTHER_REF:
        typed = ui.ask(f"Branch or tag of {app.key}", validate=validate_branch)
        if typed in refs["branch"] or typed in refs["tag"]:
            return typed
        ui.error(f"{typed!r} does not exist in {app.url}.")
    return choice


def choose_branch(ui: UI, app: AppSpec, refs: dict[str, dict[str, str]], frappe_branch: str) -> str:
    """Pick the app branch. Explicit values are verified; nothing is substituted silently."""
    if app.branch:
        validate_branch(app.branch)
        if app.branch in refs["branch"] or app.branch in refs["tag"]:
            return app.branch
        if not ui.interactive:
            raise SetupError(
                f"Branch or tag {app.branch!r} does not exist in {app.url}.",
                phase=PHASE_RESOLVE,
                remedy="Available branches include: " + ", ".join(notable_branches(refs)),
                retry_safe=False,
            )
        ui.warn(f"{app.key}: branch or tag {app.branch!r} does not exist in {app.url}. Choose the branch to use:")
        return _ask_branch(ui, app, refs, frappe_branch)
    same = default_branch_for(refs, frappe_branch)
    if same:
        return same
    notable = notable_branches(refs, limit=20)
    if not ui.interactive:
        raise SetupError(
            f"{app.url} has no {frappe_branch!r} branch; choose one explicitly.",
            phase=PHASE_RESOLVE,
            remedy=(
                f"Pass the branch, e.g. {'--app ' + app.key if app.source == 'catalog' else '--app-url ' + app.url}"
                f"@{notable[0] if notable else '<branch>'} (available include: {', '.join(notable[:10])})."
            ),
            retry_safe=False,
        )
    ui.warn(f"{app.key}: no {frappe_branch!r} branch. Choose the branch to use:")
    return _ask_branch(ui, app, refs, frappe_branch)


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


RETRY, NEW_URL, OTHER_BRANCH, SKIP, CANCEL = "retry", "url", "branch", "skip", "cancel"


def _norm(name: str) -> str:
    return name.strip().lower().replace("-", "_")


def _recover(ui: UI, label: str, problem: str, options: list[tuple[str, str]]) -> str:
    """Interactive: explain the problem and let the user fix it instead of aborting the run."""
    ui.error(problem)
    choices = [
        *options,
        (SKIP, f"Skip {label} (do not install it)"),
        (CANCEL, "Cancel the installation"),
    ]
    choice = ui.choose(f"How do you want to continue with {label}?", choices, default=choices[0][0])
    if choice == CANCEL:
        raise UserAbort(f"Installation cancelled while resolving {label}.")
    return choice


def _ask_url(ui: UI, prompt: str, default: str | None = None) -> str:
    return ui.ask(prompt, default if default is not None else "", validate=lambda s: validate_git_url(s) if s else s)


def _resolve_one(
    runner: Runner,
    ui: UI,
    app: AppSpec,
    *,
    frappe_branch: str,
    frappe_version: str | None,
    db_engine: str,
    catalog_by_url: dict[str, dict[str, object]],
) -> tuple[ResolvedApp | None, list[str]]:
    """Resolve one app. Interactive problems become questions; otherwise they are returned."""
    from .remote import RefInfo

    tried: list[str] = []
    while True:
        try:
            refs = list_refs(runner, app.url, phase=PHASE_RESOLVE)
        except SetupError as exc:
            if not ui.interactive:
                raise
            why = f"{exc.message}. {exc.cause or ''}".strip()
            choice = _recover(ui, app.key, why, [(RETRY, "Try again"), (NEW_URL, "Enter a different Git URL")])
            if choice == SKIP:
                return None, []
            if choice == NEW_URL:
                app.url = _ask_url(ui, f"Git URL for {app.key}", app.url) or app.url
            continue
        app.branch = choose_branch(ui, app, refs, frappe_branch)
        kind = "branch" if app.branch in refs["branch"] else "tag"
        ref = RefInfo(app.url, app.branch, kind, refs[kind][app.branch])
        try:
            meta = probe_app(runner, app.url, ref, phase=PHASE_RESOLVE)
        except SetupError as exc:
            if not ui.interactive:
                raise
            why = f"{exc.message} {exc.cause or ''}".strip()
            choice = _recover(
                ui,
                app.key,
                why,
                [(OTHER_BRANCH, "Choose another branch"), (NEW_URL, "Enter a different Git URL"), (RETRY, "Try again")],
            )
            if choice == SKIP:
                return None, []
            if choice == NEW_URL:
                app.url = _ask_url(ui, f"Git URL for {app.key}", app.url) or app.url
            if choice == OTHER_BRANCH:
                tried.append(app.branch)
                app.branch = _ask_branch(ui, app, refs, frappe_branch, exclude=tuple(tried))
            continue

        problems: list[str] = []
        if app.name and _norm(app.name) != meta.name:
            problems.append(f"{app.url} declares app name {meta.name!r}, not {app.name!r}.")
        entry = catalog_by_url.get(normalize_url(app.url))
        if db_engine == "postgres" and entry and entry.get("mariadb_only"):
            if ui.interactive:
                _recover(ui, meta.name, f"{meta.name} supports MariaDB only, but this site uses PostgreSQL.", [])
                return None, []
            problems.append(f"{meta.name} supports MariaDB only; choose MariaDB or drop this app.")
        mismatch = _check_framework_constraint(meta, frappe_version, frappe_branch)
        if mismatch and ui.interactive:
            choice = _recover(ui, meta.name, mismatch, [(OTHER_BRANCH, "Choose another branch of this app")])
            if choice == SKIP:
                return None, []
            tried.append(app.branch)
            app.branch = _ask_branch(ui, app, refs, frappe_branch, exclude=tuple(tried))
            continue
        if mismatch:
            problems.append(mismatch)
        app.name = meta.name
        return ResolvedApp(app, meta), problems


def _dependency_spec(
    ui: UI, dependent: ResolvedApp, dep: str, catalog_by_key: dict[str, dict[str, object]]
) -> tuple[AppSpec | None, str | None]:
    """An app that `dependent` requires but that is not selected: offer to add it.

    Returns (spec to add, None), or (None, problem) when it cannot or may not be added.
    """
    entry = catalog_by_key.get(dep)
    url = str(entry["repository"]) if entry else dependent.meta.required_app_sources.get(dep)
    title = f"{entry['title']} ({dep})" if entry else dep
    source = "catalog" if entry else "custom"
    if not ui.interactive:
        if url and ui.assume_yes:
            ui.info(f"{dependent.name} requires {title}; adding it from {url} (--yes).")
            return AppSpec(key=dep, url=url, source=source), None
        flag = f"--app {dep}" if entry else f"--app-url {url or '<Git URL of ' + dep + '>'}"
        hint = " or rerun with --yes to add it automatically" if url else ""
        return None, (
            f"{dependent.name} requires the app {dep!r}, which is not selected: add {flag} "
            f"(it is installed before {dependent.name}){hint}."
        )
    ui.warn(f"{dependent.name} requires the app {title}, which is not selected.")
    if url:
        if ui.confirm(f"Add {title} from {url} and install it before {dependent.name}?", default=True):
            return AppSpec(key=dep, url=url, source=source), None
        return None, None
    ui.info(f"{dep} is not one of the official apps known to this tool; enter its Git repository URL.")
    typed = _ask_url(ui, f"Git URL for {dep} (empty = do not add it)")
    if typed:
        return AppSpec(key=repo_basename(typed) or dep, url=typed, source="custom"), None
    return None, None


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
    """Resolve every app and its required apps, in install order.

    Interactive runs ask how to continue on each problem (add a required app, pick another
    branch or URL, skip the app) instead of aborting; non-interactive runs report every
    problem at once before anything is installed.
    """
    available = {_norm(a) for a in (existing_apps or set())}
    catalog_by_url = {normalize_url(str(c["repository"])): c for c in catalog}
    catalog_by_key = {_norm(str(c["key"])): c for c in catalog}
    queue = list(apps)
    resolved: list[ResolvedApp] = []
    problems: list[str] = []

    def selected(dep: str) -> bool:
        return any(r.name == dep for r in resolved) or any(_norm(a.name or a.key) == dep for a in queue)

    while queue:
        spec_ = queue.pop(0)
        app, found = _resolve_one(
            runner,
            ui,
            spec_,
            frappe_branch=frappe_branch,
            frappe_version=frappe_version,
            db_engine=db_engine,
            catalog_by_url=catalog_by_url,
        )
        problems += found
        if app is None:
            ui.warn(f"{spec_.key} will not be installed.")
            continue
        twin = next((r for r in resolved if r.name == app.name), None)
        if twin is not None:
            if normalize_url(twin.spec.url) == normalize_url(app.spec.url):
                continue  # selected twice (e.g. as a dependency and by hand)
            msg = f"Both {twin.spec.url} and {app.spec.url} provide the app {app.name!r}."
            if ui.interactive:
                ui.warn(msg + f" Keeping {twin.spec.url}.")
                continue
            problems.append(msg)
            continue
        resolved.append(app)
        for dep in app.meta.required_apps:
            dep = _norm(dep)
            if dep == "frappe" or dep in available or selected(dep):
                continue
            added, problem = _dependency_spec(ui, app, dep, catalog_by_key)
            if added is not None:
                queue.insert(0, added)  # resolved next; topo_order installs it first
                continue
            if problem:
                problems.append(problem)
                continue
            # Interactive and declined: the app cannot work without it.
            _recover(ui, app.name, f"{app.name} cannot be installed without {dep}.", [])
            resolved.remove(app)
            ui.warn(f"{app.name} will not be installed.")
            break

    if problems:
        raise SetupError(
            "The selected applications cannot be installed as requested. Nothing was changed.",
            phase=PHASE_RESOLVE,
            cause="; ".join(problems),
            remedy="Fix the items above (or run interactively to be asked about each one), then rerun.",
            retry_safe=False,
            details=problems,
        )
    return topo_order(resolved, available=available)


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
