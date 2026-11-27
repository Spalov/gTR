import json

from conftest import message, write_json
from PySide6.QtCore import QDate, QEventLoop, QSettings, QTimer, QUrl

from gtr import gui
from gtr.cli import main
from gtr.conversion import plan_import
from gtr.core import analyze


def test_cli_complete_offline_flow(tmp_path, synthetic, capsys):
    p, b = tmp_path / "cli-project", tmp_path / "cli-bundle"
    assert main(["analyze", str(synthetic), "--project", str(p)]) == 0
    assert json.loads(capsys.readouterr().out)["messages"] == 10
    assert main(["chats", str(p)]) == 0
    assert json.loads(capsys.readouterr().out)[0]["id"] == 1
    assert main(["verify", str(p)]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": 9}
    command = ["prepare", str(p), "--chat", "1", "--out", str(b), "--video-policy", "document"]
    assert main(command) == 2
    pending = json.loads(capsys.readouterr().err)["media_decisions_required"]
    assert {item["source_kind"] for item in pending} == {"voice", "audio"}
    choices = tmp_path / "attachment-choices.json"
    choices.write_text(json.dumps({
        item["key"]: "skip" if item["source_kind"] == "voice" else "document" for item in pending
    }), encoding="utf-8")
    assert main([*command, "--media-decisions", str(choices)]) == 0
    assert json.loads(capsys.readouterr().out)["media_count"] == 7
    assert main(["simulate", str(b)]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "mock"
    assert main(["simulate", str(b), "--scenario", "flood"]) == 2
    output = capsys.readouterr()
    assert "30" in output.err and "Договорились" not in output.err
    assert main(["live", str(b), "--peer", "@synthetic"]) == 2
    assert "интерактивном" in capsys.readouterr().err
    assert main(["baseline", str(b), "--peer", "@synthetic"]) == 2
    assert (
        main(
            [
                "observe",
                str(b),
                "--peer",
                "@synthetic",
                "--after-id",
                "0",
                "--out",
                str(tmp_path / "view.json"),
            ]
        )
        == 2
    )
    assert not (tmp_path / "view.json").exists()


def test_viewer_search_sides_reply_navigation_and_worker(tmp_path, monkeypatch):
    rows = [
        message(
            i,
            f"Строка {i}",
            **{"from": "Лея" if i % 2 else "Мирон", "from_id": "user1" if i % 2 else "user2"},
        )
        for i in range(1, 86)
    ]
    rows[-1]["reply_to_message_id"] = 1
    project = analyze(write_json(tmp_path / "src", rows), tmp_path / "project")
    app = gui.application()
    monkeypatch.setattr(
        gui, "QSettings", lambda *_: QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    )
    monkeypatch.setattr(gui, "app_home", lambda: tmp_path)
    window = gui.Window(project)
    window.show()
    app.processEvents()
    try:
        assert window.stack.count() == len(gui.SECTIONS) and window.overview.values[0].text() == "85"
        viewer = window.viewer
        window.nav.setCurrentRow(2)
        assert viewer.side.count() == 2 and viewer.next.isEnabled()
        first = viewer.browser.toHtml()
        viewer.side.setCurrentIndex(1)
        assert viewer.browser.toHtml() != first
        viewer.search.setText("сТрОкА 85")
        assert "Строка 85" in viewer.browser.toPlainText() and not viewer.next.isEnabled()
        viewer.follow(QUrl("gtrreply:1"))
        assert not viewer.search.text() and viewer.offset == 0 and "Строка 1" in viewer.browser.toPlainText()
        viewer.move(40)
        assert viewer.prev.isEnabled() and "Строка 41" in viewer.browser.toPlainText()
        viewer.day.setDate(QDate(2025, 1, 2))
        viewer.jump_day()
        assert viewer.offset == 0
        viewer.kind.setCurrentIndex(viewer.kind.findData("photo"))
        viewer.jump_day()
        assert "сообщений нет" in viewer.status.text()
        assert window.participants.grid.rowCount() == 2
        window.verify_project()
        loop = QEventLoop()
        window.worker.finished.connect(loop.quit)
        QTimer.singleShot(15000, loop.quit)
        loop.exec()
        app.processEvents()
        assert window.stage.text() == "Готово"
        assert "SHA-256" in window.outcome.text()
        assert all(private not in window.logs.toPlainText() for private in ("Строка", "Мирон", "Лея"))
    finally:
        window.close()


def test_welcome_requires_project_selection_and_media_library(tmp_path, monkeypatch, project):
    app = gui.application()
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    settings.setValue("recent", str(project))
    monkeypatch.setattr(gui, "QSettings", lambda *_: settings)
    monkeypatch.setattr(gui, "app_home", lambda: tmp_path)
    window = gui.Window()
    window.show()
    app.processEvents()
    try:
        assert window.project is None and window.overview.welcome.isVisible()
        window.open_recent()
        assert window.project == project and not window.overview.welcome.isVisible()
        window.navigate_to(gui.Section.MEDIA)
        assert window.media.grid.rowCount() == 8
        window.media.kind.setCurrentIndex(window.media.kind.findData("video"))
        assert window.media.grid.rowCount() == 1
        window.media.locate_current()
        assert window.stack.currentIndex() == gui.Section.CHAT
        window.resize(930, 650)
        window.navigate_to(gui.Section.IMPORT)
        window.show_plan(plan_import(project, 1), True)
        app.processEvents()
        assert window.import_profile.height() >= 24
    finally:
        window.close()


def test_fresh_profile_can_open_export_guide_and_load_its_first_archive(tmp_path, monkeypatch, synthetic):
    from PySide6.QtWidgets import QPushButton, QTextBrowser

    app = gui.application()
    settings = QSettings(str(tmp_path / "fresh.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(gui, "QSettings", lambda *_: settings)
    guide_opened = []

    def check_guide(dialog):
        guide = dialog.findChild(QTextBrowser)
        assert "Экспорт истории чата" in guide.toPlainText()
        assert "Выбрать папку" in guide.toPlainText()
        guide_opened.append(True)

    monkeypatch.setattr(gui.QDialog, "exec", check_guide)
    window = gui.Window()
    window.show()
    try:
        assert window.project is None and window.projects.count() == 0
        assert not settings.contains("recent")
        assert window.telegram.account is None
        assert not window.telegram.phone.text()
        assert not window.telegram.api_id.text() and not window.telegram.api_hash.text()
        assert not window.telegram.use_saved_api.isChecked() and not window.telegram.use_saved.isEnabled()

        instructions = next(
            button for button in window.overview.findChildren(QPushButton) if "Как подготовить архив" in button.text()
        )
        instructions.click()
        assert guide_opened

        window.navigate_to(gui.Section.PROFILES)
        assert not window.profiles_page.profiles
        window.navigate_to(gui.Section.HISTORY)
        assert window.history_page.table.rowCount() == 0

        window.ingest(synthetic)
        loop = QEventLoop()
        window.worker.finished.connect(loop.quit)
        QTimer.singleShot(15_000, loop.quit)
        loop.exec()
        app.processEvents()
        assert window.stats["messages"] == 10
        assert window.project.is_dir() and window.projects.count() == 1
    finally:
        window.close()
