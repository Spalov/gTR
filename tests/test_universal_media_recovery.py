"""Offline regressions for local planning, Telegram media semantics and staged repair."""

import gzip
import json
import struct
import subprocess
from datetime import UTC, datetime, timedelta

import pytest
from conftest import message, write_json
from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QColor, QImage
from telethon import errors, functions, types

from gtr.bindings import requested_kind
from gtr.conversion import load_bundle, plan_import, prepare, prepare_eligible
from gtr.core import analyze
from gtr.faults import ImportFault
from gtr.fixtures import _png
from gtr.importer import ImportController
from gtr.integrity import check_local
from gtr.journal import Journal, RecoveryRequired, State
from gtr.media_queue import MediaDecisionRequired
from gtr.media_recovery import observed_media_kind, server_issue
from gtr.mock_api import MockAPI, offline_result
from gtr.mtproto import TelegramAPI
from gtr.verification import compare
from gtr.video import MediaTools


def webp(*, alpha=True, size=14016):
    image = QImage(512, 256, QImage.Format.Format_RGBA8888)
    image.fill(QColor(48, 98, 159, 90 if alpha else 255))
    output = QBuffer()
    assert output.open(QIODevice.OpenModeFlag.WriteOnly) and image.save(output, "WEBP", 100)
    data = bytes(output.data())
    padding = size - len(data) - 8
    assert padding >= 0 and padding % 2 == 0
    return b"RIFF" + struct.pack("<I", size - 8) + data[8:] + b"JUNK" + struct.pack("<I", padding) + bytes(padding)


def sticker(name, index):
    return message(index, f"Синтетический стикер {index}", file=name, media_type="sticker")


def project_with(tmp_path, messages, files):
    source = write_json(tmp_path / "export", messages)
    for name, data in files.items():
        (source / name).write_bytes(data)
    return analyze(source, tmp_path / "project")


class TelegramUpload:
    """Return TL objects for the declared type, or a controlled conflicting response."""

    def __init__(self, responses=()):
        self.requests = []
        self.responses = iter(responses)

    async def upload_file(self, path, file_name, **_):
        return types.InputFile(1, 1, file_name, "")

    async def __call__(self, request):
        assert isinstance(request, functions.messages.UploadImportedMediaRequest)
        assert request.file_name == request.media.file.name
        self.requests.append(request)
        attrs = request.media.attributes
        outcome = next(self.responses, "matching")
        if outcome == "reject":
            raise errors.MediaInvalidError(request)
        if outcome == "document":
            attrs = [types.DocumentAttributeFilename("file.bin")]
        return types.MessageMediaDocument(document=types.Document(
            1, 1, b"", datetime.now(UTC), request.media.mime_type, 1, 1, attrs,
        ))


class HistoryWithTelegramUpload(MockAPI):
    def __init__(self, client):
        super().__init__()
        self.transport = TelegramAPI(client, "pm")

    async def upload_media_traced(self, peer, import_id, item, path, stage):
        result = await self.transport.upload_media_traced(peer, import_id, item, path, stage)
        await self.upload_media(peer, import_id, item, path)
        return result


def test_html_sticker_without_emoji_gets_native_type_and_wire_alias(tmp_path):
    source = tmp_path / "html"
    source.mkdir()
    (source / "messages.html").write_text(
        '<div class="page_header"><div class="content"><div class="text bold">Диалог</div></div></div>'
        '<div class="history"><div class="message default clearfix" id="message1">'
        '<div class="body"><div class="pull_right date details" title="02.01.2025 10:04:05 UTC+02:00">10:04</div>'
        '<div class="from_name">Автор</div><div class="media_wrap clearfix">'
        '<a class="sticker_wrap clearfix pull_left" href="sticker.webp">'
        '<img class="sticker" src="sticker.webp"></a></div></div></div></div>',
        encoding="utf-8",
    )
    data = webp()
    assert len(data) == 14016
    (source / "sticker.webp").write_bytes(data)
    project = analyze(source, tmp_path / "project")
    bundle = prepare(project, 1, tmp_path / "bundle", "source", video_policy="original")
    (item,) = bundle.data["media"]
    assert item["kind"] == "sticker" and item["size"] == 14016 and item["mime"] == "image/webp"
    assert item["attributes"] == {"type": "sticker", "emoji": ""}
    assert item["input_media"] == "InputMediaUploadedDocument" and not item["force_file"]
    assert "-STICKER-" in item["wire_name"]
    assert check_local(load_bundle(bundle.root), project)["status"] == "verified"
    client = TelegramUpload()
    api = TelegramAPI(client, "pm")
    assert offline_result(api.upload_media(types.InputPeerSelf(), 1, item, bundle.media_path(item))) == "MessageMediaDocument"
    assert {type(attr) for attr in client.requests[0].media.attributes} == {
        types.DocumentAttributeFilename, types.DocumentAttributeSticker,
    }


def test_all_312_records_and_218_files_preflight_and_keep_order_and_captions(tmp_path):
    files = {"one.webp": webp(), "two.webp": webp(), "three.webp": webp(alpha=False), "four.webp": webp()}
    rows = [
        sticker("one.webp", 1), message(2, "Прозрачная подпись", photo="two.webp"),
        message(3, "Обычное фото", photo="three.webp"), message(4, "Оригинал как файл", file="four.webp"),
    ]
    for index in range(5, 313):
        media = {}
        if index <= 218:
            key = f"attachment-{index}.pdf"
            files[key] = b"%PDF-1.7\n" + str(index).encode()
            media = {"file": key}
        rows.append(message(index, f"Контрольная запись {index}", **media))
    for index, row in enumerate(rows):
        row["date"] = (datetime(2025, 1, 2, tzinfo=UTC) + timedelta(seconds=index // 2)).isoformat()
        if index % 2:
            row["from"] = "Другой автор"
            row["from_id"] = "user11"
    project = project_with(tmp_path, rows, files)
    preview = plan_import(project, 1, "source", video_policy="original")
    pending = preview["media_decisions_required"]
    assert len(pending) == 2 and pending[0]["group"] == pending[1]["group"]
    assert all(x["source_kind"] == "webp_image" and x["recommended"] == "image" for x in pending)
    dest = tmp_path / "bundle"
    with pytest.raises(MediaDecisionRequired) as failure:
        prepare(project, 1, dest, "source", video_policy="original")
    assert len(failure.value.entries) == 2 and not dest.exists()
    selected = {entry["key"]: "image" for entry in pending}
    bundle = prepare(project, 1, dest, "source", video_policy="original", video_decisions=selected)
    bundle = load_bundle(bundle.root)
    first, transparent, opaque, explicit = bundle.data["media"][:4]
    assert transparent["mime"] == opaque["mime"] == "image/png"
    assert transparent["force_file"] and requested_kind(transparent) == "document"
    assert not opaque["force_file"] and requested_kind(opaque) == "photo"
    recovered = QImage(str(bundle.media_path(transparent)))
    assert (recovered.width(), recovered.height(), recovered.pixelColor(0, 0).alpha()) == (512, 256, 90)
    assert explicit["force_file"] and requested_kind(explicit) == "document" and explicit["mime"] == "image/webp"
    assert bundle.media_path(explicit).read_bytes() == files["four.webp"]
    assert requested_kind(first) == "sticker"
    assert [int(row["source_id"]) for row in bundle.data["records"]] == list(range(1, 313))
    assert [row["author"] for row in bundle.data["records"]] == [row["from"] for row in rows]
    assert bundle.data["included"] == 312 and bundle.data["media_count"] == 218
    assert check_local(bundle, project)["status"] == "verified"
    api = MockAPI()
    controller = ImportController(api, "test", mode="owner")
    lease = offline_result(controller.validate(bundle))
    assert len(controller.media_facts) == 218 and not controller.media_issues
    offline_result(controller.execute(lease, approved=True))
    a, b, map_ = api.observations(bundle)
    result = compare(bundle, a, b, map_, project=project)
    assert api.uploaded == api.expected and len(api.uploaded) == 218
    assert result["unresolved_attachments"] == 0
    assert all("<attached:" not in body for _, _, body, _ in api.content)


def test_tgs_is_prepared_as_sticker(tmp_path):
    value = {"v": "5.7.4", "tgs": 1, "w": 512, "h": 512, "fr": 60, "ip": 0, "op": 60, "layers": []}
    project = project_with(tmp_path, [sticker("paint.tgs", 1)], {"paint.tgs": gzip.compress(json.dumps(value).encode())})
    bundle = prepare(project, 1, tmp_path / "bundle", "source", video_policy="original")
    (item,) = bundle.data["media"]
    assert bundle.data["records"][0]["media_recovery"]["source_kind"] == "tgs_sticker"
    assert item["mime"] == "application/x-tgsticker" and requested_kind(item) == "sticker" and not item["force_file"]


def test_webm_vp9_is_prepared_as_sticker_without_video_conversion(tmp_path):
    tools = MediaTools()
    if not tools.ffmpeg or not tools.ffprobe:
        pytest.skip("FFmpeg and FFprobe required for the WebM fixture")
    source = tmp_path / "synthetic.webm"
    subprocess.run([
        str(tools.ffmpeg), "-v", "error", "-f", "lavfi", "-i", "color=s=512x256:r=15:d=0.4",
        "-an", "-c:v", "libvpx-vp9", "-threads", "1", str(source),
    ], check=True, timeout=30, capture_output=True)
    original = source.read_bytes()
    project = project_with(tmp_path, [sticker("motion.webm", 1)], {"motion.webm": original})
    bundle = prepare(project, 1, tmp_path / "bundle", "source", video_policy="original", media_tools=tools)
    (item,) = bundle.data["media"]
    assert bundle.data["records"][0]["media_recovery"]["source_kind"] == "webm_sticker"
    assert item["mime"] == "video/webm" and requested_kind(item) == "sticker"
    assert not item.get("transform") and bundle.media_path(item).read_bytes() == original


@pytest.mark.parametrize(("attribute", "kind"), [
    (types.DocumentAttributeSticker("", types.InputStickerSetEmpty()), "sticker"),
    (types.DocumentAttributeVideo(0.4, 320, 240), "video"),
    (types.DocumentAttributeVideo(0.4, 320, 320, round_message=True), "round_video"),
    (types.DocumentAttributeAudio(6), "audio"),
    (types.DocumentAttributeAudio(6, voice=True), "voice"),
    (types.DocumentAttributeAnimated(), "animation"),
    (types.DocumentAttributeFilename("test.webp"), "document"),
])
def test_returned_document_semantics_require_attributes(attribute, kind):
    media = types.MessageMediaDocument(document=types.Document(
        1, 1, b"", datetime.now(UTC), "image/webp", 1, 1, [attribute],
    ))
    assert observed_media_kind(media) == kind


def test_legacy_packet_collects_conflicts_before_telegram_and_repairs_only_requested_files(tmp_path, monkeypatch):
    rows = [message(1, "Фото", photo="one.png"), sticker("sticker.webp", 2),
            message(3, "WebP", photo="image.webp")]
    files = {"sticker.webp": webp(), "image.webp": webp(), "one.png": _png()}
    project = project_with(tmp_path, rows, files)
    old = prepare(project, 1, tmp_path / "old", "source", video_policy="original", media_workflow=False)
    assert requested_kind(old.data["media"][1]) == "document"
    api = MockAPI()
    journal = Journal(tmp_path / "history.sqlite", "test")
    controller = ImportController(api, "test", mode="owner", journal=journal)
    with pytest.raises(ImportFault) as rejected:
        offline_result(controller.validate(old))
    assert api.calls == [] and rejected.value.code == "MEDIA_LOCAL_INVALID"
    assert len(rejected.value.media_issues) == 2
    assert len(journal.diagnostic_report(controller.job)["media_attempts"]) == 2
    choices = {entry["key"]: entry["recommended"] for entry in rejected.value.media_issues}
    from gtr import conversion

    publish = conversion.publish_media

    def affected_only(path, item, destination, progress):
        assert item["source"] != "one.png"
        publish(path, item, destination, progress)

    monkeypatch.setattr(conversion, "publish_media", affected_only)
    fixed = prepare_eligible(project, old, tmp_path / "fixed", old.record_intervals(),
                             video_decisions=choices, repair=True)
    assert check_local(load_bundle(fixed.root), project)["status"] == "verified"
    assert fixed.data["media"][0] == old.data["media"][0]
    assert requested_kind(fixed.data["media"][1]) == "sticker"
    assert "-STICKER-" in fixed.data["media"][1]["wire_name"]
    assert fixed.data["media"][2]["mime"] == "image/png"


@pytest.mark.parametrize(("response", "fault"), [("reject", "MEDIA_INVALID"), ("document", "MEDIA_TYPE_CHANGED")])
def test_server_conflict_leaves_staged_operation_and_needs_explicit_new_package(tmp_path, response, fault):
    rows = [message(1, "Первый файл", file="hello.pdf"), sticker("shine.webp", 2)]
    project = project_with(tmp_path, rows, {"hello.pdf": b"%PDF-1.7\n", "shine.webp": webp()})
    bundle = prepare(project, 1, tmp_path / "first", "source", video_policy="original")
    journal = Journal(tmp_path / "registry.sqlite", "test")
    first_api = HistoryWithTelegramUpload(TelegramUpload(("matching", response)))
    controller = ImportController(first_api, types.InputPeerSelf(), mode="owner", journal=journal)
    lease = offline_result(controller.validate(bundle))
    with pytest.raises(ImportFault) as failed:
        offline_result(controller.execute(lease, approved=True))
    assert failed.value.code == fault and "start_import" not in first_api.calls
    report = journal.diagnostic_report(controller.job, private=True)
    assert report["operation"]["state"] == State.STAGED_NOT_STARTED
    assert report["operation"]["start_request_sent"] is False and report["operation"]["media_uploaded_successfully"] == 1
    accepted, attempt = report["media_attempts"]
    assert accepted["status"] == "MEDIA_ACCEPTED" and attempt["status"] == "MEDIA_REJECTED"
    if response == "document":
        assert attempt["requested_kind"] == "sticker" and attempt["received_kind"] == "document"
        assert attempt["received_attributes"] == ["DocumentAttributeFilename"]
    issue = server_issue(bundle, attempt)
    assert issue["source_kind"] == "webp_sticker" and issue["prepared_kind"] == "sticker"
    revised = prepare_eligible(project, bundle, tmp_path / "repaired", bundle.record_intervals(),
                               video_decisions={issue["key"]: "document"}, repair=True)
    assert revised.data["media"][1]["force_file"] and not revised.data["media"][1]["attributes"]
    assert revised.record_intervals() == bundle.record_intervals()
    retry_api = HistoryWithTelegramUpload(TelegramUpload())
    retry = ImportController(retry_api, types.InputPeerSelf(), mode="owner", journal=journal)
    with pytest.raises(RecoveryRequired):
        offline_result(retry.validate(revised))
    assert retry_api.calls == []
    journal.owner_disconnected(controller.job)
    journal.recover_before_start(controller.job, acknowledged=True)
    retry = ImportController(retry_api, types.InputPeerSelf(), mode="owner", journal=journal)
    lease = offline_result(retry.validate(revised))
    assert "start_import" not in retry_api.calls
    offline_result(retry.execute(lease, approved=True))
    assert retry_api.calls.count("start_import") == 1
    assert journal.diagnostic_report(controller.job, private=True)["media_attempts"] == [accepted, attempt]
