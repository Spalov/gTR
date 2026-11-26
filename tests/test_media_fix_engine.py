import json
import subprocess
from shutil import copyfile
from types import SimpleNamespace

import pytest
from test_beta5_engine import generate
from test_html_dates import message

from gtr.conversion import load_bundle, plan_import, prepare, prepare_eligible
from gtr.core import analyze
from gtr.files import digest
from gtr.integrity import check_local
from gtr.media_queue import MediaDecisionRequired, MediaQueue, cache_info, clean_cache
from gtr.model import Cancelled
from gtr.owner import app_home
from gtr.video import MediaTools


@pytest.fixture
def tools():
    tools = MediaTools()
    if not (tools.ffmpeg and tools.ffprobe):
        pytest.skip("FFmpeg and FFprobe are required for the real video check")
    return tools


def make_video(tools, path, *, color="blue", codec="mpeg4", dimensions="96x64"):
    generate(tools.ffmpeg, "-f", "lavfi", "-i", f"color=c={color}:s={dimensions}:r=20:d=0.5",
             "-c:v", codec, "-pix_fmt", "yuv420p", "-threads", "1", path)


def export_project(tmp_path, files, *, grouped=False):
    source = tmp_path / "telegram"
    date = '<div class="date details" title="10.05.2026 13:12:11 UTC+02:00">13:12</div>'
    media = [f'<div class="media_wrap"><a class="video_file_wrap" href="{file}">Video</a></div>' for file in files]
    entries = [message(1, date, extra="".join(media))] if grouped else [
        message(index, date, extra=html) for index, html in enumerate(media, start=1)
    ]
    (source / "messages.html").write_text("\n".join(entries), encoding="utf-8")
    return analyze(source, tmp_path / "project")


@pytest.fixture
def five_clips(tmp_path, tools):
    source = tmp_path / "telegram"
    source.mkdir()
    make_video(tools, source / "original.mp4", codec="libx264")
    make_video(tools, source / "clip1.mkv")
    for number in range(2, 6):
        copyfile(source / "clip1.mkv", source / f"clip{number}.mkv")
    files = ["original.mp4", *(f"clip{number}.mkv" for number in range(1, 6))]
    return export_project(tmp_path, files), {file: digest(source / file) for file in files}


def test_five_html_clips_offer_batch_choices_and_resume(tmp_path, five_clips, tools):
    project, originals = five_clips
    events = []
    queue = MediaQueue(events.append)
    plan = plan_import(project, 1, video_policy="original", media_tools=tools, media_queue=queue)
    native = plan["records"][0]["video_recovery"]
    assert native["action"] == "keep" and native["original"]["duration"] > 0
    assert native["original"]["width"] == 96 and native["original"]["height"] == 64
    entries = plan["media_decisions_required"]
    assert len(entries) == 5 and len({entry["key"] for entry in entries}) == 5
    assert {entry["group"] for entry in entries} == {entries[0]["group"]}
    assert all(entry["recommended"] == "compatible" and entry["date"] for entry in entries)
    dest = tmp_path / "package"
    with pytest.raises(MediaDecisionRequired) as blocked:
        prepare(project, 1, dest, video_policy="original", media_tools=tools, media_queue=queue)
    assert blocked.value.entries == entries and not dest.exists()
    resolved = {entry["key"]: "compatible" for entry in entries}
    bundle = prepare(project, 1, dest, video_policy="original", media_tools=tools,
                     video_decisions=resolved, media_queue=queue)
    assert events[-1]["phase"] == "ready" and events[-1]["total"] == events[-1]["done"] == 6
    assert events[-1]["converted"] == 1 and events[-1]["reused"] == 4
    assert digest(bundle.media_path(bundle.data["media"][0])) == originals["original.mp4"]
    assert all(item["attributes"]["type"] == "video" for item in bundle.data["media"])
    assert check_local(load_bundle(dest), project)["status"] == "verified"
    for name, sha in originals.items():
        assert digest(project / "source" / name) == digest(tmp_path / "telegram" / name) == sha


def test_auto_cache_survives_queue_restart_and_cleanup_isolated(tmp_path, five_clips, tools, monkeypatch):
    project, _ = five_clips
    plan = prepare(project, 1, tmp_path / "first", video_policy="compatible", media_tools=tools)
    stats = cache_info()
    assert stats["files"] == 1 and stats["size"] > 0
    assert len({item["sha256"] for item in plan.data["media"][1:]}) == 1
    assert "transform" not in plan.data["media"][0]

    def fail_reencode(*args, **kwargs):
        pytest.fail("A verified cache hit must not invoke FFmpeg again")

    monkeypatch.setattr(tools, "compatibility_copy", fail_reencode)
    events = []
    next_bundle = prepare(project, 1, tmp_path / "second", video_policy="compatible", media_tools=tools,
                          media_queue=MediaQueue(events.append))
    assert next_bundle.data == plan.data
    assert next_bundle.manifest_hash() == plan.manifest_hash()
    assert events[-1]["converted"] == 0 and events[-1]["reused"] == 5
    assert clean_cache() == stats and cache_info() == {"files": 0, "size": 0}
    assert check_local(load_bundle(next_bundle.root), project)["status"] == "verified"


def test_missing_ffmpeg_offers_document_without_silent_fallback(tmp_path, five_clips, tools):
    project, _ = five_clips
    tools.ffmpeg = None
    with pytest.raises(MediaDecisionRequired) as blocked:
        prepare(project, 1, tmp_path / "missing", video_policy="compatible", media_tools=tools)
    assert len(blocked.value.entries) == 5
    assert all(entry["reason"] == "ffmpeg_unavailable" for entry in blocked.value.entries)
    assert all(entry["options"] == ("document", "skip") for entry in blocked.value.entries)
    choices = {entry["key"]: "document" for entry in blocked.value.entries}
    bundle = prepare(project, 1, tmp_path / "document", video_policy="compatible", media_tools=tools,
                     video_decisions=choices)
    assert bundle.data["media"][0]["attributes"]["type"] == "video"
    assert all(item["force_file"] and not item["attributes"] for item in bundle.data["media"][1:])


def test_disk_space_reaches_decision_with_explicit_reason(tmp_path, five_clips, tools, monkeypatch):
    project, originals = five_clips
    monkeypatch.setattr("gtr.media_queue.shutil.disk_usage", lambda path: SimpleNamespace(free=0))
    with pytest.raises(MediaDecisionRequired) as blocked:
        prepare(project, 1, tmp_path / "full", video_policy="compatible", media_tools=tools)
    assert {entry["reason"] for entry in blocked.value.entries} == {"disk_space"}
    assert cache_info()["files"] == 0 and not (tmp_path / "full").exists()
    assert digest(project / "source" / "clip1.mkv") == originals["clip1.mkv"]


def test_earlier_conversion_survives_later_cancel_and_new_queue(tmp_path, tools):
    source = tmp_path / "telegram"
    source.mkdir()
    for index, color in enumerate(("red", "blue"), start=1):
        make_video(tools, source / f"clip{index}.mkv", color=color)
    project = export_project(tmp_path, ("clip1.mkv", "clip2.mkv"))
    events = []

    def cancel(stage, done, total):
        if stage == "convert_video" and events[-1]["current"] == "clip2.mkv":
            raise Cancelled()

    with pytest.raises(Cancelled):
        prepare(project, 1, tmp_path / "cancelled", progress=cancel, video_policy="compatible", media_tools=tools,
                media_queue=MediaQueue(events.append))
    assert cache_info()["files"] == 1 and not (tmp_path / "cancelled").exists()
    events.clear()
    bundle = prepare(project, 1, tmp_path / "resumed", video_policy="compatible", media_tools=MediaTools(),
                     media_queue=MediaQueue(events.append))
    assert events[-1]["reused"] == events[-1]["converted"] == 1
    assert check_local(load_bundle(bundle.root), project)["status"] == "verified"


def test_invalid_cache_is_verified_and_rebuilt(tmp_path, tools):
    source = tmp_path / "telegram"
    source.mkdir()
    make_video(tools, source / "clip.mkv")
    project = export_project(tmp_path, ("clip.mkv",))
    bundle = prepare(project, 1, tmp_path / "one", video_policy="compatible", media_tools=tools)
    (cached,) = (app_home() / "media-cache").glob("*/video.mp4")
    before = cached.read_bytes()
    cached.write_bytes(b"\0" * len(before))
    events = []
    result = prepare(project, 1, tmp_path / "two", video_policy="compatible", media_tools=tools,
                     media_queue=MediaQueue(events.append))
    assert events[-1]["converted"] == 1 and events[-1]["reused"] == 0
    assert cached.read_bytes() == before and result.data == bundle.data


def test_explicit_reencode_allows_retry_after_server_declines_original(tmp_path, tools):
    source = tmp_path / "telegram"
    source.mkdir()
    make_video(tools, source / "clip.mp4", codec="libx264")
    project = export_project(tmp_path, ("clip.mp4",))
    original = prepare(project, 1, tmp_path / "original", video_policy="original", media_tools=tools)
    record = original.data["records"][0]["video_recovery"]
    assert record["action"] == "keep"
    copied = prepare(project, 1, tmp_path / "converted", media_tools=tools,
                     video_decisions={record["key"]: "compatible"})
    assert copied.data["records"][0]["video_recovery"]["action"] == "convert"
    assert "transform" in copied.data["media"][0]
    assert check_local(load_bundle(copied.root), project)["status"] == "verified"


def test_corrupt_video_can_be_skipped_with_original_preserved(tmp_path, tools):
    source = tmp_path / "telegram"
    source.mkdir()
    broken = source / "invalid.mp4"
    broken.write_bytes(b"\0\0\0\x18ftypisomdead")
    project = export_project(tmp_path, (broken.name,))
    with pytest.raises(MediaDecisionRequired) as blocked:
        prepare(project, 1, tmp_path / "choice", video_policy="compatible", media_tools=tools)
    entry, = blocked.value.entries
    assert entry["reason"] in {"corrupt_container", "probe_failed"}
    assert "compatible" not in entry["options"]
    bundle = prepare(project, 1, tmp_path / "skipped", video_decisions={entry["key"]: "skip"}, media_tools=tools)
    assert bundle.data["included"] == 1 and not bundle.data["media"]
    assert digest(broken) == digest(project / "source" / broken.name)


def packet_hashes(tools, file):
    data = subprocess.run(
        [str(tools.ffprobe), "-v", "error", "-show_packets", "-show_data_hash", "sha256", "-of", "json", str(file)],
        capture_output=True, check=True, timeout=20,
    )
    return sorted((packet["stream_index"], packet["data_hash"]) for packet in json.loads(data.stdout)["packets"])


def test_quicktime_h264_aac_is_remuxed_without_changing_packets_and_can_be_repaired(tmp_path, tools):
    source = tmp_path / "telegram"
    source.mkdir()
    movie = source / "clip.mov"
    generate(tools.ffmpeg, "-f", "lavfi", "-i", "color=c=red:s=96x64:r=20:d=0.5",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5", "-c:v", "libx264",
             "-c:a", "aac", "-pix_fmt", "yuv420p", "-threads", "1", movie)
    original_hash = digest(movie)
    project = export_project(tmp_path, (movie.name,))
    bundle = prepare(project, 1, tmp_path / "faststart", media_tools=tools, video_policy="compatible")
    record = bundle.data["records"][0]["video_recovery"]
    item = bundle.data["media"][0]
    assert record["method"] == "remux" and record["original"]["container"] == "video/quicktime"
    assert item["mime"] == "video/mp4" and item["attributes"]["supports_streaming"]
    assert packet_hashes(tools, movie) == packet_hashes(tools, bundle.media_path(item))
    assert digest(movie) == digest(project / "source" / movie.name) == original_hash
    rebuilt = prepare_eligible(project, bundle, tmp_path / "repair", ((1, 1),), media_tools=tools,
                               video_decisions={record["key"]: "compatible"})
    assert rebuilt.data["records"][0]["video_recovery"]["method"] == "transcode"
    assert rebuilt.data["media"][0]["sha256"] != item["sha256"]


def test_cache_cleanup_leaves_unrelated_parent_paths_and_never_deletes_nested_project(tmp_path, tools):
    source = tmp_path / "telegram"
    source.mkdir()
    make_video(tools, source / "clip.mkv")
    project = export_project(tmp_path, ("clip.mkv",))
    prepare(project, 1, tmp_path / "one", video_policy="compatible", media_tools=tools)
    root = app_home() / "media-cache"
    cache_entry, = (file.parent for file in root.glob("*/video.mp4"))
    for name in ("a" * 64, ".gtr-user-project", ".gtr-media-user-project", cache_entry.name + "/other-project"):
        user = root / name / "work" / "source.txt"
        user.parent.mkdir(parents=True)
        user.write_bytes(b"private fixture remains local")
    assert clean_cache() == {"files": 0, "size": 0}
    assert (cache_entry / "video.mp4").is_file()
    assert len(list(root.rglob("source.txt"))) == 4
