"""Site creation, app installation on the site, and site configuration."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .commands import PromptResponse, Runner, command_error
from .config import DatabaseConfig
from .dependencies import ToolEnv
from .errors import SetupError
from .ui import UI

PHASE = "site"
NEW_SITE_TIMEOUT = 3600
INSTALL_APP_TIMEOUT = 2 * 3600


def site_dir(bench: Path, site: str) -> Path:
    return bench / "sites" / site


def site_exists(bench: Path, site: str) -> bool:
    return (site_dir(bench, site) / "site_config.json").is_file()


def read_site_config(bench: Path, site: str) -> dict[str, object]:
    path = site_dir(bench, site) / "site_config.json"
    try:
        return dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise SetupError(
            f"Site configuration {path} is not readable JSON.",
            phase=PHASE,
            remedy="Inspect the file; frappe-setup will not modify it.",
            retry_safe=False,
        ) from exc


def secret_prompts(db: DatabaseConfig, db_password: str, admin_password: str) -> list[PromptResponse]:
    """Frappe's own getpass/input prompts (verified in frappe v15/v16 source)."""
    return [
        PromptResponse(r"MySQL root password:\s*$", db_password),
        PromptResponse(r"Postgres super user password:\s*$", db_password),
        PromptResponse(r"Enter mysql super user \[root\]:\s*$", db.root_user),
        PromptResponse(r"Enter postgres super user(?: \[postgres\])?:\s*$", db.root_user),
        PromptResponse(r"Set Administrator password:\s*$", admin_password),
        PromptResponse(r"Re-enter Administrator password:\s*$", admin_password),
    ]


def new_site_argv(bench_cmd: str, site: str, db: DatabaseConfig, *, set_default: bool, options: set[str]) -> list[str]:
    argv = [bench_cmd, "new-site", site]
    if db.engine == "postgres":
        argv += ["--db-type", "postgres"]
    if "--db-root-username" in options:
        argv += ["--db-root-username", db.root_user]
    else:
        argv += ["--mariadb-root-username", db.root_user]
    if db.host not in ("127.0.0.1", "localhost"):
        argv += ["--db-host", db.host]
    if db.port:
        argv += ["--db-port", str(db.port)]
    if set_default and "--set-default" in options:
        argv.append("--set-default")
    # Deliberately never: --force (drops an existing database), --admin-password /
    # --db-root-password (would expose secrets in the process list).
    return argv


def new_site_options(runner: Runner, toolenv: ToolEnv, bench: Path, bench_cmd: str) -> set[str]:
    result = runner.run(
        [bench_cmd, "new-site", "--help"], cwd=bench, env=toolenv.env(), timeout=120, phase=PHASE, quiet=True
    )
    return set(re.findall(r"(--[a-z][a-z0-9_-]+)", result.output))


def create_site(
    runner: Runner,
    ui: UI,
    toolenv: ToolEnv,
    bench: Path,
    bench_cmd: str,
    site: str,
    db: DatabaseConfig,
    *,
    db_password: str,
    admin_password: str,
    set_default: bool,
) -> None:
    if site_exists(bench, site):
        raise SetupError(f"Site {site} already exists.", phase=PHASE, retry_safe=False)
    options = new_site_options(runner, toolenv, bench, bench_cmd)
    for forbidden in ("--force",):
        options.discard(forbidden)
    argv = new_site_argv(bench_cmd, site, db, set_default=set_default, options=options)
    with ui.progress(f"Creating site {site} (database, schema and fixtures)") as on_line:
        result = runner.run_with_prompts(
            argv,
            secret_prompts(db, db_password, admin_password),
            cwd=bench,
            env=toolenv.env(),
            timeout=NEW_SITE_TIMEOUT,
            on_line=on_line,
            phase=PHASE,
        )
    if not result.ok:
        raise command_error(result, phase=PHASE, message=f"bench new-site {site} failed")
    if not site_exists(bench, site):
        raise SetupError(
            f"bench new-site finished but {site}/site_config.json was not created.", phase=PHASE, retry_safe=False
        )
    config = read_site_config(bench, site)
    if not config.get("db_name"):
        raise SetupError(
            f"{site}/site_config.json has no db_name; the site is incomplete.", phase=PHASE, retry_safe=False
        )


def installed_apps(runner: Runner, toolenv: ToolEnv, bench: Path, bench_cmd: str, site: str) -> list[str]:
    result = runner.run(
        [bench_cmd, "--site", site, "list-apps"], cwd=bench, env=toolenv.env(), timeout=300, phase=PHASE, quiet=True
    )
    if not result.ok:
        raise command_error(
            result,
            phase=PHASE,
            message=f"Could not list apps installed on {site}",
            remedy="Check that the database server is running and the site's database is reachable.",
        )
    return parse_list_apps(result.output)


def parse_list_apps(output: str) -> list[str]:
    """`bench list-apps` prints 'frappe 15.x.x version-15' (or just names in older releases)."""
    apps: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith(("INFO", "WARN", "DEBUG")) or ":" in line.split(" ")[0]:
            continue
        token = line.split()[0]
        if re.fullmatch(r"[a-z][a-z0-9_]*", token) and token not in apps:
            apps.append(token)
    return apps


def install_app(runner: Runner, ui: UI, toolenv: ToolEnv, bench: Path, bench_cmd: str, site: str, app: str) -> None:
    with ui.progress(f"Installing {app} on {site}") as on_line:
        result = runner.run(
            [bench_cmd, "--site", site, "install-app", app],
            cwd=bench,
            env=toolenv.env(),
            timeout=INSTALL_APP_TIMEOUT,
            on_line=on_line,
            phase="site_apps",
        )
    if not result.ok:
        raise command_error(result, phase="site_apps", message=f"Installing {app} on {site} failed")


def migrate(runner: Runner, ui: UI, toolenv: ToolEnv, bench: Path, bench_cmd: str, site: str) -> None:
    with ui.progress(f"Running migrations on {site}") as on_line:
        result = runner.run(
            [bench_cmd, "--site", site, "migrate"],
            cwd=bench,
            env=toolenv.env(),
            timeout=INSTALL_APP_TIMEOUT,
            on_line=on_line,
            phase="site_apps",
        )
    if not result.ok:
        raise command_error(result, phase="site_apps", message=f"bench migrate on {site} failed")


def set_site_config(
    runner: Runner, toolenv: ToolEnv, bench: Path, bench_cmd: str, site: str, key: str, value: str, *, parse: bool
) -> None:
    argv = [bench_cmd, "--site", site, "set-config", key, value]
    if parse:
        argv.insert(-2, "--parse")
    result = runner.run(argv, cwd=bench, env=toolenv.env(), timeout=120, phase="configure")
    if not result.ok:
        raise command_error(result, phase="configure", message=f"Could not set {key} for {site}")


def use_site(runner: Runner, toolenv: ToolEnv, bench: Path, bench_cmd: str, site: str) -> None:
    result = runner.run([bench_cmd, "use", site], cwd=bench, env=toolenv.env(), timeout=120, phase="configure")
    if not result.ok:
        raise command_error(result, phase="configure", message=f"Could not make {site} the default site")
