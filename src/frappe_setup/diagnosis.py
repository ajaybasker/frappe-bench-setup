"""Map command output to a likely cause and remedy.

A finding is reported only when the output contains evidence for it; otherwise the error
report says the cause is unknown and points at the log.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Finding:
    key: str
    cause: str
    remedy: str
    retry_safe: bool


_RULES: list[tuple[str, Finding]] = [
    (
        r"No space left on device|ENOSPC",
        Finding(
            "disk_full",
            "The disk ran out of space.",
            "Free disk space (see `df -h`) or choose an installation directory on a larger "
            "filesystem, then run `frappe-setup resume`.",
            True,
        ),
    ),
    (
        r"JavaScript heap out of memory|Killed\s*$|Cannot allocate memory|MemoryError",
        Finding(
            "out_of_memory",
            "A build step ran out of memory.",
            "Close memory-heavy programs or add swap (at least 4 GB RAM+swap recommended), "
            "then run `frappe-setup resume`.",
            True,
        ),
    ),
    (
        r"Could not get lock /var/lib/(?:dpkg|apt)|Unable to acquire the dpkg frontend lock",
        Finding(
            "apt_locked",
            "Another package manager process (apt, unattended-upgrades, Software Updater) holds the lock.",
            "Wait for the other package operation to finish, then retry.",
            True,
        ),
    ),
    (
        r"Unable to fetch some archives|Failed to fetch http://",
        Finding(
            "apt_fetch",
            "APT could not download some packages (mirror unreachable, or the network blocks plain-HTTP downloads).",
            "Retry later. If the same file keeps failing, switch APT to an HTTPS mirror in "
            "/etc/apt/sources.list.d/ubuntu.sources (e.g. URIs: https://mirrors.edge.kernel.org/ubuntu/), "
            "run `sudo apt-get update`, then `frappe-setup resume`.",
            True,
        ),
    ),
    (
        r"dpkg was interrupted",
        Finding(
            "dpkg_interrupted",
            "A previous package installation was interrupted.",
            "Run `sudo dpkg --configure -a`, then retry.",
            True,
        ),
    ),
    (
        r"Unable to locate package|has no installation candidate",
        Finding(
            "apt_package_missing",
            "A system package is not available from the configured APT sources.",
            "Run `sudo apt-get update`; make sure the 'universe' component is enabled "
            "(`sudo add-apt-repository universe`).",
            True,
        ),
    ),
    (
        r"Access denied for user",
        Finding(
            "db_access_denied",
            "The database rejected the superuser credentials.",
            "Check the database superuser name and password. On Ubuntu, MariaDB root uses "
            "socket authentication by default; let frappe-setup enable password "
            "authentication or run `sudo mariadb-secure-installation`.",
            True,
        ),
    ),
    (
        r"Can't connect to (?:local )?(?:MySQL|MariaDB) server|Connection refused.*3306|"
        r"\(2002|\(2003",
        Finding(
            "db_not_running",
            "The MariaDB server is not reachable.",
            "Start it with `sudo systemctl start mariadb` and check `systemctl status mariadb`.",
            True,
        ),
    ),
    (
        r'password authentication failed for user|role ".*" does not exist',
        Finding(
            "pg_auth",
            "PostgreSQL rejected the credentials.",
            "Check the PostgreSQL superuser name/password and pg_hba.conf authentication method.",
            True,
        ),
    ),
    (
        r"Database .* already exists",
        Finding(
            "db_exists",
            "A database with the site's name already exists.",
            "frappe-setup never drops databases. Choose another site name, or inspect and "
            "drop the old database yourself if it is not needed.",
            False,
        ),
    ),
    (
        r"Site .* already exists",
        Finding(
            "site_exists",
            "The site already exists in this bench.",
            "Rerun frappe-setup; it reuses existing sites instead of recreating them.",
            False,
        ),
    ),
    (
        r"Remote branch .* not found|couldn't find remote ref|did not match any file\(s\) known to git",
        Finding(
            "git_branch_missing",
            "The requested Git branch or tag does not exist in the repository.",
            "Check the branch name with `git ls-remote --heads --tags <url>`.",
            False,
        ),
    ),
    (
        r"Repository not found|Authentication failed|could not read Username|Permission denied \(publickey\)",
        Finding(
            "git_auth",
            "The Git repository does not exist or requires authentication.",
            "Check the URL. For private repositories use an SSH URL with a loaded key or a "
            "git credential helper (never embed tokens in the URL).",
            False,
        ),
    ),
    (
        r"externally-managed-environment",
        Finding(
            "pep668",
            "pip refused to install into the system Python (PEP 668).",
            "frappe-setup installs Python tools with uv into user space; rerun the installer "
            "rather than using `pip install` against the system Python.",
            True,
        ),
    ),
    (
        r"requires a different Python|Requires-Python|No interpreter found|"
        r"is not supported by this package|python_requires",
        Finding(
            "python_version",
            "The Python interpreter version does not satisfy the package requirements.",
            "Rerun `frappe-setup install`; it selects or installs a compatible interpreter "
            "for the chosen Frappe branch.",
            True,
        ),
    ),
    (
        r"The engine \"node\" is incompatible|Unsupported engine|node: not found|yarn: (?:command )?not found",
        Finding(
            "node_version",
            "Node.js or Yarn is missing or the wrong version for this branch.",
            "Rerun the installer so the managed Node.js/Yarn are put on PATH, or "
            "`source` the generated environment script before running bench commands.",
            True,
        ),
    ),
    (
        r"mysql_config not found|mariadb_config not found|"
        r"libmariadb\S* (?:is )?not found|pkg-config: (?:command )?not found",
        Finding(
            "mariadb_dev_headers",
            "MariaDB client development headers are missing (needed to build mysqlclient).",
            "Install `libmariadb-dev` and `pkg-config` (frappe-setup does this when authorized).",
            True,
        ),
    ),
    (
        r"error: command '(?:x86_64-linux-gnu-)?gcc' failed|gcc: not found|Python\.h: No such file",
        Finding(
            "build_tools",
            "C build tools or Python headers are missing.",
            "Install `build-essential` and `python3-dev`.",
            True,
        ),
    ),
    (
        r"Error 111 connecting to 127\.0\.0\.1:(?:11000|12000|13000)|redis\.exceptions\.ConnectionError",
        Finding(
            "bench_redis_down",
            "The bench's own Redis (queue/cache) is not running.",
            "frappe-setup starts it during installation; for manual bench commands keep `bench start` "
            "running in another terminal, then retry.",
            True,
        ),
    ),
    (
        r"Address already in use|EADDRINUSE",
        Finding(
            "port_in_use",
            "A required network port is already in use (perhaps by another bench).",
            "Stop the other process (`ss -ltnp`) or change the bench ports in sites/common_site_config.json.",
            True,
        ),
    ),
    (
        r"sudo: a password is required|sudo: a terminal is required|is not in the sudoers file",
        Finding(
            "sudo",
            "Administrative privileges are not available non-interactively.",
            "Run the installer interactively so sudo can ask for your password, or use an account with sudo rights.",
            True,
        ),
    ),
    (
        r"SSL certificate problem|CERTIFICATE_VERIFY_FAILED|certificate verify failed",
        Finding(
            "tls",
            "TLS certificate verification failed (proxy or clock problem).",
            "Check the system clock and any HTTPS proxy; never disable certificate checks.",
            True,
        ),
    ),
]

# Network problems are the most common transient failure; checked after specific rules.
_NETWORK = Finding(
    "network",
    "A network request failed (DNS, timeout or reset connection).",
    "Check the internet connection / proxy settings and retry; retrying is safe.",
    True,
)

_COMPILED = [(re.compile(pattern, re.IGNORECASE | re.MULTILINE), f) for pattern, f in _RULES]


def diagnose(output: str) -> Finding | None:
    from .commands import looks_like_network_error

    if not output:
        return None
    for pattern, finding in _COMPILED:
        if pattern.search(output):
            return finding
    if looks_like_network_error(output):
        return _NETWORK
    return None
