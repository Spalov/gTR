from types import SimpleNamespace

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QDialog, QPushButton
from test_beta_wizard import finish_worker

from gtr import gui, telegram_ui
from gtr.account import Account, Target
from gtr.conversion import prepare
from gtr.journal import Phase, RecoveryConflict, RecoveryRequired, RecoveryReview


@pytest.fixture
def recovery_ui(tmp_path, project, monkeypatch):
    app = gui.application()
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(gui, "QSettings", lambda *_: settings)
    monkeypatch.setattr(gui, "app_home", lambda: tmp_path)
    monkeypatch.setattr(telegram_ui, "vault_path", lambda: tmp_path / "owner.dpapi")
    window = gui.Window(project)
    try:
        selected = prepare(project, 1, tmp_path / "selection", media_mode="text_only")
        window.use_bundle(selected)
        panel = window.telegram
        panel.account = Account(1, "Тестовый аккаунт")
        panel.target.addItem("Тестовый диалог", Target(2, "Тестовый диалог", "pm", None))
        panel.target.setCurrentIndex(0)
        panel.consent.setChecked(True)
        window.import_tabs.setCurrentWidget(panel.server_page)
        window.show()
        app.processEvents()
        yield app, window, panel, selected
    finally:
        window.close()


def action(panel, text):
    return next(button for button in panel.recovery_panel.findChildren(QPushButton) if button.text() == text)


def test_partial_overlap_prepares_only_new_records_and_reopens_consent(recovery_ui, tmp_path, monkeypatch):
    app, window, panel, selected = recovery_ui
    assert selected.record_intervals() == ((1, 3),)
    previous = RecoveryConflict("previous-1", Phase.COMPLETED, 1)
    report = RecoveryReview((previous,), 3, 1, 1, 2, 0, ((2, 3),), ((1, 1),), ())
    checked = []

    def review(bundle):
        checked.append(bundle.manifest_hash())
        return report

    monkeypatch.setattr(telegram_ui, "journal_for", lambda *_: SimpleNamespace(review_bundle=review))
    monkeypatch.setattr(telegram_ui.QFileDialog, "getExistingDirectory", lambda *_: str(tmp_path))
    panel._failure("preflight", RecoveryRequired(report))
    assert panel.recovery_panel.isVisible()
    assert "до обращения к Telegram" in panel.local_status.text()
    assert "локальная блокировка" in panel.counts.text() and "ожидает загрузки" not in panel.counts.text()
    assert "previous-1" in panel.recovery_summary(report)
    assert "Подтверждено 1 · Можно добавить 2 · Требуют проверки 0" in panel.recovery_summary(report)
    assert not panel.start_button.isEnabled()
    action(panel, "Импортировать только новые записи").click()
    finish_worker(app, window)
    assert checked == [selected.manifest_hash()]
    assert panel.package.root != selected.root and panel.package.record_intervals() == ((2, 3),)
    assert panel.package.data["media_mode"] == "text_only"
    assert panel.recovery_panel.isHidden() and not panel.consent.isChecked()
    assert not panel.preflight_button.isEnabled() and not panel.start_button.isEnabled()


def test_unknown_overlap_blocks_preparation_and_lists_original_ranges(recovery_ui, monkeypatch):
    _, window, panel, _ = recovery_ui
    previous = RecoveryConflict("previous-2", Phase.UNKNOWN_SERVER_STATE, None, can_attach=True)
    report = RecoveryReview((previous,), 3, None, 0, 0, 3, (), (), ((1, 3),))
    panel._failure("preflight", RecoveryRequired(report, locked=True))
    assert not action(panel, "Импортировать только новые записи").isEnabled()
    assert action(panel, "Проверить предыдущую операцию").isEnabled()
    assert "Состояние Telegram неизвестно" in panel.recovery_summary(report)
    assert "Совпадения: не установлено" in panel.recovery_summary(report)
    window.import_tabs.setCurrentWidget(window.wizard)
    window.wizard.pages.setCurrentIndex(5)
    window.wizard.refresh()
    assert "Проверьте предыдущую операцию" in window.wizard.hint.text()
    assert any(
        b.isVisible() and b.isEnabled()
        for b in window.wizard.findChildren(QPushButton)
        if b.text() == "Проверить предыдущую операцию"
    )
    seen = []

    def display(dialog):
        seen.extend(widget.toPlainText() for widget in dialog.findChildren(telegram_ui.QPlainTextEdit))

    monkeypatch.setattr(QDialog, "exec", display)
    panel.show_recovery_matches()
    assert seen and "Требуют проверки · 1–3" in seen[0]
    panel.dismiss_recovery()
    assert panel.recovery is None and not panel.preflight_button.isEnabled()


def test_prior_package_is_linked_locally_and_active_selection_is_preserved(
    recovery_ui, monkeypatch, tmp_path
):
    app, window, panel, selected = recovery_ui
    prior_package = prepare(window.project, 1, tmp_path / "past", media_mode="text_only", last_record=1)
    prior = RecoveryConflict("previous-3", Phase.VERIFYING, None, can_attach=True)
    initial = RecoveryReview((prior,), 3, None, 0, 0, 3, (), (), ((1, 3),))
    current = RecoveryReview(
        (RecoveryConflict(prior.operation_id, Phase.VERIFYING, 1),), 3, 1, 0, 2, 1, ((2, 3),), (), ((1, 1),)
    )
    linked = []
    journal = SimpleNamespace(
        operation=lambda _: {"manifest_sha256": prior_package.manifest_hash()},
        attach_selection=lambda job, bundle: linked.append((job, bundle.manifest_hash())),
        review_bundle=lambda _: current,
    )
    monkeypatch.setattr(telegram_ui, "journal_for", lambda *_: journal)
    monkeypatch.setattr(telegram_ui.QFileDialog, "getExistingDirectory", lambda *_: str(prior_package.root))
    panel._failure("preflight", RecoveryRequired(initial))
    action(panel, "Проверить предыдущую операцию").click()
    finish_worker(app, window)
    assert panel.package is selected and panel.recovery is current
    assert linked == [(prior.operation_id, prior_package.manifest_hash())]
    assert action(panel, "Импортировать только новые записи").isEnabled()
    assert not panel.preflight_button.isEnabled()
