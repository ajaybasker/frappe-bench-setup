"""Centralized error model.

Every failure that reaches the user is a SetupError carrying enough context to act on:
phase, redacted command, exit code, log file, likely cause, remedy, and retry safety.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SetupError(Exception):
    message: str
    phase: str = ""
    command: str | None = None  # already redacted
    exit_code: int | None = None
    log_file: str | None = None
    cause: str | None = None
    remedy: str | None = None
    retry_safe: bool | None = None
    details: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return self.message


class ValidationError(ValueError):
    """Invalid user input. The message explains which rule was violated."""


class UserAbort(Exception):
    """The user declined a confirmation or cancelled the wizard."""


class IncompatibleEnvironment(SetupError):
    """The machine cannot run the requested configuration; nothing was changed."""
