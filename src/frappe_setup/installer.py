"""Installation orchestrator: runs the phases in order, records state, verifies the result."""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .app_setup import ResolvedApp, apps_txt, get_app, resolve_apps
from .bench_setup import (
    bench_cli_version,
    bench_exe,
    classify_target,
    init_bench,
    validate_bench,
)
from .commands import Runner
from .compatibility import BranchSpec, Matrix, load_catalog, load_matrix
from .config import InstallConfig, Secrets
from .database import (
    check_mariadb,
    check_postgres,
    db_error,
    enable_root_password_auth,
    root_auth_methods,
    root_has_usable_password,
)
from .dependencies import (
    DepContext,
    Privilege,
    ToolEnv,
    blocking_after_install,
    build_toolenv,
    default_extra_path,
    ensure_mariadb_running,
    execute,
    plan,
    write_env_script,
)
from .errors import IncompatibleEnvironment, SetupError, UserAbort
from .logging_config import current_log_file, get_logger
from .preflight import PreflightReport, evaluate
from .recovery import (
    DONE,
    FAILED,
    INTERRUPTED,
    SKIPPED,
    RunState,
    StateStore,
    cleanup_candidates,
    delete_tree,
    quarantine,
)
from .redaction import REDACTOR
from .remote import FrappeMetadata, probe_frappe, resolve_ref
from .services import bench_redis
from .site_setup import (
    create_site,
    install_app,
    installed_apps,
    migrate,
    read_site_config,
    set_site_config,
    site_exists,
    use_site,
)
from .system import SystemFacts, gather_facts
from .ui import UI
from .verification import http_check


@dataclass
class InstallOptions:
    allow_untested_platform: bool = False
    allow_root: bool = False
    allow_db_version_mismatch: bool = False
    cleanup: str | None = None  # None (ask) | "quarantine" | "delete" | "abort"
    verbose: bool = False


@dataclass
class Outcome:
    bench_path: Path
    site: str
    url: str
    env_script: Path | None
    http_ok: bool | None
    http_detail: str = ""
    apps: list[str] = field(default_factory=list)
    versions: dict[str, str] = field(default_factory=dict)


def all_apt_packages(matrix: Matrix) -> list[str]:
    pkgs: list[str] = []
    for group in matrix.apt_packages.values():
        pkgs += [p for p in group if p not in pkgs]
    return [
        *pkgs,
        "python3.10-venv",
        "python3.11-venv",
        "python3.12-venv",
        "python3.13-venv",
        "python3.14-venv",
        "python3.12-dev",
    ]


def collect_facts(
    runner: Runner, matrix: Matrix, *, parent: Path | None = None, extra: list[Path] | None = None
) -> SystemFacts:
    roots = [Path.home()]
    if parent and parent not in roots:
        roots.insert(0, parent)
    return gather_facts(
        runner,
        ports=sorted(set(matrix.ports.values())),
        bench_roots=roots,
        extra_path=(extra or []) + default_extra_path(),
        apt_packages=all_apt_packages(matrix),
    )


class Installer:
    def __init__(
        self,
        config: InstallConfig,
        secrets: Secrets,
        ui: UI,
        runner: Runner,
        *,
        options: InstallOptions | None = None,
        store: StateStore | None = None,
        matrix: Matrix | None = None,
        ask_db_password: Callable[[str], str] | None = None,
        ask_admin_password: Callable[[], str] | None = None,
    ) -> None:
        self.config = config
        self.secrets = secrets
        self.ui = ui
        self.runner = runner
        self.options = options or InstallOptions()
        self.store = store or StateStore()
        self.matrix = matrix or load_matrix()
        self.ask_db_password = ask_db_password
        self.ask_admin_password = ask_admin_password
        self.log = get_logger("installer")
        self.bench = config.bench_path
        self.state = self.store.load(self.bench) or RunState(
            bench_path=str(self.bench), config=config.to_dict(), tool_version=__version__
        )
        self.state.config = config.to_dict()
        self.state.tool_version = __version__
        log = current_log_file()
        if log and str(log) not in self.state.log_files:
            self.state.log_files.append(str(log))
        self.branch: BranchSpec = self._branch()
        self.upstream: FrappeMetadata | None = None
        self.facts: SystemFacts | None = None
        self.report: PreflightReport | None = None
        self.toolenv = ToolEnv.from_dict(self.state.toolenv) if self.state.toolenv else ToolEnv()
        self.privilege: Privilege | None = None
        self.apps: list[ResolvedApp] = []
        for s in (secrets.admin_password, secrets.db_root_password):
            REDACTOR.register(s)

    # ------------------------------------------------------------------ helpers
    def _branch(self) -> BranchSpec:
        spec = self.matrix.branch(self.config.frappe_branch)
        if spec is None:
            known = ", ".join(self.matrix.branches)
            raise IncompatibleEnvironment(
                f"Frappe branch {self.config.frappe_branch!r} is not in the compatibility matrix.",
                phase="preflight",
                cause="Its dependency requirements are unknown to this tool.",
                remedy=f"Choose one of: {known}, or add the branch to config/version-matrix.yaml "
                "after checking its documented requirements.",
                retry_safe=False,
            )
        return spec

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        self.store.start(self.state, name)
        try:
            yield
        except KeyboardInterrupt:
            self.store.finish(self.state, name, INTERRUPTED, "interrupted by user")
            raise
        except (SetupError, UserAbort) as exc:
            if isinstance(exc, SetupError) and not exc.phase:
                exc.phase = name
            self.store.finish(self.state, name, FAILED, str(exc))
            raise
        except Exception as exc:
            self.store.finish(self.state, name, FAILED, f"{type(exc).__name__}: {exc}")
            raise
        else:
            if self.state.phase(name).status not in (SKIPPED,):
                self.store.finish(self.state, name, DONE)

    def skip(self, name: str, why: str) -> None:
        self.store.finish(self.state, name, SKIPPED, why)

    def _db_password(self) -> str:
        if not self.secrets.db_root_password:
            if self.ask_db_password is None:
                raise UserAbort(
                    "The database superuser password is required (--db-root-password-file or "
                    "FRAPPE_SETUP_DB_ROOT_PASSWORD)."
                )
            self.secrets.db_root_password = self.ask_db_password(self.config.database.root_user)
            REDACTOR.register(self.secrets.db_root_password)
        return self.secrets.db_root_password

    def _admin_password(self) -> str:
        if not self.secrets.admin_password:
            if self.ask_admin_password is None:
                raise UserAbort(
                    "The Administrator password is required (--admin-password-file or FRAPPE_SETUP_ADMIN_PASSWORD)."
                )
            self.secrets.admin_password = self.ask_admin_password()
            REDACTOR.register(self.secrets.admin_password)
        return self.secrets.admin_password

    def _bench_cmd(self) -> str:
        return bench_exe(self.toolenv)

    # ------------------------------------------------------------------ phases
    def run_preflight(self) -> PreflightReport:
        with self.ui.progress("Inspecting this machine"):
            self.facts = collect_facts(
                self.runner, self.matrix, parent=self.config.parent_path, extra=self.toolenv.path_entries
            )
        self.report = self._evaluate()
        return self.report

    def _evaluate(self) -> PreflightReport:
        assert self.facts is not None
        large = sum(1 for a in self.config.apps if a.key in ("erpnext", "hrms", "lms", "insights", "helpdesk", "crm"))
        return evaluate(
            self.facts,
            self.matrix,
            self.branch,
            upstream=self.upstream,
            db_engine=self.config.database.engine,
            target=self.config.parent_path,
            allow_untested_platform=self.options.allow_untested_platform,
            allow_root=self.options.allow_root,
            allow_db_version_mismatch=self.options.allow_db_version_mismatch,
            want_wkhtmltopdf=self.config.install_wkhtmltopdf,
            large_apps=large,
        )

    def ensure_not_blocked(self, report: PreflightReport) -> None:
        blocking = report.blocking
        if blocking:
            self.ui.check_table(blocking, title="Blocking problems")
            raise IncompatibleEnvironment(
                "This machine cannot run the requested configuration. Nothing was changed.",
                phase="preflight",
                cause="; ".join(f"{i.name}: {i.message or i.found}" for i in blocking),
                remedy=" / ".join(i.remedy for i in blocking if i.remedy) or None,
                retry_safe=False,
            )

    def run_resolve(self) -> None:
        """Verify the framework and app branches upstream before anything long-running."""
        with self.ui.progress(f"Resolving Frappe {self.config.frappe_branch} from {self.matrix.frappe_repository}"):
            ref = resolve_ref(self.runner, self.matrix.frappe_repository, self.config.frappe_branch)
            self.upstream = probe_frappe(self.runner, self.matrix.frappe_repository, ref)
        self.ui.success(
            f"Frappe {self.config.frappe_branch}: version {self.upstream.version}, requires Python "
            f"{self.upstream.requires_python}, Node {self.upstream.node_engine} (commit {ref.sha[:10]})"
        )
        existing = set(apps_txt(self.bench)) if self.bench.exists() else set()
        if self.config.apps:
            # No spinner here: resolution may need to ask which branch to use.
            self.ui.info("Resolving application branches and dependencies…")
            self.apps = resolve_apps(
                self.runner,
                self.ui,
                self.config.apps,
                frappe_branch=self.config.frappe_branch,
                frappe_version=self.upstream.version,
                db_engine=self.config.database.engine,
                catalog=load_catalog(),
                existing_apps=existing,
            )
            self.config.apps = [a.spec for a in self.apps]
            for app in self.apps:
                deps = f" (requires {', '.join(app.meta.required_apps)})" if app.meta.required_apps else ""
                self.ui.success(f"{app.name}: {app.meta.ref} @ {app.meta.sha[:10]}{deps}")
            self.ui.info("Install order: " + " → ".join(["frappe", *[a.name for a in self.apps]]))
        self.state.resolved = {
            "frappe": {
                "ref": ref.ref,
                "sha": ref.sha,
                "version": self.upstream.version,
                "requires_python": self.upstream.requires_python,
                "node": self.upstream.node_engine,
            },
            "apps": [{"name": a.name, "url": a.spec.url, "ref": a.meta.ref, "sha": a.meta.sha} for a in self.apps],
        }
        self.state.config = self.config.to_dict()  # branches chosen during resolution
        self.store.save(self.state)

    def run_dependencies(self) -> None:
        assert self.facts is not None and self.report is not None
        self.privilege = self.privilege or Privilege(self.runner, self.ui, self.facts)
        for attempt in range(2):
            # Runtimes already chosen by preflight (e.g. a managed Node.js from an earlier,
            # interrupted run) must be on PATH for installers such as `npm install yarn`.
            self.toolenv = build_toolenv(self.report, self.toolenv)
            ctx = DepContext(
                runner=self.runner,
                ui=self.ui,
                facts=self.facts,
                matrix=self.matrix,
                branch=self.branch,
                report=self.report,
                privilege=self.privilege,
                db_engine=self.config.database.engine,
                want_wkhtmltopdf=self.config.install_wkhtmltopdf,
                want_cron=self.config.enable_backups,
                toolenv=self.toolenv,
            )
            actions = plan(ctx)
            if not actions:
                if attempt == 0:
                    self.ui.success("All prerequisites are already installed and compatible.")
                break
            execute(ctx, actions)
            self.toolenv = ctx.toolenv
            with self.ui.progress("Re-checking prerequisites"):
                self.facts = collect_facts(
                    self.runner, self.matrix, parent=self.config.parent_path, extra=self.toolenv.path_entries
                )
                self.privilege.facts = self.facts
            self.report = self._evaluate()
        remaining = [i for i in self.report.items if i.status in ("fail", "missing") and i.name != "MariaDB service"]
        if remaining:
            self.ui.check_table(remaining, title="Still missing")
            raise SetupError(
                "Some prerequisites are still missing after installation.",
                cause="; ".join(blocking_after_install(self.report)),
                remedy="See the table above; fix these manually and rerun `frappe-setup resume`.",
                retry_safe=True,
            )
        self.toolenv = build_toolenv(self.report, self.toolenv)
        if self.report.python is not None:
            self.toolenv.python = self.report.python.path
        if self.toolenv.python is None:
            raise SetupError("No compatible Python interpreter was selected.", retry_safe=True)
        self.state.toolenv = self.toolenv.to_dict()
        self.store.save(self.state)
        version = bench_cli_version(self.runner, self.toolenv)
        self.ui.success(
            f"Using Python {self.toolenv.python}, Node.js in {self.toolenv.node_bin}, Bench CLI {version or '?'}"
        )

    def run_database(self) -> None:
        db = self.config.database
        if site_exists(self.bench, self.config.site_name):
            self.skip("database", "site already exists; superuser access not needed")
            self.ui.success(f"Site {self.config.site_name} already exists; skipping the superuser check.")
            return
        if db.engine == "postgres":
            check = check_postgres(self.runner, db, self._db_password())
            if not check.ok:
                raise db_error(check, db)
            if not check.superuser:
                raise SetupError(
                    f"PostgreSQL user {db.root_user!r} is not a superuser.",
                    retry_safe=False,
                    remedy="Use a superuser account (e.g. postgres).",
                )
            self.ui.success(f"PostgreSQL {check.version}: superuser access verified")
            return
        if self.facts and db.host in ("127.0.0.1", "localhost"):
            assert self.privilege is not None
            from .dependencies import mariadb_alive

            if not mariadb_alive(self.runner):
                ctx = DepContext(
                    self.runner,
                    self.ui,
                    self.facts,
                    self.matrix,
                    self.branch,
                    self.report,  # type: ignore[arg-type]
                    self.privilege,
                )
                self.privilege.authorize(["systemctl start mariadb"], "MariaDB is installed but not running.")
                ensure_mariadb_running(ctx)
        for attempt in range(3):
            check = check_mariadb(self.runner, db, self._db_password())
            if check.ok:
                break
            if (
                check.access_denied
                and db.root_user == "root"
                and db.host in ("127.0.0.1", "localhost")
                and attempt == 0
            ) and self._offer_root_password_auth():
                continue
            if check.access_denied and self.ask_db_password is not None and attempt < 2:
                self.ui.error("The database rejected that password.")
                self.secrets.db_root_password = None
                continue
            raise db_error(check, db)
        else:  # pragma: no cover - loop always breaks or raises
            raise db_error(check, db)
        if not check.superuser:
            self.ui.warn(f"{db.root_user!r} may lack ALL PRIVILEGES WITH GRANT OPTION; site creation may fail.")
        installed = check.version or "?"
        self.ui.success(f"MariaDB {installed}: superuser access for {db.root_user!r} verified")

    def _offer_root_password_auth(self) -> bool:
        """Fresh Ubuntu MariaDB: root has socket auth only. Offer to add password auth (keeps socket auth)."""
        assert self.privilege is not None
        self.privilege.authorize(
            ["mariadb -e \"SHOW CREATE USER 'root'@'localhost'\""],
            "MariaDB rejected the password for root. frappe-setup will check whether root has a "
            "password at all (read-only).",
        )
        if root_has_usable_password(root_auth_methods(self.runner, self.privilege)):
            return False  # a password exists: the user typed the wrong one; never reset it
        self.ui.warn("MariaDB root currently has no password (socket authentication only).")
        if not self.ui.confirm(
            "Enable password authentication for MariaDB root with the password you entered? "
            "(socket login via sudo keeps working)",
            default=True,
        ):
            raise UserAbort(
                "Frappe needs password access to a MariaDB superuser. Run "
                "`sudo mariadb-secure-installation` or provide another superuser."
            )
        enable_root_password_auth(self.runner, self.privilege, self._db_password())
        self.ui.success("Password authentication enabled for MariaDB root@localhost.")
        return True

    def run_bench(self) -> None:
        kind = classify_target(self.bench)
        expect = self.upstream.version if self.upstream else None
        req = self.report.requirements.python if self.report else None

        def validate() -> None:
            check = validate_bench(
                self.runner,
                self.toolenv,
                self.bench,
                branch=self.config.frappe_branch,
                python_constraint=req,
                expect_version=expect,
            )
            if not check.ok:
                raise SetupError(
                    f"The bench at {self.bench} is not complete or not compatible.",
                    cause="; ".join(check.problems),
                    remedy="See the log for bench init output. Rerun `frappe-setup resume` after fixing the cause.",
                    retry_safe=True,
                    details=check.problems,
                )
            self.ui.success(f"Bench verified: frappe {check.frappe_version} on Python {check.python_version}")

        created_by_us = self.state.created.bench_dir and self.state.phase("bench").status != DONE
        if kind == "bench":
            check = validate_bench(
                self.runner,
                self.toolenv,
                self.bench,
                branch=self.config.frappe_branch,
                python_constraint=req,
                expect_version=expect,
            )
            if check.ok:
                if not (self.config.reuse_existing_bench or self.state.created.bench_dir):
                    if not self.ui.confirm(
                        f"A compatible bench already exists at {self.bench}. Reuse it (nothing is deleted)?",
                        default=True,
                    ):
                        raise UserAbort("Choose another bench name or directory.")
                self.config.reuse_existing_bench = True
                self.ui.success(f"Reusing existing bench {self.bench} (frappe {check.frappe_version})")
                return
            if not created_by_us:
                raise SetupError(
                    f"{self.bench} is an existing bench that does not match the request.",
                    cause="; ".join(check.problems),
                    remedy="frappe-setup never modifies existing benches it did not create. Choose another "
                    "bench name, or fix that bench manually.",
                    retry_safe=False,
                )
            self._recover_partial_bench()
            kind = classify_target(self.bench)
        elif kind == "other":
            if created_by_us:
                self._recover_partial_bench()
                kind = classify_target(self.bench)
            else:
                raise SetupError(
                    f"{self.bench} already exists and is not a bench.",
                    remedy="Choose another bench name or parent directory; existing files are never overwritten.",
                    retry_safe=False,
                )
        if kind == "empty":
            if not self.state.created.bench_dir and not self.ui.confirm(
                f"{self.bench} is an empty directory. Use it for the bench?", default=True
            ):
                raise UserAbort("Choose another bench name.")
            os.rmdir(self.bench)  # only succeeds if still empty
        parent = self.config.parent_path
        if not parent.exists():
            parent.mkdir(parents=True)
            self.state.created.parent_dir = True
        self.state.created.bench_dir = True
        self.store.save(self.state)
        assert self.toolenv.python
        init_bench(
            self.runner,
            self.ui,
            self.toolenv,
            parent=parent,
            name=self.config.bench_name,
            branch=self.config.frappe_branch,
            python=self.toolenv.python,
            enable_backups=self.config.enable_backups,
            verbose=self.options.verbose,
        )
        validate()

    def _recover_partial_bench(self) -> None:
        items = cleanup_candidates(self.state)
        if not items:
            raise SetupError(
                f"{self.bench} is incomplete but was not created by this tool; refusing to touch it.", retry_safe=False
            )
        self.ui.warn(
            f"{self.bench} is a partially initialized bench left by an earlier, interrupted run of frappe-setup."
        )
        choice = self.options.cleanup
        if choice is None:
            if not self.ui.interactive:
                raise SetupError(
                    "An incomplete bench from an earlier run is in the way.",
                    remedy="Rerun with --cleanup quarantine (rename it) or --cleanup delete, or remove it yourself.",
                    retry_safe=True,
                )
            choice = self.ui.choose(
                "What should happen to it?",
                [
                    ("quarantine", f"Rename it to {self.bench.name}.incomplete-<time> and start over (recommended)"),
                    ("delete", "Delete it and start over"),
                    ("abort", "Stop; I will inspect it myself"),
                ],
                default="quarantine",
            )
            if choice == "delete" and not self.ui.confirm(f"Permanently delete {self.bench}?", default=False):
                choice = "abort"
        if choice == "abort":
            raise UserAbort(f"Left {self.bench} untouched.")
        if choice == "quarantine":
            moved = quarantine(self.bench)
            self.ui.success(f"Moved the incomplete bench to {moved}")
        elif choice == "delete":
            delete_tree(self.bench, self.state)
            self.ui.success(f"Deleted the incomplete bench {self.bench}")
        else:
            raise SetupError(f"Unknown cleanup choice {choice!r}", retry_safe=False)

    def run_apps(self) -> None:
        if not self.apps:
            self.skip("apps", "no additional apps selected")
            return
        bench_cmd = self._bench_cmd()
        for app in self.apps:
            if get_app(self.runner, self.ui, self.toolenv, self.bench, app, bench_cmd):
                if app.name not in self.state.created.apps:
                    self.state.created.apps.append(app.name)
                    self.store.save(self.state)
                self.ui.success(f"{app.name} fetched")

    def run_site(self) -> bool:
        """Returns True if the site already existed."""
        site = self.config.site_name
        bench_cmd = self._bench_cmd()
        if site_exists(self.bench, site):
            read_site_config(self.bench, site)
            apps = installed_apps(self.runner, self.toolenv, self.bench, bench_cmd, site)
            self.ui.success(f"Site {site} already exists (apps: {', '.join(apps)}); it will not be recreated.")
            return True
        self.state.created.site = True
        self.store.save(self.state)
        create_site(
            self.runner,
            self.ui,
            self.toolenv,
            self.bench,
            bench_cmd,
            site,
            self.config.database,
            db_password=self._db_password(),
            admin_password=self._admin_password(),
            set_default=self.config.set_default_site,
        )
        self.ui.success(f"Site {site} created")
        return False

    def run_site_apps(self, site_existed: bool) -> list[str]:
        site = self.config.site_name
        bench_cmd = self._bench_cmd()
        current = installed_apps(self.runner, self.toolenv, self.bench, bench_cmd, site)
        wanted = [a.name for a in self.apps if a.spec.install_on_site]
        todo = [a for a in wanted if a not in current]
        for name in todo:
            install_app(self.runner, self.ui, self.toolenv, self.bench, bench_cmd, site, name)
            self.ui.success(f"{name} installed on {site}")
        if todo and site_existed:
            migrate(self.runner, self.ui, self.toolenv, self.bench, bench_cmd, site)
        final = installed_apps(self.runner, self.toolenv, self.bench, bench_cmd, site)
        missing = [a for a in ["frappe", *wanted] if a not in final]
        if missing:
            raise SetupError(f"Apps not installed on {site}: {', '.join(missing)}", retry_safe=True)
        return final

    def run_configure(self) -> Path:
        site = self.config.site_name
        bench_cmd = self._bench_cmd()
        if self.config.developer_mode:
            set_site_config(self.runner, self.toolenv, self.bench, bench_cmd, site, "developer_mode", "1", parse=True)
            self.ui.success("developer_mode enabled for the site")
        if self.config.set_default_site:
            use_site(self.runner, self.toolenv, self.bench, bench_cmd, site)
        script = write_env_script(self.config.bench_name, self.bench, self.toolenv)
        self.state.resolved["env_script"] = str(script)
        self.store.save(self.state)
        self.ui.success(f"Environment script written: {script}")
        return script

    def run_verify(self, apps: list[str]) -> tuple[bool | None, str, int]:
        check = validate_bench(
            self.runner,
            self.toolenv,
            self.bench,
            branch=self.config.frappe_branch,
            python_constraint=self.report.requirements.python if self.report else None,
        )
        if not check.ok:
            raise SetupError("Final bench verification failed.", cause="; ".join(check.problems), retry_safe=True)
        port = check.webserver_port or self.matrix.ports.get("webserver", 8000)
        cfg = read_site_config(self.bench, self.config.site_name)
        if not cfg.get("db_name"):
            raise SetupError("Site configuration has no db_name.", retry_safe=False)
        if not self.config.verify_http:
            return None, "skipped (--no-verify-http)", port
        with self.ui.progress("Starting the bench briefly to check that the site responds"):
            result = http_check(self.bench, self._bench_cmd(), self.toolenv, self.config.site_name, port)
        if result.ok:
            self.ui.success(f"{result.url} answered pong")
        else:
            self.ui.warn(f"HTTP check did not pass: {result.detail}")
        return result.ok, result.detail, port

    def bench_services(self) -> contextlib.AbstractContextManager[object]:
        return bench_redis(self.bench, self.toolenv)

    # ------------------------------------------------------------------ main
    def run(self, *, preflight_done: bool = False) -> Outcome:
        if not preflight_done or self.report is None:
            with self.phase("preflight"):
                self.run_preflight()
                assert self.report is not None
                self.ensure_not_blocked(self.report)
        git_available = bool(self.facts and self.facts.tool("git").found)
        if git_available:
            with self.phase("resolve"):
                self.run_resolve()
            self.report = self._evaluate()
            self.ensure_not_blocked(self.report)
        with self.phase("dependencies"):
            self.run_dependencies()
        if not git_available:
            with self.phase("resolve"):
                self.run_resolve()
            self.report = self._evaluate()
            with self.phase("dependencies"):
                self.run_dependencies()
        with self.phase("database"):
            self.run_database()
        with self.phase("bench"):
            self.run_bench()
        # The bench's own Redis (queue/cache) must be up while apps and sites are installed;
        # it is stopped again before verification starts the full bench.
        with self.bench_services():
            with self.phase("apps"):
                self.run_apps()
            with self.phase("site"):
                site_existed = self.run_site()
            with self.phase("site_apps"):
                apps = self.run_site_apps(site_existed)
            with self.phase("configure"):
                script = self.run_configure()
        with self.phase("verify"):
            http_ok, detail, port = self.run_verify(apps)
            if http_ok is False:
                raise SetupError(
                    "The bench was created but the site did not respond to an HTTP ping.",
                    cause=detail,
                    remedy=f"Start it manually (`source {script}; cd {self.bench}; bench start`) and check "
                    "the output; another bench using the same ports is a common cause.",
                    retry_safe=True,
                )
        self.state.completed = True
        self.store.save(self.state)
        site = self.config.site_name
        url = f"http://{site}:{port}" if not self.config.set_default_site else f"http://localhost:{port}"
        return Outcome(self.bench, site, url, script, http_ok, detail, apps)
