from __future__ import annotations

from pathlib import Path

import pytest

from frappe_setup import preflight
from frappe_setup.compatibility import FAIL, MISSING, OK, WARN
from frappe_setup.preflight import evaluate
from frappe_setup.remote import FrappeMetadata
from frappe_setup.system import Interpreter, NodeRuntime

from .conftest import make_facts


@pytest.fixture(autouse=True)
def plenty_of_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight, "disk_free_gb", lambda _p: 50.0)


def report_for(matrix, facts, branch="version-15", **kw):
    return evaluate(facts, matrix, matrix.branch(branch), target=Path("/home/dev/frappe-projects"), **kw)


def status(report, name):
    item = report.item(name)
    assert item is not None, f"no check named {name}"
    return item.status


def test_fully_provisioned_ubuntu_is_ready_for_v15(matrix) -> None:
    report = report_for(matrix, make_facts())
    assert not report.blocking
    assert not report.fixable
    assert report.python and report.python.version == "3.12.3"
    assert report.node and report.node.version == "20.11.1"


def test_v16_needs_newer_python_node_and_mariadb(matrix) -> None:
    report = report_for(matrix, make_facts(), "version-16")
    assert status(report, "Python") == MISSING and report.item("Python").fixable
    assert status(report, "Node.js") == MISSING and report.item("Node.js").fixable
    # An installed MariaDB 10.11 is never upgraded automatically: blocking.
    mariadb = report.item("MariaDB server")
    assert mariadb.status == FAIL and not mariadb.fixable
    assert mariadb in report.blocking


def test_db_version_mismatch_override_downgrades_to_warning(matrix) -> None:
    report = report_for(matrix, make_facts(), "version-16", allow_db_version_mismatch=True)
    assert status(report, "MariaDB server") == WARN
    assert not report.blocking


def test_missing_mariadb_is_installable(matrix) -> None:
    facts = make_facts(tools={"mariadbd": None, "mariadb": None})
    facts.apt_installed.pop("mariadb-server")
    report = report_for(matrix, facts, "version-16")
    assert status(report, "MariaDB server") == MISSING
    assert report.item("MariaDB server").fixable


@pytest.mark.parametrize(
    "os_id,version,arch,expected",
    [
        ("ubuntu", "24.04", "x86_64", OK),
        ("ubuntu", "24.04", "aarch64", FAIL),
        ("debian", "12", "x86_64", FAIL),
        ("ubuntu", "20.04", "x86_64", FAIL),
        ("fedora", "40", "x86_64", FAIL),
        ("ubuntu", "24.04", "riscv64", FAIL),
    ],
)
def test_platforms(matrix, os_id, version, arch, expected) -> None:
    report = report_for(matrix, make_facts(os_id=os_id, os_version=version, arch=arch))
    assert status(report, "Operating system") == expected
    if expected == FAIL:
        assert report.item("Operating system") in report.blocking


def test_untested_platform_override(matrix) -> None:
    report = report_for(matrix, make_facts(os_id="debian", os_version="12"), allow_untested_platform=True)
    assert status(report, "Operating system") == WARN
    # Unsupported (not in matrix at all) stays blocking even with the override.
    report = report_for(matrix, make_facts(os_id="fedora", os_version="40"), allow_untested_platform=True)
    assert status(report, "Operating system") == FAIL


def test_missing_cpu_features_warn(matrix) -> None:
    report = report_for(matrix, make_facts(cpu_flags=["sse2"]))
    assert status(report, "CPU features") == WARN


def test_root_is_refused_unless_allowed(matrix) -> None:
    assert status(report_for(matrix, make_facts(uid=0, user="root")), "User") == FAIL
    assert status(report_for(matrix, make_facts(uid=0, user="root"), allow_root=True), "User") == WARN


def test_memory_thresholds(matrix) -> None:
    assert status(report_for(matrix, make_facts(mem_total_mb=1024, swap_total_mb=0)), "Memory") == FAIL
    assert status(report_for(matrix, make_facts(mem_total_mb=2500, swap_total_mb=0)), "Memory") == WARN


def test_disk_thresholds(matrix, monkeypatch) -> None:
    monkeypatch.setattr(preflight, "disk_free_gb", lambda _p: 2.0)
    assert status(report_for(matrix, make_facts()), "Disk space") == FAIL
    monkeypatch.setattr(preflight, "disk_free_gb", lambda _p: 6.0)
    assert status(report_for(matrix, make_facts()), "Disk space") == WARN
    # Large apps raise the minimum.
    assert status(report_for(matrix, make_facts(), large_apps=2), "Disk space") == FAIL


def test_old_node_is_not_replaced_but_a_managed_one_is_planned(matrix) -> None:
    facts = make_facts(nodes=[NodeRuntime("/usr/bin", "16.20.2", "path")])
    report = report_for(matrix, facts)
    item = report.item("Node.js")
    assert item.status == MISSING and item.fixable
    assert "system Node.js is not modified" in item.remedy


def test_nvm_node_is_used_when_path_node_is_too_old(matrix) -> None:
    facts = make_facts(
        nodes=[NodeRuntime("/usr/bin", "20.1.0", "path"), NodeRuntime("/home/dev/.nvm/v24/bin", "24.3.0", "nvm")]
    )
    report = report_for(matrix, facts, "version-16", allow_db_version_mismatch=True)
    assert report.node and report.node.source == "nvm"


def test_python_preference_and_no_venv(matrix) -> None:
    facts = make_facts(
        pythons=[
            Interpreter("/usr/bin/python3.12", "3.12.3", "system-no-venv"),
            Interpreter("/home/dev/.pyenv/versions/3.11.9/bin/python3", "3.11.9", "pyenv"),
        ]
    )
    report = report_for(matrix, facts)
    # An interpreter with venv support wins over one without.
    assert report.python.version == "3.11.9"
    only_no_venv = make_facts(pythons=[Interpreter("/usr/bin/python3.12", "3.12.3", "system-no-venv")])
    report = report_for(matrix, only_no_venv)
    assert status(report, "Python") == MISSING and report.item("Python").fixable


def test_upstream_constraint_tightens_matrix(matrix) -> None:
    upstream = FrappeMetadata("version-15", "abc", "15.99.0", ">=3.13,<3.15", ">=22")
    report = report_for(matrix, make_facts(), upstream=upstream)
    assert status(report, "Python") == MISSING
    assert status(report, "Node.js") == MISSING
    assert report.requirements.notes


def test_ports_in_use_warn(matrix) -> None:
    report = report_for(matrix, make_facts(ports_in_use={8000: True, 9000: False}))
    assert status(report, "Ports") == WARN


def test_missing_build_prereqs_are_fixable(matrix) -> None:
    facts = make_facts(tools={"git": None, "gcc": None})
    facts.apt_installed.pop("libmariadb-dev")
    report = report_for(matrix, facts)
    for name in ("Git", "gcc", "libmariadb-dev"):
        assert report.item(name).status == MISSING and report.item(name).fixable
    assert not report.blocking


def test_postgres_requires_existing_server(matrix) -> None:
    report = report_for(matrix, make_facts(tools={"psql": None}), db_engine="postgres")
    assert status(report, "PostgreSQL client") == FAIL
    assert report.item("PostgreSQL client") in report.blocking


def test_existing_benches_are_reported(matrix) -> None:
    from frappe_setup.system import BenchInfo

    report = report_for(
        matrix, make_facts(benches=[BenchInfo("/home/dev/old-bench", ["frappe"], ["a.local"], "version-15")])
    )
    item = report.item("Existing benches")
    assert item and "/home/dev/old-bench" in item.found


def test_tool_probe_uses_extended_path(tmp_path) -> None:
    """Regression (found in the e2e run): yarn in a private prefix runs via `#!/usr/bin/env node`;
    the version probe must see the managed Node.js on PATH or yarn looks missing."""
    import os
    import sys

    from frappe_setup.commands import Runner
    from frappe_setup.system import probe_tool, tool_search_path

    node_dir, yarn_dir = tmp_path / "node" / "bin", tmp_path / "yarn" / "bin"
    node_dir.mkdir(parents=True)
    yarn_dir.mkdir(parents=True)
    fake_node = node_dir / "fakenode"
    fake_node.write_text(f"#!{sys.executable}\nprint('1.22.22')\n")
    fake_yarn = yarn_dir / "yarn"
    fake_yarn.write_text("#!/usr/bin/env fakenode\n")
    for f in (fake_node, fake_yarn):
        os.chmod(f, 0o700)
    info = probe_tool(Runner(), "yarn", ["yarn", "--version"], tool_search_path([node_dir, yarn_dir]))
    assert info.version == "1.22.22"
