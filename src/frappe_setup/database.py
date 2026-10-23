"""Database superuser access checks (before any long-running step).

Credentials are passed to the client through a private, temporary option file (MariaDB) or
the child's environment (PostgreSQL) — never through argv.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .commands import Runner, command_error
from .compatibility import mariadb_server_version
from .config import DatabaseConfig
from .dependencies import Privilege
from .errors import SetupError
from .redaction import REDACTOR

PHASE = "database"


@dataclass
class DBCheck:
    ok: bool
    version: str | None = None
    access_denied: bool = False
    unreachable: bool = False
    superuser: bool = False
    output: str = ""


def _option_value(value: str) -> str:
    """Quote a value for a MySQL/MariaDB option file."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


@contextlib.contextmanager
def mariadb_option_file(db: DatabaseConfig, password: str) -> Iterator[Path]:
    directory = tempfile.mkdtemp(prefix="frappe-setup-db-")
    os.chmod(directory, 0o700)
    path = Path(directory) / "client.cnf"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(
                "[client]\n"
                f"user={_option_value(db.root_user)}\n"
                f"password={_option_value(password)}\n"
                f"host={db.host}\n"
                f"port={db.effective_port}\n"
                "protocol=TCP\n"
            )
        yield path
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def check_mariadb(runner: Runner, db: DatabaseConfig, password: str) -> DBCheck:
    REDACTOR.register(password)
    client = shutil.which("mariadb") or shutil.which("mysql")
    if not client:
        return DBCheck(False, unreachable=True, output="mariadb client not found")
    with mariadb_option_file(db, password) as cnf:
        result = runner.run(
            [
                client,
                f"--defaults-extra-file={cnf}",
                "--connect-timeout=10",
                "-N",
                "-B",
                "-e",
                "SELECT VERSION(); SHOW GRANTS;",
            ],
            timeout=30,
            phase=PHASE,
            quiet=True,
        )
    text = result.output
    if result.ok:
        lines = text.splitlines()
        version = mariadb_server_version(lines[0]) if lines else None
        grants = "\n".join(lines[1:]).upper()
        superuser = "ALL PRIVILEGES ON *.*" in grants and "WITH GRANT OPTION" in grants
        return DBCheck(True, str(version) if version else None, superuser=superuser, output=text)
    return DBCheck(
        False,
        access_denied="Access denied" in text,
        unreachable=bool(re.search(r"Can't connect|\(2002|\(2003|\(2013|Connection refused", text)),
        output=text,
    )


def root_auth_methods(runner: Runner, privilege: Privilege) -> str:
    """`SHOW CREATE USER` for root@localhost, read via the unix socket as the OS root user."""
    privilege.refresh()
    result = runner.run(
        privilege.wrap(["mariadb", "-N", "-B", "-e", "SHOW CREATE USER 'root'@'localhost'"]),
        timeout=30,
        phase=PHASE,
        new_session=False,
        quiet=True,
    )
    if not result.ok:
        raise command_error(result, phase=PHASE, message="Could not inspect the MariaDB root account")
    return result.output


def root_has_usable_password(show_create_user: str) -> bool:
    """True if root@localhost already has a password; then we must not change it."""
    text = show_create_user
    for m in re.finditer(r"VIA\s+(mysql_native_password|ed25519|parsec)\s+USING\s+'([^']*)'", text, re.I):
        if m.group(2) and m.group(2).lower() != "invalid":
            return True
    return bool(re.search(r"IDENTIFIED BY PASSWORD\s+'[^']+'", text, re.I))


def _sql_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def enable_root_password_auth(runner: Runner, privilege: Privilege, password: str) -> None:
    """Add password authentication to root@localhost while keeping unix_socket authentication."""
    REDACTOR.register(password)
    sql = (
        "ALTER USER 'root'@'localhost' IDENTIFIED VIA unix_socket OR mysql_native_password "
        f"USING PASSWORD({_sql_string(password)});\nFLUSH PRIVILEGES;\n"
    )
    privilege.refresh()
    result = runner.run(
        privilege.wrap(["mariadb", "-N", "-B"]),
        input_text=sql,  # stdin: the password never appears in argv
        timeout=60,
        phase=PHASE,
        new_session=False,
    )
    if not result.ok:
        raise command_error(result, phase=PHASE, message="Could not enable password authentication for MariaDB root")


def check_postgres(runner: Runner, db: DatabaseConfig, password: str) -> DBCheck:
    REDACTOR.register(password)
    psql = shutil.which("psql")
    if not psql:
        return DBCheck(False, unreachable=True, output="psql not found")
    env = dict(os.environ)
    env["PGPASSWORD"] = password  # child environment only
    env["PGCONNECT_TIMEOUT"] = "10"
    result = runner.run(
        [
            psql,
            "-X",
            "-A",
            "-t",
            "-h",
            db.host,
            "-p",
            str(db.effective_port),
            "-U",
            db.root_user,
            "-d",
            "postgres",
            "-c",
            "SELECT current_setting('server_version'); SELECT rolsuper FROM pg_roles WHERE rolname = current_user;",
        ],
        env=env,
        timeout=30,
        phase=PHASE,
        quiet=True,
    )
    if result.ok:
        lines = [ln.strip() for ln in result.output.splitlines() if ln.strip()]
        return DBCheck(
            True, lines[0] if lines else None, superuser=len(lines) > 1 and lines[1] == "t", output=result.output
        )
    return DBCheck(
        False,
        access_denied="password authentication failed" in result.output or "no password supplied" in result.output,
        unreachable="could not connect" in result.output.lower() or "Connection refused" in result.output,
        output=result.output,
    )


def db_error(check: DBCheck, db: DatabaseConfig) -> SetupError:
    if check.unreachable:
        return SetupError(
            f"Cannot connect to {db.engine} at {db.host}:{db.effective_port}.",
            phase=PHASE,
            cause="The database server is not running or not listening on that address.",
            remedy="Start the server (`sudo systemctl start mariadb`) and check host/port.",
            retry_safe=True,
            details=[REDACTOR.redact(x) for x in check.output.splitlines()[-5:]],
        )
    if check.access_denied:
        return SetupError(
            f"The database rejected the credentials for {db.root_user!r}.",
            phase=PHASE,
            cause="Wrong password, or the account only allows socket (sudo) authentication.",
            remedy="Re-enter the password. For a fresh Ubuntu MariaDB, let frappe-setup enable password "
            "authentication for root, or run `sudo mariadb-secure-installation`.",
            retry_safe=True,
        )
    return SetupError(
        "Database connectivity check failed.",
        phase=PHASE,
        details=[REDACTOR.redact(x) for x in check.output.splitlines()[-5:]],
        retry_safe=True,
    )
