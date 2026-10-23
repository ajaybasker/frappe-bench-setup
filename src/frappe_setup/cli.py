"""Command-line interface.

frappe-setup            interactive wizard (same as `install`)
frappe-setup check      read-only compatibility report
frappe-setup install    wizard, or fully non-interactive with --non-interactive
frappe-setup status     configured benches/sites and the state of past runs
frappe-setup doctor     diagnose common problems; --share writes a redacted report
frappe-setup resume     continue the most recent incomplete installation
frappe-setup logs       list / show diagnostic logs (redacted)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from . import __version__
from .commands import Runner
from .compatibility import load_catalog, load_matrix
from .config import (
    ENV_ADMIN_PASSWORD,
    ENV_DB_ROOT_PASSWORD,
    AppSpec,
    DatabaseConfig,
    InstallConfig,
    Secrets,
    read_secret_file,
    secret_from_env,
)
from .errors import SetupError, UserAbort, ValidationError
from .logging_config import current_log_file, list_logs, setup_logging
from .redaction import REDACTOR, redact
from .ui import UI, ScriptedUI

app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    rich_markup_mode="rich",
    help="Guided, verified installer for Frappe Framework development environments.",
    context_settings={"help_option_names": ["-h", "--help"]},
)

EXIT_FAILURE, EXIT_USAGE, EXIT_INTERRUPTED = 1, 2, 130


def _interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _fail(ui: UI, exc: BaseException) -> None:
    if isinstance(exc, SetupError):
        if not exc.log_file and current_log_file():
            exc.log_file = str(current_log_file())
        ui.show_error(exc)
        raise typer.Exit(EXIT_FAILURE)
    if isinstance(exc, (UserAbort, ValidationError)):
        ui.error(redact(str(exc)))
        log = current_log_file()
        if log:
            ui.info(f"[dim]Log: {log}[/]")
        raise typer.Exit(EXIT_USAGE)
    raise exc


def _interrupted(ui: UI) -> None:
    ui.error("Interrupted. Nothing further was changed; running commands were stopped.")
    ui.info("Continue later with: [bold]frappe-setup resume[/]")
    log = current_log_file()
    if log:
        ui.info(f"[dim]Log: {log}[/]")
    raise typer.Exit(EXIT_INTERRUPTED)


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    version: Annotated[bool, typer.Option("--version", help="Show the version and exit.")] = False,
) -> None:
    if version:
        typer.echo(f"frappe-setup {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        ctx.invoke(install)


# ===================================================================== check
@app.command()
def check(
    frappe_branch: Annotated[
        str | None, typer.Option("--frappe-branch", "-b", help="Branch to check (default: all in the matrix).")
    ] = None,
    parent_dir: Annotated[
        str, typer.Option("--parent-dir", help="Where the bench would be created (for disk checks).")
    ] = "~/frappe-projects",
    db_engine: Annotated[str, typer.Option("--db-engine", help="mariadb or postgres.")] = "mariadb",
    offline: Annotated[bool, typer.Option("--offline", help="Do not contact upstream repositories.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Inspect compatibility without modifying the machine."""
    from .installer import collect_facts
    from .preflight import evaluate
    from .remote import probe_frappe, resolve_ref
    from .validation import expand_path

    setup_logging("check")
    # With --json, human-readable progress goes to stderr so stdout stays valid JSON.
    ui = UI(console=Console(stderr=True, highlight=False)) if as_json else UI()
    runner = Runner()
    matrix = load_matrix()
    try:
        parent = expand_path(parent_dir)
        branches = [frappe_branch] if frappe_branch else list(matrix.branches)
        for name in branches:
            if matrix.branch(name) is None:
                raise ValidationError(f"Unknown branch {name!r}; known: {', '.join(matrix.branches)}")
        with ui.progress("Inspecting this machine (read-only)"):
            facts = collect_facts(runner, matrix, parent=parent)
        results = {}
        for name in branches:
            upstream = None
            if not offline and facts.tool("git").found:
                try:
                    with ui.progress(f"Reading {name} requirements from upstream"):
                        upstream = probe_frappe(
                            runner, matrix.frappe_repository, resolve_ref(runner, matrix.frappe_repository, name)
                        )
                except SetupError as exc:
                    ui.warn(f"Could not read upstream requirements for {name}: {exc.message} (using the matrix)")
            branch = matrix.branch(name)
            assert branch is not None
            report = evaluate(facts, matrix, branch, upstream=upstream, db_engine=db_engine, target=parent)
            results[name] = (report, upstream)
        if as_json:
            out = {
                name: {
                    "upstream": None
                    if up is None
                    else {
                        "version": up.version,
                        "requires_python": up.requires_python,
                        "node": up.node_engine,
                        "sha": up.sha,
                    },
                    "requirements": {
                        "python": str(r.requirements.python),
                        "node": str(r.requirements.node),
                        "mariadb": str(r.requirements.mariadb),
                        "redis": str(r.requirements.redis),
                    },
                    "blocking": [i.name for i in r.blocking],
                    "fixable": [i.name for i in r.fixable],
                    "items": [i.__dict__ for i in r.items],
                }
                for name, (r, up) in results.items()
            }
            typer.echo(json.dumps(out, indent=2))
            return
        for name, (report, upstream) in results.items():
            branch = matrix.branch(name)
            assert branch is not None
            title = f"{branch.label}"
            if upstream:
                title += f" — upstream frappe {upstream.version} @ {upstream.sha[:10]}"
            ui.check_table(report.items, title=title)
            for note in report.requirements.notes:
                ui.warn(note)
            if report.blocking:
                ui.error(f"{name}: blocked by {', '.join(i.name for i in report.blocking)}")
            elif report.fixable:
                ui.warn(
                    f"{name}: installable; frappe-setup would install/fix: {', '.join(i.name for i in report.fixable)}"
                )
            else:
                ui.success(f"{name}: all prerequisites satisfied")
        ui.info("[dim]Legend: ✔ ok   ! warning   ↻ will be installed/fixed (with your permission)   ✘ blocking[/]")
        ui.info("[dim]Nothing was changed. Log: " + str(current_log_file()) + "[/]")
    except KeyboardInterrupt:
        _interrupted(ui)
    except (SetupError, UserAbort, ValidationError) as exc:
        _fail(ui, exc)


# ===================================================================== install
def _secrets(admin_file: Path | None, db_file: Path | None) -> Secrets:
    secrets = Secrets(
        admin_password=read_secret_file(admin_file) if admin_file else secret_from_env(ENV_ADMIN_PASSWORD),
        db_root_password=read_secret_file(db_file) if db_file else secret_from_env(ENV_DB_ROOT_PASSWORD),
    )
    REDACTOR.register(secrets.admin_password)
    REDACTOR.register(secrets.db_root_password)
    return secrets


def _apps_from_args(apps: list[str], app_urls: list[str], erpnext: bool) -> list[AppSpec] | None:
    from .prompts import catalog_app, parse_app_arg
    from .validation import repo_basename, validate_git_url

    if not apps and not app_urls and not erpnext:
        return None
    catalog = load_catalog()
    result: list[AppSpec] = []
    if erpnext and not any(parse_app_arg(a)[0] == "erpnext" for a in apps):
        result.append(catalog_app("erpnext", None, catalog))
    for item in apps:
        key, branch = parse_app_arg(item)
        result.append(catalog_app(key, branch, catalog))
    for item in app_urls:
        url, branch = parse_app_arg(item)
        validate_git_url(url)
        result.append(AppSpec(key=repo_basename(url), url=url, branch=branch, source="custom"))
    return result


@app.command()
def install(
    bench_name: Annotated[
        str | None, typer.Option("--bench-name", help="Bench directory name [default: frappe-bench].")
    ] = None,
    parent_dir: Annotated[
        str | None,
        typer.Option("--parent-dir", help="Directory that will contain the bench [default: ~/frappe-projects]."),
    ] = None,
    frappe_branch: Annotated[
        str | None, typer.Option("--frappe-branch", "-b", help="version-15, version-16 or develop.")
    ] = None,
    site: Annotated[str | None, typer.Option("--site", help="Site name [default: dev.localhost].")] = None,
    db_engine: Annotated[
        str | None, typer.Option("--db-engine", help="mariadb (default) or postgres (experimental).")
    ] = None,
    db_host: Annotated[str, typer.Option("--db-host", help="Database host.")] = "127.0.0.1",
    db_port: Annotated[int | None, typer.Option("--db-port", help="Database port.")] = None,
    db_root_user: Annotated[
        str | None, typer.Option("--db-root-user", help="Database superuser [default: root].")
    ] = None,
    app_keys: Annotated[
        list[str], typer.Option("--app", help="Official app KEY[@BRANCH] (repeatable), e.g. erpnext@version-15.")
    ] = [],  # noqa: B006
    app_urls: Annotated[
        list[str], typer.Option("--app-url", help="Custom app URL[@BRANCH] (repeatable), in install order.")
    ] = [],  # noqa: B006
    erpnext: Annotated[bool, typer.Option("--erpnext", help="Shortcut for --app erpnext.")] = False,
    config_file: Annotated[Path | None, typer.Option("--config", help="YAML configuration file (no secrets).")] = None,
    admin_password_file: Annotated[
        Path | None,
        typer.Option(
            "--admin-password-file",
            help=f"File with the Administrator password (mode 600). Or set {ENV_ADMIN_PASSWORD}.",
        ),
    ] = None,
    db_root_password_file: Annotated[
        Path | None,
        typer.Option(
            "--db-root-password-file",
            help=f"File with the DB superuser password (mode 600). Or set {ENV_DB_ROOT_PASSWORD}.",
        ),
    ] = None,
    developer_mode: Annotated[
        bool | None, typer.Option("--developer-mode/--no-developer-mode", help="Enable developer_mode on the site.")
    ] = None,
    set_default: Annotated[
        bool, typer.Option("--set-default/--no-set-default", help="Make the site the bench default.")
    ] = True,
    verify_http: Annotated[
        bool, typer.Option("--verify-http/--no-verify-http", help="Start the bench briefly and ping the site.")
    ] = True,
    wkhtmltopdf: Annotated[
        bool, typer.Option("--wkhtmltopdf", help="Also install wkhtmltopdf (PDF printing).")
    ] = False,
    backups: Annotated[bool, typer.Option("--backups", help="Let bench set up periodic backups (cron).")] = False,
    reuse_existing_bench: Annotated[
        bool, typer.Option("--reuse-existing-bench", help="Allow reusing a compatible existing bench.")
    ] = False,
    non_interactive: Annotated[
        bool, typer.Option("--non-interactive", help="Never prompt; all values from flags/config/env.")
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Authorize planned changes (including sudo package installs) without asking."),
    ] = False,
    allow_untested_platform: Annotated[
        bool, typer.Option("--allow-untested-platform", help="Proceed on platforms not tested by this project.")
    ] = False,
    allow_root: Annotated[
        bool, typer.Option("--allow-root", help="Allow running as root (disposable containers only).")
    ] = False,
    allow_db_version_mismatch: Annotated[
        bool,
        typer.Option("--allow-db-version-mismatch", help="Proceed with an installed MariaDB older than documented."),
    ] = False,
    cleanup: Annotated[
        str | None,
        typer.Option(
            "--cleanup", help="What to do with an incomplete bench from an earlier run: quarantine | delete | abort."
        ),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Stream command output.")] = False,
) -> None:
    """Install a bench and site (interactive wizard unless --non-interactive)."""
    from .installer import Installer, InstallOptions, collect_facts
    from .prompts import PHASE_STEPS, WIZARD_STEPS, Presets, ask_admin_password, run_wizard

    log = setup_logging("install")
    interactive = not non_interactive and _interactive_terminal()
    ui: UI = UI(assume_yes=yes, verbose=verbose) if interactive else ScriptedUI(assume_yes=yes)
    ui.verbose = verbose
    runner = Runner(verbose=verbose)
    try:
        if cleanup not in (None, "quarantine", "delete", "abort"):
            raise ValidationError("--cleanup must be quarantine, delete or abort.")
        secrets = _secrets(admin_password_file, db_root_password_file)
        matrix = load_matrix()
        options = InstallOptions(allow_untested_platform, allow_root, allow_db_version_mismatch, cleanup, verbose)
        base = InstallConfig.from_yaml(config_file) if config_file else InstallConfig()
        cli_apps = _apps_from_args(app_keys, app_urls, erpnext)
        extra: dict[str, object] = {
            "set_default_site": set_default,
            "verify_http": verify_http,
            "install_wkhtmltopdf": wkhtmltopdf or base.install_wkhtmltopdf,
            "enable_backups": backups or base.enable_backups,
            "allow_untested_platform": allow_untested_platform,
            "allow_root": allow_root,
        }
        if interactive:
            ui.banner()
            ui.info(f"[dim]Log file: {log}[/]")
            presets = Presets(
                bench_name=bench_name or (base.bench_name if config_file else None),
                parent_dir=parent_dir or (base.parent_dir if config_file else None),
                frappe_branch=frappe_branch or (base.frappe_branch if config_file else None),
                site_name=site or (base.site_name if config_file else None),
                db_engine=db_engine or (base.database.engine if config_file else None),
                db_root_user=db_root_user or (base.database.root_user if config_file else None),
                apps=cli_apps if cli_apps is not None else (base.apps if config_file else None),
                developer_mode=developer_mode,
                extra=extra,
            )
            with ui.progress("Inspecting this machine (read-only)"):
                facts = collect_facts(runner, matrix, parent=None)
            config = run_wizard(ui, facts, matrix, presets, secrets)
            config.database.host, config.database.port = db_host, db_port
            config.reuse_existing_bench = config.reuse_existing_bench or reuse_existing_bench
        else:
            config = base
            config.bench_name = bench_name or config.bench_name
            config.parent_dir = parent_dir or config.parent_dir
            config.frappe_branch = frappe_branch or config.frappe_branch
            config.site_name = site or config.site_name
            config.database = DatabaseConfig(
                engine=db_engine or config.database.engine,
                host=db_host if db_host != "127.0.0.1" else config.database.host,
                port=db_port or config.database.port,
                root_user=db_root_user or config.database.root_user,
            )
            if cli_apps is not None:
                config.apps = cli_apps
            if developer_mode is not None:
                config.developer_mode = developer_mode
            config.set_default_site, config.verify_http = set_default, verify_http
            config.install_wkhtmltopdf = bool(extra["install_wkhtmltopdf"])
            config.enable_backups = bool(extra["enable_backups"])
            config.reuse_existing_bench = reuse_existing_bench or config.reuse_existing_bench
            config.validate()
            ui.info(f"frappe-setup {__version__} (non-interactive). Log file: {log}")

        def _ask_db(user: str) -> str:
            return ui.ask_secret(f"{user} database password")

        def _ask_admin() -> str:
            ask_admin_password(ui, secrets)
            assert secrets.admin_password
            return secrets.admin_password

        installer = Installer(
            config,
            secrets,
            ui,
            runner,
            options=options,
            matrix=matrix,
            ask_db_password=_ask_db if interactive else None,
            ask_admin_password=_ask_admin if interactive else None,
        )
        if interactive:
            _attach_steps(installer, ui, PHASE_STEPS, WIZARD_STEPS)
        outcome = installer.run()
        _final_report(ui, outcome, config)
    except KeyboardInterrupt:
        _interrupted(ui)
    except (SetupError, UserAbort, ValidationError) as exc:
        _fail(ui, exc)


def _attach_steps(installer: object, ui: UI, phase_steps: dict[str, int], titles: list[str]) -> None:
    """Print wizard step headers when the installer enters the corresponding phase."""
    original = installer.phase  # type: ignore[attr-defined]
    shown: set[str] = set()

    def phase(name: str) -> Any:
        if name in phase_steps and name not in shown:
            shown.add(name)
            number = phase_steps[name]
            ui.step(number, titles[number - 1])
        return original(name)

    installer.phase = phase  # type: ignore[attr-defined]


def _final_report(ui: UI, outcome: object, config: InstallConfig) -> None:
    from .installer import Outcome

    assert isinstance(outcome, Outcome)
    lines = [
        "[bold green]Frappe is installed and verified.[/]"
        if outcome.http_ok
        else "[bold green]Frappe is installed.[/] [yellow](HTTP check skipped)[/]",
        "",
        f"Bench:  {outcome.bench_path}",
        f"Site:   {outcome.site}   apps: {', '.join(outcome.apps)}",
        f"URL:    {outcome.url}"
        + (f"   (also http://{outcome.site}:{outcome.url.rsplit(':', 1)[-1]})" if config.set_default_site else ""),
        "Login:  Administrator / the password you chose",
        "",
        "[bold]Start the development server:[/]",
        f"  source {outcome.env_script}",
        f"  cd {outcome.bench_path}",
        "  bench start",
        "",
        "[dim]The `source` line puts the Node.js/Yarn/Bench chosen for this bench on PATH; add it to "
        "~/.bashrc if you like.[/]",
    ]
    ui.panel("\n".join(lines), title="Done", style="green")


# ===================================================================== status
@app.command()
def status(
    bench: Annotated[Path | None, typer.Option("--bench", help="Bench path (default: all known).")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Report configured benches, sites and the state of past runs."""
    from .recovery import StateStore
    from .system import inspect_bench

    ui = UI()
    store = StateStore()
    states = [s for s in store.all() if bench is None or Path(s.bench_path) == bench.expanduser().resolve()]
    paths = [Path(s.bench_path) for s in states]
    if bench is not None and bench.expanduser().resolve() not in paths:
        paths.append(bench.expanduser().resolve())
    out: list[dict[str, Any]] = []
    for path in paths:
        info = inspect_bench(path)
        state = next((s for s in states if Path(s.bench_path) == path), None)
        entry = {
            "bench": str(path),
            "exists": path.exists(),
            "frappe_branch": info.frappe_branch if info else None,
            "frappe_version": info.frappe_version if info else None,
            "apps": info.apps if info else [],
            "sites": info.sites if info else [],
            "run": None
            if state is None
            else {
                "completed": state.completed,
                "updated": state.updated_at,
                "site": state.config.get("site_name"),
                "phases": {k: p.status for k, p in state.phases.items()},
                "last_failure": None
                if not state.last_failure
                else {"phase": state.last_failure[0], "error": redact(state.last_failure[1].error or "")},
                "env_script": state.resolved.get("env_script"),
            },
        }
        out.append(entry)
    if as_json:
        typer.echo(json.dumps(out, indent=2))
        return
    if not out:
        ui.info("No installations recorded yet. Create one with: frappe-setup install")
        return
    for e in out:
        rows = [
            ("Bench", e["bench"] + ("" if e["exists"] else "  [red](missing)[/]")),
            ("Frappe", f"{e['frappe_version'] or '?'} ({e['frappe_branch'] or '?'})"),
            ("Apps", ", ".join(e["apps"]) or "-"),
            ("Sites", ", ".join(e["sites"]) or "-"),
        ]
        run = e["run"]
        if run:
            rows.append(("Last run", f"{'completed' if run['completed'] else 'incomplete'} ({run['updated']})"))
            rows.append(("Phases", " ".join(f"{k}={v}" for k, v in run["phases"].items())))
            if run["env_script"]:
                rows.append(("Start", f"source {run['env_script']} && cd {e['bench']} && bench start"))
            if run["last_failure"]:
                rows.append(("Failure", f"{run['last_failure']['phase']}: {run['last_failure']['error']}"))
                rows.append(("Next step", "frappe-setup resume"))
        ui.key_values(rows)
        ui.info("")


# ===================================================================== doctor
@app.command()
def doctor(
    bench: Annotated[Path | None, typer.Option("--bench", help="Bench to diagnose (default: most recent run).")] = None,
    share: Annotated[
        Path | None, typer.Option("--share", help="Write a redacted report to this file for sharing.")
    ] = None,
) -> None:
    """Diagnose common installation problems (read-only)."""
    from .doctor import run_doctor

    setup_logging("doctor")
    ui = UI()
    try:
        run_doctor(ui, Runner(), bench=bench, share=share)
    except KeyboardInterrupt:
        _interrupted(ui)
    except (SetupError, UserAbort, ValidationError) as exc:
        _fail(ui, exc)


# ===================================================================== resume
@app.command()
def resume(
    bench: Annotated[
        Path | None, typer.Option("--bench", help="Bench path (default: most recent incomplete run).")
    ] = None,
    admin_password_file: Annotated[Path | None, typer.Option("--admin-password-file")] = None,
    db_root_password_file: Annotated[Path | None, typer.Option("--db-root-password-file")] = None,
    non_interactive: Annotated[bool, typer.Option("--non-interactive")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
    allow_untested_platform: Annotated[bool, typer.Option("--allow-untested-platform")] = False,
    allow_root: Annotated[bool, typer.Option("--allow-root")] = False,
    allow_db_version_mismatch: Annotated[bool, typer.Option("--allow-db-version-mismatch")] = False,
    cleanup: Annotated[str | None, typer.Option("--cleanup", help="quarantine | delete | abort")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Continue the most recent incomplete installation (every phase re-checks the system first)."""
    from .installer import Installer, InstallOptions
    from .prompts import ask_admin_password
    from .recovery import StateStore

    setup_logging("resume")
    interactive = not non_interactive and _interactive_terminal()
    ui: UI = UI(assume_yes=yes, verbose=verbose) if interactive else ScriptedUI(assume_yes=yes)
    try:
        store = StateStore()
        state = store.load(bench.expanduser().resolve()) if bench else store.latest(incomplete_only=True)
        if state is None:
            raise UserAbort("No incomplete installation found. Start one with `frappe-setup install`.")
        if state.completed:
            ui.info(f"The installation at {state.bench_path} already completed; rerunning its checks (idempotent).")
        config = InstallConfig.from_dict(state.config)
        config.validate()
        failure = state.last_failure
        ui.info(f"Resuming {state.bench_path}" + (f" (last stopped in phase '{failure[0]}')" if failure else ""))
        secrets = _secrets(admin_password_file, db_root_password_file)

        def _ask_admin() -> str:
            ask_admin_password(ui, secrets)
            assert secrets.admin_password
            return secrets.admin_password

        installer = Installer(
            config,
            secrets,
            ui,
            Runner(verbose=verbose),
            store=store,
            options=InstallOptions(allow_untested_platform, allow_root, allow_db_version_mismatch, cleanup, verbose),
            ask_db_password=(lambda user: ui.ask_secret(f"{user} database password")) if interactive else None,
            ask_admin_password=_ask_admin if interactive else None,
        )
        outcome = installer.run()
        _final_report(ui, outcome, config)
    except KeyboardInterrupt:
        _interrupted(ui)
    except (SetupError, UserAbort, ValidationError) as exc:
        _fail(ui, exc)


# ===================================================================== logs
@app.command()
def logs(
    list_only: Annotated[bool, typer.Option("--list", help="List log files.")] = False,
    show: Annotated[Path | None, typer.Option("--show", help="Show this log file.")] = None,
    lines: Annotated[int, typer.Option("--lines", "-n", help="Number of trailing lines to show.")] = 80,
    path_only: Annotated[bool, typer.Option("--path", help="Print only the path of the latest log.")] = False,
) -> None:
    """Locate and inspect diagnostic logs (secrets are redacted)."""
    from .paths import logs_dir

    ui = UI()
    files = [f for f in list_logs() if not f.name.endswith(("-logs.log",))]
    if path_only:
        typer.echo(str(files[-1]) if files else str(logs_dir()))
        return
    if list_only:
        if not files:
            ui.info(f"No logs yet in {logs_dir()}")
        for f in files[-50:]:
            ui.info(f"{f}  [dim]({f.stat().st_size // 1024} KB)[/]")
        return
    target = show.expanduser() if show else (_latest_interesting(files) if files else None)
    if target is None or not target.is_file():
        ui.info(f"No logs found in {logs_dir()}")
        return
    if logs_dir().resolve() not in target.resolve().parents:
        raise typer.BadParameter("Only frappe-setup log files can be shown.")
    content = target.read_text(encoding="utf-8", errors="replace").splitlines()
    ui.info(f"[bold]{target}[/] (last {min(lines, len(content))} of {len(content)} lines)")
    for line in content[-lines:]:
        typer.echo(redact(line))


def _latest_interesting(files: list[Path]) -> Path:
    """Most recent install/resume log, falling back to the newest log."""
    for f in reversed(files):
        if "-install-" in f.name or "-resume-" in f.name:
            return f
    return files[-1]


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:  # pragma: no cover - typer normally handles this
        sys.exit(EXIT_INTERRUPTED)


if __name__ == "__main__":  # pragma: no cover
    main()
