"""Orchestrator behaviour: phase bookkeeping, interruption/resume, idempotency, safety."""

from __future__ import annotations

from pathlib import Path

import pytest

from frappe_setup import installer as inst
from frappe_setup import preflight
from frappe_setup.bench_setup import BenchCheck
from frappe_setup.config import AppSpec, InstallConfig, Secrets
from frappe_setup.dependencies import ToolEnv
from frappe_setup.errors import IncompatibleEnvironment, SetupError, UserAbort
from frappe_setup.installer import Installer, InstallOptions
from frappe_setup.preflight import evaluate
from frappe_setup.recovery import DONE, FAILED, INTERRUPTED, SKIPPED, StateStore
from frappe_setup.remote import AppMetadata
from frappe_setup.ui import ScriptedUI

from .conftest import FakeRunner, make_facts

PHASE_METHODS = ["run_resolve", "run_dependencies", "run_database", "run_bench", "run_apps", "run_configure"]


@pytest.fixture(autouse=True)
def disk(monkeypatch):
    monkeypatch.setattr(preflight, "disk_free_gb", lambda _p: 50.0)


def make_installer(isolated_home: Path, monkeypatch, *, facts=None, options=None, config=None, ui=None) -> Installer:
    config = config or InstallConfig(
        parent_dir=str(isolated_home / "projects"), bench_name="b1", site_name="dev.localhost"
    )
    installer = Installer(
        config,
        Secrets("admin-pass-1", "db-pass-1"),
        ui or ScriptedUI(assume_yes=True),
        FakeRunner(),
        options=options or InstallOptions(),
        store=StateStore(isolated_home / "runs"),
    )
    facts = facts or make_facts()

    def fake_preflight(self=installer):
        self.facts = facts
        self.report = self._evaluate()
        return self.report

    monkeypatch.setattr(installer, "run_preflight", fake_preflight)
    calls: list[str] = []
    for name in PHASE_METHODS:
        monkeypatch.setattr(installer, name, (lambda n: lambda *a, **k: calls.append(n))(name))
    monkeypatch.setattr(installer, "run_site", lambda: calls.append("run_site") or False)
    monkeypatch.setattr(installer, "run_site_apps", lambda existed: calls.append("run_site_apps") or ["frappe"])
    monkeypatch.setattr(installer, "run_configure", lambda: calls.append("run_configure") or Path("/x/env.sh"))
    monkeypatch.setattr(installer, "run_verify", lambda apps: (calls.append("run_verify"), (True, "pong", 8000))[1])
    installer.calls = calls  # type: ignore[attr-defined]
    return installer


def test_full_run_marks_every_phase_done(isolated_home, monkeypatch) -> None:
    i = make_installer(isolated_home, monkeypatch)
    outcome = i.run()
    assert outcome.http_ok and outcome.url == "http://localhost:8000"
    state = StateStore(isolated_home / "runs").load(i.bench)
    assert state.completed
    assert all(
        state.phase(p).status == DONE for p in ("preflight", "resolve", "dependencies", "bench", "site", "verify")
    )


def test_interrupt_then_resume(isolated_home, monkeypatch) -> None:
    i = make_installer(isolated_home, monkeypatch)

    def interrupted():
        raise KeyboardInterrupt

    monkeypatch.setattr(i, "run_bench", interrupted)
    with pytest.raises(KeyboardInterrupt):
        i.run()
    store = StateStore(isolated_home / "runs")
    state = store.load(i.bench)
    assert state.phase("bench").status == INTERRUPTED
    assert state.phase("dependencies").status == DONE
    assert state.phase("site").status == "pending"
    assert store.latest(incomplete_only=True).bench_path == str(i.bench)

    resumed = make_installer(isolated_home, monkeypatch, config=InstallConfig.from_dict(state.config))
    resumed.run()
    final = store.load(i.bench)
    assert final.completed and final.phase("bench").status == DONE


def test_failure_is_recorded_with_phase(isolated_home, monkeypatch) -> None:
    i = make_installer(isolated_home, monkeypatch)

    def boom():
        raise SetupError("bench new-site failed", exit_code=1)

    monkeypatch.setattr(i, "run_site", boom)
    with pytest.raises(SetupError) as exc:
        i.run()
    assert exc.value.phase == "site"
    state = StateStore(isolated_home / "runs").load(i.bench)
    assert state.phase("site").status == FAILED and "new-site" in state.phase("site").error
    assert not state.completed


def test_blocking_environment_changes_nothing(isolated_home, monkeypatch) -> None:
    i = make_installer(isolated_home, monkeypatch, facts=make_facts(os_id="fedora", os_version="40"))
    with pytest.raises(IncompatibleEnvironment):
        i.run()
    assert i.calls == []  # type: ignore[attr-defined]


def test_unknown_branch_is_refused_up_front(isolated_home) -> None:
    config = InstallConfig(parent_dir=str(isolated_home / "p"), frappe_branch="version-99")
    with pytest.raises(IncompatibleEnvironment, match="not in the compatibility matrix"):
        Installer(config, Secrets(), ScriptedUI(), FakeRunner(), store=StateStore(isolated_home / "runs"))


def test_secrets_never_reach_state(isolated_home, monkeypatch) -> None:
    i = make_installer(isolated_home, monkeypatch)
    i.run()
    text = StateStore(isolated_home / "runs").path_for(i.bench).read_text()
    assert "admin-pass-1" not in text and "db-pass-1" not in text


def test_failed_http_check_is_not_reported_as_success(isolated_home, monkeypatch) -> None:
    i = make_installer(isolated_home, monkeypatch)
    monkeypatch.setattr(i, "run_verify", lambda apps: (False, "connection refused", 8000))
    with pytest.raises(SetupError, match="did not respond"):
        i.run()
    assert not StateStore(isolated_home / "runs").load(i.bench).completed


# ---------------------------------------------------------------- run_bench (real logic)


def bench_installer(isolated_home, monkeypatch, **kw) -> Installer:
    config = InstallConfig(parent_dir=str(isolated_home / "projects"), bench_name="b1")
    i = Installer(
        config, Secrets(), ScriptedUI(assume_yes=True), FakeRunner(), store=StateStore(isolated_home / "runs"), **kw
    )
    i.toolenv = ToolEnv(python="/usr/bin/python3")
    i.report = evaluate(make_facts(), i.matrix, i.branch, target=isolated_home)
    return i


def fake_bench_dir(path: Path) -> None:
    (path / "apps" / "frappe").mkdir(parents=True)
    (path / "sites").mkdir()


def test_existing_non_bench_directory_is_refused(isolated_home, monkeypatch) -> None:
    i = bench_installer(isolated_home, monkeypatch)
    i.bench.mkdir(parents=True)
    (i.bench / "notes.txt").write_text("user data")
    with pytest.raises(SetupError, match="not a bench"):
        i.run_bench()
    assert (i.bench / "notes.txt").read_text() == "user data"


def test_existing_valid_bench_is_reused_without_init(isolated_home, monkeypatch) -> None:
    i = bench_installer(isolated_home, monkeypatch)
    fake_bench_dir(i.bench)
    monkeypatch.setattr(
        inst, "validate_bench", lambda *a, **k: BenchCheck(True, frappe_version="15.1.0", python_version="3.12.3")
    )
    called = []
    monkeypatch.setattr(inst, "init_bench", lambda *a, **k: called.append(1))
    i.run_bench()
    assert called == [] and i.config.reuse_existing_bench


def test_foreign_incompatible_bench_is_never_modified(isolated_home, monkeypatch) -> None:
    i = bench_installer(isolated_home, monkeypatch)
    fake_bench_dir(i.bench)
    monkeypatch.setattr(
        inst, "validate_bench", lambda *a, **k: BenchCheck(False, ["apps/frappe is on branch 'version-14'"])
    )
    with pytest.raises(SetupError, match="does not match"):
        i.run_bench()
    assert (i.bench / "apps" / "frappe").is_dir()


def test_partial_bench_from_our_run_needs_explicit_cleanup(isolated_home, monkeypatch) -> None:
    i = bench_installer(isolated_home, monkeypatch)
    fake_bench_dir(i.bench)
    i.state.created.bench_dir = True
    monkeypatch.setattr(inst, "validate_bench", lambda *a, **k: BenchCheck(False, ["missing env/bin/python"]))
    with pytest.raises(SetupError, match="incomplete bench"):
        i.run_bench()
    assert i.bench.exists()


def test_partial_bench_quarantine_then_reinit(isolated_home, monkeypatch) -> None:
    i = bench_installer(isolated_home, monkeypatch, options=InstallOptions(cleanup="quarantine"))
    fake_bench_dir(i.bench)
    i.state.created.bench_dir = True
    checks = iter([BenchCheck(False, ["missing env"]), BenchCheck(True, frappe_version="15.1", python_version="3.12")])
    monkeypatch.setattr(inst, "validate_bench", lambda *a, **k: next(checks))
    monkeypatch.setattr(inst, "init_bench", lambda *a, **k: fake_bench_dir(i.bench))
    i.run_bench()
    quarantined = [p for p in i.bench.parent.iterdir() if p.name.startswith("b1.incomplete-")]
    assert len(quarantined) == 1 and i.bench.exists()


def test_fresh_bench_records_creation_before_init(isolated_home, monkeypatch) -> None:
    i = bench_installer(isolated_home, monkeypatch)
    seen = {}

    def init(*a, **k):
        seen["recorded"] = StateStore(isolated_home / "runs").load(i.bench).created.bench_dir
        seen["python"] = k["python"]
        fake_bench_dir(i.bench)

    monkeypatch.setattr(inst, "init_bench", init)
    monkeypatch.setattr(
        inst, "validate_bench", lambda *a, **k: BenchCheck(True, frappe_version="15", python_version="3.12")
    )
    i.run_bench()
    assert seen == {"recorded": True, "python": "/usr/bin/python3"}
    assert i.state.created.parent_dir


# ---------------------------------------------------------------- site idempotency


def site_installer(isolated_home, monkeypatch, existing_apps, apps=()):
    i = bench_installer(isolated_home, monkeypatch)
    i.apps = [
        inst.ResolvedApp(AppSpec(a, f"https://github.com/frappe/{a}"), AppMetadata("u", "v", "s", a)) for a in apps
    ]
    monkeypatch.setattr(inst, "bench_exe", lambda env: "bench")
    installed = list(existing_apps)
    calls = []
    monkeypatch.setattr(inst, "installed_apps", lambda *a, **k: list(installed))

    def install_app(runner, ui, env, bench, cmd, site, app):
        calls.append(("install", app))
        installed.append(app)

    monkeypatch.setattr(inst, "install_app", install_app)
    monkeypatch.setattr(inst, "migrate", lambda *a, **k: calls.append(("migrate",)))
    monkeypatch.setattr(inst, "create_site", lambda *a, **k: calls.append(("create",)))
    return i, calls


def test_existing_site_is_reused_not_recreated(isolated_home, monkeypatch) -> None:
    i, calls = site_installer(isolated_home, monkeypatch, ["frappe"])
    site = i.bench / "sites" / i.config.site_name
    site.mkdir(parents=True)
    (site / "site_config.json").write_text('{"db_name": "_x"}')
    assert i.run_site() is True
    assert ("create",) not in calls


def test_only_missing_apps_are_installed_in_order(isolated_home, monkeypatch) -> None:
    i, calls = site_installer(isolated_home, monkeypatch, ["frappe", "erpnext"], apps=["erpnext", "hrms"])
    final = i.run_site_apps(site_existed=True)
    assert calls == [("install", "hrms"), ("migrate",)]
    assert final == ["frappe", "erpnext", "hrms"]


def test_rerun_with_everything_installed_does_nothing(isolated_home, monkeypatch) -> None:
    i, calls = site_installer(isolated_home, monkeypatch, ["frappe", "erpnext"], apps=["erpnext"])
    i.run_site_apps(site_existed=True)
    assert calls == []


def test_database_phase_skipped_for_existing_site(isolated_home, monkeypatch) -> None:
    i = bench_installer(isolated_home, monkeypatch)
    site = i.bench / "sites" / i.config.site_name
    site.mkdir(parents=True)
    (site / "site_config.json").write_text("{}")
    with i.phase("database"):
        i.run_database()
    assert i.state.phase("database").status == SKIPPED


def test_missing_db_password_non_interactive(isolated_home, monkeypatch) -> None:
    i = bench_installer(isolated_home, monkeypatch)
    with pytest.raises(UserAbort, match="password is required"):
        i._db_password()
