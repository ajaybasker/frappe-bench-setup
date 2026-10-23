"""End-to-end verification: start the bench briefly and ping the site over HTTP."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .dependencies import ToolEnv
from .logging_config import current_log_file, get_logger
from .system import port_in_use

PHASE = "verify"


@dataclass
class HttpCheck:
    ok: bool
    url: str
    detail: str


def ping(url: str, host_header: str, timeout: float = 5) -> tuple[bool, str]:
    request = urllib.request.Request(url, headers={"Host": host_header, "Accept": "application/json"})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - local http
            body = response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        return False, str(exc)
    try:
        return json.loads(body).get("message") == "pong", body[:200]
    except ValueError:
        return False, body[:200]


def http_check(
    bench: Path, bench_cmd: str, toolenv: ToolEnv, site: str, port: int, *, timeout: float = 300
) -> HttpCheck:
    """Run `bench start` in its own process group until /api/method/ping answers, then stop it."""
    log = get_logger(PHASE)
    url = f"http://127.0.0.1:{port}/api/method/ping"
    if port_in_use(port):
        return HttpCheck(False, url, f"port {port} is already in use; skipped starting the bench")
    log_path = current_log_file()
    out = open(log_path, "ab") if log_path else subprocess.DEVNULL  # noqa: SIM115
    log.info("$ %s start   (cwd=%s, verification run)", bench_cmd, bench)
    proc = subprocess.Popen(
        [bench_cmd, "start"],
        cwd=str(bench),
        env=toolenv.env(),
        stdin=subprocess.DEVNULL,
        stdout=out,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    detail = "timed out waiting for the web server"
    ok = False
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                detail = f"bench start exited early with code {proc.returncode}"
                break
            ok, detail = ping(url, site)
            if ok:
                break
            time.sleep(3)
    finally:
        for sig, wait in ((signal.SIGINT, 15), (signal.SIGTERM, 10), (signal.SIGKILL, 5)):
            if proc.poll() is not None:
                break
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                break
            try:
                proc.wait(timeout=wait)
            except subprocess.TimeoutExpired:
                continue
        # Children (redis, workers) share the process group; make sure none survive.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        if out is not subprocess.DEVNULL:
            out.close()  # type: ignore[union-attr]
    log.info("http check %s: %s", "passed" if ok else "failed", detail)
    return HttpCheck(ok, url, detail)
