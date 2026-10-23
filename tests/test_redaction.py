from __future__ import annotations

import logging

import pytest

from frappe_setup.logging_config import get_logger, setup_logging
from frappe_setup.redaction import MASK, REDACTOR, Redactor


@pytest.mark.parametrize(
    "text,secret_fragment",
    [
        ("bench new-site s --admin-password hunter22", "hunter22"),
        ("bench new-site s --db-root-password=topsecret1", "topsecret1"),
        ("--mariadb-root-password  abcdefgh", "abcdefgh"),
        ('{"db_password": "AbC123xyz", "db_name": "_1"}', "AbC123xyz"),
        ("admin_password: s3cr3tpass", "s3cr3tpass"),
        ("MYSQL_PWD=rootpw99 mariadb", "rootpw99"),
        ("git clone https://alice:ghp_secretsecretsecretsecret00@github.com/o/r", "ghp_secretsecretsecretsecret00"),
        ("https://user:pass1234@example.com/x.git", "pass1234"),
        ("token ghp_abcdefghijklmnopqrstuvwxyz0123456789 leaked", "ghp_abcdefghijklmnopqrstuvwxyz0123456789"),
        ("glpat-abcdefghijklmnopqrstu", "glpat-abcdefghijklmnopqrstu"),
        ("ALTER USER root IDENTIFIED BY 'p@ss''w0rd'", "w0rd"),
        ("USING PASSWORD('Zx9!yy')", "Zx9!yy"),
        ("IDENTIFIED VIA mysql_native_password USING '*ABCDEF0123456789'", "*ABCDEF0123456789"),
    ],
)
def test_patterns(text: str, secret_fragment: str) -> None:
    out = Redactor().redact(text)
    assert secret_fragment not in out
    assert MASK in out


def test_registered_values_are_masked_anywhere() -> None:
    r = Redactor()
    r.register("correct-horse-battery")
    assert r.redact("echo correct-horse-battery | x") == f"echo {MASK} | x"


def test_short_values_are_not_registered_but_harmless_text_stays() -> None:
    r = Redactor()
    r.register("ab")
    assert r.redact("about") == "about"
    assert r.redact("socket 'invalid' stays") == "socket 'invalid' stays"


def test_redact_argv_quotes_safely() -> None:
    r = Redactor()
    r.register("pw with space")
    assert MASK in r.redact_argv(["x", "--admin-password", "pw with space"])


def test_log_files_are_redacted_and_private(isolated_home) -> None:
    path = setup_logging("unit")
    REDACTOR.register("Sup3rSecretValue")
    get_logger("t").info("connecting with Sup3rSecretValue and --db-password=other1234")
    for h in logging.getLogger("frappe_setup").handlers:
        h.flush()
    text = path.read_text()
    assert "Sup3rSecretValue" not in text and "other1234" not in text
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
