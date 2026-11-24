import hashlib
import json
import struct
import wave

import pytest
from conftest import message, write_json

from gtr.conversion import prepare
from gtr.core import analyze
from gtr.faults import ImportFault
from gtr.fixtures import _png
from gtr.importer import ImportController
from gtr.journal import Journal, Phase
from gtr.media_engine import inspect, inspect_bundle, inspect_project, public_inventory
from gtr.media_preview import preview
from gtr.mock_api import MockAPI, offline_result
from gtr.model import Cancelled, RecoveryError


def test_image_inspection_and_preview_use_content_and_leave_files_untouched(tmp_path):
    path = tmp_path / "original.blob"
    data = _png()
    path.write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    facts, thumbnail = preview(path, sha, "application/pdf")
    assert (facts.mime, facts.width, facts.height, facts.sha256) == ("image/png", 256, 192, sha)
    assert facts.issues == ("mime_mismatch",) and not thumbnail.isNull()
    assert path.read_bytes() == data
    assert inspect(path, expected_size=7, expected_hash="0" * 64).issues == ("size_mismatch", "hash_mismatch")
    assert inspect(tmp_path / "absent.png").issues == ("missing",)
    with pytest.raises(RecoveryError, match="изменился"):
        preview(path, "0" * 64)

    def stop():
        raise Cancelled()

    with pytest.raises(Cancelled):
        inspect(path, checkpoint=stop)


def test_audio_metadata_and_waveform(tmp_path):
    path = tmp_path / "recording.data"
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, 8000, 0, "NONE", ""))
        stream.writeframes(b"\x80\x02" * 4000)
    facts, image = preview(path, None)
    assert (facts.mime, facts.duration, facts.sample_rate, facts.channels) == ("audio/wav", 0.5, 8000, 1)
    assert (image.width(), image.height()) == (480, 120)


def test_mp4_metadata_and_optional_video_adapter(tmp_path):
    def box(name, content):
        return struct.pack(">I4s", len(content) + 8, name) + content

    path = tmp_path / "clip.mp4"
    header = box(b"ftyp", b"isom\x00\x00\x00\x00isom")
    movie = box(b"mvhd", b"\0" * 12 + struct.pack(">II", 1000, 1250))
    track = box(b"tkhd", b"\0" * 76 + struct.pack(">II", 640 << 16, 360 << 16))
    path.write_bytes(header + box(b"moov", movie + box(b"trak", track)))

    class Adapter:
        def frame(self, source, mime):
            assert source == path and mime == "video/mp4"
            return _png()

    facts, image = preview(path, None, provider=Adapter())
    assert (facts.mime, facts.duration, facts.width, facts.height) == ("video/mp4", 1.25, 640, 360)
    assert not image.isNull()
    assert preview(path, None)[1].isNull()
    path.write_bytes(header + b"\xff\xff\xff\xffmoov")
    assert inspect(path).issues == ("corrupt_container",)


def test_project_keeps_duplicates_and_flags_missing_or_private_paths(tmp_path):
    source = write_json(
        tmp_path / "source",
        [
            message(i, photo=name)
            for i, name in enumerate(
                ("private-one.png", "private-two.png", "absent.png", "../../hidden.png"), 1
            )
        ],
    )
    for name in ("private-one.png", "private-two.png"):
        (source / name).write_bytes(_png())
    project = analyze(source, tmp_path / "project")
    result = inspect_project(project, 1)
    assert (result["count"], result["blocked"]) == (4, 2)
    assert result["items"][1]["duplicate_of"] == 1
    assert result["items"][2]["issues"] == ("missing",)
    assert result["items"][3]["issues"] == ("unreadable",)
    public = json.dumps(public_inventory(result))
    assert all(value not in public for value in ("private-one.png", "sha256", "source_id", "hidden.png"))
    bundle = prepare(project, 1, tmp_path / "package", "source")
    assert [item["source"] for item in bundle.data["media"] if item["source"]] == [
        "private-one.png",
        "private-two.png",
    ]
    inventory = inspect_bundle(bundle)
    assert inventory["issues"]["duplicate"] == 1
    assert inventory["source_issues"] == {"missing": 1, "unsafe": 1}


def test_damaged_photo_is_reported_before_rpc_and_can_be_explicit_document(tmp_path):
    source = write_json(tmp_path / "source", [message(1, photo="private-image.png", mime_type="image/png")])
    data = _png()[:-12]
    (source / "private-image.png").write_bytes(data)
    project = analyze(source, tmp_path / "project")
    bundle = prepare(project, 1, tmp_path / "photo", "source")
    assert inspect_bundle(bundle)["blocked"] == 1
    endpoint = Journal(tmp_path / "operations.sqlite", "target")
    api = MockAPI()
    controller = ImportController(api, "target", mode="owner", journal=endpoint)
    with pytest.raises(ImportFault) as rejected:
        offline_result(controller.validate(bundle))
    assert rejected.value.code == "MEDIA_LOCAL_INVALID" and api.calls == []
    assert endpoint.operation(controller.job)["phase"] == Phase.FAILED
    report = endpoint.diagnostic_report(controller.job, private=True)["media_attempts"][0]
    assert (report["index"], report["stage"], report["archive_paths"], report["detected_mime"]) == (
        1,
        "inspect_media",
        ["private-image.png"],
        "image/png",
    )
    assert "private-image.png" not in json.dumps(endpoint.diagnostic_report(controller.job))
    fallback = prepare(project, 1, tmp_path / "document", "source", media_mode="document")
    inventory = inspect_bundle(fallback)
    assert inventory["blocked"] == 0 and inventory["issues"] == {"corrupt_container": 1}
    assert fallback.data["media"][0]["upload_as"] == "document"
    assert fallback.media_path(fallback.data["media"][0]).read_bytes() == data
