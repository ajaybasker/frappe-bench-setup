from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from frappe_setup import cli, preflight
from frappe_setup.prompts import parse_app_arg

from .conftest import make_facts

runner = CliRunner()


@pytest.fixture(autouse=True)
def no_real_system(monkeypatch):
    """CLI tests never inspect or change the real machine."""
    import frappe_setup.installer as inst

    monkeypatch.setattr(inst, "collect_facts", lambda *a, **k: make_facts())
    monkeypatch.setattr(preflight, "disk_free_gb", lambda _p: 50.0)

    def no_install(self, **kw):
        raise AssertionError("Installer.run must not be reached in CLI unit tests")

    monkeypatch.setattr(inst.Installer, "run", no_install)


def test_help_lists_commands() -> None:
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    for command in ("check", "install", "status", "doctor", "resume", "logs"):
        assert command in result.output


def test_version() -> None:
    result = runner.invoke(cli.app, ["--version"])
    assert result.exit_code == 0 and "frappe-setup 1.0.0" in result.output


def test_install_help_has_no_password_flags() -> None:
    result = runner.invoke(cli.app, ["install", "--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    assert "--admin-password-file" in result.output
    assert "--admin-password " not in result.output and "--db-root-password " not in result.output


def test_check_offline_json_is_valid_and_read_only() -> None:
    result = runner.invoke(cli.app, ["check", "--offline", "--json", "-b", "version-15"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["version-15"]["blocking"] == []
    assert data["version-15"]["requirements"]["python"] == "<3.15,>=3.10"


def test_check_unknown_branch() -> None:
    result = runner.invoke(cli.app, ["check", "--offline", "-b", "version-99"])
    assert result.exit_code == 2


def test_install_rejects_invalid_bench_name(isolated_home) -> None:
    result = runner.invoke(cli.app, ["install", "--non-interactive", "--bench-name", "../escape"])
    assert result.exit_code == 2
    assert "Bench name" in result.output


def test_install_rejects_world_readable_secret_file(isolated_home) -> None:
    pw = isolated_home / "pw"
    pw.write_text("password123")
    os.chmod(pw, 0o644)
    result = runner.invoke(cli.app, ["install", "--non-interactive", "--admin-password-file", str(pw)])
    assert result.exit_code == 2 and "chmod 600" in result.output


def test_install_rejects_secrets_in_config_file(isolated_home) -> None:
    cfg = isolated_home / "c.yaml"
    cfg.write_text("bench_name: x\nadmin_password: hunter2\n")
    result = runner.invoke(cli.app, ["install", "--non-interactive", "--config", str(cfg)])
    assert result.exit_code == 2 and "secrets must not be stored" in result.output
    assert "hunter2" not in result.output


def test_install_rejects_unknown_app(isolated_home) -> None:
    result = runner.invoke(cli.app, ["install", "--non-interactive", "--app", "not-an-app"])
    assert result.exit_code == 2 and "Unknown app" in result.output


def test_install_rejects_bad_cleanup_value(isolated_home) -> None:
    result = runner.invoke(cli.app, ["install", "--non-interactive", "--cleanup", "rm-rf"])
    assert result.exit_code == 2


def test_install_builds_config_and_reaches_installer(isolated_home, monkeypatch) -> None:
    captured = {}
    import frappe_setup.installer as inst

    def fake_run(self, **kw):
        captured["config"] = self.config
        captured["secrets"] = self.secrets
        raise cli.UserAbort("stop here")

    monkeypatch.setattr(inst.Installer, "run", fake_run)
    pw = isolated_home / "pw"
    pw.write_text("Admin-Pass-1\n")
    os.chmod(pw, 0o600)
    result = runner.invoke(
        cli.app,
        [
            "install",
            "--non-interactive",
            "--bench-name",
            "b2",
            "--parent-dir",
            str(isolated_home / "p"),
            "--frappe-branch",
            "version-15",
            "--site",
            "My.Site.localhost",
            "--app",
            "erpnext@version-15",
            "--app-url",
            "git@github.com:org/custom_app.git@develop",
            "--admin-password-file",
            str(pw),
            "--no-developer-mode",
        ],
        env={"FRAPPE_SETUP_DB_ROOT_PASSWORD": "Db-Pass-2"},
    )
    assert result.exit_code == 2, result.output
    config = captured["config"]
    assert config.bench_name == "b2" and config.site_name == "my.site.localhost"
    assert [(a.key, a.branch) for a in config.apps] == [("erpnext", "version-15"), ("custom_app", "develop")]
    assert config.apps[1].url == "git@github.com:org/custom_app.git"
    assert config.developer_mode is False
    assert captured["secrets"].admin_password == "Admin-Pass-1"
    assert captured["secrets"].db_root_password == "Db-Pass-2"
    assert "Admin-Pass-1" not in result.output and "Db-Pass-2" not in result.output


@pytest.mark.parametrize(
    "value,expected",
    [
        ("erpnext", ("erpnext", None)),
        ("erpnext@version-15", ("erpnext", "version-15")),
        ("https://github.com/o/app", ("https://github.com/o/app", None)),
        ("https://github.com/o/app@feature/x", ("https://github.com/o/app", "feature/x")),
        ("git@github.com:o/app.git", ("git@github.com:o/app.git", None)),
        ("git@github.com:o/app.git@main", ("git@github.com:o/app.git", "main")),
    ],
)
def test_parse_app_arg(value, expected) -> None:
    assert parse_app_arg(value) == expected


def test_status_without_runs(isolated_home) -> None:
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0 and "No installations recorded" in result.output


def test_status_json_reports_state(isolated_home) -> None:
    from frappe_setup.recovery import FAILED, RunState, StateStore

    store = StateStore()
    state = RunState(bench_path=str(isolated_home / "b"), config={"site_name": "dev.localhost"})
    state.phase("bench").status = FAILED
    state.phase("bench").error = "bench init failed --admin-password=leak1234"
    store.save(state)
    result = runner.invoke(cli.app, ["status", "--json"])
    data = json.loads(result.stdout)
    assert data[0]["run"]["last_failure"]["phase"] == "bench"
    assert "leak1234" not in result.stdout


def test_resume_without_state(isolated_home) -> None:
    result = runner.invoke(cli.app, ["resume", "--non-interactive"])
    assert result.exit_code == 2 and "No incomplete installation" in result.output


def test_logs_path_and_show_restrictions(isolated_home, tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["logs", "--path"])
    assert result.exit_code == 0 and "frappe-setup" in result.output
    outside = tmp_path / "other.log"
    outside.write_text("x")
    result = runner.invoke(cli.app, ["logs", "--show", str(outside)])
    assert result.exit_code != 0


def test_logs_show_redacts(isolated_home) -> None:
    from frappe_setup.paths import logs_dir

    logs_dir().mkdir(parents=True)
    log = logs_dir() / "frappe-setup-20260101-000000-install-1.log"
    log.write_text("line --db-root-password=abc12345\n")
    result = runner.invoke(cli.app, ["logs"])
    assert result.exit_code == 0 and "abc12345" not in result.output and "********" in result.output


@pytest.mark.parametrize(
    "launcher,expected",
    [
        (None, "frappe-setup resume"),
        ("./install.sh", "./install.sh resume"),
        ("npx frappe-setup-cli", "npx frappe-setup-cli resume"),
        ("frappe-setup-cli", "frappe-setup-cli resume"),
        ("rm -rf ~", "frappe-setup resume"),  # unknown values are ignored
    ],
)
def test_messages_name_the_command_the_user_types(monkeypatch, launcher, expected) -> None:
    from frappe_setup.ui import localize

    if launcher is None:
        monkeypatch.delenv("FRAPPE_SETUP_COMMAND", raising=False)
    else:
        monkeypatch.setenv("FRAPPE_SETUP_COMMAND", launcher)
    assert localize("Fix it, then run `frappe-setup resume`.") == f"Fix it, then run `{expected}`."
    assert localize("frappe-setup-cli is the npm name") == "frappe-setup-cli is the npm name"
