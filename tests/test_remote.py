from __future__ import annotations

import os

import pytest

from frappe_setup.errors import SetupError, ValidationError
from frappe_setup.remote import (
    list_refs,
    parse_required_apps,
    probe_app,
    probe_frappe,
    required_app_name,
    resolve_ref,
)

from .conftest import FakeRunner, fail, ok

LS_REMOTE = "\n".join(
    [
        "aaa\trefs/heads/develop",
        "bbb\trefs/heads/version-15",
        "ccc\trefs/heads/version-16",
        "ddd\trefs/tags/v15.1.0",
        "eee\trefs/tags/v15.1.0^{}",
        *[f"{i:040x}\trefs/tags/v14.{i}.0" for i in range(3000)],  # long listings must not be truncated
    ]
)


def runner_with_repo(files: dict[str, str], refs: str = LS_REMOTE) -> FakeRunner:
    def show(argv, _kw):
        path = argv[-1].removeprefix("HEAD:")
        return ok(files[path]) if path in files else fail(f"fatal: path '{path}' does not exist", 128)

    return FakeRunner(
        [
            (r"^git ls-remote", ok(refs)),
            (r"^git clone .*--no-checkout", ok()),
            (r"^git -C .* show HEAD:", show),
        ]
    )


def test_list_refs_parses_branches_and_peeled_tags() -> None:
    refs = list_refs(runner_with_repo({}), "https://github.com/frappe/frappe")
    assert refs["branch"] == {"develop": "aaa", "version-15": "bbb", "version-16": "ccc"}
    assert refs["tag"]["v15.1.0"] == "eee"
    assert len(refs["tag"]) == 3001


def test_resolve_ref_never_substitutes() -> None:
    r = runner_with_repo({})
    assert resolve_ref(r, "https://github.com/frappe/frappe", "version-15").sha == "bbb"
    assert resolve_ref(r, "https://github.com/frappe/frappe", "v15.1.0").kind == "tag"
    with pytest.raises(SetupError) as exc:
        resolve_ref(r, "https://github.com/frappe/frappe", "version-99")
    assert exc.value.retry_safe is False
    assert "version-16" in (exc.value.remedy or "")


def test_ls_remote_network_failure_reports_retry_safe() -> None:
    r = FakeRunner(
        [
            (
                r"^git ls-remote",
                fail("fatal: unable to access 'https://github.com/x/': Could not resolve host: github.com", 128),
            )
        ]
    )
    with pytest.raises(SetupError) as exc:
        list_refs(r, "https://github.com/frappe/frappe")
    assert exc.value.retry_safe is True
    assert "network" in (exc.value.cause or "").lower()


def test_git_urls_are_passed_after_double_dash() -> None:
    r = runner_with_repo({})
    list_refs(r, "https://github.com/frappe/frappe")
    argv = r.called("ls-remote")[0]
    assert argv[argv.index("--") + 1] == "https://github.com/frappe/frappe"


def test_invalid_url_is_rejected_before_running_git() -> None:
    r = runner_with_repo({})
    with pytest.raises(ValidationError):
        list_refs(r, "ext::sh -c id")
    assert r.calls == []


def test_probe_frappe_reads_requirements() -> None:
    from frappe_setup.remote import RefInfo

    files = {
        "pyproject.toml": '[project]\nname = "frappe"\nrequires-python = ">=3.14,<3.15"\n',
        "package.json": '{"name": "frappe", "engines": {"node": ">=24"}}',
        "frappe/__init__.py": "import os\n\n" + "x = 1\n" * 300 + '__version__ = "16.53.0"\n',
    }
    meta = probe_frappe(
        runner_with_repo(files), "https://github.com/frappe/frappe", RefInfo("u", "version-16", "branch", "ccc")
    )
    assert (meta.version, meta.requires_python, meta.node_engine) == ("16.53.0", ">=3.14,<3.15", ">=24")


def test_probe_app_reads_name_dependencies_and_constraints() -> None:
    from frappe_setup.remote import RefInfo

    files = {
        "pyproject.toml": textwrap_pyproject(),
        "hrms/hooks.py": 'app_name = "hrms"\napp_title = "Frappe HR"\nrequired_apps = ["frappe/erpnext"]\n',
    }
    meta = probe_app(
        runner_with_repo(files), "https://github.com/frappe/hrms", RefInfo("u", "version-15", "branch", "bbb")
    )
    assert meta.name == "hrms" and meta.title == "Frappe HR"
    assert meta.required_apps == ["erpnext"]
    assert meta.frappe_dependencies == {"frappe": ">=15.0.0,<16.0.0", "erpnext": ">=15.0.0,<16.0.0"}


def textwrap_pyproject() -> str:
    return (
        '[project]\nname = "hrms"\nrequires-python = ">=3.10"\n\n'
        '[tool.bench.frappe-dependencies]\nfrappe = ">=15.0.0,<16.0.0"\nerpnext = ">=15.0.0,<16.0.0"\n'
    )


def test_probe_app_rejects_non_frappe_repository() -> None:
    from frappe_setup.remote import RefInfo

    r = runner_with_repo({"pyproject.toml": '[project]\nname = "requests"\n'})
    with pytest.raises(SetupError, match="does not look like a Frappe app"):
        probe_app(r, "https://github.com/psf/requests", RefInfo("u", "main", "branch", "x"))


def test_probe_snapshot_directory_is_removed(isolated_home) -> None:
    from frappe_setup.paths import cache_dir
    from frappe_setup.remote import RefInfo

    files = {"pyproject.toml": textwrap_pyproject(), "hrms/hooks.py": "required_apps = []\n"}
    probe_app(runner_with_repo(files), "https://github.com/frappe/hrms", RefInfo("u", "version-15", "branch", "b"))
    probe_dir = cache_dir() / "probe"
    assert not probe_dir.exists() or os.listdir(probe_dir) == []


@pytest.mark.parametrize(
    "hooks,expected",
    [
        ('required_apps = ["frappe/erpnext", "payments"]', ["frappe/erpnext", "payments"]),
        ("required_apps = []", []),
        ("x = 1", []),
        ("required_apps = some_function()", []),  # never executed
        ("def broken(:", []),
        (None, []),
    ],
)
def test_parse_required_apps(hooks, expected) -> None:
    assert parse_required_apps(hooks) == expected


@pytest.mark.parametrize(
    "entry,name",
    [
        ("frappe/erpnext", "erpnext"),
        ("https://github.com/frappe/hrms.git", "hrms"),
        ("telephony", "telephony"),
        ("org/print-designer", "print_designer"),
    ],
)
def test_required_app_name(entry, name) -> None:
    assert required_app_name(entry) == name


@pytest.mark.network
@pytest.mark.skipif(os.environ.get("FRAPPE_SETUP_NETWORK_TESTS") != "1", reason="set FRAPPE_SETUP_NETWORK_TESTS=1")
def test_real_upstream_branches() -> None:  # pragma: no cover - opt-in
    from frappe_setup.commands import Runner

    r = Runner()
    ref = resolve_ref(r, "https://github.com/frappe/frappe", "version-15")
    meta = probe_frappe(r, "https://github.com/frappe/frappe", ref)
    assert meta.version and meta.version.startswith("15.")
    assert meta.requires_python
