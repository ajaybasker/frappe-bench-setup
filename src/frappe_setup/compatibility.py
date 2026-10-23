"""Version matrix loading and version/constraint evaluation."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from .errors import SetupError

_VERSION_RE = re.compile(r"(\d+(?:\.\d+){0,3})(?:[-.]?(dev|a|b|rc|alpha|beta)\.?(\d*))?", re.IGNORECASE)


def parse_version(text: str | None) -> Version | None:
    """Extract the first version-looking token from tool output ("v20.1.0", "Ver 15.1 Distrib 10.11.8-MariaDB")."""
    if not text:
        return None
    match = _VERSION_RE.search(text)
    if not match:
        return None
    candidate = match.group(1)
    if match.group(2):
        tag = match.group(2).lower()
        tag = {"alpha": "a", "beta": "b"}.get(tag, tag)
        candidate += f"{'.' if tag == 'dev' else ''}{tag}{match.group(3) or '0'}"
    try:
        return Version(candidate)
    except InvalidVersion:
        return None


def mariadb_server_version(text: str) -> Version | None:
    """MariaDB version strings: 'mariadbd  Ver 11.8.3-MariaDB-ubu2404 ...', '10.11.8-MariaDB-0ubuntu0.24.04.1'."""
    match = re.search(r"(\d+\.\d+\.\d+)-MariaDB", text or "")
    if match:
        return Version(match.group(1))
    match = re.search(r"Distrib (\d+\.\d+\.\d+)", text or "")
    if match:
        return Version(match.group(1))
    match = re.search(r"from (\d+\.\d+\.\d+)-MariaDB", text or "")
    return Version(match.group(1)) if match else parse_version(text)


def spec(text: str | None) -> SpecifierSet:
    try:
        return SpecifierSet(text or "")
    except InvalidSpecifier as exc:
        raise SetupError(f"Invalid version constraint {text!r} in version matrix") from exc


def satisfies(version: Version | str | None, constraint: str | SpecifierSet | None) -> bool:
    if version is None:
        return False
    if isinstance(version, str):
        parsed = parse_version(version)
        if parsed is None:
            return False
        version = parsed
    if constraint is None or str(constraint) == "":
        return True
    s = constraint if isinstance(constraint, SpecifierSet) else spec(constraint)
    # Compare release only so "3.14.0rc1" / "17.0.0-dev" are judged by their numbers.
    return s.contains(Version(version.base_version), prereleases=True)


def combine(*constraints: str | None) -> SpecifierSet:
    """Intersection of constraints (the stricter combination)."""
    result = SpecifierSet("")
    for c in constraints:
        if c:
            result &= spec(c)
    return result


def normalize_node_engine(engine: str | None) -> str | None:
    """package.json engines use npm semver ('>=18', '^20', '18.x'); map common forms to PEP 440."""
    if not engine:
        return None
    engine = engine.strip()
    out: list[str] = []
    for part in engine.split():
        m = re.fullmatch(r"\^(\d+)(?:\.(\d+))?(?:\.(\d+))?", part)
        if m:
            out += [f">={m.group(1)}.{m.group(2) or 0}.{m.group(3) or 0}", f"<{int(m.group(1)) + 1}"]
            continue
        m = re.fullmatch(r"(\d+)\.x", part)
        if m:
            out += [f">={m.group(1)}", f"<{int(m.group(1)) + 1}"]
            continue
        m = re.fullmatch(r"(>=|<=|>|<|=|==)?v?(\d+(?:\.\d+)*)", part)
        if m:
            op, ver = m.group(1), m.group(2)
            if op and op not in ("=", "=="):
                out.append(f"{op}{ver}")
            else:  # npm: "18" means 18.x, "18.2" means 18.2.x
                out.append(f"=={ver}.*" if ver.count(".") < 2 else f"=={ver}")
            continue
        return None  # unsupported syntax (||, ranges with -) -> caller falls back to matrix
    return ",".join(out)


# ------------------------------------------------------------------- matrix model
@dataclass
class BranchSpec:
    name: str
    label: str
    major: int
    documented: dict[str, str]
    upstream_ci: dict[str, str]
    install: dict[str, Any]
    needs_wkhtmltopdf: bool
    erpnext_branch: str | None
    unstable: bool = False

    def requirement(self, key: str) -> str | None:
        value = self.documented.get(key)
        return str(value) if value is not None else None


@dataclass
class PlatformSpec:
    id: str
    version: str
    codename: str
    architectures: list[str]
    status: str
    unavailable_packages: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{'Ubuntu' if self.id == 'ubuntu' else self.id.capitalize()} {self.version}"


@dataclass
class Matrix:
    raw: dict[str, Any]
    source: Path | None = None
    branches: dict[str, BranchSpec] = field(default_factory=dict)
    platforms: list[PlatformSpec] = field(default_factory=list)

    @property
    def frappe_repository(self) -> str:
        return str(self.raw["frappe_repository"])

    @property
    def resources(self) -> dict[str, Any]:
        return dict(self.raw.get("resources", {}))

    @property
    def ports(self) -> dict[str, int]:
        return {k: int(v) for k, v in (self.raw.get("ports") or {}).items()}

    @property
    def apt_packages(self) -> dict[str, list[str]]:
        return {k: list(v) for k, v in (self.raw.get("apt_packages") or {}).items()}

    @property
    def mariadb_repository(self) -> dict[str, str]:
        return dict(self.raw.get("mariadb_repository") or {})

    @property
    def bench_cli(self) -> dict[str, str]:
        return dict(self.raw.get("bench_cli") or {})

    @property
    def tested(self) -> list[dict[str, Any]]:
        return list(self.raw.get("tested") or [])

    def branch(self, name: str) -> BranchSpec | None:
        return self.branches.get(name)

    def platform(self, os_id: str, version: str, arch: str) -> PlatformSpec | None:
        for p in self.platforms:
            if p.id == os_id and p.version == version and arch in p.architectures:
                return p
        return None

    def platform_status(self, os_id: str, version: str, arch: str) -> str:
        p = self.platform(os_id, version, arch)
        return p.status if p else "unsupported"

    def supported_platforms_text(self) -> str:
        """'Ubuntu 24.04 / 26.04 x86_64' — built from the entries marked supported."""
        groups: dict[tuple[str, str], list[str]] = {}
        for p in self.platforms:
            if p.status != "supported":
                continue
            name = p.label.rsplit(" ", 1)[0]
            for arch in p.architectures:
                versions = groups.setdefault((name, arch), [])
                if p.version not in versions:
                    versions.append(p.version)
        return ", ".join(f"{name} {' / '.join(v)} {arch}" for (name, arch), v in groups.items()) or "none"

    def tested_for(self, branch: str) -> list[dict[str, Any]]:
        return [t for t in self.tested if t.get("frappe_branch") == branch]


def _data_file(name: str) -> Path:
    """Prefer the repository copy (editable installs / checkouts) over the packaged copy."""
    repo_copy = Path(__file__).resolve().parents[2] / "config" / name
    if repo_copy.is_file():
        return repo_copy
    packaged = resources.files("frappe_setup").joinpath("data").joinpath(name)
    return Path(str(packaged))


def load_matrix(path: Path | None = None) -> Matrix:
    path = path or _data_file("version-matrix.yaml")
    return _load_matrix_cached(str(path))


@lru_cache(maxsize=8)
def _load_matrix_cached(path_text: str) -> Matrix:
    path = Path(path_text)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SetupError(f"Cannot load version matrix {path}: {exc}", phase="compatibility") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise SetupError(f"Unsupported version matrix format in {path}", phase="compatibility")
    matrix = Matrix(raw=raw, source=path)
    for name, b in (raw.get("branches") or {}).items():
        matrix.branches[name] = BranchSpec(
            name=name,
            label=b.get("label", name),
            major=int(b["major"]),
            documented={k: str(v) for k, v in (b.get("documented") or {}).items()},
            upstream_ci={k: str(v) for k, v in (b.get("upstream_ci") or {}).items()},
            install=dict(b.get("install") or {}),
            needs_wkhtmltopdf=bool(b.get("needs_wkhtmltopdf", False)),
            erpnext_branch=b.get("erpnext_branch"),
            unstable=bool(b.get("unstable", False)),
        )
        for key in ("python", "node", "mariadb", "redis"):
            spec(matrix.branches[name].documented.get(key))  # fail early on typos
    for p in raw.get("platforms") or []:
        matrix.platforms.append(
            PlatformSpec(
                id=p["id"],
                version=str(p["version"]),
                codename=p.get("codename", ""),
                architectures=list(p.get("architectures") or []),
                status=p.get("status", "untested"),
                unavailable_packages=list(p.get("unavailable_packages") or []),
            )
        )
    return matrix


def load_catalog(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or _data_file("app-catalog.yaml")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise SetupError(f"Cannot load app catalog {path}: {exc}", phase="compatibility") from exc
    return list(raw.get("apps") or [])


# ------------------------------------------------------------------- check items
OK, WARN, FAIL, MISSING, INFO = "ok", "warn", "fail", "missing", "info"


@dataclass
class CheckItem:
    name: str
    status: str
    found: str = ""
    required: str = ""
    message: str = ""
    remedy: str = ""
    fixable: bool = False  # the dependency engine can resolve it (with authorization)

    @property
    def blocking(self) -> bool:
        return self.status in (FAIL, MISSING) and not self.fixable


def version_check(
    name: str,
    found: Version | None,
    constraint: str | SpecifierSet | None,
    *,
    found_text: str = "",
    missing_remedy: str = "",
    incompatible_remedy: str = "",
    fixable: bool = True,
    optional: bool = False,
) -> CheckItem:
    required = str(constraint or "any")
    if found is None:
        return CheckItem(
            name,
            WARN if optional else MISSING,
            found="not found",
            required=required,
            message=f"{name} is not installed" + (" (optional)" if optional else ""),
            remedy=missing_remedy,
            fixable=fixable,
        )
    if satisfies(found, constraint):
        return CheckItem(name, OK, found=found_text or str(found), required=required)
    return CheckItem(
        name,
        WARN if optional else FAIL,
        found=found_text or str(found),
        required=required,
        message=f"{name} {found} does not satisfy {required}",
        remedy=incompatible_remedy,
        fixable=fixable,
    )
