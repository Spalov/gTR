import pytest
from PySide6.QtWidgets import QPushButton
from test_beta_wizard import finish_worker

from gtr import gui
from gtr.account import Account, Target
from gtr.conversion import prepare
from gtr.faults import ImportFault
from gtr.importer import ImportController
from gtr.incremental_import import remember_profile
from gtr.journal import Endpoint, Journal
from gtr.mock_api import MockAPI, offline_result
from gtr.owner import app_home


def test_profile_reopen_offline_prepare_only_eligible_and_filter_history(project, tmp_path):
    app = gui.application()
    journal = Journal(app_home() / "imports.sqlite", Endpoint.telegram(10, 11, "pm"))
    account, target = Account(10, "Первый"), Target(11, "Второй", "pm", None)
    remember_profile(journal, account, target, project, tmp_path, "text_only", {"chat": 1})
    old = prepare(project, 1, tmp_path / "previous", "source", media_mode="text_only", sample=2)
    run = ImportController(MockAPI(), "peer", journal=journal, mode="owner")
    checked = offline_result(run.validate(old))
    offline_result(run.execute(checked, approved=True))
    window = gui.Window()
    try:
        window.show()
        window.show_import_profiles()
        app.processEvents()
        page = window.profiles_page
        assert window.nav.currentItem().text() == "Профили импорта" and window.project == project
        page.timer.stop()
        page.analyze()
        finish_worker(app, window)
        assert page.ready_key and page.preview[1].unknown == 2 and page.continue_button.isEnabled()
        assert "Требуют дополнительной проверки 2" in page.summary.text()
        assert "Проверить совпадения" in [b.text() for b in page.findChildren(QPushButton)]
        page.build()
        finish_worker(app, window)
        assert window.telegram.bound_profile == (10, "pm:10/11")
        assert not window.telegram.consent.isChecked() and not window.telegram.start_button.isEnabled()
        assert not window.telegram.package.record_intervals()[0][0] <= 2
        window.show_import_history()
        hist = window.history_page
        assert hist.table.rowCount() == 1 and "Операций 1" in hist.summary.text()
        hist.filters[0].setText("5555")
        assert hist.table.rowCount() == 0
        hist.filters[0].clear()
        hist.filters[1].setText("Второй")
        assert hist.table.rowCount() == 1
        assert not any("gtr-" in hist.table.item(0, c).text() for c in (1, 2, 4))
    finally:
        window.close()


def test_verification_checkbox_reopens_per_profile_and_skips_unknown_records(project, tmp_path):
    app = gui.application()
    account = Account(10, "Первый")
    first, other = Target(11, "Второй", "pm", None), Target(12, "Другой диалог", "pm", None)
    path = app_home() / "imports.sqlite"
    endpoint = Endpoint.telegram(account.id, first.id, first.kind)
    journal = Journal(path, endpoint)
    for target in (first, other):
        profile_journal = Journal(path, Endpoint.telegram(account.id, target.id, target.kind))
        remember_profile(profile_journal, account, target, project, tmp_path, "text_only", {"chat": 1})
    old = prepare(project, 1, tmp_path / "previous", "source", media_mode="text_only", sample=2)
    run = ImportController(MockAPI("lost_reply"), "peer", journal=journal, mode="owner")
    checked = offline_result(run.validate(old))
    with pytest.raises(ImportFault, match="неизвестен"):
        offline_result(run.execute(checked, approved=True))
    window = gui.Window()
    try:
        window.show()
        window.show_import_profiles((account.id, endpoint.key))
        app.processEvents()
        page = window.profiles_page
        page.timer.stop()
        page.analyze()
        finish_worker(app, window)
        assert page.verify_previous.isChecked() and not page.continue_button.isEnabled()
        next(b for b in page.findChildren(QPushButton) if b.text() == "Изменить параметры").click()
        assert page.verify_previous.isVisible()
        assert page.verify_previous.text() == "Проверять предыдущие операции"
        page.verify_previous.click()
        assert page.ready_key is None

        page.reload((account.id, Endpoint.telegram(account.id, other.id, other.kind).key))
        assert page.verify_previous.isChecked()
        page.reload((account.id, endpoint.key))
        assert not page.verify_previous.isChecked()
        page.timer.stop()
        page.analyze()
        finish_worker(app, window)
        assert "Исключено для защиты от повторов 2" in page.summary.text()
        assert page.continue_button.isEnabled()
        page.continue_button.click()
        finish_worker(app, window)
        panel = window.telegram
        assert panel.package.record_intervals() == page.preview[1].eligible
        assert panel.package.record_intervals()[0][0] > 2
        assert panel.bound_profile == (account.id, endpoint.key) and not panel.start_button.isEnabled()
        window.show_import_history()
        assert window.history_page.table.rowCount() == 1
        assert window.history_page.table.item(0, 4).text() == "Состояние Telegram неизвестно"
    finally:
        window.close()
