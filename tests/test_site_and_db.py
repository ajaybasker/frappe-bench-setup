from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from frappe_setup import database
from frappe_setup.config import DatabaseConfig
from frappe_setup.database import (
    check_mariadb,
    enable_root_password_auth,
    mariadb_option_file,
    root_has_usable_password,
)
from frappe_setup.dependencies import Privilege, ToolEnv
from frappe_setup.errors import SetupError
from frappe_setup.site_setup import (
    create_site,
    new_site_argv,
    parse_list_apps,
    secret_prompts,
    site_exists,
)
from frappe_setup.ui import ScriptedUI

from .conftest import FakeRunner, fail, make_facts, ok

OPTIONS = {
    "--db-root-username",
    "--mariadb-root-username",
    "--set-default",
    "--db-type",
    "--force",
    "--admin-password",
    "--db-root-password",
}


def test_new_site_argv_never_contains_force_or_secrets() -> None:
    argv = new_site_argv("bench", "dev.localhost", DatabaseConfig(), set_default=True, options=OPTIONS - {"--force"})
    assert argv[:3] == ["bench", "new-site", "dev.localhost"]
    assert "--force" not in argv
    assert not any("password" in a for a in argv)
    assert "--set-default" in argv and argv[argv.index("--db-root-username") + 1] == "root"


def test_new_site_argv_remote_db_and_postgres() -> None:
    argv = new_site_argv(
        "bench",
        "s.local",
        DatabaseConfig(engine="postgres", host="db.internal", port=5433, root_user="postgres"),
        set_default=False,
        options={"--db-root-username"},
    )
    assert argv[argv.index("--db-type") + 1] == "postgres"
    assert argv[argv.index("--db-host") + 1] == "db.internal" and argv[argv.index("--db-port") + 1] == "5433"


def test_old_bench_uses_mariadb_root_username_option() -> None:
    argv = new_site_argv("bench", "s.local", DatabaseConfig(), set_default=False, options={"--mariadb-root-username"})
    assert "--mariadb-root-username" in argv


def test_secret_prompts_match_frappe_prompt_texts() -> None:
    prompts = secret_prompts(DatabaseConfig(), "dbpw-123", "adminpw-456")
    texts = {
        "MySQL root password: ": "dbpw-123",
        "Set Administrator password: ": "adminpw-456",
        "Re-enter Administrator password: ": "adminpw-456",
        "Enter mysql super user [root]: ": "root",
        "Postgres super user password: ": "dbpw-123",
    }
    for text, answer in texts.items():
        matches = [p for p in prompts if p._re.search(text)]
        assert len(matches) == 1 and matches[0].answer == answer, text


def test_create_site_passes_secrets_only_via_pty(tmp_path: Path) -> None:
    bench = tmp_path / "bench"
    (bench / "sites").mkdir(parents=True)

    def fake_new_site(argv, _kw):
        site = bench / "sites" / "dev.localhost"
        site.mkdir()
        (site / "site_config.json").write_text(json.dumps({"db_name": "_abc", "db_password": "generated"}))
        return ok("Site dev.localhost created")

    runner = FakeRunner([(r"new-site --help", ok(" ".join(OPTIONS))), (r"new-site dev.localhost", fake_new_site)])
    create_site(
        runner,
        ScriptedUI(),
        ToolEnv(),
        bench,
        "bench",
        "dev.localhost",
        DatabaseConfig(),
        db_password="dbpw-123",
        admin_password="adminpw-456",
        set_default=True,
    )
    argv, responses = runner.prompt_calls[0]
    assert "dbpw-123" not in argv and "adminpw-456" not in argv and "--force" not in argv
    assert {r.answer for r in responses} >= {"dbpw-123", "adminpw-456"}


def test_create_site_refuses_existing_site(tmp_path: Path) -> None:
    site = tmp_path / "bench" / "sites" / "dev.localhost"
    site.mkdir(parents=True)
    (site / "site_config.json").write_text("{}")
    assert site_exists(tmp_path / "bench", "dev.localhost")
    with pytest.raises(SetupError, match="already exists"):
        create_site(
            FakeRunner(),
            ScriptedUI(),
            ToolEnv(),
            tmp_path / "bench",
            "bench",
            "dev.localhost",
            DatabaseConfig(),
            db_password="x" * 8,
            admin_password="y" * 8,
            set_default=False,
        )


def test_create_site_success_without_config_is_failure(tmp_path: Path) -> None:
    bench = tmp_path / "bench"
    (bench / "sites").mkdir(parents=True)
    runner = FakeRunner([(r"new-site --help", ok("")), (r"new-site", ok())])
    with pytest.raises(SetupError, match="was not created"):
        create_site(
            runner,
            ScriptedUI(),
            ToolEnv(),
            bench,
            "bench",
            "dev.localhost",
            DatabaseConfig(),
            db_password="x" * 8,
            admin_password="y" * 8,
            set_default=False,
        )


@pytest.mark.parametrize(
    "output,apps",
    [
        ("frappe 15.124.0 version-15\nerpnext 15.80.1 version-15\n", ["frappe", "erpnext"]),
        ("frappe\nhrms\n", ["frappe", "hrms"]),
        ("INFO: A newer version of bench is available\nfrappe 16.0.0 version-16\n", ["frappe"]),
        ("", []),
    ],
)
def test_parse_list_apps(output, apps) -> None:
    assert parse_list_apps(output) == apps


# ---------------------------------------------------------------- database


def test_option_file_is_private_escaped_and_removed() -> None:
    db = DatabaseConfig(root_user="root")
    with mariadb_option_file(db, 'pa"ss\\word') as path:
        mode = stat.S_IMODE(path.stat().st_mode)
        dir_mode = stat.S_IMODE(path.parent.stat().st_mode)
        text = path.read_text()
        saved = path
    assert mode == 0o600 and dir_mode == 0o700
    assert 'password="pa\\"ss\\\\word"' in text and "protocol=TCP" in text
    assert not saved.exists() and not saved.parent.exists()


def test_check_mariadb_password_not_in_argv(monkeypatch) -> None:
    monkeypatch.setattr(database.shutil, "which", lambda name: "/usr/bin/mariadb" if name == "mariadb" else None)
    runner = FakeRunner(
        [
            (
                r"^mariadb --defaults-extra-file=",
                ok("11.8.3-MariaDB-ubu2404\nGRANT ALL PRIVILEGES ON *.* TO `root`@`localhost` WITH GRANT OPTION"),
            )
        ]
    )
    result = check_mariadb(runner, DatabaseConfig(), "Hidden-Password-1")
    assert result.ok and result.superuser and result.version == "11.8.3"
    assert all("Hidden-Password-1" not in a for a in runner.calls[0])
    assert runner.calls[0][1].startswith("--defaults-extra-file=")  # must be the first option


def test_check_mariadb_access_denied(monkeypatch) -> None:
    monkeypatch.setattr(database.shutil, "which", lambda name: "/usr/bin/mariadb" if name == "mariadb" else None)
    runner = FakeRunner([(r"^mariadb", fail("ERROR 1698 (28000): Access denied for user 'root'@'localhost'"))])
    result = check_mariadb(runner, DatabaseConfig(), "wrong")
    assert not result.ok and result.access_denied
    err = database.db_error(result, DatabaseConfig())
    assert err.retry_safe and "rejected" in err.message


@pytest.mark.parametrize(
    "show_create,has_password",
    [
        ("CREATE USER `root`@`localhost` IDENTIFIED VIA mysql_native_password USING 'invalid' OR unix_socket", False),
        ("CREATE USER `root`@`localhost` IDENTIFIED VIA unix_socket", False),
        (
            "CREATE USER `root`@`localhost` IDENTIFIED VIA mysql_native_password USING '*2470C0C06DEE42FD1618BB99005ADCA2EC9D1E19' OR unix_socket",
            True,
        ),
        ("CREATE USER `root`@`localhost` IDENTIFIED BY PASSWORD '*2470C0C06DEE42FD1618BB99005ADCA2EC9D1E19'", True),
        ("CREATE USER `root`@`localhost` IDENTIFIED VIA ed25519 USING 'abc123' OR unix_socket", True),
    ],
)
def test_root_password_detection(show_create, has_password) -> None:
    assert root_has_usable_password(show_create) is has_password


def test_enable_root_password_auth_uses_stdin_and_keeps_socket_auth() -> None:
    facts = make_facts(sudo_nopasswd=True)
    runner = FakeRunner([(r"^sudo -n -v", ok()), (r"mariadb -N -B$", ok())])
    priv = Privilege(runner, ScriptedUI(assume_yes=True), facts)
    priv.authorize(["x"], "test")
    captured = {}

    def grab(argv, kw):
        captured.update(kw)
        return ok()

    runner.add(r"mariadb -N -B$", grab)
    enable_root_password_auth(runner, priv, "it's-a-pass")
    sql = captured["input_text"]
    assert "unix_socket OR mysql_native_password" in sql
    assert "PASSWORD('it\\'s-a-pass')" in sql
    assert all("it's-a-pass" not in a for a in runner.calls[-1])


def test_secret_file_permissions(tmp_path: Path) -> None:
    from frappe_setup.config import read_secret_file
    from frappe_setup.errors import ValidationError

    f = tmp_path / "pw"
    f.write_text("s3cret-value\n")
    os.chmod(f, 0o644)
    with pytest.raises(ValidationError, match="chmod 600"):
        read_secret_file(f)
    os.chmod(f, 0o600)
    assert read_secret_file(f) == "s3cret-value"
