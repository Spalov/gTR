from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEventLoop, QSettings, QTimer
from PySide6.QtWidgets import QDialog

from gtr import gui
from gtr.i18n import set_language
from gtr.media_queue import MediaDecisionRequired
from gtr.media_queue_ui import CompatibilityDialog
from gtr.model import Cancelled


def entries(count=5):
    return [
        {
            "key": f"{index + 1:064x}", "group": "same-format", "name": f"clip-{index + 1}.mkv",
            "date": "2025-01-01", "size": 10_000, "source": "", "recommended": "compatible",
            "options": ("compatible", "document", "skip"), "reason": "transcode_required",
            "original": {"container": "video/x-matroska", "codec": "vp9", "width": 640, "height": 360},
        }
        for index in range(count)
    ]


def test_group_decision_and_batch_resolution():
    gui.application()
    tools = SimpleNamespace(ffmpeg=None, ffprobe=None)
    dialog = CompatibilityDialog(None, entries(), None, tools)
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert dialog.table.rowCount() == 5
    dialog.choices[0].setCurrentIndex(dialog.choices[0].findData("document"))
    dialog.apply_group()
    assert set(dialog.decisions().values()) == {"document"}
    dialog.batch()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert set(dialog.decisions().values()) == {"document"}


def wait_for(window, condition):
    loop = QEventLoop()
    timer = QTimer(interval=10)
    timer.timeout.connect(lambda: loop.quit() if condition() else None)
    timer.start()
    QTimer.singleShot(4000, loop.quit)
    loop.exec()
    assert condition(), window.outcome.text()


def test_waiting_resumes_in_place_and_choices_survive_restart(tmp_path, monkeypatch):
    gui.application()
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(gui, "QSettings", lambda *_: settings)
    monkeypatch.setattr(gui, "app_home", lambda: tmp_path)
    expected = entries()
    calls = []

    class Review(CompatibilityDialog):
        def exec(self):
            assert self.parent().stage.text() == "Ожидание решения пользователя"
            has_saved = len(calls) == 3
            assert bool(self.choices[0].currentData()) == has_saved
            if not has_saved:
                self.batch()
            else:
                self.accept()
            return self.result()

    def interrupted(progress, *, video_decisions, media_queue):
        calls.append(dict(video_decisions))
        if not video_decisions:
            raise MediaDecisionRequired(expected)
        raise Cancelled()

    def successful(progress, *, video_decisions, media_queue):
        calls.append(dict(video_decisions))
        if not video_decisions:
            raise MediaDecisionRequired(expected)
        return "Пакет готов"

    monkeypatch.setattr(gui, "CompatibilityDialog", Review)
    first = gui.Window()
    try:
        first.run_media_job("Подготовка пакета", interrupted, str, project=None)
        wait_for(first, lambda: len(calls) == 2 and first.worker is None)
        assert first.saved_video_decisions() == {entry["key"]: "compatible" for entry in expected}
    finally:
        first.close()
    resumed = gui.Window()
    try:
        resumed.run_media_job("Подготовка пакета", successful, str, project=None)
        wait_for(resumed, lambda: len(calls) == 4 and resumed.worker is None)
        assert calls[1] == calls[3]
        assert calls[0] == calls[2] == {}
        assert resumed.saved_video_decisions() == {}
    finally:
        resumed.close()


@pytest.mark.parametrize(
    ("locale", "wizard_start", "progress_text", "dialog_title", "automatic"),
    [
        ("ru", "Выбор архива", "Подготовка пакета", "Совместимость медиафайлов",
         "Автоматически подготавливать совместимые видео"),
        ("uk", "Вибір архіву", "Підготовка пакета", "Сумісність медіафайлів",
         "Автоматично готувати сумісні відео"),
    ],
)
def test_wizard_and_media_use_selected_language(tmp_path, monkeypatch, locale, wizard_start, progress_text, dialog_title, automatic):
    gui.application()
    settings = QSettings(str(tmp_path / "lang.ini"), QSettings.Format.IniFormat)
    settings.setValue("language", locale)
    monkeypatch.setattr(gui, "QSettings", lambda *_: settings)
    monkeypatch.setattr(gui, "app_home", lambda: tmp_path)
    window = gui.Window()
    try:
        assert wizard_start in window.wizard.title.text()
        control = window.media_auto_controls[0]
        assert control.text() == automatic
        assert window.effective_video_policy("original") == "original"
        control.setChecked(True)
        assert window.effective_video_policy("original") == "compatible"
        assert window.effective_video_policy("document") == "document"
        assert all(widget.isChecked() for widget in window.media_auto_controls)
        monkeypatch.setattr(gui, "cache_info", lambda: {"files": 7, "size": 16384})
        window.navigate_to(gui.Section.SETTINGS)
        assert "7" in window.media_cache_summary.text() and "16" in window.media_cache_summary.text()
        window.run_media_job("Подготовка пакета", lambda *_args, **_kw: "Пакет готов", str, project=None)
        wait_for(window, lambda: window.worker is None)
        assert window.job_title.text() == progress_text
        dialog = CompatibilityDialog(window, entries(1), None, SimpleNamespace(ffmpeg=None, ffprobe=None))
        assert dialog.windowTitle() == dialog_title
        if locale == "uk":
            assert "Скасувати" in dialog.cancel_button.text()
            assert window.telegram.state.text() == "Очікування пакета й облікового запису."
    finally:
        window.close()
        set_language("ru")
