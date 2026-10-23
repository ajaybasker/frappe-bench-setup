"""Secret redaction for logs, displayed commands and diagnostic reports.

Two layers:
  * exact values registered at run time (passwords the user typed), and
  * patterns for things that look like secrets (password options, tokens, URL credentials).
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable, Sequence

MASK = "********"

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # --admin-password=x, --db-password x, --mariadb-root-password=x ...
    (
        re.compile(r"(--[\w-]*password[\w-]*)(=|\s+)(?!-)(\S+)", re.IGNORECASE),
        rf"\1\2{MASK}",
    ),
    # https://user:token@host/... -> https://***@host/...
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@", re.IGNORECASE), rf"\1{MASK}@"),
    # https://token@github.com/... (token-only credentials)
    (re.compile(r"(\bhttps?://)(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})@"), rf"\1{MASK}@"),
    # Common token formats
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), MASK),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), MASK),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"), MASK),
    # JSON / INI / env style: "password": "x", password = x, MYSQL_PWD=x, admin_password: x
    (
        re.compile(
            r"""(?P<key>["']?[\w-]*(?:password|passwd|pwd|secret|token)[\w-]*["']?\s*[:=]\s*)"""
            r"""(?P<q>["']?)(?P<val>[^"'\s,}]+)(?P=q)""",
            re.IGNORECASE,
        ),
        rf"\g<key>\g<q>{MASK}\g<q>",
    ),
    # SQL: IDENTIFIED BY 'x' / PASSWORD('x') / USING PASSWORD('x')
    (re.compile(r"(IDENTIFIED\s+BY\s+)'(?:[^'\\]|\\.|'')*'", re.IGNORECASE), rf"\1'{MASK}'"),
    (re.compile(r"(PASSWORD\s*\(\s*)'(?:[^'\\]|\\.|'')*'", re.IGNORECASE), rf"\1'{MASK}'"),
    # SHOW CREATE USER output: ... USING '*HASH' (password hashes are sensitive too)
    (re.compile(r"(\bUSING\s+)'(?!invalid')(?:[^'\\]|\\.)+'", re.IGNORECASE), rf"\1'{MASK}'"),
]


class Redactor:
    """Thread-safe registry of secret values plus pattern-based masking."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: set[str] = set()

    def register(self, value: str | None) -> None:
        # Very short values would mask unrelated text; they are still covered by patterns
        # wherever they appear next to a password-like key.
        if value and len(value) >= 3:
            with self._lock:
                self._values.add(value)

    def clear(self) -> None:
        with self._lock:
            self._values.clear()

    def redact(self, text: str) -> str:
        if not text:
            return text
        with self._lock:
            values = sorted(self._values, key=len, reverse=True)
        for value in values:
            text = text.replace(value, MASK)
        for pattern, replacement in _PATTERNS:
            text = pattern.sub(replacement, text)
        return text

    def redact_argv(self, argv: Sequence[str]) -> str:
        return self.redact(format_argv(argv))


def format_argv(argv: Iterable[str]) -> str:
    """Human-readable command line (for display only, never executed)."""
    import shlex

    return " ".join(shlex.quote(str(a)) for a in argv)


# Process-wide redactor used by logging and error reporting.
REDACTOR = Redactor()


def redact(text: str) -> str:
    return REDACTOR.redact(text)
