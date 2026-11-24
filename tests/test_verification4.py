import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import message, write_json
from telethon import types

from gtr.account import Account, AccountManager, Target
from gtr.conversion import prepare
from gtr.core import analyze
from gtr.importer import run_import
from gtr.mock_api import MockAPI, offline_result
from gtr.model import RecoveryError
from gtr.observation import CaptureWindow, capture, last_id
from gtr.reports import save_report
from gtr.verification import UNSUPPORTED_BY_API, compare, compare_files


def _mock(bundle, scenario="success"):
    api = MockAPI(scenario)
    offline_result(run_import(bundle, api, "mock", True, lambda _: True))
    return api.observations(bundle)


def test_history_and_native_observations_are_independent(bundle, project):
    first, second, labels = _mock(bundle, "one_sender")
    result = compare(bundle, first, second, labels, project=project)
    assert result["levels"]["B1"] == "verified"
    assert result["levels"]["B2"] == result["native"]["native_message_id"] == UNSUPPORTED_BY_API
    assert result["native"]["sender_direction_observation"] == "differences"
    assert result["levels"]["C"] == "differences"
    assert result["verification_status"] == "NOT_CONFIRMED"
    assert result["server_messages_observed"] == bundle.data["included"]
    assert result["client_messages_verified"] < result["server_messages_observed"]


def test_duplicate_text_uses_label_and_date_instead_of_native_id(tmp_path):
    p = analyze(
        write_json(
            tmp_path / "src",
            [
                message(1, "same repeated synthetic text"),
                message(
                    2,
                    "same repeated synthetic text",
                    **{
                        "from": "Другой автор",
                        "from_id": "user11",
                        "date": "2025-01-02T10:09:05+02:00",
                    },
                ),
            ],
        ),
        tmp_path / "project",
    )
    bundle = prepare(p, 1, tmp_path / "bundle")
    a, b, labels = _mock(bundle, "reordered")
    result = compare(bundle, a, b, labels, project=p)
    view = result["views"][0]
    assert view["metrics"]["label"] == {"same": 2}
    assert view["metrics"]["order"] == {"different": 2}
    assert [match["imported_message_id"] for match in view["matched"]] == [2, 1]


def test_indistinguishable_records_leave_observed_attribution_unknown(tmp_path):
    p = analyze(
        write_json(
            tmp_path / "source",
            [
                message(1, "duplicate"),
                message(2, "duplicate"),
                message(3, "peer", **{"from": "Другой автор", "from_id": "user11"}),
            ],
        ),
        tmp_path / "project",
    )
    bundle = prepare(p, 1, tmp_path / "bundle")
    a, b, labels = _mock(bundle)
    result = compare(bundle, a, b, labels, project=p)
    assert result["levels"]["B1"] == "unknown" and result["state"] == "inconclusive"
    assert not result["accepted_evidence"] and result["server_messages_observed"] == 1
    for view in result["views"]:
        assert view["ambiguous"] == 2 and view["metrics"]["label"] == {"unknown": 2, "same": 1}
        assert [match["source_id"] for match in view["matched"]] == ["3"]


def test_missing_window_and_single_account_are_inconclusive(bundle, project, tmp_path):
    first, second, labels = _mock(bundle)
    snapshot = tmp_path / "one.json"
    snapshot.write_text(json.dumps(first), encoding="utf-8")
    one = compare_files(bundle, [snapshot], project=project)
    assert one["state"] == "inconclusive" and one["server_messages_observed"] == 0
    for item in (first, second):
        item["messages"] = []
    empty = compare(bundle, first, second, labels, project=project)
    assert empty["levels"]["B1"] == "unknown" and empty["state"] == "inconclusive"
    assert empty["evidence_status"] == "NO_EVIDENCE_IN_WINDOW" and not empty["accepted_evidence"]


def test_uncertain_start_evidence_requires_paired_baselines_and_complete_readback(bundle, project):
    a, b, labels = _mock(bundle)
    for item in (a, b):
        item.update(
            schema=3,
            mode="telegram_observation",
            after_id=0,
            through_id=None,
            captured_at="2026-01-01T00:02:00+00:00",
        )
        item["scope"] = {
            "kind": "id_window",
            "after_id": 0,
            "through_id": None,
            "truncated": False,
            "scanned": len(item["messages"]),
            "returned": len(item["messages"]),
            "scan_limit": 200,
            "baseline_captured_at": None,
        }
    assert not compare(bundle, a, b, labels, project=project)["accepted_evidence"]
    for item in (a, b):
        item["scope"]["baseline_captured_at"] = "2026-01-01T00:00:00+00:00"
    assert compare(bundle, a, b, labels, project=project)["accepted_evidence"]
    b["scope"]["truncated"] = True
    result = compare(bundle, a, b, labels, project=project)
    assert result["state"] == "inconclusive" and not result["accepted_evidence"]


def test_exported_verification_report_redacts_private_identifiers(bundle, project, tmp_path):
    a, b, labels = _mock(bundle, "one_sender")
    report = compare(bundle, a, b, labels, project=project)
    safe = save_report(tmp_path / "shared.json", verification=report)
    data = json.loads(safe.read_text(encoding="utf-8"))
    assert data["privacy"] == "redacted" and "manifest_sha256" not in data
    assert all("matched" not in v and "account_id" not in v for v in data["views"])
    assert all("server_id" not in item for v in data["views"] for item in v["differences"])
    private = save_report(tmp_path / "owner.json", verification=report, private_details=True)
    assert json.loads(private.read_text(encoding="utf-8"))["views"][0]["account_id"] == 100


def test_qt_verification_can_render_one_and_two_snapshots(bundle, project, tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings

    from gtr import gui, telegram_ui

    app = gui.application()
    monkeypatch.setattr(
        gui, "QSettings", lambda *_: QSettings(str(tmp_path / "gtr.ini"), QSettings.Format.IniFormat)
    )
    monkeypatch.setattr(gui, "app_home", lambda: tmp_path)
    monkeypatch.setattr(telegram_ui, "vault_path", lambda: tmp_path / "owner.dpapi")
    window = gui.Window(project)
    try:
        a, b, labels = _mock(bundle)
        solo = compare(bundle, a, project=project)
        assert solo["verification_status"] == "INCONCLUSIVE"
        assert "Не хватает данных" in window.show_comparison(solo)
        assert (
            "B2" in window.levels.text()
            and "вне возможностей API" in window.overview.quality["Ответы"].text()
        )
        full = compare(bundle, a, b, labels, project=project)
        assert full["verification_status"] == "CONFIRMED"
        assert "Подтверждены" in window.show_comparison(full)
        assert window.evidence_table.rowCount() > 10
    finally:
        window.close()
        app.processEvents()


class ReadClient:
    def __init__(self, rows):
        self.rows = rows
        self.requests = []

    async def get_messages(self, peer, limit):
        assert limit == 1
        return [SimpleNamespace(id=50)]

    async def iter_messages(self, peer, **options):
        self.requests.append(options)
        for row in self.rows:
            yield row

    async def iter_download(self, media, **options):
        self.requests.append(options)
        yield b"synthetic"


def _owner(rows):
    manager = AccountManager(timed=False)
    manager.account, manager.client = Account(100, "Local fixture"), ReadClient(rows)
    return manager, Target(101, "Local test dialog", "pm", types.InputPeerUser(101, 0))


def _row(index, body, *, imported=True, day=2, media=None):
    return SimpleNamespace(
        id=index,
        message=body,
        out=False,
        from_id=types.PeerUser(101),
        sender_id=101,
        date=datetime(2026, 1, 1, tzinfo=UTC),
        fwd_from=types.MessageFwdHeader(
            datetime(2025, 1, day, 8, 4, 5, tzinfo=UTC), imported=imported, from_name="Лея"
        ),
        media=media,
        file=SimpleNamespace(size=len(b"synthetic")) if media else None,
    )


def test_baseline_is_bound_to_account_peer_and_manifest(bundle, tmp_path):
    manager, target = _owner([_row(51, "fixture")])
    path = tmp_path / "baseline.json"
    saved = offline_result(last_id(manager, target, bundle=bundle, path=path))
    assert saved["before_import"] and saved["manifest_sha256"] == bundle.manifest_hash()
    assert saved["captured_at"] and saved["last_id"] == 50
    window = CaptureWindow(tmp_path / "snapshot.json", 0, None, baseline=path)
    output = offline_result(capture(manager, target, bundle, window))
    assert (
        output["scope"]["after_id"] == 50 and output["scope"]["baseline_captured_at"] == saved["captured_at"]
    )
    assert manager.client.requests[0]["min_id"] == 50
    manager.account = Account(101, "Other account")
    window = CaptureWindow(tmp_path / "invalid.json", 0, None, baseline=path)
    with pytest.raises(RecoveryError, match="граница не соответствует"):
        offline_result(capture(manager, target, bundle, window))
    assert not window.path.exists() and len(manager.client.requests) == 1


def test_bounded_historical_scan_uses_import_header_and_scope(bundle, project, tmp_path):
    body = "До встречи 👋\nв 12:00."
    manager, target = _owner(
        [
            _row(53, body, imported=False),
            _row(52, body),
            _row(51, body, day=1),
        ]
    )
    window = CaptureWindow(
        tmp_path / "historical.json",
        0,
        None,
        historical_from="2025-01-02",
        historical_through="2025-01-02",
        max_scan=2,
    )
    result = offline_result(capture(manager, target, bundle, window))
    assert result["scope"]["kind"] == "historical_date" and result["scope"]["truncated"]
    assert result["scope"]["scanned"] == 2 and manager.client.requests[0]["limit"] == 3
    view = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
    assert [row["id"] for row in view["messages"]] == [52]
    assert body not in Path(result["path"]).read_text(encoding="utf-8")
    reversed_view = deepcopy(view)
    reversed_view["account_id"], reversed_view["peer_id"] = 101, 100
    compared = compare(bundle, view, reversed_view, project=project)
    assert compared["levels"]["B1"] == "unknown" and not compared["accepted_evidence"]


def test_opt_in_readback_distinguishes_media_bytes_and_budget(bundle, tmp_path):
    manager, target = _owner(
        [
            _row(52, "fixture", media=types.MessageMediaPhoto()),
            _row(51, "fixture", media=types.MessageMediaPhoto()),
        ]
    )
    window = CaptureWindow(tmp_path / "full.json", 50, 52, media_hash_limit=len(b"synthetic"))
    result = offline_result(capture(manager, target, bundle, window))
    view = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
    skipped, full = view["messages"]
    assert (
        full["media_probe_scope"] == "full"
        and full["media_sha256"] == hashlib.sha256(b"synthetic").hexdigest()
    )
    assert full["media_accessible"] is True and skipped["media_error"] == "MEDIA_HASH_BUDGET"
    assert skipped["media_accessible"] is None and skipped["media_sha256"] is None
