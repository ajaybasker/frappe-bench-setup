from __future__ import annotations

import json
from pathlib import Path

import pytest

from frappe_setup.errors import SetupError
from frappe_setup.recovery import (
    DONE,
    FAILED,
    INTERRUPTED,
    RunState,
    StateStore,
    cleanup_candidates,
    delete_tree,
    quarantine,
)


def new_state(bench: Path) -> RunState:
    return RunState(bench_path=str(bench), config={"bench_name": bench.name, "site_name": "dev.localhost"})


def test_roundtrip_and_permissions(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "runs")
    state = new_state(tmp_path / "b")
    state.created.bench_dir = True
    path = store.save(state)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    loaded = store.load(tmp_path / "b")
    assert loaded and loaded.created.bench_dir and loaded.config["site_name"] == "dev.localhost"


def test_state_never_contains_secrets(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "runs")
    state = new_state(tmp_path / "b")
    state.config["admin_password"] = "x"
    with pytest.raises(SetupError, match="Refusing"):
        store.save(state)


def test_phase_tracking_and_last_failure(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "runs")
    state = new_state(tmp_path / "b")
    store.start(state, "preflight")
    store.finish(state, "preflight")
    store.start(state, "bench")
    store.finish(state, "bench", INTERRUPTED, "interrupted by user")
    loaded = store.load(tmp_path / "b")
    assert loaded.phase("preflight").status == DONE
    assert loaded.last_failure[0] == "bench"
    assert loaded.incomplete


def test_latest_incomplete(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "runs")
    a, b = new_state(tmp_path / "a"), new_state(tmp_path / "b")
    a.completed = True
    store.save(a)
    store.save(b)
    assert store.latest(incomplete_only=True).bench_path == str(tmp_path / "b")


def test_corrupt_state_is_reported_not_deleted(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "runs")
    path = store.path_for(tmp_path / "b")
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    with pytest.raises(SetupError, match="corrupt"):
        store.load(tmp_path / "b")
    assert path.exists()


def test_cleanup_only_for_resources_we_created(tmp_path: Path) -> None:
    bench = tmp_path / "bench"
    bench.mkdir()
    state = new_state(bench)
    assert cleanup_candidates(state) == []  # not created by us
    state.created.bench_dir = True
    assert [c.path for c in cleanup_candidates(state)] == [bench]
    state.phase("bench").status = DONE
    assert cleanup_candidates(state) == []  # a completed bench is never a cleanup candidate


def test_delete_tree_refuses_paths_not_created_by_us(tmp_path: Path) -> None:
    user_dir = tmp_path / "important"
    user_dir.mkdir()
    (user_dir / "file").write_text("keep me")
    with pytest.raises(SetupError, match="Refusing to delete"):
        delete_tree(user_dir, new_state(tmp_path / "bench"))
    assert (user_dir / "file").read_text() == "keep me"


def test_delete_tree_allowed_for_partial_bench(tmp_path: Path) -> None:
    bench = tmp_path / "bench"
    (bench / "apps").mkdir(parents=True)
    state = new_state(bench)
    state.created.bench_dir = True
    state.phase("bench").status = FAILED
    delete_tree(bench, state)
    assert not bench.exists()


def test_quarantine_renames(tmp_path: Path) -> None:
    bench = tmp_path / "bench"
    bench.mkdir()
    moved = quarantine(bench)
    assert not bench.exists() and moved.exists() and moved.name.startswith("bench.incomplete-")


def test_unknown_future_phases_survive_roundtrip(tmp_path: Path) -> None:
    state = new_state(tmp_path / "b")
    data = json.loads(state.to_json())
    data["phases"]["future_phase"] = {"status": "done", "started": None, "finished": None, "error": None}
    restored = RunState.from_json(json.dumps(data))
    assert restored.phases["future_phase"].status == "done"
