"""Validation of user-supplied values before they are used anywhere."""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from .errors import ValidationError

BENCH_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SITE_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
APP_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
DB_USER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}$")
DB_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,252})$|^\[[0-9A-Fa-f:]+\]$")
SSH_SCP_RE = re.compile(r"^(?P<user>[A-Za-z0-9._-]+)@(?P<host>[A-Za-z0-9.-]+):(?P<path>[A-Za-z0-9._~/-]+)$")
_REPO_PATH_RE = re.compile(r"^/?[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)+/?$")

# Directories that must never contain a bench (system locations).
_FORBIDDEN_ROOTS = [
    "/",
    "/bin",
    "/boot",
    "/dev",
    "/etc",
    "/lib",
    "/lib32",
    "/lib64",
    "/libx32",
    "/proc",
    "/root",
    "/run",
    "/sbin",
    "/snap",
    "/sys",
    "/usr",
    "/var",
]

MIN_PASSWORD_LENGTH = 8


def _no_control_chars(value: str, what: str) -> None:
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValidationError(f"{what} must not contain control characters or newlines.")


def validate_bench_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValidationError("Bench name is required.")
    if name in {".", ".."} or not BENCH_NAME_RE.match(name):
        raise ValidationError(
            "Bench name must start with a letter or digit and contain only letters, digits, "
            "'-' or '_' (max 64 characters)."
        )
    return name


def validate_site_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not name:
        raise ValidationError("Site name is required.")
    if len(name) > 253:
        raise ValidationError("Site name is longer than 253 characters.")
    labels = name.split(".")
    for label in labels:
        if not SITE_LABEL_RE.match(label):
            raise ValidationError(
                f"Invalid site name {name!r}: use lowercase letters, digits, '-' and dots "
                "(e.g. dev.localhost or mysite.local); labels cannot start or end with '-'."
            )
    if name in {"assets", "apps.txt", "common_site_config.json", "currentsite.txt"}:
        raise ValidationError(f"{name!r} is reserved inside a bench's sites directory.")
    return name


def validate_app_name(name: str) -> str:
    if not APP_NAME_RE.match(name or ""):
        raise ValidationError(f"Invalid app name {name!r}: expected a Python identifier in lowercase.")
    return name


def validate_branch(ref: str) -> str:
    """Validate a Git branch or tag name (subset of `git check-ref-format`)."""
    ref = (ref or "").strip()
    if not ref:
        raise ValidationError("Branch or tag is required.")
    _no_control_chars(ref, "Branch")
    invalid = (
        ref.startswith(("-", "/", "."))
        or ref.endswith(("/", ".", ".lock"))
        or ".." in ref
        or "//" in ref
        or "@{" in ref
        or ref == "@"
        or re.search(r"[\s~^:?*\[\\]", ref) is not None
        or len(ref) > 200
    )
    if invalid:
        raise ValidationError(f"{ref!r} is not a valid Git branch or tag name.")
    return ref


def validate_git_url(url: str) -> str:
    """Accept https:// and ssh URLs to a repository path; reject everything else.

    Rejected on purpose: local paths and file://, git:// (unauthenticated), ext:: and other
    remote helpers, option-looking values, and URLs with embedded credentials.
    """
    url = (url or "").strip()
    if not url:
        raise ValidationError("Repository URL is required.")
    _no_control_chars(url, "Repository URL")
    if url.startswith("-"):
        raise ValidationError("Repository URL must not start with '-'.")
    if "::" in url.split("/")[0]:
        raise ValidationError("Git remote helpers (e.g. ext::) are not allowed.")

    scp = SSH_SCP_RE.match(url)
    if scp:
        if not _REPO_PATH_RE.match("/" + scp.group("path").lstrip("/")):
            raise ValidationError(f"Repository path in {url!r} looks invalid.")
        return url

    parts = urlsplit(url)
    if parts.scheme not in {"https", "ssh"}:
        raise ValidationError(
            "Only https:// or SSH (git@host:owner/repo.git, ssh://...) repository URLs are allowed."
        )
    if not parts.hostname:
        raise ValidationError(f"Repository URL {url!r} has no host.")
    if parts.password or (parts.scheme == "https" and parts.username):
        raise ValidationError(
            "Do not embed credentials in repository URLs; they would be stored in .git/config "
            "and logs. Use an SSH key or a git credential helper instead."
        )
    if parts.query or parts.fragment:
        raise ValidationError("Repository URL must not contain a query string or fragment.")
    if not _REPO_PATH_RE.match(parts.path or ""):
        raise ValidationError(f"Repository URL {url!r} must point to a repository (owner/name).")
    return url


def repo_basename(url: str) -> str:
    """Last path component without .git (a hint only; the real app name comes from the repo)."""
    path = url.rstrip("/")
    scp = SSH_SCP_RE.match(path)
    if scp:
        path = scp.group("path")
    else:
        path = urlsplit(path).path
    name = path.rstrip("/").rsplit("/", 1)[-1]
    return name[:-4] if name.endswith(".git") else name


def normalize_url(url: str) -> str:
    """Comparable form of a repository URL (for 'is this the same remote?')."""
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    scp = SSH_SCP_RE.match(url)
    if scp:
        return f"{scp.group('host').lower()}/{scp.group('path').strip('/').lower()}"
    parts = urlsplit(url)
    return f"{(parts.hostname or '').lower()}{parts.path.lower()}"


def expand_path(value: str, *, base: Path | None = None) -> Path:
    """Expand ~ and environment-free relative paths, then normalize without requiring existence."""
    value = (value or "").strip()
    if not value:
        raise ValidationError("A directory path is required.")
    _no_control_chars(value, "Path")
    if value.startswith("~") and not (value == "~" or value.startswith("~/")):
        raise ValidationError("Only '~' or '~/...' home expansion is supported.")
    path = Path(os.path.expanduser(value))
    if not path.is_absolute():
        path = (base or Path.cwd()) / path
    # resolve(strict=False) normalizes '..' and resolves symlinks of existing components.
    return path.resolve(strict=False)


def validate_parent_dir(value: str, *, home: Path | None = None) -> Path:
    path = expand_path(value)
    home = (home or Path.home()).resolve()
    text = str(path)
    for root in _FORBIDDEN_ROOTS:
        if text == root or (root != "/" and text.startswith(root + "/") and not _inside(path, home)):
            raise ValidationError(
                f"{path} is a system location. Choose a directory you own, e.g. ~/frappe-projects."
            )
    if path.exists() and not path.is_dir():
        raise ValidationError(f"{path} exists and is not a directory.")
    # The nearest existing ancestor must be writable by the current user.
    probe = path
    while not probe.exists():
        probe = probe.parent
    if not os.access(probe, os.W_OK | os.X_OK):
        raise ValidationError(f"{probe} is not writable by the current user.")
    return path


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def bench_path(parent: Path, bench_name: str) -> Path:
    """Join parent and name and prove the result stays directly inside parent."""
    name = validate_bench_name(bench_name)
    candidate = (parent / name).resolve(strict=False)
    if candidate.parent != parent.resolve(strict=False):
        raise ValidationError(f"Bench path {candidate} escapes {parent}.")
    if candidate.is_symlink():
        raise ValidationError(f"{candidate} is a symbolic link; refusing to use it.")
    return candidate


def validate_password(value: str, *, what: str = "Password", min_length: int = MIN_PASSWORD_LENGTH) -> str:
    if value is None or value == "":
        raise ValidationError(f"{what} must not be empty.")
    if len(value) < min_length:
        raise ValidationError(f"{what} must be at least {min_length} characters.")
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ValidationError(f"{what} must not contain newlines or NUL characters.")
    return value


def validate_db_user(value: str) -> str:
    value = (value or "").strip()
    if not DB_USER_RE.match(value):
        raise ValidationError(f"Invalid database user name {value!r}.")
    return value


def validate_db_host(value: str) -> str:
    value = (value or "").strip()
    if not DB_HOST_RE.match(value):
        raise ValidationError(f"Invalid database host {value!r}.")
    return value


def validate_port(value: int | str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"Invalid port {value!r}.") from exc
    if not 1 <= port <= 65535:
        raise ValidationError(f"Port {port} is outside 1-65535.")
    return port
