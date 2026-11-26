"""Allowlisted capture dates, adapted from GGE media_dates/gme_dates/gme_extract."""

import re
from datetime import datetime, timedelta, timezone

_EXIF = re.compile(r"\d{4}:\d\d:\d\d \d\d:\d\d:\d\d")
_OFFSET = re.compile(r"([+-])(\d\d):(\d\d)")
_ISO = re.compile(r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:\.\d{1,9})?(?:Z|[+-]\d\d:\d\d)?")


def capture_date(raw, offset=None, *, exif=False):
    """A wall time without offset remains a wall time in the local report."""
    if type(raw) is not str or len(raw) > 48:
        return None
    try:
        if exif:
            if not _EXIF.fullmatch(raw):
                return None
            value = datetime.strptime(raw, "%Y:%m:%d %H:%M:%S")
            if isinstance(offset, str) and (match := _OFFSET.fullmatch(offset)):
                hours, minutes = int(match[2]), int(match[3])
                if minutes < 60 and hours * 60 + minutes <= 14 * 60:
                    delta = timedelta(hours=hours, minutes=minutes)
                    value = value.replace(tzinfo=timezone(-delta if match[1] == "-" else delta))
        else:
            if not _ISO.fullmatch(raw):
                return None
            value = datetime.fromisoformat(raw)
    except (ValueError, OverflowError):
        return None
    if value.year < 1900:
        return None
    return {"date": value.isoformat(), "timezone_known": value.tzinfo is not None,
            "source": "exif_original" if exif else "container_creation_time"}


def jpeg_capture_date(head):
    # Only EXIF DateTimeOriginal and OffsetTimeOriginal are read. IFD1, GPS and MakerNote are ignored.
    position = 2
    while position + 4 <= len(head) and head[position] == 255:
        marker = head[position + 1]
        end = position + 2 + int.from_bytes(head[position + 2:position + 4], "big")
        if marker in (0xDA, 0xD9) or not position + 4 <= end <= len(head):
            return None
        if marker == 0xE1 and head[position + 4:position + 10] == b"Exif\0\0":
            return _tiff_date(head[position + 10:end])
        position = end
    return None


def _tiff_date(tiff):
    if tiff[:4] not in (b"II*\0", b"MM\0*") or len(tiff) < 8:
        return None
    byteorder = "little" if tiff[:2] == b"II" else "big"

    def number(at, size):
        return int.from_bytes(tiff[at:at + size], byteorder)

    def tags(at):
        if 8 <= at <= len(tiff) - 2:
            for index in range(min(512, number(at, 2))):
                entry = at + 2 + 12 * index
                if entry + 12 > len(tiff):
                    return
                yield number(entry, 2), number(entry + 2, 2), number(entry + 4, 4), entry + 8

    exif = next((number(at, 4) for tag, form, count, at in tags(number(4, 4))
                 if (tag, form, count) == (0x8769, 4, 1)), 0)
    fields = {}
    for tag, form, count, at in tags(exif):
        if tag not in (0x9003, 0x9011) or form != 2 or not 1 <= count <= 32:
            continue
        start = at if count <= 4 else number(at, 4)
        if start + count <= len(tiff):
            fields[tag] = tiff[start:start + count].rstrip(b"\0").decode("ascii", "replace")
    return capture_date(fields.get(0x9003), fields.get(0x9011), exif=True)
