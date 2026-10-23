"""Run state, resumability and safe cleanup.

The state file records configuration (never secrets), per-phase status, and which resources
this tool created. Phases re-check the real system before acting, so the state file is used
for reporting, resuming, and for deciding what cleanup is *allowed* — never as proof that
something exists.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .errors import SetupError
from .paths import ensure_private_dir, runs_dir
from .utils import atomic_write_text

PENDING, RUNNING, DONE, FAILED, INTERRUPTED, SKIPPED = (
    "pending",
    "running",
    "done",
    "failed",
    "interrupted",
    "skipped",
)

PHASES = [
    "preflight",
    "resolve",
    "dependencies",
    "database",
    "bench",
    "apps",
    "site",
    "site_apps",
    "configure",
    "verify",
]


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class PhaseState:
    status: str = PENDING
    started: str | None = None
    finished: str | None = None
    error: str | None = None


@dataclass
class Created:
    """Resources created by this tool (eligible for authorized cleanup)."""

    parent_dir: bool = False
    bench_dir: bool = False
    site: bool = False
    apps: list[str] = field(default_factory=list)


@dataclass
class RunState:
    bench_path: str
    config: dict[str, Any]
    run_id: str = ""
    tool_version: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    phases: dict[str, PhaseState] = field(default_factory=lambda: {p: PhaseState() for p in PHASES})
    created: Created = field(default_factory=Created)
    toolenv: dict[str, Any] = field(default_factory=dict)
    resolved: dict[str, Any] = field(default_factory=dict)
    log_files: list[str] = field(default_factory=list)
    completed: bool = False

    # --------------------------------------------------------------- queries
    def phase(self, name: str) -> PhaseState:
        return self.phases.setdefault(name, PhaseState())

    @property
    def incomplete(self) -> bool:
        return not self.completed

    @property
    def last_failure(self) -> tuple[str, PhaseState] | None:
        for name in PHASES:
            ps = self.phases.get(name)
            if ps and ps.status in (FAILED, INTERRUPTED):
                return name, ps
        return None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=False)

    @classmethod
    def from_json(cls, text: str) -> RunState:
        data = json.loads(text)
        phases = {k: PhaseState(**v) for k, v in (data.pop("phases", {}) or {}).items()}
        created = Created(**(data.pop("created", {}) or {}))
        state = cls(**data)
        state.phases = {p: phases.get(p, PhaseState()) for p in PHASES} | {
            k: v for k, v in phases.items() if k not in PHASES
        }
        state.created = created
        return state


def state_key(bench_path: Path | str) -> str:
    return hashlib.sha256(str(bench_path).encode()).hexdigest()[:16]


class StateStore:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or runs_dir()

    def path_for(self, bench_path: Path | str) -> Path:
        return self.directory / f"{state_key(bench_path)}.json"

    def load(self, bench_path: Path | str) -> RunState | None:
        path = self.path_for(bench_path)
        if not path.is_file():
            return None
        try:
            return RunState.from_json(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            raise SetupError(
                f"State file {path} is corrupt.",
                phase="recovery",
                remedy=f"Inspect it, then move it aside (`mv {path} {path}.bak`) to start fresh.",
                retry_safe=False,
            ) from exc

    def save(self, state: RunState) -> Path:
        ensure_private_dir(self.directory)
        state.updated_at = _now()
        path = self.path_for(state.bench_path)
        text = state.to_json()
        _assert_no_secrets(text)
        atomic_write_text(path, text, mode=0o600)
        return path

    def all(self) -> list[RunState]:
        if not self.directory.is_dir():
            return []
        states: list[RunState] = []
        for path in self.directory.glob("*.json"):
            try:
                states.append(RunState.from_json(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, TypeError, KeyError):
                continue
        return sorted(states, key=lambda s: s.updated_at)

    def latest(self, *, incomplete_only: bool = False) -> RunState | None:
        candidates = [s for s in self.all() if s.incomplete or not incomplete_only]
        return candidates[-1] if candidates else None

    # --------------------------------------------------------------- phases
    def start(self, state: RunState, phase: str) -> None:
        ps = state.phase(phase)
        ps.status, ps.started, ps.finished, ps.error = RUNNING, _now(), None, None
        self.save(state)

    def finish(self, state: RunState, phase: str, status: str = DONE, error: str | None = None) -> None:
        ps = state.phase(phase)
        ps.status, ps.finished, ps.error = status, _now(), error
        self.save(state)


_SECRET_KEYS = ("admin_password", "db_root_password", "root_password")


def _assert_no_secrets(text: str) -> None:
    lowered = text.lower()
    for key in _SECRET_KEYS:
        if f'"{key}"' in lowered:
            raise SetupError(f"Refusing to write state containing {key!r}", phase="recovery")


# ===================================================================== cleanup
@dataclass
class CleanupItem:
    kind: str  # "bench_dir"
    path: Path
    reason: str


def cleanup_candidates(state: RunState) -> list[CleanupItem]:
    """Only resources recorded as created by this tool, and only if bench init never completed."""
    items: list[CleanupItem] = []
    bench = Path(state.bench_path)
    if state.created.bench_dir and state.phase("bench").status != DONE and bench.exists():
        items.append(CleanupItem("bench_dir", bench, "partially initialized bench created by frappe-setup"))
    return items


def quarantine(path: Path) -> Path:
    """Rename instead of delete; the user can inspect or remove it later."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.incomplete-{stamp}")
    os.rename(path, target)
    return target


def delete_tree(path: Path, state: RunState) -> None:
    """Delete a directory only if the state proves this tool created it."""
    allowed = {c.path for c in cleanup_candidates(state)}
    if path not in allowed:
        raise SetupError(f"Refusing to delete {path}: it was not created by this installation.", phase="recovery")
    shutil.rmtree(path)
