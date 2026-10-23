"""App resolution scenarios: required apps, wrong branches, bad URLs — asked, not aborted."""

from __future__ import annotations

import pytest

from frappe_setup import app_setup
from frappe_setup.app_setup import resolve_apps
from frappe_setup.compatibility import load_catalog
from frappe_setup.config import AppSpec
from frappe_setup.errors import SetupError, UserAbort
from frappe_setup.prompts import Presets, ask_apps
from frappe_setup.remote import AppMetadata
from frappe_setup.ui import ScriptedUI

GH = "https://github.com"
# url -> {branch: (app name, required_apps entries as written in hooks.py, frappe constraint)}
REPOS: dict[str, dict[str, tuple[str, list[str], str | None]]] = {
    f"{GH}/frappe/erpnext": {"version-16": ("erpnext", [], None), "version-15": ("erpnext", [], None)},
    f"{GH}/frappe/education": {"version-16": ("education", ["erpnext"], None), "develop": ("education", [], None)},
    f"{GH}/frappe/hrms": {"version-16": ("hrms", ["erpnext"], None)},
    f"{GH}/frappe/payments": {"version-16": ("payments", [], None)},
    f"{GH}/frappe/lms": {"version-16": ("lms", ["payments"], None)},
    f"{GH}/frappe/helpdesk": {"main": ("helpdesk", ["telephony"], None)},
    f"{GH}/frappe/telephony": {"main": ("telephony", [], None)},
    f"{GH}/acme/uniquad": {
        "bsoft-admission": ("uniquad", ["education", "acme_core"], None),
        "old": ("uniquad", [], ">=15,<16"),
    },
    f"{GH}/acme/acme_core": {"main": ("acme_core", [], None)},
    f"{GH}/acme/portal": {"version-16": ("portal", ["acme/acme_core"], None)},
}
FRAPPE_VERSION = "16.53.0"


@pytest.fixture(autouse=True)
def fake_git(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def list_refs(_runner, url, *, phase="resolve"):
        calls.append(url)
        if url not in REPOS:
            raise SetupError(f"Cannot list branches of {url}", phase=phase, cause="Repository not found.")
        return {"branch": {b: f"sha-{b}" for b in REPOS[url]}, "tag": {}}

    def probe_app(_runner, url, ref, *, phase="resolve"):
        name, required, constraint = REPOS[url][ref.ref]
        meta = AppMetadata(url, ref.ref, ref.sha, name, required_apps=[r.rsplit("/", 1)[-1] for r in required])
        meta.required_app_sources = {r.rsplit("/", 1)[-1]: f"{GH}/{r}" for r in required if "/" in r}
        if constraint:
            meta.frappe_dependencies = {"frappe": constraint}
        return meta

    monkeypatch.setattr(app_setup, "list_refs", list_refs)
    monkeypatch.setattr(app_setup, "probe_app", probe_app)
    return calls


def interactive(answers: dict[str, object] | None = None) -> ScriptedUI:
    ui = ScriptedUI(answers or {})
    ui.interactive = True
    return ui


def catalog_spec(key: str, branch: str | None = None) -> AppSpec:
    return AppSpec(key=key, url=f"{GH}/frappe/{key}", branch=branch, source="catalog")


def resolve(ui, apps, *, existing=None, db_engine="mariadb", branch="version-16"):
    return resolve_apps(
        None,  # type: ignore[arg-type]  # git is faked
        ui,
        apps,
        frappe_branch=branch,
        frappe_version=FRAPPE_VERSION,
        db_engine=db_engine,
        catalog=load_catalog(),
        existing_apps=existing,
    )


def names(apps) -> list[str]:
    return [a.name for a in apps]


# ------------------------------------------------------------------ required apps
def test_required_official_app_is_offered_and_installed_first() -> None:
    ui = interactive({"Add ERPNext (erpnext)": True})
    apps = resolve(ui, [catalog_spec("education")])
    assert names(apps) == ["erpnext", "education"]
    assert apps[0].meta.ref == "version-16"  # same-named branch picked for the added app
    assert any("Add ERPNext (erpnext)" in q for q in ui.asked)


def test_required_apps_are_resolved_transitively_and_only_once() -> None:
    ui = interactive({"Add ": True})
    spec = AppSpec("uniquad", f"{GH}/acme/uniquad", branch="bsoft-admission")
    ui.answers["Git URL for acme_core"] = f"{GH}/acme/acme_core"
    apps = resolve(ui, [spec, catalog_spec("hrms")])
    assert names(apps) == ["erpnext", "education", "acme_core", "uniquad", "hrms"]
    assert sum("erpnext" in q for q in ui.asked if q.startswith("Add ")) == 1


def test_declining_a_required_app_skips_only_the_dependent_app() -> None:
    ui = interactive({"Add ERPNext": False, "How do you want to continue with education": "skip"})
    assert names(resolve(ui, [catalog_spec("education"), catalog_spec("payments")])) == ["payments"]


def test_declining_and_cancelling_stops_cleanly() -> None:
    ui = interactive({"Add ERPNext": False, "How do you want to continue with education": "cancel"})
    with pytest.raises(UserAbort):
        resolve(ui, [catalog_spec("education")])


def test_dependency_already_selected_or_in_the_bench_is_not_offered() -> None:
    ui = interactive()
    assert names(resolve(ui, [catalog_spec("education"), catalog_spec("erpnext")])) == ["erpnext", "education"]
    assert names(resolve(ui, [catalog_spec("education")], existing={"erpnext"})) == ["education"]
    assert not [q for q in ui.asked if q.startswith("Add ")]


def test_multiple_independent_requirements_in_one_run() -> None:
    ui = interactive({"Add ": True})
    apps = resolve(ui, [catalog_spec("lms"), catalog_spec("helpdesk", "main")])
    assert names(apps) == ["payments", "lms", "telephony", "helpdesk"]


def test_unknown_required_app_asks_for_its_url() -> None:
    ui = interactive({"Git URL for acme_core": f"{GH}/acme/acme_core", "Add ": True})
    apps = resolve(ui, [AppSpec("uniquad", f"{GH}/acme/uniquad", branch="bsoft-admission")])
    assert names(apps) == ["erpnext", "education", "acme_core", "uniquad"]


def test_required_app_named_as_org_repo_uses_that_repository() -> None:
    ui = interactive({"Add acme_core from https://github.com/acme/acme_core": True})
    assert names(resolve(ui, [AppSpec("portal", f"{GH}/acme/portal")])) == ["acme_core", "portal"]


def test_non_interactive_lists_every_missing_app_with_the_flag_to_add() -> None:
    with pytest.raises(SetupError) as err:
        resolve(ScriptedUI(), [catalog_spec("education"), catalog_spec("lms")])
    text = err.value.cause or ""
    assert "--app erpnext" in text and "--app payments" in text and "--yes" in text
    assert "Nothing was changed" in err.value.message


def test_non_interactive_yes_adds_known_required_apps() -> None:
    apps = resolve(ScriptedUI(assume_yes=True), [catalog_spec("education")])
    assert names(apps) == ["erpnext", "education"]


def test_non_interactive_yes_never_guesses_unknown_repositories() -> None:
    spec = AppSpec("uniquad", f"{GH}/acme/uniquad", branch="bsoft-admission")
    with pytest.raises(SetupError) as err:
        resolve(ScriptedUI(assume_yes=True), [spec])
    assert "--app-url <Git URL of acme_core>" in (err.value.cause or "")


# ------------------------------------------------------------------ branches, URLs, compatibility
def test_missing_explicit_branch_is_asked_again_interactively() -> None:
    ui = interactive({"Branch for education": "version-16", "Add ERPNext": True})
    apps = resolve(ui, [catalog_spec("education", "version-99")])
    assert apps[-1].meta.ref == "version-16"


def test_missing_explicit_branch_is_still_an_error_non_interactively() -> None:
    with pytest.raises(SetupError, match="does not exist"):
        resolve(ScriptedUI(), [catalog_spec("education", "version-99")])


def test_unreachable_repository_can_be_corrected() -> None:
    ui = interactive(
        {"How do you want to continue with crm-typo": "url", "Git URL for crm-typo": f"{GH}/frappe/payments"}
    )
    apps = resolve(ui, [AppSpec("crm-typo", f"{GH}/frappe/crm-typo")])
    assert names(apps) == ["payments"]


def test_unreachable_repository_can_be_skipped() -> None:
    ui = interactive({"How do you want to continue with nope": "skip"})
    assert resolve(ui, [AppSpec("nope", f"{GH}/acme/nope"), catalog_spec("payments")])[0].name == "payments"


def test_framework_mismatch_offers_another_branch() -> None:
    ui = interactive(
        {
            "How do you want to continue with uniquad": "branch",
            "Branch for uniquad": "bsoft-admission",
            "Add ": True,
            "Git URL for acme_core": f"{GH}/acme/acme_core",
        }
    )
    apps = resolve(ui, [AppSpec("uniquad", f"{GH}/acme/uniquad", branch="old")])
    assert apps[-1].meta.ref == "bsoft-admission"


def test_framework_mismatch_is_reported_non_interactively() -> None:
    with pytest.raises(SetupError, match="cannot be installed as requested"):
        resolve(ScriptedUI(), [AppSpec("uniquad", f"{GH}/acme/uniquad", branch="old")])


def test_mariadb_only_app_on_postgres_can_be_skipped() -> None:
    ui = interactive({"How do you want to continue with erpnext": "skip"})
    assert resolve(ui, [catalog_spec("erpnext"), catalog_spec("payments")], db_engine="postgres")[0].name == "payments"


def test_same_app_selected_twice_is_installed_once() -> None:
    ui = interactive()
    assert names(resolve(ui, [catalog_spec("payments"), catalog_spec("payments")])) == ["payments"]


# ------------------------------------------------------------------ wizard input
def test_pasting_a_url_at_the_custom_apps_question_is_accepted() -> None:
    ui = interactive(
        {
            "Applications": "official",
            "Select apps": ["education"],
            "Add custom apps by Git URL as well?": f"{GH}/acme/uniquad.git",
            "Branch or tag": "bsoft-admission",
        }
    )
    ui.answers["Git repository URL"] = ""
    apps = ask_apps(ui, Presets(), "version-16", "mariadb")
    assert [(a.key, a.branch) for a in apps] == [("education", None), ("uniquad", "bsoft-admission")]


# ------------------------------------------------------------------ messages
def test_known_problems_do_not_claim_an_unknown_cause() -> None:
    import io

    from rich.console import Console

    ui = ScriptedUI()
    buf = io.StringIO()
    ui.err = Console(file=buf, width=200)
    ui.show_error(SetupError("education requires erpnext", phase="resolve", remedy="Add erpnext"))
    assert "Unknown" not in buf.getvalue() and "Add erpnext" in buf.getvalue()


@pytest.mark.parametrize(
    "entry,expected",
    [
        ("erpnext", None),
        ("frappe/erpnext", "https://github.com/frappe/erpnext"),
        ("https://gitlab.com/acme/core.git", "https://gitlab.com/acme/core.git"),
        ("git@github.com:acme/core.git", "git@github.com:acme/core.git"),
    ],
)
def test_required_app_source(entry: str, expected: str | None) -> None:
    from frappe_setup.remote import required_app_source

    assert required_app_source(entry) == expected
