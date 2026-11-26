import json
from datetime import UTC, datetime
from hashlib import sha256

import pytest
from conftest import message, write_json
from telethon import functions, types

from gtr.bindings import UNRESOLVED, AttachmentBindingError, literal_marker
from gtr.conversion import load_bundle, prepare
from gtr.core import analyze
from gtr.faults import ImportFault
from gtr.fixtures import _png
from gtr.mock_api import MockAPI, offline_result
from gtr.mtproto import TelegramAPI
from gtr.profiles import read_records
from gtr.verification import compare


def test_bound_names_are_ascii_unique_and_independent_of_display_names(tmp_path):
    rows = [message(i, "Подпись\nещё строка", photo=f"{i}/Фото 🐈.png") for i in (1, 2)]
    folder = write_json(tmp_path / "export", rows)
    for i in (1, 2):
        (folder / str(i)).mkdir()
        (folder / str(i) / "Фото 🐈.png").write_bytes(_png())
    project = analyze(folder, tmp_path / "project")
    for profile in ("ios", "android"):
        bundle = prepare(project, 1, tmp_path / profile, profile=profile)
        data = load_bundle(bundle.root).data
        first, second = data["media"]
        assert first["display_name"] == second["display_name"] == "Фото 🐈.png"
        assert first["wire_name"] != second["wire_name"]
        assert first["wire_name"].isascii() and second["wire_name"].isascii()
        assert first["attachment_id"] == data["records"][0]["attachment_id"]
        with bundle.transcript.open(encoding="utf-8") as transcript:
            assert [r[3] for r in read_records(transcript, data["profile"])] == [m["wire_name"] for m in data["media"]]


def test_editing_both_the_manifest_and_wire_alias_still_fails(bundle):
    case = bundle.data["media"][0]
    before = case["wire_name"]
    case["wire_name"] = "bad-name.jpg"
    transcript = bundle.transcript.read_bytes().replace(before.encode(), b"bad-name.jpg", 1)
    bundle.transcript.write_bytes(transcript)
    bundle.data["text_sha256"] = sha256(transcript).hexdigest()
    (bundle.root / "manifest.json").write_text(json.dumps(bundle.data), encoding="utf-8")
    with pytest.raises(AttachmentBindingError):
        load_bundle(bundle.root)


def test_literal_photo_voice_and_webp_markers_are_not_success(bundle, project):
    mock = MockAPI("success")
    mock.started = True
    with bundle.transcript.open(encoding="utf-8") as stream:
        mock.content = list(read_records(stream, bundle.data["profile"]))
    mock.uploaded = {item["wire_name"] for item in bundle.data["media"]}

    source, target, mapping = mock.observations(bundle)
    attachments = {item["name"]: item for item in bundle.data["media"]}
    selected = {"photo", "voice", "sticker"}
    for snapshot in (source, target):
        for record, got, (_, _, caption, _) in zip(bundle.data["records"], snapshot["messages"], mock.content):
            item = attachments.get(record.get("media"))
            if not item or item["kind"] not in selected:
                continue
            literal = f"\u200e<attached: {item['wire_name']}>" + ("\n" + caption if caption else "")
            got["text_sha256"] = sha256(literal.encode()).hexdigest()
            got["attachment_marker_sha256"], got["caption_without_marker_sha256"] = literal_marker(literal)
            got.update(has_media=False, visual_media=False, media_accessible=None, media_sha256=None)
    result = compare(bundle, source, target, mapping, project=project)
    assert result["unresolved_attachments"] == 3
    assert result["verification_status"] == "PARTIALLY_CONFIRMED"
    assert result["levels"]["B1"] == "differences"
    assert sum(row["binding_error"] == UNRESOLVED for row in result["views"][0]["matched"]) == 3


def test_nested_input_file_and_rpc_carry_the_same_wire_alias(bundle):
    item = next(row for row in bundle.data["media"] if row["kind"] == "voice").copy()
    item["attributes"] = {"type": "audio", "voice": True, "duration": 1}

    class Client:
        wrong_name = False

        async def upload_file(self, path, file_name, part_size_kb=None, **options):
            assert part_size_kb is None or part_size_kb > 0
            assert file_name == item["wire_name"]
            return types.InputFile(10, 1, item["display_name"] if self.wrong_name else file_name, "")

        async def __call__(self, request):
            assert isinstance(request, functions.messages.UploadImportedMediaRequest)
            assert request.file_name == request.media.file.name == item["wire_name"]
            filename = next(a for a in request.media.attributes if isinstance(a, types.DocumentAttributeFilename))
            assert filename.file_name == item["display_name"]
            return types.MessageMediaDocument(document=types.Document(
                1, 1, b"", datetime.now(UTC),
                item["mime"], item["size"], 1, [],
            ))

    client = Client()
    api = TelegramAPI(client, "pm")
    with pytest.raises(ImportFault, match="другого типа") as mismatch:
        offline_result(api.upload_media(types.InputPeerSelf(), 2, item, bundle.media_path(item)))
    assert mismatch.value.code == "MEDIA_TYPE_CHANGED"
    client.wrong_name = True
    with pytest.raises(ImportFault) as bad:
        offline_result(api.upload_media(types.InputPeerSelf(), 2, item, bundle.media_path(item)))
    assert bad.value.code == UNRESOLVED
