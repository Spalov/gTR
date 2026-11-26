import json
import sqlite3
from types import SimpleNamespace

import pytest
from telethon import errors, types

from gtr.faults import ImportFault
from gtr.importer import ImportController
from gtr.journal import Endpoint, Journal, State
from gtr.mock_api import MockAPI, offline_result
from gtr.model import RecoveryError
from gtr.mtproto import TelegramAPI


class FakeTelegram:
    def __init__(self, failed_index=0, rejection="MEDIA_INVALID"):
        self.failed_index, self.rejection = failed_index, rejection
        self.requests, self.files = [], []
        self.media = 0

    async def upload_file(self, path, file_name):
        self.files.append(file_name)
        if len(self.files) == self.failed_index + 1 and self.rejection == "PARTS_TIMEOUT":
            raise TimeoutError
        return types.InputFile(10, 1, file_name, "")

    async def __call__(self, request):
        kind = type(request).__name__
        self.requests.append(kind)
        match kind:
            case "CheckHistoryImportRequest":
                return SimpleNamespace(pm=True, group=False)
            case "CheckHistoryImportPeerRequest":
                return SimpleNamespace(confirm_text="Тестовое подтверждение")
            case "InitHistoryImportRequest":
                return SimpleNamespace(id=2)
            case "UploadImportedMediaRequest":
                self.media += 1
                if self.media == self.failed_index:
                    match self.rejection:
                        case "MEDIA_INVALID":
                            raise errors.MediaInvalidError(request)
                        case "EMPTY":
                            return types.MessageMediaEmpty()
                        case "UNSUPPORTED":
                            return types.MessageMediaUnsupported()
                        case "TIMEOUT":
                            raise TimeoutError
                if isinstance(request.media, types.InputMediaUploadedPhoto):
                    return types.MessageMediaPhoto(photo=types.Photo(51, 0, b"", None, [], 0))
                return types.MessageMediaDocument(
                    document=types.Document(57, 0, b"", None, request.media.mime_type, 1, 0, request.media.attributes)
                )
            case "StartHistoryImportRequest":
                return True
            case _:
                raise AssertionError(kind)


@pytest.mark.parametrize(
    ("index", "rejection", "stage", "status", "constructor"),
    [
        (1, "MEDIA_INVALID", "upload_imported_media", "MEDIA_REJECTED", None),
        (3, "MEDIA_INVALID", "upload_imported_media", "MEDIA_REJECTED", None),
        (8, "EMPTY", "upload_imported_media", "MEDIA_REJECTED", "MessageMediaEmpty"),
        (3, "UNSUPPORTED", "upload_imported_media", "MEDIA_REJECTED", "MessageMediaUnsupported"),
        (3, "TIMEOUT", "upload_imported_media", "MEDIA_OUTCOME_UNKNOWN", None),
        (3, "PARTS_TIMEOUT", "upload_file_parts", "MEDIA_REJECTED", None),
    ],
)
def test_media_failure_is_indexed_before_start(
    bundle, tmp_path, index, rejection, stage, status, constructor
):
    path = tmp_path / "operations.sqlite"
    endpoint = Endpoint.telegram(900001, 900002, "pm")
    journal = Journal(path, endpoint)
    client = FakeTelegram(index, rejection)
    controller = ImportController(
        TelegramAPI(client, "pm"), types.InputPeerSelf(), mode="owner", journal=journal
    )
    check = offline_result(controller.validate(bundle))
    assert client.files == [] and journal.media_attempts(check.operation_id) == []
    with pytest.raises(ImportFault) as error:
        offline_result(controller.execute(check, approved=True))
    assert "StartHistoryImportRequest" not in client.requests
    assert error.value.media_attempt["index"] == index
    renewed = Journal(path, endpoint)
    job = renewed.operation(check.operation_id)
    assert job["state"] == State.STAGED_NOT_STARTED and job["transcript_upload_done"] is True
    assert job["media_uploaded_successfully"] == index - 1 and job["media_total"] == 8
    assert job["start_request_sent"] is False and job["start_request_accepted"] is False
    assert job["server_messages_observed"] is None and job["client_messages_verified"] is None
    attempts = renewed.media_attempts(check.operation_id)
    assert [a["status"] for a in attempts] == ["MEDIA_ACCEPTED"] * (index - 1) + [status]
    assert attempts[-1]["stage"] == stage and attempts[-1]["response_constructor"] == constructor
    assert attempts[-1]["name"] == bundle.data["media"][index - 1]["name"]
    assert attempts[-1]["source_ordinal"] is not None
    assert attempts[-1]["input_media"] == (
        "InputMediaUploadedPhoto" if index == 1 else "InputMediaUploadedDocument"
    )
    assert all(a["started_at"] and a["ended_at"] for a in attempts)
    if index > 1:
        assert attempts[0]["response_constructor"] == "MessageMediaPhoto"
    report = renewed.diagnostic_report(check.operation_id)
    shared = json.dumps(report)
    assert "900001" not in shared and "900002" not in shared and str(bundle.root) not in shared
    assert "source_id" not in report["media_attempts"][-1] and "sha256" not in report["media_attempts"][-1]
    private = renewed.diagnostic_report(check.operation_id, private=True)
    assert private["media_attempts"][-1]["sha256"] == bundle.data["media"][index - 1]["sha256"]
    assert private["media_attempts"][-1]["source_id"] is not None
    assert private["media_attempts"][-1]["archive_paths"] == [bundle.data["media"][index - 1]["source"]]
    assert "archive_paths" not in report["media_attempts"][-1]


def test_explicit_closed_session_release_cannot_repeat_start(bundle, tmp_path):
    journal = Journal(tmp_path / "operations.sqlite", "private")
    controller = ImportController(MockAPI("media"), "peer", mode="owner", journal=journal)
    check = offline_result(controller.validate(bundle))
    with pytest.raises(ImportFault):
        offline_result(controller.execute(check, approved=True))
    with pytest.raises(RecoveryError):
        journal.recover_before_start(check.operation_id)
    with pytest.raises(RecoveryError, match="ещё работает"):
        journal.recover_before_start(check.operation_id, acknowledged=True)
    journal.owner_disconnected(check.operation_id)
    journal.recover_before_start(check.operation_id, acknowledged=True)
    row = journal.operation(check.operation_id)
    assert row["state"] == State.STAGED_NOT_STARTED and row["outcome"] == "staged_closed"
    journal.check("other", "other")


def test_disk_full_after_final_dispatch_keeps_durable_lock(bundle, tmp_path, monkeypatch):
    path = tmp_path / "operations.sqlite"
    journal = Journal(path, "peer")
    controller = ImportController(MockAPI(), "peer", mode="owner", journal=journal)
    check = offline_result(controller.validate(bundle))
    transition = journal.transition

    def full(job, state, stage, **kwargs):
        if state in {State.VERIFYING, State.START_UNCERTAIN}:
            raise sqlite3.OperationalError("disk full")
        transition(job, state, stage, **kwargs)

    monkeypatch.setattr(journal, "transition", full)
    with pytest.raises(ImportFault) as error:
        offline_result(controller.execute(check, approved=True))
    assert error.value.code == "START_UNCERTAIN"
    renewed = Journal(path, "peer")
    job = renewed.operation(check.operation_id)
    assert job["state"] == State.STARTING and job["start_request_sent"] is None
    with pytest.raises(RecoveryError):
        renewed.recover_before_start(check.operation_id, acknowledged=True)
    with pytest.raises(RecoveryError):
        renewed.check("other", "other")


def test_legacy_starts_detects_new_manifest_fingerprint(bundle, tmp_path):
    path = tmp_path / "operations.sqlite"
    with sqlite3.connect(path) as old:
        old.execute("CREATE TABLE starts(peer TEXT,fingerprint TEXT)")
        old.execute("INSERT INTO starts VALUES (?,?)", ("pm:11/12", bundle.content_fingerprint()))
    journal = Journal(path, "pm:11/12")
    api = MockAPI()
    with pytest.raises(RecoveryError, match="предыдущей попытке"):
        offline_result(ImportController(api, "peer", journal=journal).validate(bundle))
    assert not api.calls


def test_legacy_operation_migration_preserves_ack_and_accepted_media(tmp_path):
    path = tmp_path / "operations.sqlite"
    with sqlite3.connect(path) as old:
        old.executescript("""CREATE TABLE operations(
            id TEXT PRIMARY KEY,created TEXT,peer TEXT,account INTEGER,mode TEXT,fingerprint TEXT,scope TEXT,
            manifest_sha256 TEXT,state TEXT,stage TEXT,outcome TEXT,server_id INTEGER,retry_after TEXT,
            messages INTEGER,media INTEGER,skipped INTEGER,duplicates INTEGER,media_done INTEGER,error_code TEXT);
            CREATE TABLE operation_events(seq INTEGER PRIMARY KEY,operation TEXT,ts TEXT,state TEXT,stage TEXT,
                done INTEGER,total INTEGER,code TEXT);
            INSERT INTO operations VALUES ('staged','2025-01-01T00:00:00+00:00','target',101,'owner','hash-a',
                'scope-a','manifest-a','PARTIAL','upload','rejected',24,'',7,3,0,0,0,'MEDIA_INVALID');
            INSERT INTO operations VALUES ('approved','2025-01-01T00:00:00+00:00','target',101,'owner','hash-b',
                'scope-b','manifest-b','VERIFYING','verifying','accepted',25,'',4,0,0,0,0,'');
            INSERT INTO operation_events VALUES (1,'staged','2025-01-01T00:00:00+00:00','UPLOADING','upload',2,3,'');
            INSERT INTO operation_events VALUES (2,'staged','2025-01-01T00:00:00+00:00','PARTIAL','upload',0,0,'MEDIA_INVALID');
            INSERT INTO operation_events VALUES (3,'approved','2025-01-01T00:00:00+00:00','VERIFYING','verifying',0,0,'');
        """)
    journal = Journal(path, "target")
    staged, approved = [journal.operation(job) for job in ("staged", "approved")]
    assert staged["transcript_upload_done"] and staged["media_uploaded_successfully"] == 2
    assert staged["start_request_sent"] is False and not staged["start_request_accepted"]
    assert approved["start_request_sent"] and approved["start_request_accepted"]
    with pytest.raises(RecoveryError, match="уже связан"):
        journal.check("hash-b", "unrelated-scope")
    assert journal.media_attempts("staged") == []


def test_windows_process_identity_recovers_reused_pid(bundle, tmp_path, monkeypatch):
    monkeypatch.setattr("gtr.journal.worker_signature", lambda _: "original-creation-time")
    monkeypatch.setattr("gtr.journal.worker_exited", lambda _: False)
    journal = Journal(tmp_path / "operations.sqlite", "peer")
    controller = ImportController(MockAPI("media"), "peer", mode="owner", journal=journal)
    check = offline_result(controller.validate(bundle))
    with pytest.raises(ImportFault):
        offline_result(controller.execute(check, approved=True))
    with pytest.raises(RecoveryError, match="ещё работает"):
        journal.recover_before_start(check.operation_id, acknowledged=True)
    monkeypatch.setattr("gtr.journal.worker_signature", lambda _: "different-process-at-same-pid")
    assert journal.recover_before_start(check.operation_id, acknowledged=True) == check.operation_id
