import json
import os
from functools import partial
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import message, write_json
from telethon import errors, types

from gtr.account import Account, AccountManager, Target
from gtr.backup import backup_project
from gtr.conversion import load_bundle, prepare
from gtr.core import analyze, summary, verify
from gtr.faults import ImportFault
from gtr.importer import ImportController
from gtr.integrity import check_local
from gtr.journal import Endpoint, Journal, State, worker_exited
from gtr.mock_api import MockAPI, offline_result, rehearse
from gtr.model import Cancelled, RecoveryError, quiet
from gtr.observation import CaptureWindow, capture, last_id
from gtr.reports import save_report
from gtr.verification import compare


def test_historical_plan_keeps_ties_offset_and_source_order(tmp_path):
    rows = [
        message(i, str(i), date=f"2025-01-02T10:00:{seconds:02}+03:00")
        for i, seconds in enumerate((4, 4, 5, 3, 8), 1)
    ]
    source = write_json(tmp_path / "src", rows)
    original = (source / "result.json").read_bytes()
    project = analyze(source, tmp_path / "p")
    historical = prepare(project, 1, tmp_path / "a", "source")
    alternative = prepare(project, 1, tmp_path / "b", "source", date_policy="ordered")
    h, a = (load_bundle(b.root).data for b in (historical, alternative))
    assert [row["source_id"] for row in h["records"]] == [str(i) for i in range(1, 6)]
    assert [row["import_date"][-2:] for row in h["records"]] == ["04", "04", "05", "03", "08"]
    assert [r["import_date"] for r in a["records"]] == [r["import_date"] for r in h["records"]]
    assert h["timeline"] == {"equal": 1, "regression": 1, "cascade": 0, "max_shift_seconds": 0}
    assert a["timeline"] == h["timeline"] and "ordered_time" not in a["losses"]
    first = h["records"][0]
    assert first["source_identity"]["original_timezone"] == "+03:00"
    assert first["source_identity"]["original_author_id"] == "user10"
    assert first["import_utc"] == "2025-01-02T07:00:04+00:00"
    assert (source / "result.json").read_bytes() == original


def test_old_ordered_package_is_still_readable(tmp_path):
    source = write_json(tmp_path / "source", [
        message(1, "A"),
        message(2, "B", date="2025-01-02T10:04:06+02:00"),
    ])
    project = analyze(source, tmp_path / "project")
    prepared = prepare(project, 1, tmp_path / "bundle2", "UTC")
    old = {**prepared.data, "schema": 2, "date_policy": "ordered"}
    (prepared.root / "manifest.json").write_text(json.dumps(old), encoding="utf-8")
    bundle = load_bundle(prepared.root)
    assert bundle.data["schema"] == 2 and check_local(bundle, project)["status"] == "unknown"
    assert rehearse(bundle, "success", quiet)["state"] == "accepted"


def test_backup_and_local_integrity_detect_changed_source(bundle, project, tmp_path):
    copy = backup_project(project, tmp_path / "copy")
    assert summary(copy) == summary(project) and verify(copy) == {"ok": 9}
    assert check_local(load_bundle(bundle.root), copy)["status"] == "verified"
    (project / "source" / "files" / "a.jpg").write_bytes(b"modified")
    evidence = check_local(load_bundle(bundle.root), project)
    assert evidence["status"] == "differences"
    assert {item["code"] for item in evidence["differences"]} == {"project_file_hashes"}
    failed = tmp_path / "failed"
    with pytest.raises(RecoveryError):
        backup_project(project, failed)
    assert not failed.exists() and verify(copy) == {"ok": 9}


def test_local_integrity_crosschecks_every_payload_with_source(bundle, project):
    entry = bundle.data["media"][0]
    data = b"\xff\xd8\xff\xe0different photo"
    bundle.media_path(entry).write_bytes(data)
    entry["size"], entry["sha256"] = len(data), sha256(data).hexdigest()
    (bundle.root / "manifest.json").write_text(json.dumps(bundle.data), encoding="utf-8")
    evidence = check_local(load_bundle(bundle.root), project)
    assert evidence["status"] == "differences"
    assert any(item["code"] == "package_media" for item in evidence["differences"])


def test_preflight_only_checks_and_owner_denial_stops(bundle, tmp_path):
    journal = Journal(tmp_path / "journal.sqlite", "pm:100/101")
    api = MockAPI()
    controller = ImportController(api, "target", journal=journal, mode="owner")
    ready = offline_result(controller.validate(bundle))
    assert api.calls == ["check_history", "check_peer"] and controller.state == State.AWAITING_USER
    assert ready.confirmation and ready.messages == 10 and ready.media == 8
    with pytest.raises(Cancelled):
        offline_result(controller.execute(ready, approved=False))
    assert journal.operations()[0]["state"] == State.CANCELLED
    assert not api.initialized and not api.started


def test_package_limit_blocks_before_transcript_upload(bundle, tmp_path):
    api = MockAPI()
    journal = Journal(tmp_path / "journal.sqlite", "test")

    async def limit():
        return 1

    controller = ImportController(api, "target", journal=journal, limits=limit)
    with pytest.raises(ImportFault) as exc:
        offline_result(controller.validate(bundle))
    assert (exc.value.code, exc.value.stage) == ("MEDIA_SIZE_LIMIT", "check_limits")
    assert not api.initialized and journal.operations()[0]["state"] == State.FAILED


def test_uncertain_start_uses_matching_readback_and_keeps_source_guard(bundle, project, tmp_path):
    path = tmp_path / "import.sqlite"
    journal = Journal(path, Endpoint.telegram(100, 101, "pm"))
    api = MockAPI("timeout")
    controller = ImportController(api, "target", mode="owner", journal=journal)
    ready = offline_result(controller.validate(bundle))
    with pytest.raises(ImportFault) as exc:
        offline_result(controller.execute(ready, approved=True))
    assert exc.value.code == "START_UNCERTAIN" and not exc.value.details()["retry_allowed"]
    events = journal.events(ready.operation_id)
    assert list(dict.fromkeys(row["state"] for row in events)) == [
        State.IDLE,
        State.VALIDATING,
        State.READY,
        State.AWAITING_USER,
        State.CONFIRMED,
        State.INITIALIZING,
        State.UPLOADING,
        State.STARTING,
        State.START_UNCERTAIN,
    ]
    assert journal.operations()[0]["media_done"] == 8
    other = Journal(path, Endpoint.telegram(101, 100, "pm"))
    with pytest.raises(RecoveryError, match="неопределённая"):
        other.check("new fingerprint", "different source")
    with pytest.raises(RecoveryError):
        other.recover_before_start(ready.operation_id, acknowledged=True)

    # These synthetic captures exercise the owner-evidence journal path locally.
    a, b, authors = api.observations(bundle)
    simulated = compare(bundle, a, b, authors, project=project)
    assert other.reconcile(simulated) is None
    for view in (a, b):
        view["mode"] = "telegram_observation"
        view["after_restart"] = None
    readback = compare(bundle, a, b, authors, project=project)
    assert readback["state"] == "inconclusive" and readback["accepted_evidence"]
    assert other.reconcile(readback) is None  # A new operation needs saved pre-start baselines.
    for view in readback["views"]:
        view["scope"]["baseline_captured_at"] = "2000-01-01T00:00:00+00:00"
    assert other.reconcile(readback) == ready.operation_id
    assert journal.operations()[0]["state"] == State.VERIFYING
    with pytest.raises(RecoveryError, match="уже связан"):
        other.check(bundle.fingerprint(), "different source")
    other.check("new fingerprint", "different source")


def test_recovery_can_release_lock_before_final_boundary(bundle, tmp_path, monkeypatch):
    path = tmp_path / "import.sqlite"
    journal = Journal(path, "pm:100/101")
    controller = ImportController(MockAPI(), "target", mode="owner", journal=journal)
    checked = offline_result(controller.validate(bundle))
    op = checked.operation_id
    journal.transition(op, State.CONFIRMED, "confirmed", reserve=True)
    journal.transition(op, State.INITIALIZING, "init_import")
    other = Journal(path, "")
    assert not worker_exited(os.getpid())
    try:
        with pytest.raises(RecoveryError, match="ещё работает"):
            other.recover_before_start(op, acknowledged=True)
        monkeypatch.setattr("gtr.journal.worker_exited", lambda pid: pid == os.getpid())
        assert other.recover_before_start(op, acknowledged=True) == op
        assert other.events(op)[-1]["stage"] == "recovered_before_start"
        journal.check("another fingerprint", "another source")
    finally:
        controller.close()


def test_server_active_wait_is_kept_after_reopen(bundle, tmp_path):
    path = tmp_path / "import.sqlite"
    journal = Journal(path, "same peer")
    api = MockAPI("active")
    controller = ImportController(api, "target", journal=journal)
    ready = offline_result(controller.validate(bundle))
    with pytest.raises(ImportFault) as exc:
        offline_result(controller.execute(ready, approved=True))
    assert exc.value.code == "PREVIOUS_CHAT_IMPORT_ACTIVE_WAIT" and not api.started
    with pytest.raises(RecoveryError, match="интервал ожидания"):
        Journal(path, "same peer").check("another fingerprint", "another source")


@pytest.mark.parametrize(
    "scenario, findings",
    [
        ("metadata_only", ("verified", True)),
        ("media_unavailable", ("differences", False)),
    ],
)
def test_metadata_and_media_are_distinct_from_visual_evidence(bundle, project, tmp_path, scenario, findings):
    result = rehearse(bundle, scenario, quiet, project=project)["verification"]
    assert (result["levels"]["B"], result["historical_metadata_only"]) == findings
    assert result["levels"]["A"]["status"] == "verified" and result["levels"]["C"] == "differences"
    assert result["visual_status"] == "VISUAL_RECONSTRUCTION_UNCONFIRMED"
    report = save_report(tmp_path / "verification.html", verification=result)
    html = report.read_text(encoding="utf-8")
    assert "VISUAL_RECONSTRUCTION_UNCONFIRMED" in html and "До встречи" not in html


def test_visual_minutes_are_separate_from_server_seconds_and_restart(bundle, project):
    api = MockAPI()
    controller = ImportController(api, "target")
    ready = offline_result(controller.validate(bundle))
    offline_result(controller.execute(ready, approved=True))
    a, b, authors = api.observations(bundle)
    for view in (a, b):
        for row in view["messages"]:
            row["visual_date"] = row["visual_date"][:16] + ":00+00:00"
            row["visual_precision"] = "minute"
    assert compare(bundle, a, b, authors, project=project)["state"] == "verified"
    b["after_restart"] = False
    result = compare(bundle, a, b, authors, project=project)
    assert result["levels"]["C"] == "unknown" and result["quality"]["Даты"] == "unknown"


class LoginClient:
    session = SimpleNamespace(save=lambda: "synthetic-session")

    def __init__(self, require_password=False):
        self.require_password = require_password
        self.disconnected = False

    async def connect(self):
        pass

    async def disconnect(self):
        self.disconnected = True

    async def is_user_authorized(self):
        return False

    async def send_code_request(self, phone):
        return SimpleNamespace(phone_code_hash="synthetic-code-hash")

    async def sign_in(self, *args, password=None, **kwargs):
        if self.require_password and password is None:
            raise errors.SessionPasswordNeededError(None)

    async def get_me(self):
        return types.User(100, first_name="Account A", is_self=True)

    async def get_input_entity(self, user):
        return types.InputPeerUser(user.id, 1)

    async def iter_dialogs(self):
        for entity in [types.User(101, first_name="Account B"), types.User(102, bot=True)]:
            yield SimpleNamespace(entity=entity)


def test_account_login_2fa_dialog_list_and_disconnect():
    client = LoginClient(require_password=True)
    manager = AccountManager(lambda *_: client, timed=False)
    login = offline_result(manager.connect(11, "a" * 32, "+10000000001"))
    assert login.challenge == "code"
    assert offline_result(manager.submit_code("12345")).challenge == "password"
    assert not manager.phone and not manager.code_hash
    assert offline_result(manager.submit_password("test-only")).account == Account(100, "Account A")
    assert [target.id for target in offline_result(manager.dialogs())] == [101]
    assert offline_result(manager.resolve("101")).title == "Account B"
    assert manager.session() == "synthetic-session"
    offline_result(manager.disconnect())
    assert client.disconnected and manager.account is None and manager.client is None


@pytest.mark.parametrize("fails", [False, True])
def test_import_control_center_runs_owner_flow_on_fake_transport(bundle, project, tmp_path, monkeypatch, fails):
    from PySide6.QtCore import QObject, QSettings, Signal
    from PySide6.QtWidgets import QInputDialog, QMessageBox

    from gtr import gui, telegram_ui
    from gtr.server import OwnerSession

    class API(MockAPI):
        async def upload_limit(self):
            return 1024**3

        async def upload_media(self, *args):
            if fails and len(self.uploaded) == 2:
                raise ImportFault("MEDIA_INVALID")
            return await super().upload_media(*args)

    api = API()
    journal = Journal(tmp_path / "imports.sqlite", Endpoint.telegram(100, 101, "pm"))
    session = OwnerSession(
        AccountManager(lambda *_: LoginClient(), timed=False), lambda *_, **kw: api, lambda *_: journal
    )

    class InlineWorker(QObject):
        online, finished = Signal(), Signal()
        response, failure, update = Signal(str, object), Signal(str, object), Signal(str, int, int)

        def start(self):
            self.online.emit()

        def submit(self, name, operation):
            try:
                value = offline_result(operation(session, self.update.emit))
            except ImportFault as fault:
                self.failure.emit(name, fault)
            else:
                self.response.emit(name, value)

        def shutdown(self):
            offline_result(session.disconnect())
            self.finished.emit()

    def accept(dialog):
        assert "Account A" in dialog.text() and "Account B" in dialog.text()
        assert "Сообщения 10 · Медиа 8" in dialog.text()
        assert dialog.informativeText().endswith(session.review.check.confirmation)
        next(item for item in dialog.buttons() if item.text() == "Начать импорт").click()

    monkeypatch.setattr(
        gui, "QSettings", lambda *_: QSettings(str(tmp_path / "gtr.ini"), QSettings.Format.IniFormat)
    )
    monkeypatch.setattr(gui, "app_home", lambda: tmp_path)
    monkeypatch.setattr(telegram_ui, "vault_path", lambda: tmp_path / "owner.dpapi")
    monkeypatch.setattr(QInputDialog, "getText", lambda *_: ("12345", True))
    monkeypatch.setattr(QMessageBox, "exec", accept)
    app = gui.application()
    window = gui.Window(
        project, telegram_factory=partial(telegram_ui.TelegramControls, thread_factory=InlineWorker)
    )
    window.show()
    panel = window.telegram
    try:
        window.use_bundle(bundle)
        app.processEvents()
        assert not panel.start_button.isEnabled() and not panel.preflight_button.isEnabled()
        assert not api.calls
        panel.api_id.setText("11")
        panel.api_hash.setText("a" * 32)
        panel.phone.setText("+10000000001")
        panel.connect_button.click()
        assert panel.account == Account(100, "Account A") and not panel.api_hash.text()
        panel.dialogs_button.click()
        panel.target.setCurrentIndex(1)
        assert not panel.preflight_button.isEnabled()
        panel.consent.setChecked(True)
        assert panel.preflight_button.isEnabled() and not panel.start_button.isEnabled()
        panel.preflight_button.click()
        assert panel.start_button.isEnabled() and api.calls == ["check_history", "check_peer"]
        panel.start_button.click()
        assert not panel.start_button.isEnabled() and "Сообщения 10 / 10" not in panel.counts.text()
        if fails:
            assert journal.operations()[0]["state"] == State.STAGED_NOT_STARTED and not api.started
            assert "Ошибка на вложении 3 из 8" in panel.state.text()
            assert "Медиа принято 2 из 8" in panel.counts.text() and "Запуск: Не начат" in panel.counts.text()
            assert panel.diagnostic_button.isEnabled()
        else:
            assert journal.operations()[0]["state"] == State.VERIFYING and api.started
            assert "Принят Telegram" in panel.counts.text() and "Сверено в двух клиентах —" in panel.counts.text()
        panel.disconnect_button.click()
        assert not panel.account and not panel.start_button.isEnabled()
    finally:
        if panel.thread:
            panel.thread.deleteLater()
            panel.thread = None
        window.close()
        app.processEvents()


def test_readback_uses_independent_rpc_dates_and_media(bundle, tmp_path):
    from datetime import UTC, datetime

    old = datetime(2020, 3, 1, 13, 11, tzinfo=UTC)
    new = datetime(2026, 11, 21, 14, 2, tzinfo=UTC)

    class ReadClient:
        async def get_messages(self, peer, limit):
            return [SimpleNamespace(id=73)]

        async def iter_messages(self, peer, **options):
            assert options == {"min_id": 73, "max_id": 76, "limit": 211}
            for mid in (75, 74):
                yield types.Message(
                    mid,
                    types.PeerUser(101),
                    date=new,
                    message="fixture body",
                    out=True,
                    from_id=types.PeerUser(100),
                    fwd_from=types.MessageFwdHeader(old, from_name="Historical", imported=True),
                    media=types.MessageMediaPhoto(),
                )

        async def iter_download(self, media, **options):
            assert options == {"request_size": 4096, "limit": 1}
            yield b"sample block"

    manager = AccountManager(timed=False)
    manager.account, manager.client = Account(100, "Account A"), ReadClient()
    target = Target(101, "Account B", "pm", types.InputPeerUser(101, 1))
    assert offline_result(last_id(manager, target))["last_id"] == 73
    window = CaptureWindow(tmp_path / "read.json", 73, 75, True)
    saved = offline_result(capture(manager, target, bundle, window))
    raw = Path(saved["path"]).read_text(encoding="utf-8")
    data = json.loads(raw)
    assert [item["id"] for item in data["messages"]] == [74, 75]
    assert data["after_restart"] is None and "fixture body" not in raw
    item = data["messages"][0]
    assert item["original_date"] == old.isoformat() and item["server_date"] == new.isoformat()
    assert item["visual_date"] is None and item["media_accessible"] is True and item["from_id"] == 100
