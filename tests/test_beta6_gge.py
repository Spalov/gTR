import gzip
import io
import json
import struct
from zipfile import ZipFile

import pytest

from gtr.gge_io import BoundedReader, media_source, preflight_zip
from gtr.gge_metadata import capture_date
from gtr.media import identify
from gtr.media_engine import inspect
from gtr.model import RecoveryError
from gtr.video import VideoEvidence, interpret_probe, video_options


def test_media_source_enforces_seek_budget_and_zip_member_names(tmp_path):
    stream = io.BytesIO(b"abcdef")
    reader = BoundedReader(stream, 6, 4)
    assert reader.read(3) == b"abc"
    assert reader.seek(-1, io.SEEK_END) == 5 and reader.read(1) == b"f"
    with pytest.raises(ValueError, match="budget"):
        reader.seek(0)
        reader.read(1)
    with pytest.raises(ValueError, match="outside"):
        reader.seek(7)

    path = tmp_path / "media.zip"
    with ZipFile(path, "w") as archive:
        archive.writestr("photos/scene.jpg", b"local synthetic media")
    with media_source(path, member="photos/scene.jpg") as source:
        assert source.read(5) == b"local"
    with pytest.raises(RecoveryError), media_source(path, member="../scene.jpg"):
        pytest.fail("path traversal must fail before opening a member")
    original = path.read_bytes()
    forged = bytearray(original)
    struct.pack_into("<HH", forged, len(forged) - 14, 2, 2)
    with pytest.raises(RecoveryError):
        preflight_zip(io.BytesIO(forged))


def test_webm_alpha_disallows_implicit_loss_and_sticker_is_distinct():
    fields = {"streams": [
        {"codec_type": "video", "codec_name": "vp9", "width": 320, "height": 480, "duration": "2.5",
         "pix_fmt": "yuv420p", "avg_frame_rate": "30/1", "tags": {"alpha_mode": "1"}},
        {"codec_type": "audio", "codec_name": "opus"},
    ]}
    evidence = interpret_probe(fields, "video/webm")
    assert evidence.alpha and evidence.streams == 2 and evidence.audio == ("opus",)
    assert evidence.fps == 30 and (evidence.width, evidence.height) == (320, 480)
    assert not evidence.playable

    class Tools:
        ffmpeg = ffprobe = True

    assert video_options(evidence, {"kind": "video"}, Tools()) == ("document", "skip")
    assert not VideoEvidence("unknown").alpha


def test_tgs_container_and_private_capture_date_are_local(tmp_path):
    sample = tmp_path / "synthetic.tgs"
    sample.write_bytes(gzip.compress(json.dumps({"w": 512, "h": 512, "fr": 30, "ip": 0,
                                                 "op": 60, "layers": []}).encode()))
    info = inspect(sample)
    assert info.mime == "application/x-tgsticker" and info.animated and info.duration == 2
    sample.write_bytes(gzip.compress(b"dummy"))
    assert "corrupt_container" in inspect(sample).issues
    assert identify("audio", b"fLaC\0")[0] == "audio/flac"
    assert identify("audio", bytes.fromhex("fff1508001fffc"))[0] == "audio/aac"
    assert capture_date("2025:03:04 05:06:07", exif=True)["timezone_known"] is False
    assert capture_date("2025:03:04 05:06:07", "+02:00", exif=True)["date"].endswith("+02:00")
    assert capture_date("2025-02-31T10:20:30Z") is None
