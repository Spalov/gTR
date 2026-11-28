from pathlib import Path

import pytest
from conftest import message, write_json
from test_beta6_ipc import service

from gtr.conversion import load_bundle
from gtr.core import analyze
from gtr.editor import check_snapshot, edit, page, review
from gtr.files import digest
from gtr.fixtures import _png
from gtr.package_errors import PackageValidationError


@pytest.fixture
def selection_project(tmp_path):
    source = write_json(tmp_path / "source", [
        message(101, "First", photo="photo.png", grouped_id=7),
        message(102, "Reply", reply_to_message_id=101, grouped_id=7),
        message(103, "Другие даты", date="2025-01-05T10:04:05+02:00"),
        message(104, "Following"),
    ])
    (source / "photo.png").write_bytes(_png())
    return source, analyze(source, tmp_path / "project")


def test_selection_binds_prepared_package_and_leaves_source_intact(selection_project):
    source, project = selection_project
    original = {path: digest(path) for path in (*source.iterdir(), project / "history.sqlite")}
    scope = {"project": str(project), "chat": 1, "preset": ""}
    with service() as (_, _, call, _):
        call("hello")
        lines = call("selection.page", {**scope, "filters": {"field": "file", "query": "Photo"}})
        assert [entry["source_id"] for entry in lines["records"]] == ["101"]
        call("selection.edit", {**scope, "revision": lines["revision"], "action": "exclude", "ranges": [[1, 1]]})
        preview = call("selection.preview", scope)
        assert preview["selected"] == 3 and preview["attachments"] == 0
        assert preview["broken_replies"] == preview["split_albums"] == 1
        assert preview["partial"] is True
        result = call("recovery.prepare", {**scope, "confirmed": True, "snapshot": preview["snapshot"]})
        bundle = load_bundle(Path(result["path"]))
        assert bundle.record_intervals() == ((2, 4),)
        assert not bundle.data["media"]
        assert call("verify.local", {"project": str(project), "bundle": str(bundle.root)})["status"] == "verified"
        check_snapshot(project, bundle.data)
        assert "First" not in bundle.transcript.read_text(encoding="utf-8")
        call("selection.edit", {**scope, "revision": preview["snapshot"]["revision"], "action": "undo"})
        with pytest.raises(PackageValidationError, match="выборка"):
            check_snapshot(project, bundle.data)
        assert call("recovery.prepare", {**scope, "confirmed": True, "snapshot": preview["snapshot"]})["code"] == "SELECTION_STALE"
    assert {path: digest(path) for path in original} == original


def test_presets_undo_redo_filter_and_private_mark(selection_project):
    _, project = selection_project
    edit(project, 1, "", "initial", "save_as", name="Январь")
    changed = page(project, 1, "Январь", {})["revision"]
    edit(project, 1, "Январь", changed, "exclude", filters={"start": "2025-01-05", "end": "2025-01-05"})
    ranges, stamp, info = review(project, 1, "Январь")
    assert ranges == ((1, 2), (4, 4)) and info["gaps"] == 1
    assert review(project, 1, "")[0] == ((1, 4),)
    edit(project, 1, "Январь", stamp["revision"], "mark", ranges=((4, 4),), note="Личное")
    assert review(project, 1, "Январь")[1] == stamp
    values = page(project, 1, "Январь", {"query": "104", "field": "id"})
    assert len(values["records"]) == 1 and values["records"][0]["note"] == "Личное"
    edit(project, 1, "Январь", stamp["revision"], "undo")
    assert review(project, 1, "Январь")[0] == ((1, 4),)
    undone = page(project, 1, "Январь", {})
    assert undone["redo"]
    edit(project, 1, "Январь", undone["revision"], "redo")
    excluded = page(project, 1, "Январь", {"view": "excluded"})["records"]
    assert [row["source_id"] for row in excluded] == ["103"]


def test_large_interval_does_not_expand_into_individual_edits(tmp_path):
    source = write_json(tmp_path / "source", [message(i, f"Test {i}") for i in range(1, 6001)])
    project = analyze(source, tmp_path / "project")
    edit(project, 1, "", "initial", "exclude", ranges=[(2, 5999)])
    ranges, stamp, info = review(project, 1, "")
    assert ranges == ((1, 1), (6000, 6000)) and info["selected"] == 2
    reopened = page(project, 1, "", {"view": "included"})
    assert reopened["revision"] == stamp["revision"]
    assert [row["source_id"] for row in reopened["records"]] == ["6000", "1"]
