import json
import sqlite3

import pytest
from conftest import message, write_json

from gtr.conversion import load_bundle, prepare, prepare_eligible
from gtr.core import analyze, encode
from gtr.faults import ImportFault
from gtr.fixtures import _png
from gtr.importer import ImportController
from gtr.integrity import check_local
from gtr.journal import Endpoint, Journal, Phase, RecoveryRequired, State
from gtr.mock_api import MockAPI, offline_result
from gtr.model import RecoveryError


@pytest.fixture
def recovery_project(tmp_path):
    records = [
        message(i, f"Historical message {i}", **({"photo": "sample.png"} if i in (3, 6) else {}))
        for i in range(1, 13)
    ]
    source = write_json(tmp_path / "source", records)
    (source / "sample.png").write_bytes(_png())
    return analyze(source, tmp_path / "project")


def _controller(journal, scenario="success"):
    api = MockAPI(scenario)
    return ImportController(api, "target", journal=journal, mode="owner"), api


def _accept(journal, bundle):
    controller, api = _controller(journal)
    ticket = offline_result(controller.validate(bundle))
    result = offline_result(controller.execute(ticket, approved=True))
    assert result["state"] == "accepted" and api.calls.count("start_import") == 1
    return ticket.operation_id


def _legacy_database(path, endpoint, bundle):
    # The pre-Beta columns represent a successful final dispatch without a recorded selection.
    with sqlite3.connect(path) as old:
        old.executescript("""
            CREATE TABLE operations(
              id TEXT PRIMARY KEY,created TEXT,peer TEXT,account INTEGER,mode TEXT,fingerprint TEXT,scope TEXT,
              manifest_sha256 TEXT,state TEXT,stage TEXT,outcome TEXT,server_id INTEGER,retry_after TEXT,
              messages INTEGER,media INTEGER,skipped INTEGER,duplicates INTEGER,media_done INTEGER,error_code TEXT);
            CREATE TABLE operation_events(seq INTEGER PRIMARY KEY,operation TEXT,ts TEXT,state TEXT,stage TEXT,
              done INTEGER,total INTEGER,code TEXT);
            INSERT INTO operation_events VALUES (1,'prior','2025-01-01T00:00:00+00:00','STARTING','start_import',0,0,'');
            INSERT INTO operation_events VALUES (2,'prior','2025-01-01T00:00:01+00:00','VERIFYING','verifying',0,0,'');
        """)
        old.execute(
            """INSERT INTO operations VALUES ('prior','2025-01-01T00:00:00+00:00',?,?,'owner',?,?,?,
               'VERIFYING','verifying','accepted',25,'',?,0,?,0,0,'')""",
            (
                endpoint.key,
                endpoint.account_id,
                bundle.fingerprint(),
                bundle.scope(),
                bundle.manifest_hash(),
                bundle.data["included"],
                bundle.data["skipped"],
            ),
        )


def test_migration_is_atomic_and_exact_legacy_package_restores_only_its_selection(
    tmp_path, recovery_project, monkeypatch
):
    prior = prepare(recovery_project, 1, tmp_path / "legacy", media_mode="text_only", sample=4)
    for field in ("selection", "selected_records"):
        prior.data.pop(field)
    (prior.root / "manifest.json").write_text(encode(prior.data) + "\n", encoding="utf-8")
    prior = load_bundle(prior.root)
    newer = prepare(recovery_project, 1, tmp_path / "newer", first_record=3, last_record=9)
    path = tmp_path / "journal.sqlite"
    endpoint = Endpoint.telegram(100, 101, "pm")
    _legacy_database(path, endpoint, prior)

    def dump():
        with sqlite3.connect(path) as db:
            return list(db.iterdump())

    original = dump()

    def migration_fault(*_):
        raise sqlite3.OperationalError("Synthetic failure during migration")

    with monkeypatch.context() as patch:
        patch.setattr(Journal, "_backfill_phases", staticmethod(migration_fault))
        with pytest.raises(sqlite3.OperationalError, match="Synthetic failure"):
            Journal(path, endpoint)
        assert dump() == original

    journal = Journal(path, endpoint)
    earlier = journal.operation("prior")
    events = journal.events("prior")
    assert earlier["phase"] == Phase.VERIFYING and earlier["start_request_accepted"]
    assert earlier["selection"] is None and earlier["server_messages_observed"] is None
    assert [e["state"] for e in events] == [State.STARTING, State.VERIFYING]
    review = journal.review_bundle(newer)
    assert (review.overlap, review.eligible_count, review.unknown) == (None, 0, 7)
    assert review.conflicts[0].can_attach
    with pytest.raises(RecoveryError, match="не совпадает"):
        journal.attach_selection("prior", newer)

    journal.attach_selection("prior", prior)
    journal = Journal(path, endpoint)
    review = journal.review_bundle(newer)
    assert (review.overlap, review.confirmed, review.eligible_count, review.unknown) == (2, 0, 5, 2)
    assert review.unknown_records == ((3, 4),) and review.eligible == ((5, 9),)
    assert journal.operation("prior") == earlier and journal.events("prior") == events


def test_completed_overlap_rebuilds_noncontiguous_package_with_original_media_and_dates(
    tmp_path, recovery_project
):
    first = prepare(
        recovery_project,
        1,
        tmp_path / "previous",
        media_mode="text_only",
        record_ranges=((3, 4), (7, 7), (11, 11)),
    )
    current = prepare(recovery_project, 1, tmp_path / "current", first_record=2, last_record=10)
    destination = Endpoint.telegram(100, 101, "pm")
    journal = Journal(tmp_path / "journal.sqlite", destination)
    completed = _accept(journal, first)
    journal.transition(completed, State.COMPLETED, "verification")

    controller, api = _controller(journal)
    with pytest.raises(RecoveryRequired) as result:
        offline_result(controller.validate(current))
    review = result.value.review
    assert (review.selected, review.overlap, review.confirmed, review.unknown) == (9, 3, 3, 0)
    assert review.confirmed_records == ((3, 4), (7, 7))
    assert review.eligible == ((2, 2), (5, 6), (8, 10))
    assert review.eligible_count == 6 and api.calls == []
    eligible = prepare_eligible(recovery_project, current, tmp_path / "only-new", review.eligible)
    eligible = load_bundle(eligible.root)
    assert eligible.record_intervals() == review.eligible
    assert eligible.data["included"] == 6 and len(eligible.data["media"]) == 1
    assert eligible.media_path(eligible.data["media"][0]).read_bytes() == _png()
    dates = {r["source_ordinal"]: r["original_date"] for r in current.data["records"] if r["included"]}
    assert all(
        r["original_date"] == dates[r["source_ordinal"]] for r in eligible.data["records"] if r["included"]
    )
    assert check_local(eligible, recovery_project)["status"] == "verified"
    assert json.loads((eligible.root / "manifest.json").read_text(encoding="utf-8"))["selection"][
        "ranges"
    ] == [
        [2, 2],
        [5, 6],
        [8, 10],
    ]
    assert not journal.review_bundle(eligible).conflicts


def test_reserve_rechecks_stale_preflight_for_both_accounts_in_same_dialog(tmp_path, recovery_project):
    package = prepare(recovery_project, 1, tmp_path / "bundle", media_mode="text_only", last_record=4)
    path = tmp_path / "journal.sqlite"
    forward = Journal(path, Endpoint.telegram(100, 101, "pm"))
    reverse = Journal(path, Endpoint.telegram(101, 100, "pm"))
    first, first_api = _controller(forward)
    second, second_api = _controller(reverse)
    ready_first = offline_result(first.validate(package))
    ready_second = offline_result(second.validate(package))

    offline_result(first.execute(ready_first, approved=True))
    with pytest.raises(RecoveryRequired) as blocked:
        offline_result(second.execute(ready_second, approved=True))
    assert blocked.value.review.overlap == 4 and blocked.value.review.unknown == 4
    assert first_api.calls.count("start_import") == 1
    assert second_api.calls == ["check_history", "check_peer"]
    assert reverse.operation(ready_second.operation_id)["phase"] == Phase.FAILED
    assert forward.operation(ready_first.operation_id)["account"] == 100
    assert reverse.operation(ready_second.operation_id)["account"] == 101

    unrelated = Journal(path, Endpoint.telegram(101, 102, "pm"))
    assert not unrelated.review_bundle(package).conflicts
    _accept(unrelated, package)


def test_no_initialization_after_failed_preflight_and_retry_stays_eligible(tmp_path, recovery_project):
    bundle = prepare(recovery_project, 1, tmp_path / "bundle", media_mode="text_only")
    path = tmp_path / "journal.sqlite"
    endpoint = Endpoint.telegram(100, 101, "pm")
    journal = Journal(path, endpoint)
    failed, api = _controller(journal, "peer")
    with pytest.raises(ImportFault) as error:
        offline_result(failed.validate(bundle))
    assert error.value.code == "USER_NOT_MUTUAL_CONTACT"
    assert api.calls == ["check_history", "check_peer"] and not api.initialized
    failed_state = journal.operation(failed.job)
    assert failed_state["phase"] == Phase.FAILED and not failed_state["start_request_sent"]

    restarted = Journal(path, endpoint)
    assert not restarted.review_bundle(bundle).conflicts
    _accept(restarted, bundle)
    assert restarted.operation(failed.job) == failed_state


def test_explicit_recovery_after_initialization_never_contacts_telegram(tmp_path, recovery_project):
    bundle = prepare(recovery_project, 1, tmp_path / "bundle")
    path = tmp_path / "journal.sqlite"
    endpoint = Endpoint.telegram(100, 101, "pm")
    journal = Journal(path, endpoint)
    pending, api = _controller(journal, "media")
    ready = offline_result(pending.validate(bundle))
    with pytest.raises(ImportFault, match="вложен"):
        offline_result(pending.execute(ready, approved=True))
    assert api.initialized and not api.started
    assert journal.operation(ready.operation_id)["state"] == State.STAGED_NOT_STARTED

    before = api.calls[:]
    restarted = Journal(path, endpoint)
    review = restarted.review_bundle(bundle)
    assert not review.conflicts[0].can_recover and review.eligible_count == 0
    with pytest.raises(RecoveryError, match="ещё работает"):
        restarted.recover_before_start(ready.operation_id, acknowledged=True)
    journal.owner_disconnected(ready.operation_id)
    assert restarted.review_bundle(bundle).conflicts[0].can_recover
    restarted.recover_before_start(ready.operation_id, acknowledged=True)
    assert api.calls == before
    assert restarted.operation(ready.operation_id)["outcome"] == "staged_closed"
    assert not restarted.review_bundle(bundle).conflicts
    _accept(restarted, bundle)


def test_uncertain_final_request_keeps_destination_locked_for_disjoint_records(tmp_path, recovery_project):
    first = prepare(recovery_project, 1, tmp_path / "first", media_mode="text_only", last_record=2)
    disjoint = prepare(
        recovery_project, 1, tmp_path / "disjoint", media_mode="text_only", first_record=3, last_record=4
    )
    path = tmp_path / "journal.sqlite"
    endpoint = Endpoint.telegram(100, 101, "pm")
    journal = Journal(path, endpoint)
    controller, api = _controller(journal, "lost_reply")
    ready = offline_result(controller.validate(first))
    with pytest.raises(ImportFault) as error:
        offline_result(controller.execute(ready, approved=True))
    assert error.value.code == "START_UNCERTAIN" and api.calls.count("start_import") == 1
    journal.owner_disconnected(ready.operation_id)

    reopened = Journal(path, endpoint)
    reopened.classify_interrupted()
    review = reopened.review_bundle(disjoint)
    assert (review.overlap, review.confirmed, review.eligible_count, review.unknown) == (0, 0, 0, 2)
    assert review.conflicts[0].phase == Phase.UNKNOWN_SERVER_STATE and not review.conflicts[0].can_recover
    with pytest.raises(RecoveryError):
        reopened.recover_before_start(ready.operation_id, acknowledged=True)
    next_attempt, next_api = _controller(reopened)
    with pytest.raises(RecoveryRequired):
        offline_result(next_attempt.validate(disjoint))
    assert next_api.calls == []
    assert reopened.operation(ready.operation_id)["start_request_sent"] is None
