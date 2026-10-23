from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from frappe_setup import dependencies as deps
from frappe_setup import preflight
from frappe_setup.dependencies import DepContext, Privilege, ToolEnv, plan, select_node_release
from frappe_setup.errors import SetupError, UserAbort
from frappe_setup.preflight import evaluate
from frappe_setup.system import Interpreter, NodeRuntime
from frappe_setup.ui import ScriptedUI

from .conftest import FakeRunner, make_facts, ok


@pytest.fixture(autouse=True)
def disk(monkeypatch):
    monkeypatch.setattr(preflight, "disk_free_gb", lambda _p: 50.0)


def ctx_for(matrix, facts, branch="version-15", runner=None, ui=None, **kw) -> DepContext:
    report = evaluate(facts, matrix, matrix.branch(branch), target=Path("/home/dev/x"), **kw.pop("evaluate", {}))
    runner = runner or FakeRunner(
        [(r"^apt-cache policy mariadb-server", ok("  Installed: (none)\n  Candidate: 1:10.11.8-0ubuntu0.24.04.1\n"))]
    )
    ui = ui or ScriptedUI(assume_yes=True)
    return DepContext(runner, ui, facts, matrix, matrix.branch(branch), report, Privilege(runner, ui, facts), **kw)


def keys(actions) -> list[str]:
    return [a.key for a in actions]


def test_nothing_to_do_on_a_ready_machine(matrix) -> None:
    assert plan(ctx_for(matrix, make_facts())) == []


def test_existing_compatible_installations_are_reused(matrix) -> None:
    ctx = ctx_for(matrix, make_facts())
    env = deps.build_toolenv(ctx.report)
    assert env.python == "/usr/bin/python3.12"
    assert env.node_bin == "/usr/bin"


def test_missing_system_packages_become_one_apt_action(matrix) -> None:
    facts = make_facts(tools={"git": None, "redis-server": None})
    for p in ("git", "libmariadb-dev", "build-essential", "redis-server"):
        facts.apt_installed.pop(p)
    actions = plan(ctx_for(matrix, facts))
    assert keys(actions) == ["apt"]
    assert actions[0].privileged
    preview = " ".join(actions[0].preview)
    for pkg in ("git", "libmariadb-dev", "build-essential", "redis-server"):
        assert pkg in preview


def test_v16_on_fresh_ubuntu_plans_user_space_runtimes_and_mariadb_repo(matrix) -> None:
    facts = make_facts(tools={"mariadbd": None, "mariadb": None, "uv": None, "bench": None})
    facts.apt_installed.pop("mariadb-server")
    facts.apt_installed.pop("mariadb-client")
    actions = plan(ctx_for(matrix, facts, "version-16"))
    k = keys(actions)
    # Ubuntu ships MariaDB 10.11 < 11.8: the official repository is used (not the distribution package).
    assert "mariadb-repo" in k and "mariadb-service" in k
    assert k.index("uv") < k.index("python") and k.index("uv") < k.index("bench")
    assert "node" in k
    user_space = [a for a in actions if a.key in ("uv", "python", "node", "bench")]
    assert all(not a.privileged for a in user_space)
    apt_previews = " ".join(" ".join(a.preview) for a in actions if a.key == "apt")
    assert "mariadb-server" not in apt_previews


def test_v15_uses_distribution_mariadb(matrix) -> None:
    facts = make_facts(tools={"mariadbd": None, "mariadb": None})
    facts.apt_installed.pop("mariadb-server")
    facts.apt_installed.pop("mariadb-client")
    actions = plan(ctx_for(matrix, facts))
    assert "mariadb-repo" not in keys(actions)
    assert "mariadb-server" in " ".join(next(a for a in actions if a.key == "apt").preview)


def test_system_python_without_venv_gets_versioned_venv_package(matrix) -> None:
    facts = make_facts(pythons=[Interpreter("/usr/bin/python3.12", "3.12.3", "system-no-venv")])
    facts.apt_installed.pop("python3.12-venv")
    actions = plan(ctx_for(matrix, facts))
    assert "python3.12-venv" in " ".join(actions[0].preview)


def test_old_yarn_is_replaced_in_user_space(matrix) -> None:
    actions = plan(ctx_for(matrix, make_facts(tools={"yarn": "4.1.0"})))
    assert keys(actions) == ["yarn"] and not actions[0].privileged


def test_privileged_commands_require_authorization(matrix) -> None:
    facts = make_facts()
    priv = Privilege(FakeRunner(), ScriptedUI(), facts)
    with pytest.raises(SetupError, match="without authorization"):
        priv.wrap(["apt-get", "install", "x"])


def test_declining_sudo_aborts_and_runs_nothing(matrix) -> None:
    facts = make_facts(tools={"git": None})
    facts.apt_installed.pop("git")
    runner = FakeRunner()
    ui = ScriptedUI({"Run these commands with sudo?": False}, assume_yes=False)
    ctx = ctx_for(matrix, facts, runner=runner, ui=ui)
    with pytest.raises(UserAbort):
        deps.execute(ctx, plan(ctx))
    assert not runner.called("apt-get")


def test_non_interactive_without_cached_sudo_fails_clearly(matrix) -> None:
    facts = make_facts()
    runner = FakeRunner([(r"^sudo -n -v", ok().__class__([], 1, "sudo: a password is required"))])
    priv = Privilege(runner, ScriptedUI(assume_yes=True), facts)
    with pytest.raises(SetupError, match="non-interactively"):
        priv.authorize(["apt-get update"], "test")


def test_sudo_wraps_with_noninteractive_env(matrix) -> None:
    facts = make_facts(sudo_nopasswd=True)
    runner = FakeRunner([(r"^sudo -n -v", ok())])
    priv = Privilege(runner, ScriptedUI(assume_yes=True), facts)
    priv.authorize(["apt-get update"], "test")
    argv = priv.wrap(["apt-get", "update"], env={"DEBIAN_FRONTEND": "noninteractive"})
    assert argv[:3] == ["sudo", "-n", "env"] and "DEBIAN_FRONTEND=noninteractive" in argv
    root = Privilege(runner, ScriptedUI(assume_yes=True), make_facts(uid=0))
    root.authorize(["x"], "test")
    assert root.wrap(["apt-get", "update"]) == ["apt-get", "update"]


def test_apt_install_verifies_packages(matrix) -> None:
    facts = make_facts(sudo_nopasswd=True, tools={"git": None})
    facts.apt_installed.pop("git")
    runner = FakeRunner(
        [
            (r"^sudo -n -v", ok()),
            (r"apt-get", ok()),
            (r"^dpkg-query", ok("")),  # apt "succeeded" but nothing is installed
            (r"^apt-cache", ok("Candidate: 1:10.11.8")),
        ]
    )
    ctx = ctx_for(matrix, facts, runner=runner)
    with pytest.raises(SetupError, match="still not installed"):
        deps.execute(ctx, plan(ctx))


def test_select_node_release() -> None:
    index = [
        {"version": "v24.2.0", "files": ["linux-x64", "linux-arm64"]},
        {"version": "v24.10.1", "files": ["linux-x64"]},
        {"version": "v25.0.0", "files": ["linux-x64"]},
        {"version": "v24.11.0", "files": ["darwin-x64"]},
    ]
    assert select_node_release(index, 24, "x64") == "v24.10.1"
    assert select_node_release(index, 24, "arm64") == "v24.2.0"
    with pytest.raises(SetupError):
        select_node_release(index, 18, "x64")


def _node_tarball(version: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        script = b"#!/bin/sh\necho " + version.encode() + b"\n"
        info = tarfile.TarInfo(f"node-{version}-linux-x64/bin/node")
        info.size, info.mode = len(script), 0o755
        tar.addfile(info, io.BytesIO(script))
    return buf.getvalue()


def test_install_node_verifies_checksum(matrix, monkeypatch, isolated_home) -> None:
    data = _node_tarball("v20.99.0")
    monkeypatch.setattr(deps, "_node_arch", lambda: "x64")
    monkeypatch.setattr(deps, "fetch_json", lambda url, phase: [{"version": "v20.99.0", "files": ["linux-x64"]}])
    monkeypatch.setattr(
        deps, "fetch_bytes", lambda url, phase: f"{'0' * 64}  node-v20.99.0-linux-x64.tar.gz\n".encode()
    )

    def fake_download(url, dest, phase):
        dest.write_bytes(data)
        return dest

    monkeypatch.setattr(deps, "download", fake_download)
    facts = make_facts(nodes=[NodeRuntime("/usr/bin", "16.0.0", "path")])
    ctx = ctx_for(matrix, facts)
    with pytest.raises(SetupError, match="checksum"):
        deps.install_node(ctx, 20)
    assert not (deps.managed_dirs()["node"] / "node-v20.99.0-linux-x64").exists()


def test_install_node_success_is_user_space(matrix, monkeypatch, isolated_home) -> None:
    data = _node_tarball("v20.99.0")
    digest = hashlib.sha256(data).hexdigest()
    monkeypatch.setattr(deps, "_node_arch", lambda: "x64")
    monkeypatch.setattr(deps, "fetch_json", lambda url, phase: [{"version": "v20.99.0", "files": ["linux-x64"]}])
    monkeypatch.setattr(deps, "fetch_bytes", lambda url, phase: f"{digest}  node-v20.99.0-linux-x64.tar.gz\n".encode())
    monkeypatch.setattr(deps, "download", lambda url, dest, phase: (dest.write_bytes(data), dest)[1])
    runner = FakeRunner([(r"^node --version$", ok("v20.99.0"))])
    ctx = ctx_for(matrix, make_facts(nodes=[]), runner=runner)
    target = deps.install_node(ctx, 20)
    assert str(target).startswith(str(isolated_home))
    assert ctx.toolenv.path_entries[0] == target / "bin"


def test_unsafe_archive_is_rejected(tmp_path: Path) -> None:
    from frappe_setup.utils import safe_extract_tar

    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("../../escape.txt")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(SetupError, match="Unsafe"):
        safe_extract_tar(archive, tmp_path / "out")
    assert not (tmp_path.parent / "escape.txt").exists()


def test_mariadb_repo_refuses_existing_repo_config(matrix, monkeypatch, tmp_path) -> None:
    ctx = ctx_for(matrix, make_facts())
    monkeypatch.setattr(
        deps.Path,
        "glob",
        lambda self, pat: [Path("/etc/apt/sources.list.d/mariadb-old.list")] if "sources.list.d" in str(self) else [],
    )
    with pytest.raises(SetupError, match="already configured"):
        deps._install_mariadb_repo(ctx, "11.8")


def test_mariadb_key_fingerprint_mismatch_stops(matrix, monkeypatch) -> None:
    ctx = ctx_for(matrix, make_facts())
    monkeypatch.setattr(deps.Path, "glob", lambda self, pat: [])
    monkeypatch.setattr(deps, "fetch_bytes", lambda url, phase: b"fake key")
    monkeypatch.setattr(deps, "_gpg_fingerprints", lambda runner, key: ["DEADBEEF" * 5])
    with pytest.raises(SetupError, match="fingerprint"):
        deps._install_mariadb_repo(ctx, "11.8")
    assert not ctx.runner.called("install -m 0644")


def test_toolenv_script_and_roundtrip(tmp_path: Path) -> None:
    env = ToolEnv(python="/p/python")
    env.add(tmp_path / "node" / "bin")
    env.add(tmp_path / "node" / "bin")
    assert len(env.path_entries) == 1
    assert f'export PATH="{tmp_path}/node/bin:$PATH"' in env.script()
    assert ToolEnv.from_dict(env.to_dict()).path_entries == env.path_entries
    assert env.env()["PATH"].startswith(str(tmp_path / "node" / "bin"))


def test_toolenv_includes_preselected_node_for_installers(matrix, tmp_path) -> None:
    """Regression (found by a real resume): a Node.js installed by an earlier run is chosen by
    preflight; installers like `npm install yarn` must find npm through the ToolEnv PATH."""
    import os

    node_bin = tmp_path / "node-v20" / "bin"
    node_bin.mkdir(parents=True)
    npm = node_bin / "npm"
    npm.write_text("#!/bin/sh\n")
    os.chmod(npm, 0o700)
    facts = make_facts(nodes=[NodeRuntime(str(node_bin), "20.20.2", "managed")], tools={"node": None, "npm": None})
    report = evaluate(facts, matrix, matrix.branch("version-15"), target=tmp_path)
    env = deps.build_toolenv(report, ToolEnv())
    assert env.which("npm") == str(npm)
