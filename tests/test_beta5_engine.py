import json
import subprocess
from datetime import UTC, datetime
from hashlib import sha256

import pytest
from conftest import message, write_json
from telethon import functions, types

from gtr.account import Account, AccountManager
from gtr.conversion import load_bundle, plan_import, prepare
from gtr.core import analyze, catalogue, summary
from gtr.export_json import fields
from gtr.faults import ImportFault
from gtr.fidelity import (
    ERROR,
    PARTIAL,
    PREPARED,
    UNSUPPORTED,
    fidelity,
    public_fidelity,
)
from gtr.integrity import check_local
from gtr.mock_api import offline_result
from gtr.model import Cancelled, RecoveryError
from gtr.mtproto import TelegramAPI
from gtr.structure import forward_from_meta
from gtr.video import MediaTools, interpret_probe, observed_media_kind


def generate(tool, *args):
    subprocess.run(
        [str(tool), "-nostdin", "-v", "error", "-n", *map(str, args)],
        stdin=subprocess.DEVNULL,
        check=True,
        timeout=30,
    )


@pytest.fixture
def media_export(tmp_path):
    tools = MediaTools()
    if not tools.ffmpeg or not tools.ffprobe:
        pytest.skip("Local FFmpeg and FFprobe are required for real-media regression tests")
    root = tmp_path / "export"
    root.mkdir()
    for name, encoder in (("native.mp4", "libx264"), ("needs_copy.mkv", "mpeg4")):
        generate(
            tools.ffmpeg, "-f", "lavfi", "-i", "color=c=blue:s=96x96:r=20:d=0.5",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5", "-c:v", encoder,
            "-c:a", "aac", "-pix_fmt", "yuv420p", "-threads", "1", "-shortest", root / name,
        )
    generate(tools.ffmpeg, "-f", "lavfi", "-i", "sine=frequency=440:duration=0.3",
             "-c:a", "libopus", root / "voice.ogg")
    root.joinpath("broken.mp4").write_bytes(b"\0\0\0\x18ftypisomdead")
    rows = [
        message(1, "Первый кадр", media_type="video_file", file="native.mp4"),
        message(2, "Копия", media_type="video_file", file="needs_copy.mkv"),
        message(3, "Архивный документ", file="native.mp4"),
        message(4, "Круг", media_type="video_message", file="native.mp4"),
        message(5, "Голос", media_type="voice_message", file="voice.ogg"),
        message(6, "Сохраняемый текст", media_type="video_file", file="broken.mp4"),
        message(7, "Подпись без файла", file="missing.pdf"),
    ]
    return write_json(root, rows), tools


def test_beta5_detects_video_needing_a_decision_without_publishing(tmp_path, media_export):
    source, tools = media_export
    project = analyze(source, tmp_path / "project")
    opts = {"video_policy": "original", "media_tools": tools}
    plan = plan_import(project, 1, **opts)
    records = plan["records"]
    assert records[0]["video_recovery"]["original"]["decode"] == "sample_ok"
    assert records[0]["video_recovery"]["action"] == "keep"
    assert records[1]["video_recovery"]["original"]["status"] == "transcode_required"
    assert records[3]["video_recovery"]["output"]["playable"]
    assert records[4]["audio_recovery"]["attributes"]["voice"] is True
    assert records[5]["video_recovery"]["original"]["status"] in {"corrupt_container", "probe_failed"}
    assert records[6]["included"] and not records[6].get("media")
    dest = tmp_path / "blocked"
    with pytest.raises(RecoveryError, match="выберите совместимую копию"):
        prepare(project, 1, dest, **opts)
    assert not dest.exists()


def test_beta5_prepares_h264_retains_original_and_does_not_upgrade_documents(tmp_path, media_export):
    source, tools = media_export
    project = analyze(source, tmp_path / "project")
    before = (source / "needs_copy.mkv").read_bytes()
    bundle = prepare(project, 1, tmp_path / "bundle", first_record=1, last_record=5,
                     video_policy="compatible", media_tools=tools)
    validated = load_bundle(bundle.root)
    native, converted, document, circle, voice = validated.data["media"]
    assert native["attributes"]["video_codec"] == "h264"
    assert validated.media_path(native).read_bytes() == (source / "native.mp4").read_bytes()
    assert document["attributes"] == {} and document["kind"] == "document"
    assert circle["attributes"]["round"] is True and voice["attributes"]["voice"] is True
    assert converted["attributes"]["supports_streaming"] is True
    assert converted["transform"]["original_sha256"] == sha256(before).hexdigest()
    assert converted["sha256"] != converted["transform"]["original_sha256"]
    assert (source / "needs_copy.mkv").read_bytes() == before
    assert check_local(validated, project)["status"] == "verified"
    assert fidelity(bundle.data)["records"][1]["fields"]["attachments"] == PARTIAL


def test_rotation_and_dimensions_are_probed_without_guessing():
    streams = [{
        "codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p",
        "width": 240, "height": 400, "duration": "0.5", "avg_frame_rate": "30000/1001",
        "side_data_list": [{"rotation": -90}],
    }]
    result = interpret_probe({"streams": streams}, "video/mp4", False, "sample_ok")
    assert (result.width, result.height, result.rotation) == (240, 400, 270)
    assert result.fps == pytest.approx(29.97, abs=0.001)
    assert result.attributes("video")["supports_streaming"] is False
    assert not interpret_probe({"streams": streams}, "video/mp4", kind="round_video").playable


def test_media_returned_as_file_is_distinguished_from_video(tmp_path):
    real = types.Document(
        id=54, access_hash=55, file_reference=b"x", date=datetime(2025, 7, 8, tzinfo=UTC),
        mime_type="video/mp4", size=10, dc_id=2, attributes=[types.DocumentAttributeFilename("clip.mp4")],
    )

    class Client:
        async def upload_file(self, *_args, **_kwargs):
            return types.InputFile(1, 1, "clip.mp4", "")

        async def __call__(self, request):
            assert isinstance(request, functions.messages.UploadImportedMediaRequest)
            video = next(a for a in request.media.attributes if isinstance(a, types.DocumentAttributeVideo))
            assert video.nosound and video.video_codec == "h264" and video.supports_streaming
            assert request.media.nosound_video and not request.media.force_file
            return types.MessageMediaDocument(document=real)

    path = tmp_path / "clip.mp4"
    path.write_bytes(b"mp4")
    item = {
        "upload_as": "document", "mime": "video/mp4", "kind": "video", "name": path.name,
        "attributes": {"type": "video", "duration": 1, "width": 96, "height": 96,
                       "round": False, "nosound": True, "supports_streaming": True, "video_codec": "h264"},
    }
    received = []

    def stage(_):
        pass

    stage.response = received.append
    with pytest.raises(ImportFault) as failure:
        offline_result(TelegramAPI(Client(), "pm").upload_media_traced(types.InputPeerSelf(), 1, item, path, stage))
    assert failure.value.code == "MEDIA_TYPE_CHANGED"
    assert received == ["document"]
    real.attributes.append(types.DocumentAttributeVideo(1, 96, 96, round_message=True))
    assert observed_media_kind(types.MessageMediaDocument(document=real)) == "round_video"


def test_forward_annotations_exclude_hidden_ids_and_current_contact_names(tmp_path):
    source = write_json(tmp_path / "src", [
        message(1, "Текст\n👌", forwarded_from="Подпись [история]", forwarded_from_id="user9999987",
                forwarded_from_hidden=True, reply_to_message_id=1000, reply_to_quote="<текст>",
                grouped_id=119, edited="2025-01-03T08:04:50+02:00", reactions=[{"emoji": "👌", "count": 1}]),
        message(2, "Элемент", forwarded_from="Подпись [история]", forwarded_from_id="user32", grouped_id=119),
        message(3, "Канал", forwarded_from_id="channel44"),
        message(4, "Без метаданных", forward_info={"unrecognized": True}),
        message(5, "Без пересылки"),
    ])
    project = analyze(source, tmp_path / "project")
    with catalogue(project) as db:
        hidden, open_user, channel, unknown, direct = [
            forward_from_meta(json.loads(row[0])) for row in db.execute("SELECT meta FROM messages ORDER BY id")
        ]
    assert hidden.privacy_state == "hidden" and hidden.user_id is None
    assert not hidden.resolve({9999987: ("Частное имя", "contacts")}).resolved_display_name
    assert open_user.resolve({32: ("Частное имя", "contacts")}).resolved_display_name == "Частное имя"
    assert open_user.resolve({}).privacy_state == "unavailable"
    assert channel.original_peer_type == "channel" and channel.user_id is None
    assert unknown.is_forwarded and unknown.attribution_confidence == "unknown"
    assert not direct.is_forwarded
    plain = prepare(project, 1, tmp_path / "plain", media_mode="text_only")
    annotated = prepare(project, 1, tmp_path / "annotated", media_mode="text_only", annotate=True)
    assert "Историческая пометка" not in plain.transcript.read_text(encoding="utf-8")
    wire = annotated.transcript.read_text(encoding="utf-8")
    assert "Историческая пометка" in wire and "Историческая цитата" in wire
    assert "9999987" not in wire and "Частное имя" not in wire and "👌" in wire
    assert "9999987" not in (annotated.root / "manifest.json").read_text(encoding="utf-8")
    assert check_local(load_bundle(annotated.root), project)["status"] == "verified"
    report = fidelity(plain.data)
    assert report["records"][0]["fields"]["forward"] == UNSUPPORTED
    assert report["records"][0]["fields"]["text"] == PREPARED
    assert report["records"][0]["fields"]["reactions"] == UNSUPPORTED
    assert report["quality"]["index"] < 100
    redacted = public_fidelity(report)["records"][0]
    assert "source_id" not in redacted and "historical_date" not in redacted


def test_resolved_names_use_only_exact_verified_user_id():
    looked_up = []

    class Client:
        async def __call__(self, request):
            assert isinstance(request, functions.contacts.GetContactsRequest)
            return types.contacts.Contacts(
                contacts=[types.Contact(12, False)], saved_count=1,
                users=[types.User(12, first_name="Сохранённый контакт"), types.User(30, first_name="Другое лицо")],
            )

        async def get_entity(self, peer):
            assert isinstance(peer, types.PeerUser)
            looked_up.append(peer.user_id)
            return types.User(13 if peer.user_id == 13 else 30, first_name="Публичное имя")

    manager = AccountManager(timed=False)
    manager.client = Client()
    manager.account = Account(1, "Тест")
    result = offline_result(manager.forward_names([12, 13, 14, "old name", False]))
    assert result == {12: ("Сохранённый контакт", "contacts"), 13: ("Публичное имя", "public")}
    assert looked_up == [13, 14]


def test_dual_format_date_conflict_is_local_diagnostic(tmp_path):
    source = write_json(tmp_path / "src", [message(1, "Совпадающий текст", forwarded_from="Имя")])
    (source / "messages.html").write_text("""
        <div class="page_header"><div class="text">Мастерская</div></div>
        <div class="history"><div class="message default" id="message1"><div class="body">
        <div class="date details" title="02.01.2025 10:04:05 UTC+03:00">10:04</div>
        <div class="from_name">Лея</div><div class="text">Совпадающий текст</div></div></div></div>
    """, encoding="utf-8")
    project = analyze(source, tmp_path / "project")
    with catalogue(project) as db:
        meta = json.loads(db.execute("SELECT meta FROM messages").fetchone()[0])
    assert meta["format_comparison"]["date_conflict"]
    draft = plan_import(project, 1, media_mode="text_only")
    assert draft["included"] == 0 and draft["losses"]["date_conflict"] == 1
    assert fidelity(draft)["records"][0]["fields"]["date"] == ERROR


def test_streaming_reader_preserves_large_rows_and_arbitrary_header_order(tmp_path, monkeypatch):
    monkeypatch.setattr("gtr.export_json.CHUNK", 11)
    body = '"quoted" \n 🐈\tword'
    path = tmp_path / "export.json"
    path.write_text(json.dumps({"messages": [{"text": body}, {"id": 492}], "id": 42}, ensure_ascii=False),
                    encoding="utf-8-sig")
    assert list(fields(path)) == [("messages", None), (None, {"text": body}), (None, {"id": 492}), ("id", 42)]


def test_large_local_archive_keeps_ties_and_replies(tmp_path):
    text = "Длинное синтетическое сообщение " * 20
    source = write_json(tmp_path / "src", [
        message(index, text + str(index), reply_to_message_id=index - 1)
        for index in range(1, 1601)
    ])
    project = analyze(source, tmp_path / "project")
    assert summary(project)["resolved_replies"] == 1599
    plan = plan_import(project, 1, media_mode="text_only")
    assert plan["included"] == 1600
    assert plan["timeline"]["equal"] == 1599
    assert plan["timeline"]["max_shift_seconds"] == 0


def test_power_loss_simulation_during_package_leaves_no_published_data(project, tmp_path):
    target = tmp_path / "new"
    with pytest.raises(Cancelled):
        prepare(project, 1, target, progress=lambda stage, *_: stop_on_manifest(stage))
    assert not target.exists()


def stop_on_manifest(stage):
    if stage == "write_manifest":
        raise Cancelled
