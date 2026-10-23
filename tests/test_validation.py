from __future__ import annotations

import os
from pathlib import Path

import pytest

from frappe_setup import validation as v
from frappe_setup.errors import ValidationError


@pytest.mark.parametrize("name", ["frappe-bench", "bench_15", "B1", "a" * 64])
def test_valid_bench_names(name: str) -> None:
    assert v.validate_bench_name(name) == name


@pytest.mark.parametrize(
    "name", ["", " ", ".", "..", "../x", "a/b", "-bench", "_x", "bench name", "a" * 65, "x;rm -rf ~"]
)
def test_invalid_bench_names(name: str) -> None:
    with pytest.raises(ValidationError):
        v.validate_bench_name(name)


@pytest.mark.parametrize("name", ["dev.localhost", "site1.local", "erp.example.com", "a", "my-site.test"])
def test_valid_site_names(name: str) -> None:
    assert v.validate_site_name(name) == name


def test_site_name_is_lowercased() -> None:
    assert v.validate_site_name("Dev.Localhost") == "dev.localhost"


@pytest.mark.parametrize(
    "name", ["", "-a.local", "a-.local", "a..b", "a_b.local", "a b", "assets", "x/../y", "a" * 64 + ".l"]
)
def test_invalid_site_names(name: str) -> None:
    with pytest.raises(ValidationError):
        v.validate_site_name(name)


@pytest.mark.parametrize("ref", ["version-15", "develop", "main", "v15.80.0", "feature/x-y", "release_1.2"])
def test_valid_branches(ref: str) -> None:
    assert v.validate_branch(ref) == ref


@pytest.mark.parametrize(
    "ref",
    ["", "-x", "--upload-pack=evil", "a..b", "a b", "x~1", "x^", "a:b", "x.lock", "/x", "x/", "@", "a@{1}", "a\nb"],
)
def test_invalid_branches(ref: str) -> None:
    with pytest.raises(ValidationError):
        v.validate_branch(ref)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/frappe/erpnext",
        "https://github.com/frappe/erpnext.git",
        "https://gitlab.example.com/group/sub/app.git",
        "git@github.com:org/custom_app.git",
        "ssh://git@github.com/org/custom_app.git",
    ],
)
def test_valid_git_urls(url: str) -> None:
    assert v.validate_git_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "",
        "file:///etc/passwd",
        "/home/user/app",
        "../app",
        "git://github.com/org/app",
        "http://github.com/org/app",
        "ext::sh -c touch% /tmp/pwned",
        "--upload-pack=touch /tmp/x",
        "https://user:token@github.com/org/app.git",
        "https://ghp_abcdefghijklmnopqrstuvwxyz0123456789@github.com/org/app",
        "https://github.com/org",
        "https://github.com/org/app?x=1",
        "https://github.com/org/app\nmalicious",
    ],
)
def test_invalid_git_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        v.validate_git_url(url)


def test_repo_basename_and_normalize() -> None:
    assert v.repo_basename("https://github.com/frappe/erpnext.git") == "erpnext"
    assert v.repo_basename("git@github.com:org/my_app.git") == "my_app"
    assert v.normalize_url("https://GitHub.com/frappe/ERPNext.git/") == v.normalize_url(
        "https://github.com/frappe/erpnext"
    )
    assert v.normalize_url("git@github.com:frappe/erpnext.git") == "github.com/frappe/erpnext"


def test_expand_path_handles_tilde_and_dotdot(isolated_home: Path) -> None:
    assert v.expand_path("~/frappe-projects") == isolated_home / "frappe-projects"
    assert v.expand_path("~/a/../b") == isolated_home / "b"
    assert v.expand_path("~") == isolated_home


def test_expand_path_rejects_other_users_home() -> None:
    with pytest.raises(ValidationError):
        v.expand_path("~root/x")


def test_relative_path_is_resolved_against_base(tmp_path: Path) -> None:
    assert v.expand_path("x/y", base=tmp_path) == (tmp_path / "x" / "y").resolve()


@pytest.mark.parametrize("path", ["/", "/etc", "/usr/local/frappe", "/var/lib/x", "/proc/1", "/root"])
def test_parent_dir_rejects_system_locations(path: str) -> None:
    with pytest.raises(ValidationError):
        v.validate_parent_dir(path)


def test_parent_dir_accepts_home_subdirectory(isolated_home: Path) -> None:
    assert v.validate_parent_dir("~/frappe-projects") == isolated_home / "frappe-projects"


def test_parent_dir_rejects_file(isolated_home: Path) -> None:
    (isolated_home / "afile").write_text("x")
    with pytest.raises(ValidationError, match="not a directory"):
        v.validate_parent_dir("~/afile")


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_parent_dir_rejects_unwritable(isolated_home: Path) -> None:
    locked = isolated_home / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        with pytest.raises(ValidationError, match="not writable"):
            v.validate_parent_dir("~/locked/sub")
    finally:
        locked.chmod(0o700)


def test_bench_path_stays_inside_parent(isolated_home: Path) -> None:
    parent = isolated_home / "p"
    assert v.bench_path(parent, "frappe-bench") == parent / "frappe-bench"
    with pytest.raises(ValidationError):
        v.bench_path(parent, "../escape")


def test_bench_path_refuses_symlink(isolated_home: Path) -> None:
    parent = isolated_home / "p"
    parent.mkdir()
    (isolated_home / "elsewhere").mkdir()
    (parent / "link").symlink_to(isolated_home / "elsewhere")
    with pytest.raises(ValidationError):
        v.bench_path(parent, "link")


def test_passwords() -> None:
    assert v.validate_password("longenough") == "longenough"
    for bad in ("", "short", "with\nnewline12", "nul\x00inside1"):
        with pytest.raises(ValidationError):
            v.validate_password(bad)


def test_db_values() -> None:
    assert v.validate_db_user("root") == "root"
    assert v.validate_db_host("127.0.0.1") == "127.0.0.1"
    assert v.validate_port("3306") == 3306
    for bad_user in ("", "a b", "x;drop"):
        with pytest.raises(ValidationError):
            v.validate_db_user(bad_user)
    for bad_port in ("0", "70000", "abc"):
        with pytest.raises(ValidationError):
            v.validate_port(bad_port)
