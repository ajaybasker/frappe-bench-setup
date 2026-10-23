"""Temporary bench services needed while installing (not a replacement for `bench start`).

`bench --site … install-app erpnext` talks to the bench's own Redis (queue and cache). Those
Redis processes normally run under `bench start`, which is not running during installation, so
they are started here from the bench's generated `config/redis_*.conf` — exactly as the
Procfile does — and stopped again afterwards.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

from .dependencies import ToolEnv
from .errors import SetupError
from .logging_config import current_log_file, get_logger
from .system import port_in_use

PHASE = "services"


def redis_configs(bench: Path) -> list[tuple[Path, int]]:
    """(config file, port) for every Redis the Procfile would start."""
    found: list[tuple[Path, int]] = []
    for conf in sorted((bench / "config").glob("redis_*.conf")):
        m = re.search(r"^port\s+(\d+)", conf.read_text(encoding="utf-8"), re.M)
        if m:
            found.append((conf, int(m.group(1))))
    return found


@contextlib.contextmanager
def bench_redis(bench: Path, toolenv: ToolEnv, *, timeout: float = 30) -> Iterator[list[int]]:
    """Start the bench's Redis servers that are not already running; stop only those on exit."""
    log = get_logger(PHASE)
    exe = toolenv.which("redis-server")
    started: list[subprocess.Popen[bytes]] = []
    ports: list[int] = []
    log_path = current_log_file()
    out = open(log_path, "ab") if log_path else subprocess.DEVNULL  # noqa: SIM115
    try:
        for conf, port in redis_configs(bench):
            if port_in_use(port):
                log.info("redis on port %s already running; leaving it alone", port)
                continue
            if exe is None:
                raise SetupError(
                    "redis-server is not installed, but the bench needs it.",
                    phase=PHASE,
                    remedy="Install redis-server (apt) and rerun `frappe-setup resume`.",
                    retry_safe=True,
                )
            log.info("$ %s %s   (temporary, cwd=%s)", exe, conf.relative_to(bench), bench)
            proc = subprocess.Popen(
                [exe, str(conf.relative_to(bench))],
                cwd=str(bench),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            started.append(proc)
            ports.append(port)
            deadline = time.monotonic() + timeout
            while not port_in_use(port):
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise SetupError(
                        f"The bench's Redis ({conf.name}, port {port}) did not start.",
                        phase=PHASE,
                        cause=f"redis-server exited with code {proc.poll()}" if proc.poll() is not None else "timeout",
                        remedy="Check the log; another process may be using the port (`ss -ltnp`).",
                        retry_safe=True,
                        log_file=str(log_path) if log_path else None,
                    )
                time.sleep(0.2)
        yield ports
    finally:
        for proc in started:
            if proc.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
        if out is not subprocess.DEVNULL:
            out.close()  # type: ignore[union-attr]
        if started:
            log.info("stopped temporary redis on ports %s", ports)
