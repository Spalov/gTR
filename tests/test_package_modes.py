import hashlib
import json

import pytest
from conftest import message, write_json
from PySide6.QtGui import QImage

from gtr.archive import MAX_TOTAL_BYTES
from gtr.conversion import load_bundle, plan_import, prepare
from gtr.core import analyze
from gtr.fixtures import prepare_fixture
from gtr.importer import ImportController
from gtr.integrity import check_local
from gtr.mock_api import MockAPI, offline_result
from gtr.model import Cancelled, RecoveryError


def test_text_only_keeps_captions_and_documents_every_omission(project, bundle, tmp_path):
    database = project / "history.sqlite"
    old_digest = hashlib.sha256(database.read_bytes()).digest()
    plan = plan_import(project, 1, media_mode="text_only")
    text = load_bundle(prepare(project, 1, tmp_path / "text", media_mode="text_only").root)
    assert (plan["source_records"], plan["included"], plan["skipped"]) == (10, 3, 7)
    assert text.data["media_mode"] == "text_only" and text.data["media_count"] == 0
    assert text.data["media"] == [] and list((text.root / "media").iterdir()) == []
    assert text.data["omitted_assets"] == text.data["losses"]["text_only_media"] == 8
    assert text.data["losses"]["text_only_empty"] == 7
    assert sum(len(row["original_media"]) for row in text.data["records"]) == 8
    assert "<attached:" not in text.transcript.read_text(encoding="utf-8")
    assert "Подпись" in text.transcript.read_text(encoding="utf-8")
    assert text.fingerprint() != bundle.fingerprint()
    assert check_local(text, project)["status"] == "verified"
    assert hashlib.sha256(database.read_bytes()).digest() == old_digest


def test_separate_document_representation_keeps_bytes_and_binds_manifest(project, bundle, tmp_path):
    docs = load_bundle(prepare(project, 1, tmp_path / "docs", media_mode="document").root)
    first, same_content = bundle.data["media"][0], docs.data["media"][0]
    assert first["upload_as"] == "photo" and same_content["upload_as"] == "document"
    assert same_content["force_file"] and same_content["sha256"] == first["sha256"]
    assert bundle.transcript.read_bytes() == docs.transcript.read_bytes()
    assert docs.fingerprint() != bundle.fingerprint() and check_local(docs, project)["status"] == "verified"


def test_synthetic_text_and_png_are_deterministic_and_separate(tmp_path):
    a = prepare_fixture(tmp_path / "text", "text")
    b = prepare_fixture(tmp_path / "duplicate-text", "text")
    media = load_bundle(prepare_fixture(tmp_path / "media-fixture", "media").root)
    assert a.fingerprint() == b.fingerprint()
    assert a.fingerprint() != media.fingerprint() and a.data["source_sha256"] != media.data["source_sha256"]
    assert a.data["fixture"] == "text" and a.data["included"] == 2 and a.data["media_count"] == 0
    assert media.data["fixture"] == "media" and media.data["included"] == 2 and media.data["media_count"] == 1
    assert {row["source_author_id"] for row in media.data["records"]} == {"fixture:1", "fixture:2"}
    item = media.data["media"][0]
    assert item["input_media"] == "InputMediaUploadedPhoto" and item["detected_mime"] == "image/png"
    image = QImage(str(media.media_path(item)))
    assert (image.width(), image.height()) == (256, 192)


def test_zero_media_starts_only_after_explicit_owner_approval(tmp_path):
    bundle = prepare_fixture(tmp_path / "text", "text")
    api = MockAPI()
    controller = ImportController(api, "local")
    checked = offline_result(controller.validate(bundle))
    assert api.calls == ["check_history", "check_peer"]
    with pytest.raises(Cancelled):
        offline_result(controller.execute(checked, approved=False))
    assert "start_import" not in api.calls
    api = MockAPI()
    controller = ImportController(api, "local")
    checked = offline_result(controller.validate(bundle))
    offline_result(controller.execute(checked, approved=True))
    assert api.calls == ["check_history", "check_peer", "init_import", "start_import"]


@pytest.mark.parametrize("damage", ["count", "sequence", "bytes", "constructor", "empty", "size"])
def test_bundle_preflight_rejects_invalid_media_before_transport(bundle, damage):
    data = bundle.data
    item = data["media"][0]
    if damage == "count":
        data["media_count"] += 1
    elif damage == "sequence":
        data["media"][:2] = reversed(data["media"][:2])
    elif damage == "constructor":
        item["input_media"] = "InputMediaUploadedDocument"
    elif damage == "size":
        item["size"] = MAX_TOTAL_BYTES + 1
    else:
        contents = b"" if damage == "empty" else b"not the claimed photo type"
        bundle.media_path(item).write_bytes(contents)
        item.update(size=len(contents), sha256=hashlib.sha256(contents).hexdigest())
    (bundle.root / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(RecoveryError):
        load_bundle(bundle.root)


def test_media_candidate_matrix_keeps_unverified_originals_and_fallbacks(tmp_path):
    # Container signatures exercise local proposals, not codec decoding or Telegram acceptance.
    assets = [
        ("j.jpg", "photo", b"\xff\xd8\xffJPEG", {}, "photo"),
        ("d.pdf", "document", b"%PDF-1.7\n", {}, "document"),
        ("v.ogg", "voice_message", b"OggS\0OpusHead", {"duration_seconds": 2}, "specialized_document"),
        (
            "m.mp4",
            "video_file",
            b"\0\0\0\x18ftypisom",
            {"duration_seconds": 1.2, "width": 320, "height": 240},
            "specialized_document",
        ),
        ("s.webp", "sticker", b"RIFF0123WEBPVP8L", {"sticker_emoji": "★"}, "specialized_document"),
        ("s.tgs", "sticker", b"\x1f\x8bsynthetic", {"sticker_emoji": "★"}, "specialized_document"),
        ("a.gif", "animation", b"GIF89a0123", {}, "specialized_document"),
        ("bad.jpg", "photo", b"not a recognized image", {}, "document"),
        ("empty.jpg", "photo", b"", {}, "generated_document"),
    ]
    records = [
        message(
            i,
            "Подпись",
            **({"photo": name} if kind == "photo" else {"file": name, "media_type": kind}),
            **meta,
        )
        for i, (name, kind, _, meta, _) in enumerate(assets, 1)
    ]
    source = write_json(tmp_path / "export", records)
    for name, _, contents, _, _ in assets:
        (source / name).write_bytes(contents)
    project = analyze(source, tmp_path / "project")
    bundle = load_bundle(prepare(project, 1, tmp_path / "package").root)
    media = bundle.data["media"]
    assert [item["representation"] for item in media] == [representation for *_, representation in assets]
    assert {item["compatibility"] for item in media} == {"unverified"}
    assert media[2]["attributes"]["voice"] is True
    assert not media[-2]["detected_mime"]
    assert bundle.media_path(media[-2]).read_bytes() == assets[-2][2]
    assert bundle.data["losses"]["media_unavailable"] == 1
