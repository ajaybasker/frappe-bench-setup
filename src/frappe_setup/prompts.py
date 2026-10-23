"""The guided wizard. Collects and validates configuration; values given as flags are not asked."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import validation as v
from .bench_setup import classify_target
from .compatibility import FAIL, MISSING, Matrix, load_catalog
from .config import (
    DEFAULT_BENCH_NAME,
    DEFAULT_PARENT_DIR,
    DEFAULT_SITE_NAME,
    AppSpec,
    DatabaseConfig,
    InstallConfig,
    Secrets,
)
from .errors import ValidationError
from .preflight import evaluate
from .site_setup import site_exists
from .system import SystemFacts, inspect_bench
from .ui import UI

WIZARD_STEPS = [
    "System compatibility",
    "Installation directory",
    "Bench configuration",
    "Frappe version",
    "Site configuration",
    "Applications",
    "Review",
    "Dependency installation",
    "Bench and site provisioning",
    "Environment verification",
]

# Installer phase -> wizard step number (1-based)
PHASE_STEPS = {"dependencies": 8, "bench": 9, "verify": 10}


@dataclass
class Presets:
    """Values supplied on the command line; the wizard does not ask for these."""

    bench_name: str | None = None
    parent_dir: str | None = None
    frappe_branch: str | None = None
    site_name: str | None = None
    db_engine: str | None = None
    db_root_user: str | None = None
    apps: list[AppSpec] | None = None
    developer_mode: bool | None = None
    extra: dict[str, object] = field(default_factory=dict)


def system_overview(ui: UI, facts: SystemFacts, matrix: Matrix, parent: Path) -> dict[str, str]:
    """Step 1: show the machine and which branches are feasible. Returns {branch: verdict}."""
    ui.key_values(
        [
            ("OS", f"{facts.os_pretty} ({facts.arch}), kernel {facts.kernel}"),
            ("Platform status", matrix.platform_status(facts.os_id, facts.os_version, facts.arch)),
            ("CPU / RAM", f"{facts.cpu_count} cores, {facts.mem_total_mb} MB RAM, {facts.swap_total_mb} MB swap"),
            ("User", f"{facts.user}{' (root)' if facts.is_root else ''}"),
            ("Python", ", ".join(f"{p.version} [{p.source}]" for p in facts.pythons) or "none"),
            ("Node.js", ", ".join(f"{n.version} [{n.source}]" for n in facts.nodes) or "none"),
            ("MariaDB", facts.tool("mariadbd").version or facts.apt_installed.get("mariadb-server", "not installed")),
            ("Redis", facts.tool("redis-server").version or "not installed"),
            ("Bench CLI", facts.tool("bench").version or "not installed"),
            ("Existing benches", ", ".join(b.path for b in facts.benches) or "none"),
        ]
    )
    verdicts: dict[str, str] = {}
    rows = []
    for name, branch in matrix.branches.items():
        report = evaluate(facts, matrix, branch, target=parent, allow_untested_platform=True, allow_root=True)
        blocking = [i.name for i in report.blocking]
        fixes = [i.name for i in report.fixable]
        if blocking:
            verdict = "blocked: " + ", ".join(blocking)
        elif fixes:
            verdict = "will install: " + ", ".join(fixes)
        else:
            verdict = "ready"
        verdicts[name] = verdict
        rows.append((branch.label, verdict))
    ui.key_values(rows, title="Branch feasibility on this machine")
    return verdicts


def ask_parent_dir(ui: UI, presets: Presets) -> str:
    if presets.parent_dir:
        v.validate_parent_dir(presets.parent_dir)
        return presets.parent_dir
    value = ui.ask(
        "Parent directory for the bench", DEFAULT_PARENT_DIR, validate=lambda s: str(v.validate_parent_dir(s)) and s
    )
    ui.info(f"  → {v.expand_path(value)}")
    return value


def ask_bench(ui: UI, presets: Presets, parent: Path) -> tuple[str, bool]:
    """Returns (bench_name, reuse_existing)."""
    while True:
        name = presets.bench_name or ui.ask("Bench name", DEFAULT_BENCH_NAME, validate=v.validate_bench_name)
        path = v.bench_path(parent, name)
        kind = classify_target(path)
        ui.info(f"  → Full path: {path}")
        if kind == "absent":
            ui.success("Directory does not exist yet; it will be created.")
            return name, False
        if kind == "empty":
            ui.warn("The directory exists and is empty; it will be used.")
            return name, False
        if kind == "bench":
            info = inspect_bench(path)
            assert info is not None
            ui.warn(
                f"A bench already exists here: frappe {info.frappe_version or '?'} ({info.frappe_branch or '?'}), "
                f"apps: {', '.join(info.apps) or '-'}, sites: {', '.join(info.sites) or '-'}"
            )
            if ui.confirm("Reuse this bench after compatibility checks? (nothing will be deleted)", default=False):
                return name, True
        else:
            ui.error(f"{path} exists and is not a bench; it will not be overwritten.")
        if presets.bench_name:
            raise ValidationError(f"Bench {name!r} cannot be used at {path}.")
        presets.bench_name = None


def ask_branch(ui: UI, presets: Presets, matrix: Matrix, verdicts: dict[str, str], existing_branch: str | None) -> str:
    if presets.frappe_branch:
        return v.validate_branch(presets.frappe_branch)
    if existing_branch and existing_branch in matrix.branches:
        ui.info(f"The existing bench uses {existing_branch}; that branch will be used.")
        return existing_branch
    options = []
    for name, branch in matrix.branches.items():
        tested = " [tested by this project]" if matrix.tested_for(name) else ""
        unstable = " [unstable]" if branch.unstable else ""
        options.append(
            (
                name,
                f"{branch.label}{unstable}{tested} — Python {branch.requirement('python')}, "
                f"Node {branch.requirement('node')}, MariaDB {branch.requirement('mariadb')} "
                f"({verdicts.get(name, '?')})",
            )
        )
    choice = ui.choose("Frappe version", options, default=next(iter(matrix.branches)))
    if matrix.branches[choice].unstable and not ui.confirm(
        f"{choice} is a development branch and may be broken at any time. Continue?", default=False
    ):
        return ask_branch(ui, presets, matrix, verdicts, existing_branch)
    return choice


def ask_site(ui: UI, presets: Presets, bench: Path) -> tuple[str, bool]:
    name = presets.site_name or ui.ask(
        "Site name (*.localhost names work in browsers without /etc/hosts changes)",
        DEFAULT_SITE_NAME,
        validate=v.validate_site_name,
    )
    name = v.validate_site_name(name)
    exists = bench.exists() and site_exists(bench, name)
    if exists:
        ui.warn(f"Site {name} already exists in this bench; it will be reused, not recreated.")
    return name, exists


def ask_database(ui: UI, presets: Presets, need_password: bool, secrets: Secrets) -> DatabaseConfig:
    engine = presets.db_engine
    if not engine:
        engine = ui.choose(
            "Database engine",
            [
                ("mariadb", "MariaDB (recommended, required for ERPNext)"),
                ("postgres", "PostgreSQL (experimental; existing server only; Frappe-only sites)"),
            ],
            default="mariadb",
        )
    default_user = "root" if engine == "mariadb" else "postgres"
    user = presets.db_root_user or ui.ask("Database superuser", default_user, validate=v.validate_db_user)
    db = DatabaseConfig(engine=engine, root_user=user)
    if need_password and not secrets.db_root_password:
        ui.info("[dim]The password is used only to create the site's database; it is never stored or logged.[/]")
        hint = " (existing password, or a new one to set if MariaDB is fresh)" if engine == "mariadb" else ""
        secrets.db_root_password = ui.ask_secret(
            f"{user} password{hint}", validate=lambda s: v.validate_password(s, what="Database password", min_length=1)
        )
    return db


def ask_admin_password(ui: UI, secrets: Secrets) -> None:
    if not secrets.admin_password:
        secrets.admin_password = ui.ask_secret(
            "Administrator password for the new site",
            confirm=True,
            validate=lambda s: v.validate_password(s, what="Administrator password"),
        )


def parse_app_arg(value: str) -> tuple[str, str | None]:
    """'erpnext@version-15' / 'https://host/org/app@main' / 'git@host:org/app.git@dev' -> (target, branch)."""
    import re

    if "@" in value:
        left, right = value.rsplit("@", 1)
        left_is_target = "/" in left or ":" in left or re.fullmatch(r"[a-z][a-z0-9_]*", left) is not None
        if right and ":" not in right and left_is_target:
            return left, v.validate_branch(right)
    return value, None


def catalog_app(key: str, branch: str | None, catalog: list[dict[str, object]]) -> AppSpec:
    entry = next((c for c in catalog if c["key"] == key), None)
    if entry is None:
        known = ", ".join(str(c["key"]) for c in catalog)
        raise ValidationError(f"Unknown app {key!r}. Official apps: {known}. Use --app-url for other repositories.")
    return AppSpec(key=key, url=str(entry["repository"]), branch=branch, source="catalog")


def ask_apps(ui: UI, presets: Presets, frappe_branch: str, db_engine: str) -> list[AppSpec]:
    if presets.apps is not None:
        return presets.apps
    catalog = load_catalog()
    mode = ui.choose(
        "Applications",
        [
            ("none", "Frappe Framework only"),
            ("erpnext", "Frappe Framework + ERPNext"),
            ("official", "Frappe Framework + selected official apps"),
            ("custom", "Frappe Framework + custom apps (Git URLs)"),
        ],
        default="none",
    )
    apps: list[AppSpec] = []
    if mode == "erpnext":
        apps.append(catalog_app("erpnext", None, catalog))
    elif mode == "official":
        options = [
            (str(c["key"]), f"{c['title']} — {c['description']}")
            for c in catalog
            if not (db_engine == "postgres" and c.get("mariadb_only"))
        ]
        for key in ui.choose_many("Select apps", options):
            apps.append(catalog_app(key, None, catalog))
        ui.info(
            "[dim]Branches are resolved from each repository; apps without a "
            f"{frappe_branch} branch will ask which branch to use.[/]"
        )
    if mode in ("custom", "official") and (
        mode == "custom" or ui.confirm("Add custom apps by Git URL as well?", default=False)
    ):
        ui.info("Enter custom apps in installation order (dependencies first). Leave the URL empty to finish.")
        while True:
            url = ui.ask("Git repository URL (https:// or SSH)", "")
            if not url:
                break
            try:
                v.validate_git_url(url)
            except ValidationError as exc:
                ui.error(str(exc))
                continue
            branch = ui.ask("Branch or tag", frappe_branch, validate=v.validate_branch)
            apps.append(AppSpec(key=v.repo_basename(url), url=url, branch=branch, source="custom"))
    return apps


def run_wizard(
    ui: UI,
    facts: SystemFacts,
    matrix: Matrix,
    presets: Presets,
    secrets: Secrets,
) -> InstallConfig:
    ui.set_steps(len(WIZARD_STEPS))
    ui.step(1, WIZARD_STEPS[0])
    parent_guess = v.expand_path(presets.parent_dir or DEFAULT_PARENT_DIR)
    verdicts = system_overview(ui, facts, matrix, parent_guess)
    hard = [
        i
        for i in evaluate(
            facts,
            matrix,
            next(iter(matrix.branches.values())),
            target=parent_guess,
            allow_untested_platform=bool(presets.extra.get("allow_untested_platform")),
            allow_root=bool(presets.extra.get("allow_root")),
        ).items
        if i.status in (FAIL, MISSING)
        and not i.fixable
        and i.name in ("Operating system", "Memory", "User", "Disk space")
    ]
    if hard:
        ui.check_table(hard, title="This machine cannot be used")
        from .errors import IncompatibleEnvironment

        raise IncompatibleEnvironment(
            "The machine does not meet the basic requirements. Nothing was changed.",
            phase="preflight",
            cause="; ".join(f"{i.name}: {i.message}" for i in hard),
            remedy=" / ".join(i.remedy for i in hard if i.remedy),
            retry_safe=False,
        )

    ui.step(2, WIZARD_STEPS[1])
    parent_dir = ask_parent_dir(ui, presets)
    parent = v.expand_path(parent_dir)

    ui.step(3, WIZARD_STEPS[2])
    bench_name, reuse = ask_bench(ui, presets, parent)
    bench = v.bench_path(parent, bench_name)
    existing = inspect_bench(bench) if reuse else None

    ui.step(4, WIZARD_STEPS[3])
    branch = ask_branch(ui, presets, matrix, verdicts, existing.frappe_branch if existing else None)
    spec = matrix.branch(branch)
    if spec is not None:
        ui.info(
            f"Documented requirements for {branch}: Python {spec.requirement('python')}, Node.js "
            f"{spec.requirement('node')}, MariaDB {spec.requirement('mariadb')}, Redis {spec.requirement('redis')}"
        )

    ui.step(5, WIZARD_STEPS[4])
    site, site_already = ask_site(ui, presets, bench)
    db = ask_database(ui, presets, need_password=not site_already, secrets=secrets)
    if not site_already:
        ask_admin_password(ui, secrets)
    developer_mode = presets.developer_mode
    if developer_mode is None:
        developer_mode = ui.confirm("Enable developer mode on the site?", default=True)

    ui.step(6, WIZARD_STEPS[5])
    apps = ask_apps(ui, presets, branch, db.engine)

    config = InstallConfig(
        bench_name=bench_name,
        parent_dir=parent_dir,
        frappe_branch=branch,
        site_name=site,
        database=db,
        apps=apps,
        developer_mode=developer_mode,
        reuse_existing_bench=reuse,
        **{k: val for k, val in presets.extra.items() if k in InstallConfig.__dataclass_fields__},  # type: ignore[arg-type]
    )
    config.validate()

    ui.step(7, WIZARD_STEPS[6])
    review(ui, config)
    if not ui.confirm("Start the installation?", default=True):
        from .errors import UserAbort

        raise UserAbort("Installation cancelled before any change was made.")
    return config


def review(ui: UI, config: InstallConfig) -> None:
    apps = ", ".join(f"{a.key}@{a.branch or 'auto'}" for a in config.apps) or "none"
    ui.key_values(
        [
            ("Bench path", str(config.bench_path) + (" (reuse)" if config.reuse_existing_bench else "")),
            ("Frappe branch", config.frappe_branch),
            ("Site", config.site_name),
            (
                "Database",
                f"{config.database.engine} at {config.database.host}:{config.database.effective_port}, "
                f"superuser {config.database.root_user}",
            ),
            ("Apps", apps),
            ("Developer mode", "yes" if config.developer_mode else "no"),
            ("Default site", "yes" if config.set_default_site else "no"),
            ("HTTP verification", "yes" if config.verify_http else "no"),
        ],
        title="Review",
    )
