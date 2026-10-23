from __future__ import annotations

from pathlib import Path

import pytest
from packaging.version import Version

from frappe_setup.compatibility import (
    FAIL,
    MISSING,
    OK,
    combine,
    load_catalog,
    load_matrix,
    mariadb_server_version,
    normalize_node_engine,
    parse_version,
    satisfies,
    version_check,
)
from frappe_setup.errors import SetupError


@pytest.mark.parametrize(
    "text,expected",
    [
        ("v20.11.1", "20.11.1"),
        ("Python 3.12.3", "3.12.3"),
        ("git version 2.43.0", "2.43.0"),
        ("1.22.22", "1.22.22"),
        ("17.0.0-dev", "17.0.0.dev0"),
        ("uv 0.9.30 (abc 2026-01-01)", "0.9.30"),
        ("3.14.0rc1", "3.14.0rc1"),
        ("", None),
        (None, None),
        ("no digits here", None),
    ],
)
def test_parse_version(text, expected) -> None:
    result = parse_version(text)
    assert (str(result) if result else None) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("mariadbd  Ver 11.8.3-MariaDB-ubu2404 for debian-linux-gnu on x86_64", "11.8.3"),
        ("mariadb from 11.8.9-MariaDB, client 15.2 for debian-linux-gnu", "11.8.9"),
        ("mysql  Ver 15.1 Distrib 10.11.8-MariaDB, for debian-linux-gnu", "10.11.8"),
        ("10.6.24-MariaDB-ubu2204", "10.6.24"),
    ],
)
def test_mariadb_version_strings(text, expected) -> None:
    assert str(mariadb_server_version(text)) == expected


def test_satisfies_judges_prereleases_by_release_numbers() -> None:
    assert satisfies("3.14.0rc1", ">=3.14,<3.15")
    assert satisfies("17.0.0-dev", ">=17.0.0-dev,<18")
    assert not satisfies("3.13.5", ">=3.14,<3.15")
    assert not satisfies(None, ">=1")
    assert satisfies("1.0", None)


def test_combine_is_intersection() -> None:
    s = combine(">=3.10,<3.15", ">=3.12")
    assert satisfies("3.12.1", s)
    assert not satisfies("3.11.9", s)
    assert not satisfies("3.15.0", s)


@pytest.mark.parametrize(
    "engine,good,bad",
    [
        (">=18", "18.0.0", "16.20.0"),
        (">=24", "24.1.0", "22.9.0"),
        ("^20", "20.5.0", "21.0.0"),
        ("18.x", "18.19.1", "19.0.0"),
        ("18", "18.2.0", "20.0.0"),
        (">=18 <21", "20.9.0", "21.1.0"),
    ],
)
def test_normalize_node_engine(engine, good, bad) -> None:
    s = normalize_node_engine(engine)
    assert s is not None
    assert satisfies(good, s)
    assert not satisfies(bad, s)


def test_normalize_node_engine_unsupported_syntax() -> None:
    assert normalize_node_engine(">=18 || >=20") is None
    assert normalize_node_engine(None) is None


def test_matrix_is_valid_and_per_branch() -> None:
    m = load_matrix()
    assert {"version-15", "version-16", "develop"} <= set(m.branches)
    v15, v16 = m.branch("version-15"), m.branch("version-16")
    assert v15 and v16
    # Branch-specific, not a global dependency set.
    assert v15.requirement("python") != v16.requirement("python")
    assert v15.requirement("node") != v16.requirement("node")
    assert satisfies("3.12.3", v15.requirement("python"))
    assert not satisfies("3.12.3", v16.requirement("python"))
    assert m.mariadb_repository["key_fingerprint"] == "177F4010FE56CA3336300305F1656F24C74CD1D8"
    for name, branch in m.branches.items():
        assert branch.install.get("python"), name
        assert satisfies(str(branch.install["python"]) + ".0", branch.requirement("python")), name
        assert satisfies(f"{branch.install['node_major']}.0.0", branch.requirement("node")), name


def test_platform_status() -> None:
    m = load_matrix()
    assert m.platform_status("ubuntu", "24.04", "x86_64") == "supported"
    assert m.platform_status("ubuntu", "24.04", "aarch64") == "untested"
    assert m.platform_status("ubuntu", "22.04", "x86_64") == "unsupported"
    assert m.platform_status("fedora", "40", "x86_64") == "unsupported"


def test_tested_section_only_contains_real_runs() -> None:
    m = load_matrix()
    for entry in m.tested:
        for key in ("platform", "frappe_branch", "frappe_version", "date", "result"):
            assert key in entry, f"tested entry missing {key}: {entry}"


def test_invalid_matrix_is_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "m.yaml"
    bad.write_text("schema_version: 99\n")
    with pytest.raises(SetupError):
        load_matrix(bad)
    typo = tmp_path / "t.yaml"
    typo.write_text(
        "schema_version: 1\nfrappe_repository: x\nbranches:\n  b:\n    major: 1\n    documented:\n      python: '=>3.10'\n"
    )
    with pytest.raises(SetupError):
        load_matrix(typo)


def test_catalog_has_only_https_urls_and_no_branches() -> None:
    for app in load_catalog():
        assert str(app["repository"]).startswith("https://github.com/frappe/")
        assert "branch" not in app


def test_version_check_statuses() -> None:
    assert version_check("X", Version("2.0"), ">=1").status == OK
    assert version_check("X", Version("0.5"), ">=1").status == FAIL
    assert version_check("X", None, ">=1").status == MISSING
    assert version_check("X", None, ">=1", optional=True).status == "warn"
