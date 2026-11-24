from PySide6.QtCore import QEventLoop, QSettings, QTimer

from gtr import gui, telegram_ui
from gtr.wizard import STEPS


def finish_worker(app, window):
    loop = QEventLoop()
    window.worker.finished.connect(loop.quit)
    QTimer.singleShot(15000, loop.quit)
    loop.exec()
    app.processEvents()
    assert window.worker is None, "The Qt worker did not finish"
    assert window.stage.text() == "Готово", window.outcome.text()


def test_wizard_text_selection_and_local_diagnostics(tmp_path, monkeypatch, project):
    app = gui.application()
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(gui, "QSettings", lambda *_: settings)
    monkeypatch.setattr(gui, "app_home", lambda: tmp_path)
    monkeypatch.setattr(telegram_ui, "vault_path", lambda: tmp_path / "owner.dpapi")
    monkeypatch.setattr(gui.QFileDialog, "getExistingDirectory", lambda *_: str(tmp_path))
    window = gui.Window(project)
    try:
        window.resize(930, 650)
        window.show()
        app.processEvents()
        wizard = window.wizard
        assert wizard.pages.count() == len(STEPS) == 8
        assert STEPS[0] in wizard.title.text() and not wizard.forward.isEnabled()
        wizard.project_ready()
        wizard.go(1)
        assert STEPS[1] in wizard.title.text() and not wizard.forward.isEnabled()
        wizard.check_history()
        finish_worker(app, window)
        assert wizard.checked and wizard.forward.isEnabled()
        wizard.go(1)
        assert STEPS[2] in wizard.title.text() and not wizard.forward.isEnabled()
        wizard.offline.setChecked(True)
        wizard.go(1)
        assert wizard.mode.currentData() == "text_only"
        wizard.first.setValue(2)
        wizard.last.setValue(1)
        assert not wizard.forward.isEnabled()
        wizard.last.setValue(3)
        wizard.go(1)
        wizard.build_package()
        finish_worker(app, window)
        assert wizard.reviewed.data["selected_records"] == 2
        assert wizard.inventory["count"] == wizard.inventory["blocked"] == 0
        assert wizard.forward.isEnabled() and wizard.local_report["status"] == "verified"
        wizard.go(1)
        assert STEPS[5] in wizard.title.text()
        assert (
            not wizard.consent.isEnabled()
            and not wizard.preflight.isEnabled()
            and not wizard.forward.isEnabled()
        )
        window.log_search.setText("сВеРкА ИсХоДнОй")
        assert "Сверка исходной истории" in window.logs.toPlainText()
        assert "Подготовка и проверка пакета" not in window.logs.toPlainText()
        window.log_search.clear()
        assert "Подготовка и проверка пакета" in window.logs.toPlainText()
    finally:
        window.close()


def test_wizard_explicit_synthetic_package(tmp_path, monkeypatch):
    app = gui.application()
    monkeypatch.setattr(gui, "app_home", lambda: tmp_path)
    monkeypatch.setattr(telegram_ui, "vault_path", lambda: tmp_path / "owner.dpapi")
    monkeypatch.setattr(gui.QFileDialog, "getExistingDirectory", lambda *_: str(tmp_path))
    window = gui.Window()
    try:
        wizard = window.wizard
        wizard.choose_source("fixture")
        wizard.go(1)
        assert wizard.checked and wizard.forward.isEnabled()
        wizard.go(1)
        wizard.offline.setChecked(True)
        wizard.go(1)
        wizard.test_size.setCurrentIndex(wizard.test_size.findData(10))
        wizard.test_type.setCurrentIndex(wizard.test_type.findData("media"))
        wizard.go(1)
        wizard.build_package()
        finish_worker(app, window)
        assert wizard.reviewed.data["fixture"] == "media"
        assert wizard.reviewed.data["included"] == 10 and wizard.inventory["count"] == 1
        assert wizard.forward.isEnabled() and not window.telegram.start_button.isEnabled()
    finally:
        window.close()
