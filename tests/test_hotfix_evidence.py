import pytest
from conftest import message, write_json

from gtr.conversion import prepare
from gtr.core import analyze
from gtr.faults import ImportFault
from gtr.importer import ImportController
from gtr.journal import Endpoint, Journal, RecoveryRequired, State
from gtr.mock_api import MockAPI, offline_result
from gtr.reports import redact_verification
from gtr.verification import compare


@pytest.mark.parametrize("lost_reply", [False, True])
def test_partial_two_account_proof_persists_without_releasing_uncertain_start(tmp_path, lost_reply):
    project = analyze(
        write_json(tmp_path / "export", [message(i, f"test {i}") for i in range(1, 5)]),
        tmp_path / "project",
    )
    previous = prepare(project, 1, tmp_path / "previous", "source", last_record=3)
    current = prepare(project, 1, tmp_path / "current", "source")
    path = tmp_path / "operations.sqlite"
    peer = Endpoint.telegram(100, 101, "pm")
    journal = Journal(path, peer)
    api = MockAPI("lost_reply" if lost_reply else "success")
    controller = ImportController(api, "peer", journal=journal, mode="owner")
    checked = offline_result(controller.validate(previous))
    if lost_reply:
        with pytest.raises(ImportFault) as error:
            offline_result(controller.execute(checked, approved=True))
        assert error.value.code == "START_UNCERTAIN"
    else:
        offline_result(controller.execute(checked, approved=True))
    a, b, authors = api.observations(previous)
    b["messages"].pop()
    for snapshot in (a, b):
        size = len(snapshot["messages"])
        snapshot.update(
            schema=3,
            mode="telegram_observation",
            after_id=0,
            through_id=None,
            captured_at="2090-01-01T00:00:00+00:00",
            scope={
                "kind": "id_window",
                "after_id": 0,
                "through_id": None,
                "truncated": False,
                "scanned": size,
                "returned": size,
                "scan_limit": 20,
                "baseline_captured_at": "2020-01-01T00:00:00+00:00",
            },
        )
    report = compare(previous, a, b, authors, project=project)
    assert not report["accepted_evidence"]
    assert report["confirmed_source_ordinals"] == [1, 2]
    assert "confirmed_source_ordinals" not in redact_verification(report)
    assert journal.reconcile(report) == (None if lost_reply else checked.operation_id)
    journal = Journal(path, Endpoint.telegram(101, 100, "pm"))
    review = journal.review_bundle(current)
    assert (review.overlap, review.confirmed) == (3, 2)
    assert review.confirmed_records == ((1, 2),)
    if lost_reply:
        assert journal.operation(checked.operation_id)["state"] == State.START_UNCERTAIN
        assert review.unknown_records == ((3, 4),) and review.eligible_count == 0
    else:
        assert review.unknown_records == ((3, 3),) and review.eligible == ((4, 4),)
    replay = MockAPI()
    attempt = ImportController(replay, "peer", journal=journal, mode="owner")
    with pytest.raises(RecoveryRequired):
        offline_result(attempt.validate(current))
    assert not replay.calls
