import json
from types import SimpleNamespace

import pytest
from conftest import message, write_json

from gtr.conversion import load_bundle, prepare
from gtr.core import analyze
from gtr.faults import ImportFault
from gtr.fixtures import prepare_fixture
from gtr.importer import ImportController, run_import
from gtr.integrity import check_local
from gtr.journal import Endpoint, Journal, Phase, State
from gtr.mock_api import MockAPI, offline_result
from gtr.model import RecoveryError
from gtr.mtproto import repeat_read


def test_disjoint_ranges_allow_distinct_equal_records_but_overlap_is_blocked(tmp_path):
    archive = write_json(tmp_path / "archive", [message(1, "same"), message(2, "same"), message(3, "again")])
    project = analyze(archive, tmp_path / "project")
    first = prepare(project, 1, tmp_path / "first", media_mode="text_only", last_record=1)
    second = prepare(project, 1, tmp_path / "second", media_mode="text_only", first_record=2, last_record=2)
    overlap = prepare(project, 1, tmp_path / "overlap", first_record=2)
    assert first.content_fingerprint() == second.content_fingerprint()
    assert first.fingerprint() != second.fingerprint()
    assert second.record_intervals() == ((2, 2),)
    assert second.data["selected_records"] == 1
    path = tmp_path / "operations.sqlite"
    destination = Endpoint.telegram(5, 6, "pm")
    journal = Journal(path, destination)
    offline_result(run_import(first, MockAPI(), "peer", True, lambda _: True, journal=journal))
    job = journal.operations()[0]
    assert job["phase"] == Phase.VERIFYING and job["accuracy"]["verification"] == "INCONCLUSIVE"
    assert job["selected_records"] == job["validated_records"] == job["package_records"] == 1
    phases = list(dict.fromkeys(event["phase"] for event in journal.events(job["id"])))
    assert phases == [
        Phase.CREATED,
        Phase.VALIDATED,
        Phase.PREFLIGHT,
        Phase.AUTHORIZED,
        Phase.INITIALIZED,
        Phase.UPLOADING,
        Phase.STARTING,
        Phase.VERIFYING,
    ]
    journal = Journal(path, Endpoint.telegram(6, 5, "pm"))
    offline_result(run_import(second, MockAPI(), "peer", True, lambda _: True, journal=journal))
    transport = MockAPI()
    with pytest.raises(RecoveryError, match="уже связаны"):
        offline_result(run_import(overlap, transport, "peer", True, lambda _: True, journal=journal))
    assert not transport.calls


def test_missing_old_selection_keeps_scope_guard(tmp_path, project):
    first = prepare(project, 1, tmp_path / "first", media_mode="text_only", last_record=1)
    second = prepare(project, 1, tmp_path / "second", media_mode="text_only", first_record=2, last_record=2)
    journal = Journal(tmp_path / "operations.sqlite", "peer")
    offline_result(run_import(first, MockAPI(), "peer", True, lambda _: True, journal=journal))
    with journal.connect(lock=True) as db:
        db.execute("UPDATE operations SET selection=NULL")
    with pytest.raises(RecoveryError, match="уже связаны"):
        offline_result(run_import(second, MockAPI(), "peer", True, lambda _: True, journal=journal))


def test_abandoned_start_is_durable_unknown_and_cannot_be_released(tmp_path, project, monkeypatch):
    bundle = prepare(project, 1, tmp_path / "bundle", media_mode="text_only")
    journal = Journal(tmp_path / "operations.sqlite", "peer")
    controller = ImportController(MockAPI(), "peer", journal=journal, mode="owner")
    ready = offline_result(controller.validate(bundle))
    job = ready.operation_id
    journal.transition(job, State.CONFIRMED, "confirmed", reserve=True)
    journal.transition(job, State.INITIALIZING, "init_import")
    journal.transition(job, State.INITIALIZING, "initialized", server_id=1)
    journal.transition(job, State.UPLOADING, "upload")
    journal.transition(job, State.STARTING, "start_import", outcome="uncertain", start_sent=None)
    monkeypatch.setattr("gtr.journal.worker_stopped", lambda _: True)
    journal.classify_interrupted()
    assert journal.operation(job)["phase"] == Phase.UNKNOWN_SERVER_STATE
    assert journal.events(job)[-1]["code"] == "PROCESS_INTERRUPTED"
    with pytest.raises(RecoveryError):
        journal.recover_before_start(job, acknowledged=True)
    with pytest.raises(RecoveryError, match="неопределённая"):
        journal.check("different", "other archive")
    controller.close()


def test_text_only_integrity_is_independent_from_media_files(tmp_path, project):
    selected = prepare(project, 1, tmp_path / "text", media_mode="text_only")
    (project / "source" / "files" / "a.jpg").unlink()
    result = check_local(load_bundle(selected.root), project)
    assert result["status"] == "verified" and result["file_scope"] == "history"
    assert result["project_files"] == {"ok": 1}
    (project / "source" / "result.json").write_text("{}", encoding="utf-8")
    assert check_local(load_bundle(selected.root), project)["status"] == "differences"


@pytest.mark.parametrize("size", [2, 10, 100])
def test_controlled_sizes_are_synthetic_and_ordered(tmp_path, size):
    bundle = load_bundle(prepare_fixture(tmp_path / "text", size=size).root)
    assert bundle.data["fixture"] == "text" and bundle.data["included"] == size
    assert bundle.data["media_count"] == 0 and bundle.record_intervals() == ((1, size),)
    assert {row["source_author_id"] for row in bundle.data["records"]} == {"fixture:1", "fixture:2"}
    dates = [row["import_date"] for row in bundle.data["records"]]
    assert dates == sorted(dates)
    assert len(set(dates)) == (size // 2 if size > 2 else size)


def test_read_backoff_is_bounded_and_respects_flood_wait():
    faults = ["TIMEOUT", "SERVER_FAILURE"]
    calls = []

    async def repeatable():
        if faults:
            raise ImportFault(faults.pop(0))
        return SimpleNamespace(ok=True)

    async def delay(value):
        calls.append(value)

    assert offline_result(repeat_read(repeatable, sleep=delay)).ok
    assert calls == [0.25, 0.5]
    faults.append("FLOOD_WAIT")
    with pytest.raises(ImportFault) as result:
        offline_result(repeat_read(repeatable, sleep=delay))
    assert result.value.code == "FLOOD_WAIT" and calls == [0.25, 0.5]


def test_public_diagnostic_omits_selected_ordinals_and_paths(tmp_path, project):
    bundle = prepare(project, 1, tmp_path / "text", media_mode="text_only", first_record=2)
    journal = Journal(tmp_path / "capture.sqlite", Endpoint.telegram(5, 6, "pm"))
    offline_result(run_import(bundle, MockAPI(), "peer", True, lambda _: True, journal=journal))
    job = journal.operations()[0]["id"]
    assert json.loads(journal.diagnostic_report(job, private=True)["operation"]["selection"])
    assert "selection" not in journal.diagnostic_report(job)["operation"]
