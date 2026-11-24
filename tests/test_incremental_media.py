import json
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import message, write_json
from telethon import types
from test_transport_alpha4 import FakeTelegram

from gtr.conversion import load_bundle, prepare
from gtr.core import analyze, encode
from gtr.filenames import safe_filename
from gtr.fixtures import _png
from gtr.integrity import check_local
from gtr.media_engine import _orientation, inspect_bundle, public_inventory
from gtr.mock_api import offline_result
from gtr.model import RecoveryError
from gtr.mtproto import TelegramAPI, rpc_error
from gtr.profiles import read_records


@pytest.mark.parametrize("profile", ["ios", "android"])
def test_unicode_display_marker_file_parts_and_document_attributes(tmp_path, profile):
    rows = [
        message(n, "Вложение", file=f"{n}/local.png", **({"file_name": "Чертёж №1.png"} if n < 3 else {}))
        for n in (1, 2, 3)
    ]
    source = write_json(tmp_path / "source", rows)
    for r in rows:
        path = source / r["file"]
        path.parent.mkdir()
        path.write_bytes(_png())
    p = analyze(source, tmp_path / "project")
    bundle = load_bundle(prepare(p, 1, tmp_path / "out", "source", profile=profile).root)
    first, second, last = bundle.data["media"]
    assert [x["display_name"] for x in (first, second, last)] == [
        "Чертёж №1.png",
        "Чертёж №1.png",
        "local.png",
    ]
    assert first["name"] != second["name"] and first["filename_source"] == "export_metadata"
    assert last["filename_source"] == "export_basename"
    assert check_local(bundle, p)["status"] == "verified"
    with bundle.transcript.open(encoding="utf-8") as stream:
        assert [r[3] for r in read_records(stream, bundle.data["profile"])] == [
            x["name"] for x in bundle.data["media"]
        ]

    class Wire(FakeTelegram):
        def __init__(self):
            super().__init__()
            self.wire = []

        async def upload_file(self, path, file_name, progress_callback=None):
            size = Path(path).stat().st_size
            progress_callback(size, size)
            return await super().upload_file(path, file_name)

        async def __call__(self, request):
            self.wire.append(request)
            return await super().__call__(request)

    client = Wire()
    api = TelegramAPI(client, "pm")
    progress = []

    def stage(_):
        pass

    stage.upload_bytes = lambda *args: progress.append(args)
    for item in bundle.data["media"]:
        offline_result(api.upload_media_traced("target", 123, item, bundle.media_path(item), stage))
    assert client.files == [x["display_name"] for x in bundle.data["media"]]
    for item, request in zip(bundle.data["media"], client.wire):
        assert request.file_name == item["name"] and request.media.file.name == item["display_name"]
        assert request.media.attributes[0].file_name == item["display_name"]
        assert isinstance(request.media, types.InputMediaUploadedDocument)
    assert len(progress) == 3 and all(sent == size and speed >= 0 for _, sent, size, speed in progress)
    assert "Чертёж" not in json.dumps(public_inventory(inspect_bundle(bundle)), ensure_ascii=False)
    first["display_name"] = "../обход.png"
    (bundle.root / "manifest.json").write_text(encode(bundle.data), encoding="utf-8")
    with pytest.raises(RecoveryError):
        load_bundle(bundle.root)


@pytest.mark.parametrize(
    "original",
    [
        "../../CON.pdf",
        "AUX",
        "..\\папка\\не?:годно.pdf",
        "a\u202eb.pdf",
        "🟦" * 170 + ".pdf",
        "отчёт\u2028новая строка.pdf",
    ],
)
def test_windows_filename_safety_and_utf8_budget(original):
    name = safe_filename(original)
    assert name and name == safe_filename(name) and len(name.encode()) <= 180
    assert not set('<>:"/\\|?*\u202e\u2028').intersection(name)
    assert name.split(".")[0] not in ("AUX", "CON")


def test_legacy_upload_keeps_existing_marker_but_requests_export_basename():
    api = TelegramAPI(SimpleNamespace(), "pm")
    legacy = {
        "name": "gtr-0000001.pdf",
        "source": "files/Квитанция.pdf",
        "kind": "document",
        "upload_as": "document",
        "mime": "application/pdf",
    }
    media = api.input_media(types.InputFile(1, 1, "Квитанция.pdf", ""), legacy)
    assert media.attributes[0].file_name == "Квитанция.pdf"


def test_bounded_exif_orientation_and_real_server_wait():
    tiff = b"II*\0" + struct.pack("<IHHHIH", 8, 1, 274, 3, 1, 6) + bytes(6)
    body = b"Exif\0\0" + tiff
    head = b"\xff\xd8\xff\xe1" + struct.pack(">H", len(body) + 2) + body
    assert _orientation(head) == 6 and _orientation(head[:12]) is None

    class PreviousChatImportActiveWaitMinError(Exception):
        minutes = 7

    assert rpc_error(PreviousChatImportActiveWaitMinError()).seconds == 420
    assert (
        rpc_error(type("ActiveWait", (Exception,), {"message": "PREVIOUS_CHAT_IMPORT_ACTIVE_WAIT"})()).seconds
        == 0
    )


def test_html_document_title_is_preferred_to_export_basename(tmp_path):
    source = tmp_path / "export"
    source.mkdir()
    (source / "local.png").write_bytes(_png())
    (source / "messages.html").write_text(
        """
    <div class="history"><div class="message default" id="message42"><div class="body">
    <div class="pull_right date details" title="21.11.2026 12:00:00 UTC+02:00"></div>
    <div class="from_name">Лея</div><div class="media_wrap"><a class="media_file" href="local.png">
    <div class="title bold">Расписание.png</div></a></div><div class="text">Приложение</div>
    </div></div></div>""",
        encoding="utf-8",
    )
    p = analyze(source, tmp_path / "project")
    bundle = prepare(p, 1, tmp_path / "html", "source")
    assert bundle.data["media"][0]["display_name"] == "Расписание.png"
    assert bundle.data["media"][0]["filename_source"] == "export_metadata"
