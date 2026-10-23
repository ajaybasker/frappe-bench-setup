"""`frappe-setup doctor`: read-only diagnosis and a shareable, redacted report."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import __version__
from .bench_setup import read_common_config, validate_bench
from .commands import Runner
from .compatibility import FAIL, INFO, MISSING, OK, WARN, CheckItem, load_matrix
from .dependencies import ToolEnv, build_toolenv, mariadb_alive
from .diagnosis import diagnose
from .installer import collect_facts
from .logging_config import current_log_file
from .preflight import evaluate
from .recovery import RunState, StateStore
from .redaction import redact
from .site_setup import site_exists
from .system import inspect_bench, port_in_use
from .ui import UI
from .utils import atomic_write_text


def _state_for(store: StateStore, bench: Path | None) -> RunState | None:
    if bench is not None:
        return store.load(bench.expanduser().resolve())
    return store.latest()


def _log_finding(state: RunState | None) -> CheckItem | None:
    if not state or not state.log_files:
        return None
    path = Path(state.log_files[-1])
    if not path.is_file():
        return None
    tail = "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-400:])
    finding = diagnose(tail)
    if not finding:
        return None
    return CheckItem("Last run log", WARN, found=path.name, message=finding.cause, remedy=finding.remedy)


def run_doctor(ui: UI, runner: Runner, *, bench: Path | None, share: Path | None) -> list[CheckItem]:
    matrix = load_matrix()
    store = StateStore()
    state = _state_for(store, bench)
    bench_path = Path(state.bench_path) if state else (bench.expanduser().resolve() if bench else None)
    toolenv = ToolEnv.from_dict(state.toolenv) if state and state.toolenv else ToolEnv()
    branch_name = (state.config.get("frappe_branch") if state else None) or next(iter(matrix.branches))
    branch = matrix.branch(str(branch_name)) or next(iter(matrix.branches.values()))

    with ui.progress("Collecting diagnostics (read-only)"):
        facts = collect_facts(
            runner, matrix, parent=bench_path.parent if bench_path else None, extra=toolenv.path_entries
        )
    report = evaluate(
        facts,
        matrix,
        branch,
        target=bench_path.parent if bench_path else Path.home(),
        allow_untested_platform=True,
        allow_root=True,
        allow_db_version_mismatch=True,
    )
    toolenv = build_toolenv(report, toolenv)
    items: list[CheckItem] = [
        i
        for i in report.items
        if i.status != OK or i.name in ("Operating system", "Python", "Node.js", "MariaDB server", "Bench CLI")
    ]

    # Services
    alive = mariadb_alive(runner)
    items.append(
        CheckItem(
            "MariaDB responds",
            OK if alive else FAIL,
            found="yes" if alive else "no",
            remedy="" if alive else "sudo systemctl start mariadb && systemctl status mariadb",
        )
    )

    # Bench
    if bench_path:
        info = inspect_bench(bench_path)
        if info is None:
            items.append(
                CheckItem("Bench", FAIL, found=f"{bench_path} missing or incomplete", remedy="frappe-setup resume")
            )
        else:
            check = validate_bench(
                runner, toolenv, bench_path, branch=info.frappe_branch, python_constraint=report.requirements.python
            )
            items.append(
                CheckItem(
                    "Bench structure",
                    OK if check.ok else FAIL,
                    found=f"frappe {check.frappe_version} / Python {check.python_version}",
                    message="; ".join(check.problems),
                    remedy="" if check.ok else "frappe-setup resume",
                )
            )
            cfg = read_common_config(bench_path)
            for key in ("webserver_port", "socketio_port"):
                port = cfg.get(key)
                if isinstance(port, int):
                    items.append(
                        CheckItem(
                            f"Port {port} ({key})",
                            INFO,
                            found="in use (bench running?)" if port_in_use(port) else "free",
                        )
                    )
            site = state.config.get("site_name") if state else None
            if site:
                present = site_exists(bench_path, str(site))
                items.append(
                    CheckItem(
                        f"Site {site}",
                        OK if present else MISSING,
                        found="present" if present else "missing",
                        remedy="" if present else "frappe-setup resume",
                    )
                )
        if state and state.resolved.get("env_script") and not Path(str(state.resolved["env_script"])).is_file():
            items.append(
                CheckItem("Environment script", WARN, found="missing", remedy="frappe-setup resume (regenerates it)")
            )
        if toolenv.node_bin and not Path(toolenv.node_bin, "node").exists():
            items.append(
                CheckItem(
                    "Node.js for bench",
                    FAIL,
                    found=f"{toolenv.node_bin}/node missing",
                    remedy="frappe-setup resume (reinstalls the managed Node.js)",
                )
            )

    # Run state
    if state:
        failure = state.last_failure
        if failure:
            items.append(
                CheckItem(
                    "Last run",
                    FAIL,
                    found=f"phase {failure[0]}",
                    message=redact(failure[1].error or ""),
                    remedy="Fix the cause, then `frappe-setup resume`.",
                )
            )
        else:
            items.append(
                CheckItem(
                    "Last run", OK if state.completed else WARN, found="completed" if state.completed else "incomplete"
                )
            )
        finding = _log_finding(state)
        if finding:
            items.append(finding)

    ui.check_table(items, title="frappe-setup doctor")
    problems = [i for i in items if i.status in (FAIL, MISSING)]
    if problems:
        ui.error(f"{len(problems)} problem(s) found.")
    else:
        ui.success("No problems found.")

    if share:
        payload = {
            "tool_version": __version__,
            "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
            "os": facts.os_pretty,
            "arch": facts.arch,
            "in_container": facts.in_container,
            "memory_mb": facts.mem_total_mb,
            "tools": {k: t.version for k, t in facts.tools.items()},
            "pythons": [f"{p.version} [{p.source}]" for p in facts.pythons],
            "nodes": [f"{n.version} [{n.source}]" for n in facts.nodes],
            "branch": branch.name,
            "bench_exists": bool(bench_path and bench_path.exists()),
            "phases": {k: v.status for k, v in state.phases.items()} if state else {},
            "checks": [{"name": i.name, "status": i.status, "found": i.found, "message": i.message} for i in items],
        }
        # Paths can contain user names; replace the home directory.
        text = redact(json.dumps(payload, indent=2)).replace(str(Path.home()), "~")
        atomic_write_text(share.expanduser(), text + "\n", mode=0o644)
        ui.success(f"Shareable report written to {share} (no passwords, tokens or environment variables).")
    log = current_log_file()
    if log:
        ui.info(f"[dim]Log: {log}[/]")
    return items
