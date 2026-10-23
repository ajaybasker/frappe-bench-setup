"""bench_redis(): temporary Redis for install-app (regression from the ERPNext e2e run)."""

from __future__ import annotations

import shutil
import socket
from pathlib import Path

import pytest

from frappe_setup.dependencies import ToolEnv
from frappe_setup.diagnosis import diagnose
from frappe_setup.services import bench_redis, redis_configs
from frappe_setup.system import port_in_use


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def make_bench(tmp_path: Path, port: int) -> Path:
    bench = tmp_path / "bench"
    (bench / "config" / "pids").mkdir(parents=True)
    (bench / "config" / "redis_queue.conf").write_text(
        f'dbfilename redis_queue.rdb\ndir {bench}/config/pids\nbind 127.0.0.1\nport {port}\nsave ""\n'
    )
    return bench


def test_redis_configs_reads_ports(tmp_path: Path) -> None:
    bench = make_bench(tmp_path, 11000)
    assert redis_configs(bench) == [(bench / "config" / "redis_queue.conf", 11000)]
    assert redis_configs(tmp_path / "missing") == []


@pytest.mark.skipif(shutil.which("redis-server") is None, reason="redis-server not installed")
def test_starts_and_stops_bench_redis(tmp_path: Path) -> None:
    port = free_port()
    bench = make_bench(tmp_path, port)
    with bench_redis(bench, ToolEnv()) as ports:
        assert ports == [port]
        assert port_in_use(port)
    assert not port_in_use(port)


@pytest.mark.skipif(shutil.which("redis-server") is None, reason="redis-server not installed")
def test_already_running_redis_is_left_alone(tmp_path: Path) -> None:
    port = free_port()
    bench = make_bench(tmp_path, port)
    with bench_redis(bench, ToolEnv()), bench_redis(bench, ToolEnv()) as inner:
        assert inner == []  # did not start a second one
    assert not port_in_use(port)


def test_redis_down_is_not_misdiagnosed_as_network() -> None:
    finding = diagnose("redis.exceptions.ConnectionError: Error 111 connecting to 127.0.0.1:11000. Connection refused.")
    assert finding is not None and finding.key == "bench_redis_down"
