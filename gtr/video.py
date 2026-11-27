"""Local video evidence and opt-in, independently checked compatibility copies."""

import json
import math
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, replace
from importlib.util import find_spec
from pathlib import Path
from threading import BoundedSemaphore, Thread
from time import monotonic
from uuid import uuid4

from .bindings import requested_kind
from .files import digest, local
from .gge_process import CREATE_SUSPENDED, resume_worker, tool_environment
from .media_engine import MalformedMedia, _box_headers, inspect
from .media_queue import MIN_FREE, MediaDiskSpace
from .media_recovery import observed_media_kind  # noqa: F401 - retained for existing callers
from .model import Cancelled, RecoveryError, quiet
from .structure import unicode_text

VIDEO_POLICIES = {
    "original": "Оригинал",
    "compatible": "Совместимость (разрешить создание копий)",
    "remux": "Смена контейнера без перекодирования",
    "fidelity": "Максимальная точность",
    "compact": "Уменьшенная копия",
    "document": "Передать видео документом",
    "skip": "Пропустить несовместимые видео",
}
VIDEO_KINDS = {"video", "round_video"}
DEMUXERS = {
    "video/mp4": "mov",
    "video/quicktime": "mov",
    "video/webm": "matroska",
    "video/x-matroska": "matroska",
    "video/x-msvideo": "avi",
    "audio/ogg": "ogg",
    "audio/opus": "ogg",
    "audio/mp4": "mov",
    "audio/mpeg": "mp3",
    "audio/aac": "aac",
    "audio/wav": "wav",
    "audio/flac": "flac",
    "image/gif": "gif",
}
PROCESSES = BoundedSemaphore(2)
MAX_PROBE = 1024 * 1024
MAX_COPY = 2 * 1024**3
MAX_DURATION = 7 * 86400


@dataclass(frozen=True)
class VideoEvidence:
    status: str
    container: str | None = None
    codec: str | None = None
    audio: tuple[str, ...] | None = None
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    rotation: int | None = None
    streaming: bool | None = None
    pixel_format: str | None = None
    decode: str = "unknown"
    playable: bool = False
    sample_aspect_ratio: float | None = None
    stream_index: int | None = None
    method: str | None = None
    alpha: bool = False
    color_space: str | None = None
    streams: int | None = None

    def attributes(self, kind):
        if not self.playable:
            return {}
        return {
            "type": "video", "duration": self.duration, "width": self.width, "height": self.height,
            "round": kind == "round_video", "video_codec": self.codec,
            "nosound": not self.audio, "supports_streaming": self.streaming is True,
        }

    def summary(self):
        return asdict(self)


def tool_path(name, selected=None):
    candidate = selected or shutil.which(name)
    if not candidate:
        return None
    try:
        path = local(Path(candidate).resolve(strict=True))
    except (OSError, RecoveryError):
        return None
    return path if path.name.casefold() in {name, name + ".exe"} and path.is_file() else None


def _input(path, mime):
    demuxer = DEMUXERS[mime]
    args = ["-protocol_whitelist", "file", "-max_alloc", "67108864",
            "-probesize", "8388608", "-analyzeduration", "5000000", "-max_streams", "32", "-f", demuxer]
    if demuxer == "mov":
        args += ["-enable_drefs", "0", "-use_absolute_path", "0"]
    return [*args, "-i", str(path)]


def _limit(process, *, python_worker=False):
    if sys.platform != "win32":
        return None
    import win32api
    import win32job

    job = win32job.CreateJobObject(None, f"gTR.Media.{uuid4().hex}")
    try:
        limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        limits["BasicLimitInformation"]["LimitFlags"] = (
            win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | win32job.JOB_OBJECT_LIMIT_PROCESS_MEMORY
            | win32job.JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        )
        limits["BasicLimitInformation"]["ActiveProcessLimit"] = 2 if python_worker else 1
        limits["ProcessMemoryLimit"] = 1024**3
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
        handle = win32api.OpenProcess(0x0101, False, process.pid)
        try:
            win32job.AssignProcessToJobObject(job, handle)
        finally:
            handle.Close()
        return job
    except Exception:
        job.Close()
        raise


def _run(executable, args, seconds, progress, *, duration_ms=None, python_worker=False, cwd=None):
    while not PROCESSES.acquire(timeout=0.25):
        progress("probe_media", 0, 0)
    job = None
    try:
        with subprocess.Popen(
            [str(executable), *args], cwd=cwd or executable.parent, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=tool_environment(executable, python_worker=python_worker),
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) | CREATE_SUSPENDED) if sys.platform == "win32" else 0,
        ) as process:
            output = []
            media_ms = 0

            def read():
                nonlocal media_ms
                try:
                    if duration_ms is None:
                        output.append(process.stdout.read(MAX_PROBE + 1))
                    else:
                        while line := process.stdout.readline(256):
                            if line.startswith(b"out_time_us=") and line[12:].strip().isdigit():
                                media_ms = min(duration_ms, int(line[12:].strip()) // 1000)
                        output.append(b"")
                except OSError:
                    pass

            reader = Thread(target=read, daemon=True)
            try:
                try:
                    job = _limit(process, python_worker=python_worker)
                    if job:
                        resume_worker(process.pid)
                except Exception:  # noqa: BLE001 - fail closed if the OS refuses process limits
                    return None
                reader.start()
                deadline = monotonic() + seconds
                while monotonic() < deadline:
                    if duration_ms is None:
                        progress("probe_media", 0, 0)
                    else:
                        progress("convert_video", media_ms, duration_ms)
                    reader.join(0.15)
                    if not reader.is_alive():
                        if not output or len(output[0]) > MAX_PROBE:
                            return None
                        if process.poll() is None:
                            try:
                                process.wait(timeout=0.15)
                            except subprocess.TimeoutExpired:
                                continue
                        return output[0] if process.returncode == 0 else None
                return None
            finally:
                if process.poll() is None:
                    process.kill()
                if reader.ident is not None:
                    reader.join(2)
    except OSError:
        return None
    finally:
        if job:
            job.Close()
        PROCESSES.release()


def _number(value, maximum):
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) and 0 < result <= maximum else None


def _fps(value):
    left, sep, right = str(value).partition("/")
    numerator = _number(left, 1e9)
    denominator = _number(right, 1e9) if sep else 1
    return round(numerator / denominator, 5) if numerator and denominator and numerator / denominator <= 1000 else None


def _rotation(stream):
    values = [d.get("rotation") for d in stream.get("side_data_list", []) if isinstance(d, dict)]
    values.append((stream.get("tags") or {}).get("rotate"))
    for value in values:
        try:
            angle = float(value)
        except (ValueError, TypeError):
            continue
        if math.isfinite(angle) and angle.is_integer() and -360 <= angle <= 360:
            return int(angle) % 360
    return None


def _streaming(path, size):
    try:
        with path.open("rb") as source:
            kinds = [kind for kind, *_ in _box_headers(source, 0, size, [1024])]
        if b"moov" in kinds and b"mdat" in kinds:
            return kinds.index(b"moov") < kinds.index(b"mdat")
    except (OSError, MalformedMedia):
        pass
    return None


def interpret_probe(data, mime, streaming=None, decode="unknown", kind="video"):
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video" and
                  not (s.get("disposition") or {}).get("attached_pic")), None)
    if video is None:
        return VideoEvidence("no_video", mime, streaming=streaming, decode=decode)
    audio = tuple(s.get("codec_name") or "unknown" for s in streams if s.get("codec_type") == "audio")
    duration = _number(video.get("duration"), MAX_DURATION) or _number(
        (data.get("format") or {}).get("duration"), MAX_DURATION
    )
    width, height = (_number(video.get(key), 16384) for key in ("width", "height"))
    if width and not width.is_integer() or height and not height.is_integer():
        width = height = None
    width, height = (int(i) if i else None for i in (width, height))
    codec, pixel = video.get("codec_name"), video.get("pix_fmt")
    alpha = ((pixel or "").startswith(("yuva", "rgba", "bgra", "argb", "abgr", "gbrap"))
             or (video.get("tags") or {}).get("alpha_mode") == "1")
    compatible = (
        mime == "video/mp4" and codec == "h264" and pixel in {"yuv420p", "yuvj420p"}
        and not alpha and set(audio) <= {"aac"} and (kind != "round_video" or width == height)
    )
    status = (
        "decode_failed" if decode == "failed" else "missing_metadata" if not (duration and width and height)
        else "compatible" if compatible else "transcode_required"
    )
    return VideoEvidence(
        status, mime, codec, audio, duration, width, height, _fps(video.get("avg_frame_rate")),
        _rotation(video), streaming, pixel, decode, status == "compatible",
        _fps(str(video.get("sample_aspect_ratio", "")).replace(":", "/")),
        video["index"] if type(video.get("index")) is int and 0 <= video["index"] < 128 else None,
        alpha=alpha, color_space=video.get("color_space"), streams=len(streams),
    )


def _video_stream(data):
    return next((stream for stream in data.get("streams", []) if stream.get("codec_type") == "video"
                 and not (stream.get("disposition") or {}).get("attached_pic")), {})


def _stream_selector(index):
    return f"0:{index}" if type(index) is int and 0 <= index < 128 else "0:V:0"


def _aspect(evidence):
    ratio = evidence.width * (evidence.sample_aspect_ratio or 1) / evidence.height
    return 1 / ratio if (evidence.rotation or 0) % 180 == 90 else ratio


def _remux_possible(evidence):
    return (
        evidence.container in {"video/quicktime", "video/mp4", "video/x-matroska"} and evidence.codec == "h264"
        and not evidence.alpha
        and evidence.pixel_format in {"yuv420p", "yuvj420p"} and set(evidence.audio or ()) <= {"aac"}
    )


def compatible_copy_matches(actual, evidence, *, profile="compatible"):
    return (
        actual.playable and actual.streaming and actual.decode == "sample_ok" and actual.duration
        and abs(actual.duration - evidence.duration) <= max(0.2, 2 / (evidence.fps or 25))
        and len(actual.audio or ()) == len(evidence.audio or ())
        and (not actual.fps or not evidence.fps or abs(actual.fps / evidence.fps - 1) <= 0.03)
        and (profile != "fidelity" or min(actual.width, actual.height) >= min(evidence.width, evidence.height))
        and abs(_aspect(actual) / _aspect(evidence) - 1) <= 0.03
    )


class MediaTools:
    def __init__(self, ffmpeg=None, ffprobe=None):
        self.ffmpeg = tool_path("ffmpeg", ffmpeg)
        sibling = self.ffmpeg.with_name("ffprobe" + self.ffmpeg.suffix) if self.ffmpeg else None
        self.ffprobe = tool_path("ffprobe", ffprobe or sibling) or (tool_path("ffprobe") if not ffprobe else None)

    def probe(self, path, mime, progress=quiet):
        if not self.ffprobe or mime not in DEMUXERS:
            return None
        out = _run(self.ffprobe, [
            "-v", "error", *_input(path, mime), "-show_entries",
            ("format=duration:format_tags=title,artist:stream=index,codec_type,codec_name,pix_fmt,width,height,duration,"
             "avg_frame_rate,sample_aspect_ratio,color_space:stream_disposition=attached_pic:"
             "stream_side_data=rotation:stream_tags=rotate,title,artist,alpha_mode"),
            "-of", "json",
        ], 15, progress)
        try:
            result = json.loads(out or b"")
            if isinstance(result, dict) and isinstance(result.get("streams"), list):
                return result
        except (ValueError, RecursionError):
            pass
        return None

    def analyze(self, path, kind, *, expected_size=None, expected_hash=None, progress=quiet):
        path = local(Path(path))
        facts = inspect(path, expected_size=expected_size, expected_hash=expected_hash,
                        checkpoint=lambda: progress("probe_media", 0, 0))
        mime = facts.mime
        if not facts.intact:
            return VideoEvidence(next(iter(facts.issues), "unreadable"), mime)
        if "corrupt_container" in facts.issues or mime not in DEMUXERS:
            return VideoEvidence("corrupt_container" if mime in DEMUXERS else "unknown_container", mime)
        data = self.probe(path, mime, progress)
        if data is None:
            return VideoEvidence("probe_failed" if self.ffprobe else "ffprobe_unavailable", mime)
        decode = "unknown"
        if self.ffmpeg:
            decoded = _run(self.ffmpeg, [
                "-nostdin", "-v", "error", "-xerror", "-threads", "1", *_input(path, mime),
                "-map", _stream_selector(_video_stream(data).get("index")), "-frames:v", "30",
                "-an", "-sn", "-dn", "-f", "null", "-",
            ], 18, progress)
            decode = "sample_ok" if decoded is not None else "failed"
        if expected_hash and digest(path, lambda: progress("probe_media", 0, 0)) != expected_hash:
            return VideoEvidence("changed", mime)
        return interpret_probe(
            data, mime, _streaming(path, facts.size) if mime == "video/mp4" else None, decode, kind
        )

    def audio(self, path, item, progress):
        facts = inspect(
            path, expected_size=item["size"], expected_hash=item["sha256"],
            checkpoint=lambda: progress("probe_media", 0, 0),
        )
        if not facts.intact or "corrupt_container" in facts.issues:
            return {"status": "audio_unreadable", "attributes": {}}
        data = self.probe(path, facts.mime, progress) or {}
        stream = next((track for track in data.get("streams", []) if track.get("codec_type") == "audio"), {})
        duration = (
            _number(stream.get("duration"), MAX_DURATION)
            or _number((data.get("format") or {}).get("duration"), MAX_DURATION)
            or facts.duration
        )
        codec = stream.get("codec_name") or facts.audio_codec
        voice = item["kind"] == "voice"
        suitable = facts.mime and facts.mime.startswith("audio/") and (
            not voice or (facts.mime in {"audio/ogg", "audio/opus"} and codec == "opus")
        )
        if not suitable or not duration:
            return {"status": "audio_unconfirmed", "codec": codec, "attributes": {}}
        if digest(path, lambda: progress("probe_media", 0, 0)) != item["sha256"]:
            return {"status": "audio_unreadable", "attributes": {}}
        attributes = {"type": "audio", "duration": math.ceil(duration), "voice": voice}
        tags = {**(data.get("format", {}).get("tags") or {}), **(stream.get("tags") or {})}
        for source, field in (("title", "title"), ("artist", "performer")):
            if text := " ".join(unicode_text(tags.get(source)).split())[:128]:
                attributes[field] = text
        return {"status": "audio_confirmed", "codec": codec, "attributes": attributes}

    def compatibility_copy(self, source, dest, kind, evidence, expected_hash, progress=quiet, *,
                           force_transcode=False, profile="compatible"):
        if not (self.ffmpeg and self.ffprobe and evidence.container in DEMUXERS and evidence.duration
                and evidence.codec and evidence.decode != "failed" and not evidence.alpha):
            raise RecoveryError("Совместимая копия недоступна. Проверьте FFmpeg и состояние исходного видео.")
        src, dest = local(Path(source)), local(Path(dest))
        if dest.exists() or src == dest:
            raise RecoveryError("Выберите новую папку для совместимой копии.")
        checkpoint = lambda: progress("verify_video_source", 0, 0)
        if digest(src, checkpoint) != expected_hash:
            raise RecoveryError("Оригинал видео изменился после анализа.")
        available = shutil.disk_usage(dest.parent).free - MIN_FREE
        if available <= 0:
            raise MediaDiskSpace()
        # FFmpeg applies display rotation while decoding. Scale by at most one pixel
        # for 4:2:0 and let the scale filter adjust the sample aspect ratio.
        vf = "scale=ceil(iw/2)*2:ceil(ih/2)*2"
        if profile == "compact":
            vf = "scale=w='min(iw,1280)':h='min(ih,1280)':force_original_aspect_ratio=decrease:force_divisible_by=2"
        base = [
            "-nostdin", "-v", "error", "-xerror", "-n", "-threads", "1", *_input(src, evidence.container),
            "-map", _stream_selector(evidence.stream_index), "-map", "0:a?", "-map_metadata", "-1", "-map_chapters", "-1",
            "-sn", "-dn",
        ]
        output = [
            "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats", "-stats_period", "0.25",
            "-fs", str(min(MAX_COPY, available)), "-f", "mp4", str(dest),
        ]
        encode_options = ["-filter_threads", "1", "-vf", vf, "-c:v", "libx264", "-pix_fmt", "yuv420p",
                          "-preset", "fast", "-crf", "23" if profile == "compact" else "18", "-threads", "1",
                          "-fps_mode", "passthrough", "-c:a", "aac"]
        attempts = [] if profile == "remux" else [("transcode", encode_options)]
        if not force_transcode and profile != "compact" and _remux_possible(evidence):
            attempts.insert(0, ("remux", ["-c", "copy"]))
        duration_ms = max(1, round(evidence.duration * 1000))
        for method, options in attempts:
            progress("convert_video", 0, duration_ms)
            result = _run(self.ffmpeg, [*base, *options, *output], min(7200, max(120, evidence.duration * 8)),
                          progress, duration_ms=duration_ms)
            if digest(src, checkpoint) != expected_hash:
                raise RecoveryError("Оригинал видео изменился при подготовке.")
            if dest.is_file() and result is not None:
                actual = self.analyze(dest, kind, progress=progress)
                same_rotation = (actual.rotation or 0) == (evidence.rotation or 0)
                if compatible_copy_matches(actual, evidence, profile=profile) and (method != "remux" or same_rotation):
                    decoded = _run(self.ffmpeg, [
                        "-nostdin", "-v", "error", "-xerror", "-threads", "1", *_input(dest, "video/mp4"),
                        "-map", "0:v:0", "-map", "0:a?", "-sn", "-dn", "-progress", "pipe:1", "-nostats",
                        "-f", "null", "-",
                    ], min(7200, max(90, evidence.duration * 5)), progress, duration_ms=duration_ms)
                    if decoded is not None:
                        return replace(actual, method=method)
            dest.unlink(missing_ok=True)
            if shutil.disk_usage(dest.parent).free <= MIN_FREE:
                raise MediaDiskSpace()
        raise RecoveryError("Совместимая копия не прошла проверку типа, потоков или длительности.")


def video_representation(item, decision):
    """Apply the recorded preparation decision to a source-bound candidate."""
    action = decision["action"]
    if action in {"skip", "local", "blocked"}:
        return None
    item = dict(item)
    output = decision.get("output")
    if action == "document":
        item.update(attributes={}, force_file=True, representation="document")
    elif output:
        evidence = VideoEvidence(**output)
        attributes = evidence.attributes(item["kind"])
        if not attributes:
            raise RecoveryError("В пакете отсутствуют подтверждённые атрибуты видео.")
        item.update(attributes=attributes, representation="specialized_document", force_file=False)
        if action == "convert":
            item.update(
                mime="video/mp4", detected_mime="video/mp4", format_confirmed=True,
                size=decision["size"], sha256=decision["sha256"],
                transform={"type": "mp4_h264_aac", "original_sha256": decision["original_sha256"]},
            )
            item["name"] = Path(item["name"]).with_suffix(".mp4").name
    return item


def prepare_video(item, path, policy, tools, progress, work=None):
    original = tools.analyze(
        path, item["kind"], expected_size=item["size"], expected_hash=item["sha256"], progress=progress
    )
    action = (
        "document" if policy == "document" else "keep" if original.playable
        else "skip" if policy == "skip" else "convert" if policy == "compatible"
        and original.status == "transcode_required" and tools.ffmpeg is not None else "blocked"
    )
    record = {"action": action, "original": original.summary(), "original_sha256": item["sha256"]}
    if action == "keep":
        record["output"] = original.summary()
    if action == "convert" and work is not None:
        work.mkdir(exist_ok=True)
        target = work / Path(item["name"]).with_suffix(".mp4").name
        converted = tools.compatibility_copy(
            path, target, item["kind"], original, item["sha256"], progress
        )
        record.update(output=converted.summary(), size=target.stat().st_size, sha256=digest(target))
    return video_representation(item, record), record


def video_options(original, item, tools):
    actions = []
    if original.playable:
        actions.append("original")
    if (original.status in {"compatible", "transcode_required"} and tools.ffmpeg and tools.ffprobe and not original.alpha
            and (item["kind"] != "round_video" or original.width == original.height)):
        actions.extend(("compatible", "fidelity", "compact"))
        if _remux_possible(original):
            actions.append("remux")
    if original.status not in {"missing", "empty", "size_mismatch", "hash_mismatch", "changed", "unreadable"}:
        actions.append("document")
    actions.extend(("local", "skip"))
    return tuple(actions)


def queue_video(item, path, policy, tools, progress, work, queue, entry, explicit=None):
    queue.start(entry["name"])
    original = queue.probe(tools, path, item, progress)
    options = video_options(original, item, tools)
    force_transcode = explicit == "reencode"
    desired = ("compatible" if force_transcode else explicit) or (
        "original" if original.playable and policy not in {"document", "compact"} else policy
    )
    copies = {"compatible", "remux", "fidelity", "compact"}
    action = ("keep" if desired == "original" else "convert" if desired in copies else desired) if desired in options else "blocked"
    problem = "alpha_requires_original" if original.alpha and action == "blocked" else original.status
    if action == "blocked" and original.status == "transcode_required" and not tools.ffmpeg:
        problem = "ffmpeg_unavailable"
    record = {"key": entry["key"], "action": action, "original": original.summary(), "original_sha256": item["sha256"]}
    if action == "keep":
        record["output"] = original.summary()
    if action == "convert" and work is not None:
        work.mkdir(exist_ok=True)
        target = work / Path(item["name"]).with_suffix(".mp4").name
        try:
            record.update(queue.copy(tools, path, target, item, original, progress, force_transcode=force_transcode,
                                     profile=desired))
        except MediaDiskSpace:
            target.unlink(missing_ok=True)
            problem = "disk_space"
        except Cancelled:
            raise
        except (RecoveryError, OSError):
            target.unlink(missing_ok=True)
            problem = "conversion_failed"
        if "output" not in record:
            record["action"] = "blocked"
    if record["action"] == "blocked":
        group = ":".join(str(value or "-") for value in (problem, item["kind"], original.container, original.codec, *options))
        entry.update(group=group, original=original.summary(), options=options, reason=problem,
                     source_kind=item["kind"], prepared_kind=requested_kind(item),
                     recommended="compatible" if "compatible" in options and problem != "conversion_failed"
                     else "document" if "document" in options else "local")
        queue.finish(entry)
    else:
        queue.finish()
    return video_representation(item, record), record


def gge_probe(path, kind, progress=quiet):
    """Call the optional GGE/PyAV helper under the same limits as local tools."""
    if find_spec("PIL" if kind in {"image", "image-copy"} else "av") is None:
        return None
    executable = local(Path(sys.executable))
    return _run(executable, ["-X", "utf8", "-m", "gtr.gge_video", kind, str(path)], 16, progress,
                python_worker=True, cwd=Path(__file__).resolve().parent.parent)


def detailed_probe(path, mime, progress=quiet):
    raw = gge_probe(path, "probe", progress)
    if raw:
        return {"engine": "gge", **json.loads(raw)}
    data = MediaTools().probe(path, mime, progress)
    if data is None:
        return {}
    if mime.startswith("video/"):
        return {"engine": "ffprobe", **interpret_probe(data, mime).summary()}
    return {"engine": "ffprobe", "streams": len(data["streams"]),
            "audio": [s.get("codec_name") for s in data["streams"] if s.get("codec_type") == "audio"]}
