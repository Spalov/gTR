"""All fixtures are invented locally. Networking is blocked for the full test session."""

import json
import os
import socket
import sys
from pathlib import Path

import pytest

from gtr.conversion import prepare
from gtr.core import analyze

os.environ["QT_QPA_PLATFORM"] = "offscreen"
_NETWORK = {
    "socket.connect",
    "socket.bind",
    "socket.sendto",
    "socket.sendmsg",
    "socket.getaddrinfo",
    "socket.gethostbyname",
    "socket.gethostbyaddr",
    "socket.getnameinfo",
}


def network_guard(event, args):
    if event in _NETWORK or (event == "socket.__new__" and args[1] in (socket.AF_INET, socket.AF_INET6)):
        raise AssertionError("Tests must not open a network socket or resolve a host")


sys.addaudithook(network_guard)


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-profile"))
    from PySide6.QtCore import QSettings

    from gtr import gui

    settings = QSettings(str(tmp_path / "gtr.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(gui, "QSettings", lambda *_: settings)


def message(sid, text="", **fields):
    return {
        "id": sid,
        "type": "message",
        "from": "Лея",
        "from_id": "user10",
        "date": "2025-01-02T10:04:05+02:00",
        "text": text,
        **fields,
    }


def write_json(root: Path, rows, **fields):
    root.mkdir(exist_ok=True)
    (root / "result.json").write_text(
        json.dumps(
            {"id": 42, "name": "Мастерская", "type": "personal_chat", "messages": rows, **fields},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def synthetic(tmp_path):
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QImage

    picture = QImage(32, 24, QImage.Format.Format_RGB32)
    picture.fill(0xFF406080)
    buffer = QBuffer()
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly) and picture.save(buffer, "JPEG")
    sticker = QImage(512, 256, QImage.Format.Format_RGBA8888)
    sticker.fill(0x80C06040)
    webp = QBuffer()
    assert webp.open(QIODevice.OpenModeFlag.WriteOnly) and sticker.save(webp, "WEBP", 100)
    root = tmp_path / "export"
    rows = [
        message(1, ["До ", {"type": "bold", "text": "встречи 👋"}, "\nв 12:00."]),
        message(
            2,
            "Договорились!",
            **{
                "from": "Мирон",
                "from_id": "user11",
                "reply_to_message_id": 1,
                "date": "2025-01-02T11:04:05+02:00",
            },
        ),
    ]
    files = [
        ("photo", "photo", "a.jpg", bytes(buffer.data())),
        ("video_file", "file", "v.mp4", b"\0\0\0\x18ftypisom000"),
        ("video_message", "file", "r.mp4", b"\0\0\0\x18ftypisom001"),
        ("voice_message", "file", "v.ogg", b"OggSdata"),
        ("sticker", "file", "s.webp", bytes(webp.data())),
        (
            "animation",
            "file",
            "a.gif",
            bytes.fromhex(
                "47494638396101000100800000000000ffffff21f90401000000002c000000000100010000020144003b"
            ),
        ),
        ("audio_file", "file", "a.mp3", b"ID3audio"),
        ("document", "file", "a.pdf", b"%PDF-1.7\n"),
    ]
    write_json(root, rows)
    media = root / "files"
    media.mkdir()
    for sid, (kind, field, filename, data) in enumerate(files, 3):
        (media / filename).write_bytes(data)
        rows.append(
            message(
                sid,
                "Подпись" if sid == 3 else "",
                media_type=kind,
                **{field: "files/" + filename, "file_size": len(data), "photo_file_size": len(data)},
                **({"sticker_emoji": "★"} if kind == "sticker" else {}),
            )
        )
    return write_json(root, rows)


@pytest.fixture
def project(tmp_path, synthetic):
    return analyze(synthetic, tmp_path / "project")


@pytest.fixture
def bundle(tmp_path, project):
    return prepare(project, 1, tmp_path / "bundle")
