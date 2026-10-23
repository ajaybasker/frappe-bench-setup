"""Read-only detection of the machine: OS, hardware, tools, services, ports, benches.

Nothing in this module changes the system.
"""

from __future__ import annotations

import contextlib
import glob
import json
import os
import platform
import pwd
import shutil
import socket
from dataclasses import asdict, dataclass, field
from pathlib import Path

from packaging.version import Version

from .commands import Runner
from .compatibility import mariadb_server_version, parse_version
from .paths import data_dir

PROBE_TIMEOUT = 20


@dataclass
class ToolInfo:
    name: str
    path: str | None = None
    version: str | None = None
    raw: str = ""

    @property
    def found(self) -> bool:
        return self.path is not None

    @property
    def parsed(self) -> Version | None:
        return parse_version(self.version) if self.version else None


@dataclass
class Interpreter:
    path: str
    version: str
    source: str  # system | pyenv | uv | path | bench-env


@dataclass
class NodeRuntime:
    bin_dir: str
    version: str
    source: str  # path | nvm | managed


@dataclass
class BenchInfo:
    path: str
    apps: list[str] = field(default_factory=list)
    sites: list[str] = field(default_factory=list)
    frappe_branch: str | None = None
    frappe_version: str | None = None


@dataclass
class SystemFacts:
    os_id: str = ""
    os_version: str = ""
    os_codename: str = ""
    os_pretty: str = ""
    os_like: str = ""
    kernel: str = ""
    arch: str = ""
    cpu_count: int = 0
    cpu_flags: list[str] = field(default_factory=list)
    mem_total_mb: int = 0
    mem_available_mb: int = 0
    swap_total_mb: int = 0
    user: str = ""
    uid: int = 0
    home: str = ""
    in_container: bool = False
    systemd: bool = False
    sudo_available: bool = False
    sudo_nopasswd: bool = False
    tools: dict[str, ToolInfo] = field(default_factory=dict)
    pythons: list[Interpreter] = field(default_factory=list)
    nodes: list[NodeRuntime] = field(default_factory=list)
    services: dict[str, str] = field(default_factory=dict)
    ports_in_use: dict[int, bool] = field(default_factory=dict)
    benches: list[BenchInfo] = field(default_factory=list)
    apt_installed: dict[str, str] = field(default_factory=dict)

    @property
    def is_root(self) -> bool:
        return self.uid == 0

    def tool(self, name: str) -> ToolInfo:
        return self.tools.get(name) or ToolInfo(name)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


# ------------------------------------------------------------------- OS & hardware
def read_os_release(path: str = "/etc/os-release") -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, _, value = line.partition("=")
                    data[key] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return data


def read_meminfo(path: str = "/proc/meminfo") -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts and parts[0].isdigit():
                    out[key] = int(parts[0]) // 1024  # kB -> MB
    except OSError:
        pass
    return out


def read_cpu_flags(path: str = "/proc/cpuinfo") -> list[str]:
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith(("flags", "Features")):
                    return sorted(set(line.partition(":")[2].split()))
    except OSError:
        pass
    return []


# x86-64-v2 feature set (informational: some prebuilt binaries target it).
X86_64_V2_FLAGS = {"cx16", "lahf_lm", "popcnt", "sse4_1", "sse4_2", "ssse3"}


def detect_container() -> bool:
    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return True
    try:
        with open("/proc/1/cgroup", encoding="utf-8") as fh:
            text = fh.read()
        return any(k in text for k in ("docker", "containerd", "kubepods", "lxc"))
    except OSError:
        return False


def disk_free_gb(path: Path) -> float:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    usage = shutil.disk_usage(probe)
    return round(usage.free / (1024**3), 1)


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


# ------------------------------------------------------------------- tools
_TOOL_PROBES: dict[str, list[str]] = {
    "git": ["git", "--version"],
    "curl": ["curl", "--version"],
    "python3": ["python3", "--version"],
    "pip": ["python3", "-m", "pip", "--version"],
    "node": ["node", "--version"],
    "npm": ["npm", "--version"],
    "yarn": ["yarn", "--version"],
    "uv": ["uv", "--version"],
    "bench": ["bench", "--version"],
    "mariadb": ["mariadb", "--version"],
    "mariadbd": ["mariadbd", "--version"],
    "psql": ["psql", "--version"],
    "redis-server": ["redis-server", "--version"],
    "redis-cli": ["redis-cli", "--version"],
    "gcc": ["gcc", "--version"],
    "make": ["make", "--version"],
    "pkg-config": ["pkg-config", "--version"],
    "wkhtmltopdf": ["wkhtmltopdf", "--version"],
    "crontab": ["crontab", "-V"],
    "sudo": ["sudo", "--version"],
}

# Server binaries often live in sbin, which is not on a regular user's PATH.
_EXTRA_SEARCH = ["/usr/sbin", "/usr/local/sbin", "/sbin"]


def tool_search_path(extra: list[Path] | None = None) -> str:
    parts = [str(p) for p in (extra or [])] + os.environ.get("PATH", "").split(os.pathsep) + _EXTRA_SEARCH
    seen: list[str] = []
    for p in parts:
        if p and p not in seen:
            seen.append(p)
    return os.pathsep.join(seen)


def probe_tool(runner: Runner, name: str, argv: list[str], search_path: str) -> ToolInfo:
    exe = shutil.which(argv[0], path=search_path)
    if exe is None:
        return ToolInfo(name)
    # Probe with the same PATH the tool was found on: launchers such as yarn or npm use
    # `#!/usr/bin/env node`, which must resolve to the Node.js selected for the bench.
    env = dict(os.environ, PATH=search_path)
    result = runner.run([exe, *argv[1:]], env=env, timeout=PROBE_TIMEOUT, quiet=True, phase="detect")
    text = result.output.strip()
    if name == "crontab":  # `crontab -V` is not supported everywhere; presence is enough
        return ToolInfo(name, exe, "present", text[:200])
    if not result.ok and name != "sudo":
        return ToolInfo(name, exe, None, text[:500])
    first = text.splitlines()[0] if text else ""
    if name in ("mariadb", "mariadbd"):
        ver = mariadb_server_version(text)
    elif name == "redis-server":
        import re

        m = re.search(r"v=(\d+\.\d+\.\d+)", text)
        ver = parse_version(m.group(1)) if m else parse_version(first)
    elif name == "pip":
        ver = parse_version(first.replace("pip ", "", 1))
    else:
        ver = parse_version(first)
    return ToolInfo(name, exe, str(ver) if ver else None, first[:200])


def find_pythons(runner: Runner, search_path: str) -> list[Interpreter]:
    candidates: list[tuple[str, str]] = []
    for name in ("python3", "python3.10", "python3.11", "python3.12", "python3.13", "python3.14"):
        exe = shutil.which(name, path=search_path)
        if exe:
            candidates.append((exe, "system" if exe.startswith("/usr/") else "path"))
    for exe in sorted(glob.glob(str(Path.home() / ".pyenv/versions/*/bin/python3"))):
        candidates.append((exe, "pyenv"))
    for exe in sorted(glob.glob(str(Path.home() / ".local/share/uv/python/cpython-*/bin/python3"))):
        candidates.append((exe, "uv"))
    seen: set[str] = set()
    found: list[Interpreter] = []
    for exe, source in candidates:
        try:
            real = os.path.realpath(exe)
        except OSError:
            continue
        if real in seen:
            continue
        seen.add(real)
        result = runner.run(
            [exe, "-c", "import sys,venv,ensurepip; print('%d.%d.%d' % sys.version_info[:3])"],
            timeout=PROBE_TIMEOUT,
            quiet=True,
            phase="detect",
        )
        if result.ok and result.output.strip():
            found.append(Interpreter(exe, result.output.strip().splitlines()[-1], source))
        else:
            # Interpreter without venv/ensurepip (Ubuntu without python3-venv) is still reported.
            plain = runner.run([exe, "--version"], timeout=PROBE_TIMEOUT, quiet=True, phase="detect")
            ver = parse_version(plain.output)
            if ver:
                found.append(Interpreter(exe, str(ver), source + "-no-venv"))
    return found


def find_nodes(runner: Runner, search_path: str) -> list[NodeRuntime]:
    candidates: list[tuple[str, str]] = []
    exe = shutil.which("node", path=search_path)
    if exe:
        candidates.append((exe, "path"))
    for exe in sorted(glob.glob(str(Path.home() / ".nvm/versions/node/*/bin/node"))):
        candidates.append((exe, "nvm"))
    for exe in sorted(glob.glob(str(data_dir() / "node" / "*" / "bin" / "node"))):
        candidates.append((exe, "managed"))
    seen: set[str] = set()
    found: list[NodeRuntime] = []
    for exe, source in candidates:
        real = os.path.realpath(exe)
        if real in seen:
            continue
        seen.add(real)
        result = runner.run([exe, "--version"], timeout=PROBE_TIMEOUT, quiet=True, phase="detect")
        ver = parse_version(result.output) if result.ok else None
        if ver:
            found.append(NodeRuntime(str(Path(exe).parent), str(ver), source))
    return found


def service_state(runner: Runner, name: str, systemd: bool) -> str:
    if systemd and shutil.which("systemctl"):
        result = runner.run(["systemctl", "is-active", name], timeout=PROBE_TIMEOUT, quiet=True, phase="detect")
        return result.output.strip().splitlines()[-1] if result.output.strip() else "unknown"
    return "unknown"


def dpkg_installed(runner: Runner, packages: list[str]) -> dict[str, str]:
    if not shutil.which("dpkg-query") or not packages:
        return {}
    result = runner.run(
        ["dpkg-query", "-W", "-f=${Package}\t${Status}\t${Version}\n", *packages],
        timeout=PROBE_TIMEOUT,
        quiet=True,
        phase="detect",
    )
    out: dict[str, str] = {}
    for line in result.output.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[1].endswith(" installed"):
            out[parts[0]] = parts[2]
    return out


# ------------------------------------------------------------------- benches
def inspect_bench(path: Path, runner: Runner | None = None) -> BenchInfo | None:
    """Return details if `path` looks like a bench (apps/frappe + sites/), else None."""
    if not (path / "apps" / "frappe").is_dir() or not (path / "sites").is_dir():
        return None
    info = BenchInfo(str(path))
    apps_txt = path / "sites" / "apps.txt"
    if apps_txt.is_file():
        info.apps = [a.strip() for a in apps_txt.read_text(encoding="utf-8").splitlines() if a.strip()]
    sites_dir = path / "sites"
    with contextlib.suppress(OSError):
        info.sites = sorted(p.name for p in sites_dir.iterdir() if (p / "site_config.json").is_file())
    head = path / "apps" / "frappe" / ".git" / "HEAD"
    try:
        text = head.read_text(encoding="utf-8").strip()
        if text.startswith("ref: refs/heads/"):
            info.frappe_branch = text.removeprefix("ref: refs/heads/")
    except OSError:
        pass
    init_py = path / "apps" / "frappe" / "frappe" / "__init__.py"
    try:
        import re

        m = re.search(r'^__version__\s*=\s*["\']([^"\']+)', init_py.read_text(encoding="utf-8"), re.M)
        info.frappe_version = m.group(1) if m else None
    except OSError:
        pass
    return info


def find_benches(roots: list[Path], max_depth: int = 2, limit: int = 25) -> list[BenchInfo]:
    found: list[BenchInfo] = []
    seen: set[str] = set()

    def walk(directory: Path, depth: int) -> None:
        if len(found) >= limit or depth > max_depth:
            return
        try:
            entries = [e for e in directory.iterdir() if e.is_dir() and not e.is_symlink()]
        except OSError:
            return
        for entry in entries:
            if entry.name.startswith(".") or entry.name in {"node_modules", "env", "apps", "sites"}:
                continue
            info = inspect_bench(entry)
            if info:
                if info.path not in seen:
                    seen.add(info.path)
                    found.append(info)
                continue
            walk(entry, depth + 1)

    for root in roots:
        if root.is_dir():
            info = inspect_bench(root)
            if info and info.path not in seen:
                seen.add(info.path)
                found.append(info)
            walk(root, 1)
    return found


# ------------------------------------------------------------------- top level
def gather_facts(
    runner: Runner,
    *,
    ports: list[int] | None = None,
    bench_roots: list[Path] | None = None,
    extra_path: list[Path] | None = None,
    apt_packages: list[str] | None = None,
) -> SystemFacts:
    osr = read_os_release()
    mem = read_meminfo()
    pw = pwd.getpwuid(os.getuid())
    facts = SystemFacts(
        os_id=osr.get("ID", platform.system().lower()),
        os_version=osr.get("VERSION_ID", ""),
        os_codename=osr.get("VERSION_CODENAME", ""),
        os_pretty=osr.get("PRETTY_NAME", platform.platform()),
        os_like=osr.get("ID_LIKE", ""),
        kernel=platform.release(),
        arch=platform.machine(),
        cpu_count=os.cpu_count() or 0,
        cpu_flags=read_cpu_flags(),
        mem_total_mb=mem.get("MemTotal", 0),
        mem_available_mb=mem.get("MemAvailable", 0),
        swap_total_mb=mem.get("SwapTotal", 0),
        user=pw.pw_name,
        uid=os.getuid(),
        home=str(Path.home()),
        in_container=detect_container(),
        systemd=os.path.isdir("/run/systemd/system"),
    )
    search_path = tool_search_path(extra_path)
    for name, argv in _TOOL_PROBES.items():
        facts.tools[name] = probe_tool(runner, name, argv, search_path)
    facts.sudo_available = facts.tools["sudo"].found
    if facts.sudo_available and not facts.is_root:
        facts.sudo_nopasswd = runner.run(["sudo", "-n", "true"], timeout=10, quiet=True, phase="detect").ok
    facts.pythons = find_pythons(runner, search_path)
    facts.nodes = find_nodes(runner, search_path)
    for svc in ("mariadb", "postgresql", "redis-server"):
        facts.services[svc] = service_state(runner, svc, facts.systemd)
    for port in ports or []:
        facts.ports_in_use[port] = port_in_use(port)
    facts.benches = find_benches(bench_roots or [Path.home()])
    facts.apt_installed = dpkg_installed(runner, apt_packages or [])
    return facts


def facts_summary_json(facts: SystemFacts) -> str:
    """Shareable subset (no environment variables, no file contents)."""
    data = facts.to_dict()
    data.pop("cpu_flags", None)
    return json.dumps(data, indent=2, default=str)
