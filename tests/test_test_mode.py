from functools import partial
from types import SimpleNamespace

import pytest
from conftest import message, write_json
from telethon import errors, functions, types

from gtr.account import AccountManager, Login, Target
from gtr.conversion import prepare
from gtr.core import analyze
from gtr.faults import ImportFault
from gtr.headless_owner import OwnerFlow
from gtr.importer import ImportController
from gtr.incremental_import import history, local_preview, recover_package
from gtr.journal import Endpoint, Journal, RecoveryRequired, State
from gtr.mock_api import MockAPI, offline_result
from gtr.model import RecoveryError, quiet
from gtr.server import OwnerSession
from gtr.test_mode import reset_test_registry

FIRST, SECOND = 100, 101


class TestServer:
    __test__ = False

    def __init__(self):
        self.messages = {FIRST: [], SECOND: []}
        self.reads = []
        self.clients = []
        self.override = {}
        self.on_read = None

    def make_client(self, *_):
        client = HistoryClient(self)
        self.clients.append(client)
        return client

    def endpoint(self, account):
        other = SECOND if account == FIRST else FIRST
        return types.InputPeerUser(other, account * 10000 + other)


class HistoryClient:
    def __init__(self, server):
        self.server = server
        self.id = None
        self.disconnected = False

    async def connect(self):
        pass

    async def disconnect(self):
        self.disconnected = True

    async def is_user_authorized(self):
        return False

    async def send_code_request(self, phone):
        self.id = int(phone[-3:])
        return SimpleNamespace(phone_code_hash="synthetic")

    async def sign_in(self, *args, password=None, **kwargs):
        if self.id == SECOND and not password:
            raise errors.SessionPasswordNeededError(None)

    async def get_me(self):
        return types.User(self.id, first_name=f"Тестовый аккаунт {self.id}", is_self=True)

    async def get_input_entity(self, user):
        if getattr(user, "id", None) is None:
            raise ValueError("The empty dialog is absent from the cache.")
        assert user.id == self.server.endpoint(self.id).user_id
        return self.server.endpoint(self.id)

    async def __call__(self, request):
        if isinstance(request, functions.contacts.GetContactsRequest):
            assert request.hash == 0
            peer = self.server.endpoint(self.id)
            return types.contacts.Contacts([], 1, [types.User(peer.user_id, first_name="Тестовый контакт")])
        assert isinstance(request, functions.messages.GetHistoryRequest)
        assert request.peer == self.server.endpoint(self.id)
        assert (
            request.limit,
            request.hash,
            request.offset_id,
            request.offset_date,
            request.add_offset,
            request.min_id,
            request.max_id,
        ) == (1, 0, 0, None, 0, 0, 0)
        self.server.reads.append(self.id)
        if self.server.on_read:
            self.server.on_read(self.id)
        result = self.server.override.get(self.id)
        if isinstance(result, Exception):
            raise result
        return (
            result
            if result is not None
            else types.messages.Messages(self.server.messages[self.id][:1], [], [], [])
        )


class ImportToMemory(MockAPI):
    def __init__(self, server):
        super().__init__()
        self.server = server

    async def upload_limit(self):
        return None

    async def start(self, peer, import_id):
        result = await super().start(peer, import_id)
        for account in (FIRST, SECOND):
            self.server.messages[account].extend(self.content)
        return result


def journal_for(path, account, target):
    return Journal(path, Endpoint.telegram(account.id, target.id, target.kind))


def login(manager, account):
    assert offline_result(manager.connect(11, "a" * 32, f"+1234567{account}")).challenge == "code"
    authorized = offline_result(manager.submit_code("12345"))
    if authorized.challenge:
        authorized = offline_result(manager.submit_password("synthetic-password"))
    assert authorized.account.id == account


def second_account_reset(session, target, second=SECOND):
    auth = offline_result(session.begin_test_reset(FIRST, target, f"+1234567{second}", consented=True))
    assert auth.challenge == "code"
    auth = offline_result(session.answer_test_login("12345"))
    if isinstance(auth, Login):
        assert auth.challenge == "password"
        return offline_result(session.answer_test_login("synthetic-password", password=True))
    return auth


@pytest.fixture
def trial(tmp_path):
    source = write_json(tmp_path / "export", [message(i, f"Запись {i}") for i in range(1, 4)])
    project = analyze(source, tmp_path / "project")
    server = TestServer()
    endpoint = Endpoint.telegram(FIRST, SECOND, "pm")
    journal = Journal(tmp_path / "journal.sqlite", endpoint)
    target = Target(SECOND, "Второй", "pm", server.endpoint(FIRST))
    return project, server, journal, target


def test_import_delete_on_both_accounts_and_repeat_without_erasing_audit(tmp_path, trial):
    project, server, journal, target = trial
    api = []

    def adapter(*args, **kwargs):
        transport = ImportToMemory(server)
        api.append(transport)
        return transport

    session = OwnerSession(
        AccountManager(server.make_client, timed=False), adapter, partial(journal_for, journal.path)
    )
    testing = Journal(journal.path, journal.endpoint, test_reset_id=0)
    try:
        assert offline_result(session.connect(11, "a" * 32, f"+1234567{FIRST}", False)).challenge == "code"
        offline_result(session.manager.submit_code("12345"))
        bundle = prepare(project, 1, tmp_path / "first", "source", test_reset_id=0)
        receipt = offline_result(
            session.validate(bundle.root, target, project, consented=True, test_reset_id=0)
        )
        job = offline_result(session.execute(receipt))["operation_id"]
        original_report = journal.diagnostic_report(job, private=True)
        assert [len(server.messages[a]) for a in (FIRST, SECOND)] == [3, 3]
        assert not testing.review_bundle(bundle).eligible

        # The fixture models history deletion on each iPhone, outside of gTR.
        server.messages[FIRST].clear()
        with pytest.raises(RecoveryError, match="ещё есть сообщения"):
            second_account_reset(session, target)
        assert server.reads == [FIRST, SECOND] and journal.latest_test_reset() == 0
        assert server.clients[-1].disconnected and not server.clients[0].disconnected
        server.messages[SECOND].clear()
        result = second_account_reset(session, Target(SECOND, "Второй", "pm", None))
        assert server.reads == [FIRST, SECOND, FIRST, SECOND] and result.id > 0
        assert server.clients[-1].disconnected and not server.clients[0].disconnected
        assert journal.diagnostic_report(job, private=True) == original_report

        cycle = Journal(journal.path, journal.endpoint, test_reset_id=result.id)
        _, review, _ = local_preview(cycle, project, 1, "text_media")
        assert review.eligible == ((1, 3),)
        repeat = prepare(project, 1, tmp_path / "repeat", "source", test_reset_id=result.id)
        assert repeat.manifest_hash() != bundle.manifest_hash()
        assert repeat.transcript.read_bytes() == bundle.transcript.read_bytes()
        with pytest.raises(RecoveryRequired):
            offline_result(session.validate(repeat.root, target, project, consented=True))
        assert api[-1].calls == []
        with pytest.raises(RecoveryError, match="цикл изменился"):
            testing.review_bundle(bundle)

        receipt = offline_result(
            session.validate(
                repeat.root,
                Target(SECOND, "Второй", "pm", None),
                project,
                consented=True,
                test_reset_id=result.id,
            )
        )
        successor = offline_result(session.execute(receipt))["operation_id"]
        assert api[-1].calls.count("start_import") == 1
        assert [len(server.messages[a]) for a in (FIRST, SECOND)] == [3, 3]
        assert not cycle.review_bundle(repeat).eligible and not journal.review_bundle(bundle).eligible
        _, ops = history(journal)
        assert {row["id"] for row in ops if row["state"] == State.VERIFYING} == {job, successor}
        assert journal.diagnostic_report(job, private=True) == original_report
        assert recover_package(journal, job).manifest_hash() == bundle.manifest_hash()
    finally:
        offline_result(session.disconnect())


def test_native_prepare_test_package_after_both_accounts_confirm_an_empty_dialog(tmp_path, trial):
    project, server, journal, target = trial
    transport = ImportToMemory(server)
    session = OwnerSession(
        AccountManager(server.make_client, timed=False),
        lambda *args, **kwargs: transport,
        partial(journal_for, journal.path),
    )
    bundle = prepare(project, 1, tmp_path / "normal", "source", media_mode="text_only")
    params = {"target": str(SECOND), "project": str(project), "bundle": str(bundle.root), "test": True}
    flow = OwnerFlow(session)
    flow.targets[str(SECOND)] = target
    try:
        offline_result(session.connect(11, "a" * 32, f"+1234567{FIRST}", False))
        offline_result(session.manager.submit_code("12345"))
        with pytest.raises(ImportFault) as error:
            offline_result(flow.run("import.validate", {**params, "consented": True}, quiet))
        assert (error.value.code, error.value.stage) == ("TEST_RESET_REQUIRED", "test_scope")
        assert not transport.calls

        flow.test_login(second_account_reset(session, target))
        with pytest.raises(ImportFault) as error:
            offline_result(flow.run("import.validate", {**params, "consented": True}, quiet))
        assert (error.value.code, error.value.stage) == ("TEST_PACKAGE_REQUIRED", "test_scope")
        assert not transport.calls

        prepared = offline_result(flow.run("import.test_package", {**params, "confirmed": True}, quiet))
        assert server.reads == [FIRST, SECOND]
        receipt = offline_result(flow.run(
            "import.validate",
            {**params, "bundle": prepared["path"], "manifest": prepared["manifest"], "consented": True},
            quiet,
        ))
        assert receipt["messages"] == bundle.data["included"] == 3 and receipt["lease"]
        assert transport.calls == ["check_history", "check_peer"]
    finally:
        offline_result(session.disconnect())


def test_native_saved_dialog_unavailable_before_validating_the_package(trial):
    project, server, journal, target = trial

    class EmptyContacts(HistoryClient):
        async def __call__(self, request):
            assert isinstance(request, functions.contacts.GetContactsRequest)
            return types.contacts.Contacts([], 0, [])

    manager = AccountManager(lambda *_: EmptyContacts(server), timed=False)
    login(manager, FIRST)
    transport = ImportToMemory(server)
    flow = OwnerFlow(OwnerSession(manager, lambda *args, **kwargs: transport, partial(journal_for, journal.path)))
    flow.targets[str(SECOND)] = Target(SECOND, target.title, "pm", None)
    try:
        params = {"target": str(SECOND), "project": str(project), "bundle": str(project), "consented": True}
        with pytest.raises(ImportFault) as error:
            offline_result(flow.run("import.validate", params, quiet))
        assert (error.value.code, error.value.stage) == ("SAVED_DIALOG_UNAVAILABLE", "resolve_target")
        assert not transport.calls
    finally:
        offline_result(manager.disconnect())


@pytest.mark.parametrize(
    "condition", ["not_modified", "inexact", "inconsistent", "rpc", "other_account", "live_import"]
)
def test_reset_fails_closed(tmp_path, trial, condition):
    project, server, journal, _ = trial
    primary = AccountManager(server.make_client, timed=False)
    partner = AccountManager(server.make_client, timed=False)
    login(primary, FIRST)
    login(partner, FIRST if condition == "other_account" else SECOND)
    if condition == "not_modified":
        server.override[SECOND] = types.messages.MessagesNotModified(0)
    elif condition == "inexact":
        server.override[SECOND] = types.messages.MessagesSlice(0, [], [], [], [], inexact=True)
    elif condition == "inconsistent":
        server.override[SECOND] = types.messages.MessagesSlice(1, [], [], [], [])
    elif condition == "rpc":
        server.override[SECOND] = ImportFault("TIMEOUT")
    elif condition == "live_import":
        bundle = prepare(project, 1, tmp_path / "hold")
        run = ImportController(MockAPI(), "target", journal=journal, mode="owner")
        check = offline_result(run.validate(bundle))
        journal.transition(check.operation_id, State.CONFIRMED, "confirmed", reserve=True)
        run.close()
    try:
        with pytest.raises(RecoveryError):
            offline_result(reset_test_registry(journal, primary, partner, SECOND))
        assert journal.latest_test_reset() == 0
        if condition in {"other_account", "live_import"}:
            assert not server.reads
    finally:
        for manager in (primary, partner):
            offline_result(manager.disconnect())


def test_reset_requires_fresh_reads_and_keeps_operations_started_after_proof(tmp_path, trial):
    project, server, journal, _ = trial
    primary = AccountManager(server.make_client, timed=False)
    partner = AccountManager(server.make_client, timed=False)
    login(primary, FIRST)
    login(partner, SECOND)
    full = prepare(project, 1, tmp_path / "ordinary", "source")
    standard = ImportController(MockAPI(), "target", journal=journal, mode="owner")
    ready = offline_result(standard.validate(full))

    def race(account):
        if account == SECOND:
            journal.create(full, "owner", full.scope())

    server.on_read = race
    with pytest.raises(RecoveryError, match="За время проверки"):
        offline_result(reset_test_registry(journal, primary, partner, SECOND))
    assert journal.latest_test_reset() == 0
    server.on_read = None
    try:
        result = offline_result(reset_test_registry(journal, primary, partner, SECOND))
        offline_result(standard.execute(ready, approved=True))
        cycle = Journal(journal.path, journal.endpoint, test_reset_id=result.id)
        assert not cycle.review_bundle(full).eligible
        assert Journal(journal.path, Endpoint.telegram(SECOND, FIRST, "pm")).latest_test_reset() == result.id
        assert Journal(journal.path, Endpoint.telegram(FIRST, 102, "pm")).latest_test_reset() == 0
    finally:
        for manager in (primary, partner):
            offline_result(manager.disconnect())


def test_new_reset_rejects_a_previously_approved_test_package(tmp_path, trial):
    project, server, journal, _ = trial
    primary = AccountManager(server.make_client, timed=False)
    partner = AccountManager(server.make_client, timed=False)
    login(primary, FIRST)
    login(partner, SECOND)
    initial = Journal(journal.path, journal.endpoint, test_reset_id=0)
    first = prepare(project, 1, tmp_path / "first", "source", test_reset_id=0)
    api = MockAPI()
    run = ImportController(api, "target", journal=initial, mode="owner")
    ready = offline_result(run.validate(first))
    try:
        offline_result(reset_test_registry(journal, primary, partner, SECOND))
        with pytest.raises(RecoveryError, match="цикл изменился"):
            offline_result(run.execute(ready, approved=True))
        assert api.calls == ["check_history", "check_peer"]
    finally:
        for manager in (primary, partner):
            offline_result(manager.disconnect())


def test_verified_reset_keeps_unknown_start_protected_in_standard_mode(tmp_path, trial):
    project, server, journal, _ = trial
    source = prepare(project, 1, tmp_path / "old", "source")
    old = ImportController(MockAPI("lost_reply"), "target", journal=journal, mode="owner")
    ready = offline_result(old.validate(source))
    with pytest.raises(ImportFault, match="неизвестен"):
        offline_result(old.execute(ready, approved=True))

    primary, partner = [AccountManager(server.make_client, timed=False) for _ in range(2)]
    login(primary, FIRST)
    login(partner, SECOND)
    try:
        result = offline_result(reset_test_registry(journal, primary, partner, SECOND))
        cycle = Journal(journal.path, journal.endpoint, test_reset_id=result.id)
        assert not journal.review_bundle(source).eligible
        repeat = prepare(project, 1, tmp_path / "repeat", "source", test_reset_id=result.id)
        run = ImportController(MockAPI(), "target", journal=cycle, mode="owner")
        receipt = offline_result(run.validate(repeat))
        offline_result(run.execute(receipt, approved=True))
        assert journal.operation(ready.operation_id)["state"] == State.START_UNCERTAIN
        assert not journal.review_bundle(source).eligible
        assert not cycle.review_bundle(repeat).eligible
    finally:
        for manager in (primary, partner):
            offline_result(manager.disconnect())


def test_profile_button_opens_second_account_login_and_full_archive(tmp_path, trial, monkeypatch):
    from PySide6.QtCore import QObject, QSettings, Signal
    from PySide6.QtWidgets import QInputDialog, QMessageBox, QPushButton
    from test_beta_wizard import finish_worker

    from gtr import gui, telegram_ui
    from gtr.incremental_import import remember_profile
    from gtr.owner import app_home

    project, server, _, target = trial
    path = app_home() / "imports.sqlite"
    session = OwnerSession(
        AccountManager(server.make_client, timed=False),
        lambda *_, **kwargs: ImportToMemory(server),
        partial(journal_for, path),
    )
    offline_result(session.connect(11, "a" * 32, f"+1234567{FIRST}", False))
    account = offline_result(session.manager.submit_code("12345")).account
    journal = journal_for(path, account, target)
    remember_profile(journal, account, target, project, None, "text_only", {"chat": 1})
    previous = prepare(project, 1, tmp_path / "old", "source", media_mode="text_only")
    receipt = offline_result(session.validate(previous.root, target, project, consented=True))
    offline_result(session.execute(receipt))
    for messages in server.messages.values():
        messages.clear()

    class InlineWorker(QObject):
        online, finished = Signal(), Signal()
        response, failure, update = Signal(str, object), Signal(str, object), Signal(str, int, int)

        def start(self):
            self.online.emit()

        def submit(self, name, operation):
            try:
                value = offline_result(operation(session, self.update.emit))
            except RecoveryError as error:
                self.failure.emit(name, error)
            else:
                self.response.emit(name, value)

        def shutdown(self):
            offline_result(session.disconnect())
            self.finished.emit()

    values = iter((f"+1234567{SECOND}", "12345", "synthetic-password"))
    monkeypatch.setattr(QInputDialog, "getText", lambda *_: (next(values), True))
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(
        gui, "QSettings", lambda *_: QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    )
    app = gui.application()
    factory = partial(telegram_ui.TelegramControls, thread_factory=InlineWorker)
    window = gui.Window(project, telegram_factory=factory)
    try:
        panel = window.telegram
        panel.account = account
        window.show()
        window.show_import_profiles()
        page = window.profiles_page
        # Exercise the reset controls before the delayed profile preview navigates away.
        page.timer.stop()
        app.processEvents()
        next(b for b in page.findChildren(QPushButton) if b.text() == "Изменить параметры").click()
        assert not page.test_mode.isChecked() and page.reset_tests.isHidden() and panel.target.count() == 0
        page.test_mode.click()
        page.timer.stop()
        assert page.reset_tests.isVisible() and page.reset_tests.isEnabled()
        page.first.setValue(2)
        page.last.setValue(2)
        page.reset_tests.click()
        assert server.reads == [FIRST, SECOND]
        assert page.first.value() == 1 and page.last.value() == 0
        assert "Новый тестовый цикл открыт" in page.test_status.text()
        reset_id = journal.latest_test_reset()
        page.timer.stop()
        page.analyze()
        finish_worker(app, window)
        assert page.continue_button.isEnabled() and page.preview[1].eligible == ((1, 3),)
        page.continue_button.click()
        finish_worker(app, window)
        assert panel.package.data["test_reset_id"] == panel.test_reset_id == reset_id
        assert panel.target.currentData().id == SECOND
        assert not panel.start_button.isEnabled()
        panel.consent.setChecked(True)
        panel.preflight_button.click()
        assert panel.review.test_reset_id == reset_id and panel.start_button.isEnabled()
        window.show_import_profiles()
        page.test_mode.click()
        assert not page.test_mode.isChecked() and panel.test_reset_id is None
        assert panel.review is None and not panel.start_button.isEnabled()
    finally:
        window.close()
