from __future__ import annotations

from pathlib import Path

import pytest

from frappe_setup.app_setup import (
    ResolvedApp,
    _check_framework_constraint,
    choose_branch,
    existing_app_matches,
    get_app,
    notable_branches,
    topo_order,
)
from frappe_setup.config import AppSpec
from frappe_setup.dependencies import ToolEnv
from frappe_setup.errors import SetupError
from frappe_setup.remote import AppMetadata
from frappe_setup.ui import ScriptedUI

from .conftest import FakeRunner, ok


def app(name: str, requires: list[str] | None = None, ref: str = "version-15", url: str | None = None) -> ResolvedApp:
    url = url or f"https://github.com/frappe/{name}"
    return ResolvedApp(
        AppSpec(key=name, url=url, branch=ref), AppMetadata(url, ref, "sha", name, required_apps=requires or [])
    )


def names(apps: list[ResolvedApp]) -> list[str]:
    return [a.name for a in apps]


def test_dependencies_are_installed_first() -> None:
    ordered = topo_order([app("hrms", ["erpnext"]), app("erpnext"), app("wiki")], available=set())
    assert names(ordered) == ["erpnext", "hrms", "wiki"]


def test_user_order_kept_when_independent() -> None:
    assert names(topo_order([app("wiki"), app("crm"), app("payments")], set())) == ["wiki", "crm", "payments"]


def test_transitive_dependencies() -> None:
    ordered = topo_order([app("education", ["hrms"]), app("hrms", ["erpnext"]), app("erpnext")], set())
    assert names(ordered) == ["erpnext", "hrms", "education"]


def test_missing_dependency_is_an_error_before_anything_is_installed() -> None:
    with pytest.raises(SetupError, match="requires the app 'telephony'"):
        topo_order([app("helpdesk", ["telephony"])], set())


def test_dependency_already_in_bench_is_accepted() -> None:
    assert names(topo_order([app("hrms", ["erpnext"])], available={"erpnext"})) == ["hrms"]


def test_frappe_itself_is_not_a_missing_dependency() -> None:
    assert names(topo_order([app("x", ["frappe"])], set())) == ["x"]


def test_cycles_are_detected() -> None:
    with pytest.raises(SetupError, match="Circular"):
        topo_order([app("a", ["b"]), app("b", ["a"])], set())


def test_framework_constraint() -> None:
    meta = AppMetadata("u", "version-15", "s", "erpnext", frappe_dependencies={"frappe": ">=15.111.0,<16.0.0"})
    assert _check_framework_constraint(meta, "15.124.0", "version-15") is None
    assert "declares frappe" in (_check_framework_constraint(meta, "16.53.0", "version-16") or "")
    dev = AppMetadata("u", "develop", "s", "erpnext", frappe_dependencies={"frappe": ">=17.0.0-dev,<18.0.0"})
    assert _check_framework_constraint(dev, "17.0.0-dev", "develop") is None


REFS = {"branch": {"develop": "1", "main": "2", "version-15": "3", "feature/x": "4"}, "tag": {"v1.0.0": "5"}}


def test_choose_branch_defaults_to_same_name() -> None:
    assert (
        choose_branch(ScriptedUI(), AppSpec("erpnext", "https://github.com/frappe/erpnext"), REFS, "version-15")
        == "version-15"
    )


def test_choose_branch_explicit_must_exist() -> None:
    spec = AppSpec("x", "https://github.com/o/x", branch="version-99")
    with pytest.raises(SetupError, match="does not exist"):
        choose_branch(ScriptedUI(), spec, REFS, "version-15")
    assert (
        choose_branch(ScriptedUI(), AppSpec("x", "https://github.com/o/x", branch="v1.0.0"), REFS, "version-15")
        == "v1.0.0"
    )


def test_choose_branch_without_match_is_never_guessed_non_interactively() -> None:
    with pytest.raises(SetupError, match="choose one explicitly"):
        choose_branch(
            ScriptedUI(), AppSpec("crm", "https://github.com/frappe/crm", source="catalog"), REFS, "version-16"
        )


def test_choose_branch_asks_interactively() -> None:
    ui = ScriptedUI({"Branch for crm": "main"})
    ui.interactive = True
    assert choose_branch(ui, AppSpec("crm", "https://github.com/frappe/crm"), REFS, "version-16") == "main"


def test_notable_branches_order() -> None:
    refs = {"branch": {"zz": "1", "version-15": "2", "version-16": "3", "main": "4", "develop": "5"}, "tag": {}}
    assert notable_branches(refs) == ["main", "develop", "version-16", "version-15", "zz"]


def git_rules(remote: str, branch: str) -> FakeRunner:
    return FakeRunner(
        [
            (r"^git -C .* remote get-url upstream", ok(remote)),
            (r"^git -C .* rev-parse --abbrev-ref HEAD", ok(branch)),
        ]
    )


def make_bench(tmp_path: Path, apps: list[str]) -> Path:
    bench = tmp_path / "bench"
    (bench / "sites").mkdir(parents=True)
    (bench / "sites" / "apps.txt").write_text("\n".join(["frappe", *apps]) + "\n")
    for a in ["frappe", *apps]:
        (bench / "apps" / a).mkdir(parents=True)
    return bench


def test_existing_app_with_other_remote_is_never_overwritten(tmp_path: Path) -> None:
    bench = make_bench(tmp_path, ["erpnext"])
    runner = git_rules("https://github.com/someone-else/erpnext.git", "version-15")
    target = app("erpnext")
    assert existing_app_matches(runner, bench, target)[0] is False
    with pytest.raises(SetupError, match="different remote") as exc:
        get_app(runner, ScriptedUI(), ToolEnv(), bench, target, "bench")
    assert "never overwrites" in (exc.value.remedy or "")
    assert not runner.called("get-app")


def test_existing_matching_app_is_skipped(tmp_path: Path) -> None:
    bench = make_bench(tmp_path, ["erpnext"])
    runner = git_rules("https://github.com/frappe/erpnext.git", "version-15")
    assert get_app(runner, ScriptedUI(), ToolEnv(), bench, app("erpnext"), "bench") is False
    assert not runner.called("get-app")


def test_get_app_never_passes_overwrite(tmp_path: Path) -> None:
    bench = make_bench(tmp_path, [])

    def fake_get_app(argv, _kw):
        (bench / "apps" / "wiki").mkdir()
        with (bench / "sites" / "apps.txt").open("a") as fh:
            fh.write("wiki\n")
        return ok()

    runner = FakeRunner([(r"^bench get-app", fake_get_app), (r"python -c import wiki", ok())])
    assert get_app(runner, ScriptedUI(), ToolEnv(), bench, app("wiki", ref="master"), "bench") is True
    argv = runner.called("get-app")[0]
    assert "--overwrite" not in argv
    assert (
        argv[-3:] == ["--branch", "master", "https://github.com/frappe/wiki"]
        or argv[-1] == "https://github.com/frappe/wiki"
    )


def test_get_app_success_without_registration_is_a_failure(tmp_path: Path) -> None:
    bench = make_bench(tmp_path, [])
    runner = FakeRunner([(r"^bench get-app", ok())])
    with pytest.raises(SetupError, match="not registered"):
        get_app(runner, ScriptedUI(), ToolEnv(), bench, app("wiki"), "bench")
