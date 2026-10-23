"""Preflight: evaluate detected facts against the requirements of a Frappe branch.

Pure functions over SystemFacts — no commands are run here, so this is trivially
side-effect free and unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from packaging.specifiers import SpecifierSet
from packaging.version import Version

from .compatibility import (
    FAIL,
    INFO,
    MISSING,
    OK,
    WARN,
    BranchSpec,
    CheckItem,
    Matrix,
    combine,
    normalize_node_engine,
    parse_version,
    satisfies,
    version_check,
)
from .remote import FrappeMetadata
from .system import X86_64_V2_FLAGS, Interpreter, NodeRuntime, SystemFacts, disk_free_gb


@dataclass
class Requirements:
    """Effective constraints for one branch (matrix ∩ upstream)."""

    branch: str
    python: SpecifierSet
    node: SpecifierSet
    yarn: SpecifierSet
    mariadb: SpecifierSet
    redis: SpecifierSet
    postgres: SpecifierSet
    bench: SpecifierSet
    notes: list[str] = field(default_factory=list)


def effective_requirements(matrix: Matrix, branch: BranchSpec, upstream: FrappeMetadata | None = None) -> Requirements:
    notes: list[str] = []
    py_doc = branch.requirement("python")
    node_doc = branch.requirement("node")
    py_up = upstream.requires_python if upstream else None
    node_up = normalize_node_engine(upstream.node_engine) if upstream else None
    if upstream and upstream.node_engine and node_up is None:
        notes.append(f"Could not interpret upstream engines.node {upstream.node_engine!r}; using matrix value.")
    if py_up and py_doc and str(combine(py_up)) != str(combine(py_doc)):
        notes.append(f"Upstream requires-python is {py_up!r}; matrix says {py_doc!r}. Using both.")
    if node_up and node_doc and str(combine(node_up)) != str(combine(node_doc)):
        notes.append(f"Upstream engines.node is {upstream.node_engine!r}; matrix says {node_doc!r}. Using both.")  # type: ignore[union-attr]
    return Requirements(
        branch=branch.name,
        python=combine(py_doc, py_up),
        node=combine(node_doc, node_up),
        yarn=combine(branch.requirement("yarn")),
        mariadb=combine(branch.requirement("mariadb")),
        redis=combine(branch.requirement("redis")),
        postgres=combine(branch.requirement("postgres")),
        bench=combine(matrix.bench_cli.get("documented")),
        notes=notes,
    )


def pick_python(facts: SystemFacts, constraint: SpecifierSet, preferred: str | None) -> Interpreter | None:
    """Best usable interpreter: preferred minor first, then the newest compatible one.

    Interpreters lacking the venv module ("-no-venv") are only used if nothing else fits.
    """
    usable = [p for p in facts.pythons if satisfies(p.version, constraint)]
    if not usable:
        return None

    def key(p: Interpreter) -> tuple[int, int, Version]:
        v = parse_version(p.version) or Version("0")
        is_preferred = preferred is not None and p.version.startswith(preferred + ".")
        return (0 if p.source.endswith("-no-venv") else 1, 1 if is_preferred else 0, v)

    return sorted(usable, key=key, reverse=True)[0]


def pick_node(facts: SystemFacts, constraint: SpecifierSet) -> NodeRuntime | None:
    usable = [n for n in facts.nodes if satisfies(n.version, constraint)]
    if not usable:
        return None
    order = {"path": 2, "nvm": 1, "managed": 0}
    return sorted(
        usable, key=lambda n: (order.get(n.source, 0), parse_version(n.version) or Version("0")), reverse=True
    )[0]


def mariadb_installed_version(facts: SystemFacts) -> Version | None:
    server = facts.tool("mariadbd")
    if server.parsed:
        return server.parsed
    pkg = facts.apt_installed.get("mariadb-server")
    if pkg:
        # Debian version strings: 1:10.11.8-0ubuntu0.24.04.1 or 1:11.8.3+maria~ubu2404
        return parse_version(pkg.split(":", 1)[-1])
    return None


@dataclass
class PreflightReport:
    branch: str
    items: list[CheckItem]
    requirements: Requirements
    python: Interpreter | None = None
    node: NodeRuntime | None = None

    @property
    def blocking(self) -> list[CheckItem]:
        return [i for i in self.items if i.blocking]

    @property
    def fixable(self) -> list[CheckItem]:
        return [i for i in self.items if i.status in (FAIL, MISSING) and i.fixable]

    @property
    def warnings(self) -> list[CheckItem]:
        return [i for i in self.items if i.status == WARN]

    def item(self, name: str) -> CheckItem | None:
        return next((i for i in self.items if i.name == name), None)


def evaluate(
    facts: SystemFacts,
    matrix: Matrix,
    branch: BranchSpec,
    *,
    upstream: FrappeMetadata | None = None,
    db_engine: str = "mariadb",
    target: Path | None = None,
    allow_untested_platform: bool = False,
    allow_root: bool = False,
    allow_db_version_mismatch: bool = False,
    want_wkhtmltopdf: bool = False,
    large_apps: int = 0,
) -> PreflightReport:
    req = effective_requirements(matrix, branch, upstream)
    items: list[CheckItem] = []
    add = items.append

    # ---------------------------------------------------------------- platform
    status = matrix.platform_status(facts.os_id, facts.os_version, facts.arch)
    os_text = f"{facts.os_pretty} ({facts.arch})"
    if status == "supported":
        add(CheckItem("Operating system", OK, found=os_text, required="Ubuntu 24.04 x86_64"))
    elif status == "untested":
        add(
            CheckItem(
                "Operating system",
                WARN if allow_untested_platform else FAIL,
                found=os_text,
                required="Ubuntu 24.04 x86_64 (tested)",
                message="This platform is in the matrix but has not been tested by this project.",
                remedy="Use Ubuntu 24.04 x86_64, or pass --allow-untested-platform to proceed at your own risk.",
            )
        )
    else:
        add(
            CheckItem(
                "Operating system",
                FAIL,
                found=os_text,
                required="Ubuntu 24.04 x86_64",
                message="This operating system / architecture is not supported.",
                remedy="Use Ubuntu 24.04 x86_64 (a VM or container is fine), or the official frappe_docker images.",
            )
        )
    if facts.arch == "x86_64" and facts.cpu_flags:
        missing = sorted(X86_64_V2_FLAGS - set(facts.cpu_flags))
        if missing:
            add(
                CheckItem(
                    "CPU features",
                    WARN,
                    found="x86-64 baseline",
                    required="x86-64-v2 recommended",
                    message=f"CPU lacks {', '.join(missing)}; some prebuilt binaries may not run.",
                )
            )
        else:
            add(CheckItem("CPU features", OK, found="x86-64-v2", required="x86-64-v2 recommended"))
    add(CheckItem("CPU cores", INFO, found=str(facts.cpu_count)))

    # ---------------------------------------------------------------- resources
    res = matrix.resources
    min_ram, rec_ram = int(res.get("min_ram_mb", 2048)), int(res.get("recommended_ram_mb", 4096))
    ram_text = f"{facts.mem_total_mb} MB total, {facts.mem_available_mb} MB available, {facts.swap_total_mb} MB swap"
    if facts.mem_total_mb < min_ram:
        add(
            CheckItem(
                "Memory",
                FAIL,
                ram_text,
                f">= {min_ram} MB",
                "Not enough RAM to build Frappe assets.",
                "Use a machine with at least 2 GB RAM (4 GB recommended).",
            )
        )
    elif facts.mem_total_mb + facts.swap_total_mb < rec_ram:
        add(
            CheckItem(
                "Memory",
                WARN,
                ram_text,
                f">= {rec_ram} MB recommended",
                "Asset builds may fail with out-of-memory errors.",
                "Add swap space.",
            )
        )
    else:
        add(CheckItem("Memory", OK, ram_text, f">= {min_ram} MB"))

    if target is not None:
        free = disk_free_gb(target)
        need = float(res.get("min_free_disk_gb", 4)) + large_apps * float(res.get("free_disk_per_large_app_gb", 2))
        rec = float(res.get("recommended_free_disk_gb", 10))
        disk_text = f"{free} GB free at {target}"
        if free < need:
            add(
                CheckItem(
                    "Disk space",
                    FAIL,
                    disk_text,
                    f">= {need:g} GB",
                    "Not enough free disk space for the bench and its dependencies.",
                    "Free space or choose an installation directory on another filesystem.",
                )
            )
        elif free < rec:
            add(
                CheckItem(
                    "Disk space",
                    WARN,
                    disk_text,
                    f">= {rec:g} GB recommended",
                    "Disk space is tight; more apps or sites may not fit.",
                )
            )
        else:
            add(CheckItem("Disk space", OK, disk_text, f">= {need:g} GB"))

    # ---------------------------------------------------------------- user
    if facts.is_root:
        add(
            CheckItem(
                "User",
                WARN if allow_root else FAIL,
                found="root",
                required="regular user with sudo",
                message="Bench should not be run as root.",
                remedy="Create a regular user (`sudo adduser frappe && sudo usermod -aG sudo frappe`) "
                "and run the installer as that user. --allow-root exists for disposable containers.",
            )
        )
    else:
        sudo_text = (
            "no sudo"
            if not facts.sudo_available
            else ("sudo (no password)" if facts.sudo_nopasswd else "sudo (password required)")
        )
        add(
            CheckItem(
                "User",
                OK if facts.sudo_available else WARN,
                found=f"{facts.user}, {sudo_text}",
                required="regular user; sudo only needed for system packages",
                message="" if facts.sudo_available else "Missing system packages cannot be installed without sudo.",
            )
        )

    # ---------------------------------------------------------------- runtimes
    preferred_py = str(branch.install.get("python")) if branch.install.get("python") else None
    py = pick_python(facts, req.python, preferred_py)
    if py:
        note = " (venv module missing: python3-venv will be installed)" if py.source.endswith("-no-venv") else ""
        add(
            CheckItem(
                "Python",
                OK if not note else MISSING,
                found=f"{py.version} {py.path} [{py.source}]{note}",
                required=str(req.python),
                fixable=True,
                message="Python venv support is missing" if note else "",
            )
        )
    else:
        found = ", ".join(sorted({p.version for p in facts.pythons})) or "none"
        add(
            CheckItem(
                "Python",
                MISSING,
                found=found,
                required=str(req.python),
                message=f"No installed Python satisfies {req.python}.",
                remedy=f"frappe-setup can install Python {preferred_py} in user space with uv "
                "(system Python is not modified).",
                fixable=True,
            )
        )

    node = pick_node(facts, req.node)
    if node:
        add(CheckItem("Node.js", OK, found=f"{node.version} [{node.source}] {node.bin_dir}", required=str(req.node)))
    else:
        found = ", ".join(sorted({f"{n.version} [{n.source}]" for n in facts.nodes})) or "none"
        add(
            CheckItem(
                "Node.js",
                MISSING,
                found=found,
                required=str(req.node),
                message=f"No installed Node.js satisfies {req.node}.",
                remedy=f"frappe-setup can install Node.js {branch.install.get('node_major')} from nodejs.org "
                "into ~/.local/share/frappe-setup (system Node.js is not modified).",
                fixable=True,
            )
        )

    add(
        version_check(
            "Yarn",
            facts.tool("yarn").parsed,
            req.yarn,
            missing_remedy="Installed with npm into a user-owned prefix.",
            incompatible_remedy="Yarn 1.x (classic) is required; installed into a user-owned prefix.",
        )
    )
    pip = facts.tool("pip")
    add(
        CheckItem(
            "pip",
            INFO,
            found=pip.version or "not found",
            required=branch.requirement("pip") or "",
            message="Bench creates its own virtualenv and upgrades pip inside it.",
        )
    )
    add(
        version_check(
            "Bench CLI",
            facts.tool("bench").parsed,
            req.bench,
            missing_remedy="Installed with `uv tool install frappe-bench` (user space).",
            incompatible_remedy="Upgraded with `uv tool install --upgrade frappe-bench`.",
        )
    )
    uv = facts.tool("uv")
    add(
        CheckItem(
            "uv",
            OK if uv.found else MISSING,
            found=uv.version or "not found",
            required="any",
            message="" if uv.found else "uv is used to install Bench (and Python if needed).",
            remedy="Installed into ~/.local/share/frappe-setup/tools with pip (user space).",
            fixable=True,
        )
    )

    # ---------------------------------------------------------------- system tools
    add(version_check("Git", facts.tool("git").parsed, None, missing_remedy="apt: git"))
    for tool, pkg in (("gcc", "build-essential"), ("make", "build-essential"), ("pkg-config", "pkg-config")):
        add(
            version_check(
                tool,
                facts.tool(tool).parsed or (Version("0") if facts.tool(tool).found else None),
                None,
                missing_remedy=f"apt: {pkg}",
            )
        )
    if facts.os_id in ("ubuntu", "debian"):
        for pkg in ("libmariadb-dev", "libffi-dev", "libssl-dev"):
            present = pkg in facts.apt_installed
            add(
                CheckItem(
                    f"{pkg}",
                    OK if present else MISSING,
                    found="installed" if present else "not installed",
                    required="installed",
                    remedy=f"apt: {pkg}",
                    fixable=True,
                )
            )

    # ---------------------------------------------------------------- database
    if db_engine == "mariadb":
        installed = mariadb_installed_version(facts)
        if installed is None:
            add(
                CheckItem(
                    "MariaDB server",
                    MISSING,
                    found="not installed",
                    required=str(req.mariadb),
                    remedy="Installed with apt (from the MariaDB repository if the distribution version is too old).",
                    fixable=True,
                )
            )
        elif satisfies(installed, req.mariadb):
            warn_above = branch.requirement("mariadb_upstream_warns_above")
            msg = ""
            if warn_above and installed.release[:2] > (parse_version(warn_above) or Version("0")).release[:2]:
                msg = (
                    f"Frappe prints an 'untested above {warn_above}' warning for this MariaDB version; "
                    "the documented minimum is met."
                )
            add(CheckItem("MariaDB server", OK, found=str(installed), required=str(req.mariadb), message=msg))
        else:
            add(
                CheckItem(
                    "MariaDB server",
                    WARN if allow_db_version_mismatch else FAIL,
                    found=str(installed),
                    required=str(req.mariadb),
                    message=f"Installed MariaDB {installed} is older than the {branch.label} requirement.",
                    remedy="frappe-setup does not upgrade an existing database server. Upgrade MariaDB "
                    "yourself (https://mariadb.org/download/), use a fresh VM/container, or pass "
                    "--allow-db-version-mismatch to continue at your own risk.",
                )
            )
        client = facts.tool("mariadb")
        add(
            CheckItem(
                "MariaDB client",
                OK if client.found else MISSING,
                found=client.version or "not found",
                required="installed",
                remedy="apt: mariadb-client",
                fixable=True,
            )
        )
        state = facts.services.get("mariadb", "unknown")
        if installed is not None:
            if state == "active":
                add(CheckItem("MariaDB service", OK, found=state, required="active"))
            elif state == "unknown":
                add(
                    CheckItem(
                        "MariaDB service",
                        INFO,
                        found="unknown (no systemd)",
                        required="active",
                        message="Service state cannot be read without systemd; connectivity is checked later.",
                    )
                )
            else:
                add(
                    CheckItem(
                        "MariaDB service",
                        MISSING,
                        found=state,
                        required="active",
                        message="The MariaDB service is not running.",
                        remedy="Started with systemctl/service (requires sudo).",
                        fixable=True,
                    )
                )
    else:
        psql = facts.tool("psql")
        add(
            CheckItem(
                "PostgreSQL client",
                OK if psql.found else FAIL,
                found=psql.version or "not found",
                required=str(req.postgres),
                message="" if psql.found else "PostgreSQL support requires an existing PostgreSQL server.",
                remedy="Install and configure PostgreSQL yourself (`sudo apt install postgresql`); "
                "PostgreSQL support in frappe-setup is experimental.",
            )
        )
        add(
            CheckItem(
                "PostgreSQL service",
                OK if facts.services.get("postgresql") == "active" else WARN,
                found=facts.services.get("postgresql", "unknown"),
                required="active",
            )
        )

    add(
        version_check(
            "Redis",
            facts.tool("redis-server").parsed,
            req.redis,
            missing_remedy="apt: redis-server (Bench starts its own Redis processes).",
            incompatible_remedy="Install a newer redis-server.",
        )
    )

    # ---------------------------------------------------------------- optional
    if branch.needs_wkhtmltopdf:
        wk = facts.tool("wkhtmltopdf")
        add(
            CheckItem(
                "wkhtmltopdf",
                OK if wk.found else (MISSING if want_wkhtmltopdf else WARN),
                found=wk.version or "not found",
                required="optional (PDF printing)",
                message="" if wk.found else "PDF generation will not work until wkhtmltopdf is installed.",
                remedy="apt: wkhtmltopdf (Ubuntu build without patched Qt).",
                fixable=want_wkhtmltopdf,
            )
        )

    # ---------------------------------------------------------------- ports
    busy = [str(p) for p, used in sorted(facts.ports_in_use.items()) if used]
    if busy:
        add(
            CheckItem(
                "Ports",
                WARN,
                found=f"in use: {', '.join(busy)}",
                required=", ".join(str(p) for p in sorted(facts.ports_in_use)),
                message="Some default Bench ports are in use (another bench may be running). "
                "`bench start` will fail until they are free.",
                remedy="Stop the other bench, or edit ports in sites/common_site_config.json and config/redis_*.conf.",
            )
        )
    elif facts.ports_in_use:
        add(CheckItem("Ports", OK, found="free", required=", ".join(str(p) for p in sorted(facts.ports_in_use))))

    # ---------------------------------------------------------------- benches
    if facts.benches:
        listing = "; ".join(f"{b.path} ({b.frappe_branch or '?'}, {len(b.sites)} site(s))" for b in facts.benches[:5])
        add(
            CheckItem(
                "Existing benches",
                INFO,
                found=listing,
                message="Existing benches are never modified unless you choose to reuse one.",
            )
        )

    return PreflightReport(branch=branch.name, items=items, requirements=req, python=py, node=node)
