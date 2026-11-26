"""Read-only media facts and compatibility checks, independent of the UI and transport."""

import hashlib
import io
import json
import mimetypes
import os
import re
import struct
import wave
import zlib
from collections import Counter
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .archive import MAX_FILE_BYTES
from .files import BLOCK, local, safe_parts
from .gge_metadata import jpeg_capture_date
from .media import identify
from .model import Cancelled, RecoveryError, quiet

HEADER_BYTES = 256 * 1024
MAX_INSPECTION_ITEMS = 20_000
PICTURE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MIME_LABEL = re.compile(r"[a-zA-Z0-9.+-]{1,60}/[a-zA-Z0-9.+-]{1,100}")
MEDIA_ISSUES = {
    "missing": "Файл отсутствует",
    "unreadable": "Файл недоступен или путь небезопасен",
    "empty": "Файл пуст",
    "oversized": "Превышен локальный предел размера",
    "size_mismatch": "Размер расходится с каталогом",
    "hash_mismatch": "SHA-256 расходится с каталогом",
    "changed": "Файл изменился во время проверки",
    "corrupt_container": "Обнаружено повреждение структуры файла",
    "metadata_unavailable": "Метаданные недоступны при ограниченном чтении",
    "unconfirmed_type": "Сигнатура формата не распознана",
    "mime_mismatch": "Объявленный MIME расходится с типом по содержимому",
    "extension_mismatch": "Расширение имени расходится с типом содержимого",
    "duplicate": "Эти же байты есть у другого вложения",
    "unsupported_representation": "Файл не подходит для выбранного типа отправки",
}
FILE_BLOCKERS = {"missing", "unreadable", "empty", "oversized", "size_mismatch", "hash_mismatch", "changed"}


class MalformedMedia(ValueError):
    pass


@dataclass(frozen=True)
class MediaFacts:
    size: int | None = None
    sha256: str | None = None
    mime: str | None = None
    declared_mime: str | None = None
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    sample_rate: int | None = None
    channels: int | None = None
    orientation: int | None = None
    audio_codec: str | None = None
    alpha: bool | None = None
    animated: bool | None = None
    capture_date: dict | None = None
    issues: tuple[str, ...] = ()

    @property
    def intact(self):
        return FILE_BLOCKERS.isdisjoint(self.issues)


def _tail(stream, size, limit):
    stream.seek(max(0, size - limit))
    return stream.read(min(size, limit))


def _orientation(head):
    pos = 2
    while pos + 4 <= len(head) and head[pos] == 255:
        marker = head[pos + 1]
        length = int.from_bytes(head[pos + 2 : pos + 4], "big")
        if marker in (0xDA, 0xD9) or length < 2 or pos + 2 + length > len(head):
            break
        if marker == 0xE1 and head[pos + 4 : pos + 10] == b"Exif\0\0":
            tiff = head[pos + 10 : pos + 2 + length]
            if tiff[:4] not in (b"II*\0", b"MM\0*"):
                return None
            order = "little" if tiff[:2] == b"II" else "big"
            start = int.from_bytes(tiff[4:8], order)
            entries = int.from_bytes(tiff[start : start + 2], order)
            for i in range(min(entries, 1024)):
                at = start + 2 + 12 * i
                entry = tiff[at : at + 12]
                if len(entry) < 12:
                    break
                if [int.from_bytes(entry[a:b], order) for a, b in ((0, 2), (2, 4), (4, 8))] == [274, 3, 1]:
                    value = int.from_bytes(entry[8:10], order)
                    return value if 1 <= value <= 8 else None
        pos += length + 2
    return None


def _jpeg_size(head):
    pos = 2
    while pos + 4 <= len(head):
        if head[pos] != 255:
            break
        pos += 1
        while pos < len(head) and head[pos] == 255:
            pos += 1
        if pos + 3 > len(head):
            break
        marker, start = head[pos], pos + 1
        length = int.from_bytes(head[start : start + 2], "big")
        if marker in {0xDA, 0xD9} or length < 2 or start + length > len(head):
            break
        if (
            marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
            and length >= 8
        ):
            height, width = struct.unpack_from(">HH", head, start + 3)
            return width, height
        pos = start + length
    return None


def _image(stream, head, size, mime):
    details = {}
    if mime == "image/png":
        if (
            len(head) < 33
            or head[8:16] != b"\0\0\0\rIHDR"
            or zlib.crc32(head[12:29]) != int.from_bytes(head[29:33], "big")
            or _tail(stream, size, 12) != b"\0\0\0\0IEND\xaeB`\x82"
        ):
            raise MalformedMedia
        dims = struct.unpack_from(">II", head, 16)
        details["alpha"] = head[25] in (4, 6) or b"tRNS" in head
        details["animated"] = b"acTL" in head
    elif mime == "image/jpeg":
        dims = _jpeg_size(head)
        if b"\xff\xd9" not in _tail(stream, size, 64 * 1024) or (dims is None and size <= len(head)):
            raise MalformedMedia
    elif mime == "image/gif":
        if len(head) < 14 or _tail(stream, size, 1) != b";":
            raise MalformedMedia
        dims = struct.unpack_from("<HH", head, 6)
    else:
        if len(head) < 20 or int.from_bytes(head[4:8], "little") + 8 > size:
            raise MalformedMedia
        magic = head[12:16]
        dims = None
        if magic == b"VP8X" and len(head) >= 30:
            dims = tuple(int.from_bytes(head[i : i + 3], "little") + 1 for i in (24, 27))
            details.update(alpha=bool(head[20] & 0x10), animated=bool(head[20] & 0x02))
        elif magic == b"VP8L" and len(head) >= 25 and head[20] == 0x2F:
            shape = int.from_bytes(head[21:25], "little")
            dims = (1 + (shape & 0x3FFF), 1 + ((shape >> 14) & 0x3FFF))
            details.update(alpha=bool(shape & (1 << 28)), animated=False)
        elif magic == b"VP8 " and len(head) >= 30 and head[23:26] == b"\x9d\x01\x2a":
            dims = tuple(value & 0x3FFF for value in struct.unpack_from("<HH", head, 26))
    if dims is not None and not all(dims):
        raise MalformedMedia
    return {**details, "width": dims[0], "height": dims[1]} if dims else {"issues": ("metadata_unavailable",)}


def _box_headers(stream, start, end, budget):
    while start + 8 <= end and budget[0]:
        budget[0] -= 1
        stream.seek(start)
        header = stream.read(8)
        if len(header) != 8:
            raise MalformedMedia
        length, kind = struct.unpack(">I4s", header)
        width = 8
        if length == 1:
            extended = stream.read(8)
            if len(extended) != 8:
                raise MalformedMedia
            length = int.from_bytes(extended, "big")
            width = 16
        elif length == 0:
            length = end - start
        if not width <= length <= end - start:
            raise MalformedMedia
        yield kind, start + width, start + length
        start += length


def _mp4(stream, size):
    fields = {}
    budget = [2048]
    for kind, start, end in _box_headers(stream, 0, size, budget):
        if kind != b"moov":
            continue
        for nested, body, stop in _box_headers(stream, start, end, budget):
            if nested == b"mvhd":
                stream.seek(body)
                header = stream.read(min(36, stop - body))
                version = header[0] if header else -1
                if version in (0, 1) and len(header) >= (32 if version == 1 else 20):
                    rate, ticks = struct.unpack_from(
                        ">IQ" if version else ">II", header, 20 if version else 12
                    )
                    unknown = (1 << (64 if version else 32)) - 1
                    if rate and ticks != unknown:
                        fields["duration"] = round(ticks / rate, 3)
            elif nested == b"trak":
                for track, data, last in _box_headers(stream, body, stop, budget):
                    if track == b"tkhd" and last - data >= 84:
                        stream.seek(last - 8)
                        width, height = (value >> 16 for value in struct.unpack(">II", stream.read(8)))
                        if width and height:
                            fields.update(width=width, height=height)
        break
    return fields or {"issues": ("metadata_unavailable",)}


def _audio(stream, head, size, mime):
    if mime == "audio/wav":
        if int.from_bytes(head[4:8], "little") + 8 > size:
            raise MalformedMedia
        with wave.open(io.BytesIO(head)) as wav:
            rate = wav.getframerate()
            return {
                "sample_rate": rate,
                "channels": wav.getnchannels(),
                "duration": round(wav.getnframes() / rate, 3),
            }
    if len(head) < 27 or head[4] != 0:
        raise MalformedMedia
    opus = head.find(b"OpusHead", 27, 512)
    vorbis = head.find(b"\x01vorbis", 27, 512)
    if opus != -1 and len(head) >= opus + 19:
        rate, channels, skip = 48000, head[opus + 9], int.from_bytes(head[opus + 10 : opus + 12], "little")
        codec = "opus"
    elif vorbis != -1 and len(head) >= vorbis + 30:
        rate = int.from_bytes(head[vorbis + 12 : vorbis + 16], "little")
        channels, skip = head[vorbis + 11], 0
        codec = "vorbis"
    else:
        return {"issues": ("metadata_unavailable",)}
    if not rate or not channels:
        raise MalformedMedia
    result = {"sample_rate": rate, "channels": channels, "audio_codec": codec}
    tail = _tail(stream, size, 128 * 1024)
    end = tail.rfind(b"OggS")
    if end >= 0 and len(tail) >= end + 27 and tail[end + 14 : end + 18] == head[14:18]:
        ticks = int.from_bytes(tail[end + 6 : end + 14], "little", signed=True)
        if ticks >= skip:
            result["duration"] = round((ticks - skip) / rate, 3)
    return result


def _tgs(head, size):
    if size > len(head):
        raise MalformedMedia
    stream = zlib.decompressobj(16 + zlib.MAX_WBITS)
    raw = stream.decompress(head, 1024**2 + 1)
    if not stream.eof or stream.unused_data or len(raw) > 1024**2:
        raise MalformedMedia
    lottie = json.loads(raw)
    if not isinstance(lottie, dict) or not isinstance(lottie.get("layers"), list):
        raise MalformedMedia
    w, h, fps, start, end = (lottie.get(key) for key in ("w", "h", "fr", "ip", "op"))
    if not all(type(n) in (int, float) for n in (w, h, fps, start, end)):
        raise MalformedMedia
    if not (0 < w <= 512 and 0 < h <= 512 and 0 < fps <= 120 and 0 <= start < end <= start + 60 * fps):
        raise MalformedMedia
    return {"width": int(w), "height": int(h), "duration": (end - start) / fps, "animated": True}


def _metadata(stream, head, size, mime):
    try:
        if mime in PICTURE_TYPES:
            return _image(stream, head, size, mime)
        if mime in {"video/mp4", "video/quicktime", "audio/mp4"}:
            return _mp4(stream, size)
        if mime in {"audio/wav", "audio/ogg"}:
            return _audio(stream, head, size, mime)
        if mime == "application/x-tgsticker":
            return _tgs(head, size)
    except (EOFError, ValueError, RecursionError, struct.error, wave.Error, zlib.error):
        return {"issues": ("corrupt_container",)}
    return {}


def inspect(
    path: Path,
    *,
    expected_size=None,
    expected_hash=None,
    declared_mime=None,
    hash_verified=False,
    checkpoint=None,
) -> MediaFacts:
    declared_mime = (
        declared_mime if isinstance(declared_mime, str) and MIME_LABEL.fullmatch(declared_mime) else None
    )
    try:
        path = local(Path(path))
        if not path.is_file():
            return MediaFacts(declared_mime=declared_mime, issues=("missing",))
        with path.open("rb") as stream:
            initial = os.fstat(stream.fileno())
            size = initial.st_size
            if size == 0 or size > MAX_FILE_BYTES:
                return MediaFacts(
                    size=size, declared_mime=declared_mime, issues=("empty" if size == 0 else "oversized",)
                )
            head = stream.read(min(size, HEADER_BYTES))
            mime, recognized = identify(path.name, head)
            metadata = _metadata(stream, head, size, mime) if recognized else {}
            if mime == "image/jpeg":
                metadata["orientation"] = _orientation(head)
                metadata["capture_date"] = jpeg_capture_date(head)
            issues = list(metadata.pop("issues", ()))
            if expected_size is not None and size != expected_size:
                issues.append("size_mismatch")
            if not recognized:
                issues.append("unconfirmed_type")
            if recognized and declared_mime and declared_mime not in (mime, "application/octet-stream"):
                issues.append("mime_mismatch")
            sha = expected_hash if hash_verified else _hash_stream(stream, size, checkpoint)
            final = os.fstat(stream.fileno())
            if sha is None or (initial.st_size, initial.st_mtime_ns) != (final.st_size, final.st_mtime_ns):
                issues.append("changed")
            elif expected_hash is not None and sha != expected_hash:
                issues.append("hash_mismatch")
            return MediaFacts(
                size, sha, mime if recognized else None, declared_mime, **metadata, issues=tuple(issues)
            )
    except Cancelled:
        raise
    except (OSError, RecoveryError):
        return MediaFacts(declared_mime=declared_mime, issues=("unreadable",))


def _hash_stream(stream, size, checkpoint):
    stream.seek(0)
    sha = hashlib.sha256()
    remaining = size
    while remaining:
        if checkpoint:
            checkpoint()
        chunk = stream.read(min(remaining, BLOCK))
        if not chunk:
            return None
        sha.update(chunk)
        remaining -= len(chunk)
    return sha.hexdigest()


def archive_paths(item, record):
    paths = [
        item.get("source"),
        *(member.get("source") for member in item.get("members", [])),
        *(asset.get("path") for asset in record.get("original_media", [])),
    ]
    result = []
    for name in dict.fromkeys(path for path in paths if isinstance(path, str)):
        if 0 < len(name) <= 1024 and len(result) < 32:
            try:
                result.append("/".join(safe_parts(name)))
            except RecoveryError:
                continue
    return result


def _supported(item, facts):
    if item["upload_as"] == "photo":
        return (
            facts.mime in {"image/jpeg", "image/png"}
            and "corrupt_container" not in facts.issues
            and (facts.width is None or facts.width * facts.height <= 64_000_000)
        )
    return not item.get("attributes") or "corrupt_container" not in facts.issues


def inspect_bundle(bundle, progress=quiet, *, hash_verified=False):
    media = bundle.data["media"]
    if len(media) > MAX_INSPECTION_ITEMS:
        raise RecoveryError(f"Выберите пакет не более чем с {MAX_INSPECTION_ITEMS} вложениями.")
    records = {r.get("media"): r for r in bundle.data["records"] if r.get("media")}
    items, first_seen, totals = [], {}, Counter()
    for index, item in enumerate(media, 1):
        progress("inspect_media", index, len(media))
        facts = inspect(
            bundle.media_path(item),
            expected_size=item["size"],
            expected_hash=item["sha256"],
            declared_mime=item.get("declared_mime") or item.get("mime"),
            hash_verified=hash_verified,
            checkpoint=lambda index=index: progress("inspect_media", index, len(media)),
        )
        supported = _supported(item, facts)
        duplicate = first_seen.setdefault(facts.sha256, index) if facts.sha256 and facts.intact else index
        extra = ([] if supported else ["unsupported_representation"]) + (
            ["duplicate"] if duplicate != index else []
        )
        from .filenames import display_name

        extension_mime = mimetypes.guess_type(display_name(item), strict=False)[0]
        if facts.mime and extension_mime and extension_mime != facts.mime:
            extra.append("extension_mismatch")
        facts = replace(facts, issues=(*facts.issues, *extra))
        totals.update(facts.issues)
        record = records.get(item["name"], {})
        items.append(
            {
                "index": index,
                "name": item["name"],
                "source_ordinal": record.get("source_ordinal"),
                "archive_paths": archive_paths(item, record),
                "representation": item["upload_as"],
                "input_media": "InputMediaUploadedPhoto"
                if item["upload_as"] == "photo"
                else "InputMediaUploadedDocument",
                "server_compatibility": "unverified",
                "blocked": not facts.intact or not supported,
                "duplicate_of": duplicate if duplicate != index else None,
                **asdict(facts),
            }
        )
    omitted = Counter(
        a["status"]
        for r in bundle.data["records"]
        for a in r.get("original_media", [])
        if a.get("status") in {"missing", "empty", "unsafe", "size_mismatch", "type_mismatch"}
    )
    return {
        "items": items,
        "issues": dict(totals),
        "source_issues": dict(omitted),
        "blocked": sum(item["blocked"] for item in items),
        "count": len(items),
    }


def inspect_project(project: Path, chat: int | None = None, progress=quiet):
    from .core import catalogue

    project = local(Path(project))
    items, cache, first_seen, issues = [], {}, {}, Counter()
    with catalogue(project) as db:
        query = """SELECT a.path,a.status,a.expected_mime,f.size,f.sha256,m.source_id
                   FROM assets a JOIN messages m ON m.id=a.msg LEFT JOIN files f ON f.path=a.path
                   WHERE a.status!='metadata' AND (? IS NULL OR m.chat=?)"""
        total = db.execute(f"SELECT count(*) FROM ({query})", (chat, chat)).fetchone()[0]
        for index, row in enumerate(
            db.execute(f"{query} ORDER BY a.id LIMIT ?", (chat, chat, MAX_INSPECTION_ITEMS)), 1
        ):
            progress("inspect_media", index, min(total, MAX_INSPECTION_ITEMS))
            path, status = row["path"], row["status"]
            key = (path, row["expected_mime"])
            if not path or status == "unsafe":
                facts = MediaFacts(issues=("unreadable" if status == "unsafe" else "missing",))
            else:
                try:
                    if key not in cache:
                        cache[key] = inspect(
                            project.joinpath("source", *safe_parts(path)),
                            expected_size=row["size"],
                            expected_hash=row["sha256"],
                            declared_mime=row["expected_mime"],
                            checkpoint=lambda index=index: progress(
                                "inspect_media", index, min(total, MAX_INSPECTION_ITEMS)
                            ),
                        )
                    facts = cache[key]
                except Cancelled:
                    raise
                except RecoveryError:
                    facts = MediaFacts(issues=("unreadable",))
            first = first_seen.setdefault(facts.sha256, index) if facts.sha256 and facts.intact else index
            notices = tuple(dict.fromkeys((*facts.issues, *(("duplicate",) if first != index else ()))))
            issues.update(notices)
            items.append(
                {
                    "index": index,
                    "archive_paths": archive_paths({"source": path}, {}),
                    "source_id": row["source_id"],
                    **asdict(facts),
                    "issues": notices,
                    "duplicate_of": first if first != index else None,
                    "blocked": not facts.intact,
                }
            )
    return {
        "items": items,
        "count": len(items),
        "total": total,
        "truncated": total > len(items),
        "blocked": sum(item["blocked"] for item in items),
        "issues": dict(issues),
        "source_issues": {},
    }


def public_inventory(report):
    return {
        **report,
        "items": [
            {
                key: value
                for key, value in item.items()
                if key not in {"archive_paths", "capture_date", "sha256", "source_id", "source_ordinal", "name"}
            }
            for item in report["items"]
        ],
    }
