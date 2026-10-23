"""Installation configuration (persisted) and secrets (never persisted)."""

from __future__ import annotations

import os
import stat
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import validation as v
from .errors import ValidationError

DEFAULT_BENCH_NAME = "frappe-bench"
DEFAULT_PARENT_DIR = "~/frappe-projects"
DEFAULT_SITE_NAME = "dev.localhost"
DEFAULT_BRANCH = "version-15"


@dataclass
class AppSpec:
    """An application to fetch into the bench.

    `name` is the Python package name as declared by the repository (filled in by the
    resolver); `key` is the catalog key or the URL basename for custom apps.
    """

    key: str
    url: str
    branch: str | None = None
    name: str | None = None
    source: str = "custom"  # "catalog" | "custom"
    install_on_site: bool = True

    def validate(self) -> AppSpec:
        v.validate_git_url(self.url)
        if self.branch:
            v.validate_branch(self.branch)
        if self.name:
            v.validate_app_name(self.name)
        return self


@dataclass
class DatabaseConfig:
    engine: str = "mariadb"  # "mariadb" | "postgres"
    host: str = "127.0.0.1"
    port: int | None = None
    root_user: str = "root"

    def validate(self) -> DatabaseConfig:
        if self.engine not in {"mariadb", "postgres"}:
            raise ValidationError(f"Unsupported database engine {self.engine!r}.")
        v.validate_db_host(self.host)
        if self.port is not None:
            self.port = v.validate_port(self.port)
        v.validate_db_user(self.root_user)
        return self

    @property
    def effective_port(self) -> int:
        return self.port or (3306 if self.engine == "mariadb" else 5432)


@dataclass
class InstallConfig:
    bench_name: str = DEFAULT_BENCH_NAME
    parent_dir: str = DEFAULT_PARENT_DIR
    frappe_branch: str = DEFAULT_BRANCH
    site_name: str = DEFAULT_SITE_NAME
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    apps: list[AppSpec] = field(default_factory=list)
    developer_mode: bool = True
    set_default_site: bool = True
    install_wkhtmltopdf: bool = False
    enable_backups: bool = False
    verify_http: bool = True
    reuse_existing_bench: bool = False

    # ---------------------------------------------------------------- derived
    @property
    def parent_path(self) -> Path:
        return v.expand_path(self.parent_dir)

    @property
    def bench_path(self) -> Path:
        return v.bench_path(self.parent_path, self.bench_name)

    def validate(self) -> InstallConfig:
        self.bench_name = v.validate_bench_name(self.bench_name)
        v.validate_parent_dir(self.parent_dir)
        _ = self.bench_path
        self.frappe_branch = v.validate_branch(self.frappe_branch)
        self.site_name = v.validate_site_name(self.site_name)
        self.database.validate()
        seen: set[str] = set()
        for app in self.apps:
            app.validate()
            norm = v.normalize_url(app.url)
            if norm in seen:
                raise ValidationError(f"App repository listed twice: {app.url}")
            seen.add(norm)
        return self

    # ---------------------------------------------------------- serialization
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InstallConfig:
        data = dict(data or {})
        unknown = set(data) - {f for f in cls.__dataclass_fields__}
        if unknown:
            raise ValidationError(f"Unknown configuration keys: {', '.join(sorted(unknown))}")
        db = data.pop("database", None) or {}
        if not isinstance(db, dict):
            raise ValidationError("'database' must be a mapping.")
        db_unknown = set(db) - set(DatabaseConfig.__dataclass_fields__)
        if db_unknown:
            raise ValidationError(f"Unknown database keys: {', '.join(sorted(db_unknown))}")
        apps_raw = data.pop("apps", None) or []
        apps: list[AppSpec] = []
        for item in apps_raw:
            if not isinstance(item, dict):
                raise ValidationError("Each entry in 'apps' must be a mapping with at least 'url'.")
            extra = set(item) - set(AppSpec.__dataclass_fields__)
            if extra:
                raise ValidationError(f"Unknown app keys: {', '.join(sorted(extra))}")
            if "url" not in item:
                raise ValidationError("App entry is missing 'url'.")
            item.setdefault("key", v.repo_basename(item["url"]))
            apps.append(AppSpec(**item))
        return cls(database=DatabaseConfig(**db), apps=apps, **data)

    @classmethod
    def from_yaml(cls, path: Path) -> InstallConfig:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ValidationError(f"{path}: invalid YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValidationError(f"{path}: top level must be a mapping.")
        for key in ("admin_password", "db_root_password", "password"):
            if key in raw or key in (raw.get("database") or {}):
                raise ValidationError(
                    f"{path}: secrets must not be stored in the configuration file; use "
                    "--admin-password-file / --db-root-password-file or environment variables."
                )
        return cls.from_dict(raw)


@dataclass
class Secrets:
    """Held in memory only. repr() never reveals values."""

    admin_password: str | None = None
    db_root_password: str | None = None

    def __repr__(self) -> str:
        def state(x: str | None) -> str:
            return "set" if x else "unset"

        return f"Secrets(admin_password={state(self.admin_password)}, db_root_password={state(self.db_root_password)})"


ENV_ADMIN_PASSWORD = "FRAPPE_SETUP_ADMIN_PASSWORD"
ENV_DB_ROOT_PASSWORD = "FRAPPE_SETUP_DB_ROOT_PASSWORD"


def read_secret_file(path: Path) -> str:
    """Read a secret from a file the user controls; warn-level checks on permissions."""
    path = path.expanduser()
    try:
        st = path.stat()
    except OSError as exc:
        raise ValidationError(f"Cannot read secret file {path}: {exc.strerror}") from exc
    if not stat.S_ISREG(st.st_mode):
        raise ValidationError(f"Secret file {path} is not a regular file.")
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ValidationError(
            f"Secret file {path} is readable by other users; run `chmod 600 {path}` first."
        )
    value = path.read_text(encoding="utf-8").rstrip("\r\n")
    if not value:
        raise ValidationError(f"Secret file {path} is empty.")
    return value


def secret_from_env(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value else None
