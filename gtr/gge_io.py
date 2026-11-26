"""Bounded media IO and central-directory checks adapted from Gruzin's GGE 1.6.0."""

import io
import struct
import zipfile
from contextlib import contextmanager
from pathlib import Path

from .files import local, safe_parts
from .model import RecoveryError

MAX_DIRECTORY_BYTES = 64 * 1024**2
MAX_DIRECTORY_ENTRIES = 200_000
READ_CHUNK = 1024**2


class BoundedReader(io.RawIOBase):
    """Seek over caller-owned input, bounding each read and the cumulative read budget."""

    def __init__(self, stream, size, budget):
        super().__init__()
        if type(size) is not int or not 0 <= size <= 8 * 1024**3 or not 0 < budget <= 16 * 1024**3:
            raise ValueError("invalid media range")
        self._stream = stream
        self._size = size
        self._budget = budget

    def readable(self):
        return True

    def seekable(self):
        return self._stream.seekable()

    def tell(self):
        return self._stream.tell()

    def seek(self, offset, whence=io.SEEK_SET):
        if whence not in {io.SEEK_SET, io.SEEK_CUR, io.SEEK_END}:
            raise ValueError("invalid seek")
        absolute = offset + (self.tell() if whence == io.SEEK_CUR else self._size if whence == io.SEEK_END else 0)
        if not 0 <= absolute <= self._size:
            raise ValueError("seek outside media")
        return self._stream.seek(absolute)

    def read(self, size=-1):
        amount = min(READ_CHUNK if size is None or size < 0 else min(size, READ_CHUNK), self._size - self.tell())
        if amount > self._budget:
            raise ValueError("media read budget exceeded")
        data = self._stream.read(amount)
        self._budget -= len(data)
        return data


def preflight_zip(source, *, max_entries=MAX_DIRECTORY_ENTRIES):
    """Validate EOCD and actual directory size/count before ZipFile allocates ZipInfo objects."""
    original_position = source.tell()
    try:
        length = source.seek(0, io.SEEK_END)
        end = zipfile._EndRecData(source)
        if not end:
            raise ValueError("Invalid ZIP directory")
        size, entries = end[zipfile._ECD_SIZE], end[zipfile._ECD_ENTRIES_TOTAL]
        if (end[zipfile._ECD_DISK_NUMBER] or end[zipfile._ECD_DISK_START]
                or end[zipfile._ECD_ENTRIES_THIS_DISK] != entries
                or not 0 <= size <= MAX_DIRECTORY_BYTES or not 0 <= entries <= max_entries):
            raise ValueError("ZIP directory limit")
        start = end[zipfile._ECD_LOCATION] - size
        if end[zipfile._ECD_SIGNATURE] == zipfile.stringEndArchive64:
            start -= zipfile.sizeEndCentDir64 + zipfile.sizeEndCentDir64Locator
        if start < 0 or start + size > length:
            raise ValueError("Invalid ZIP directory bounds")
        source.seek(start)
        consumed = count = 0
        while consumed < size:
            header = source.read(46)
            if len(header) != 46 or header[:4] != b"PK\x01\x02":
                raise ValueError("Invalid ZIP directory record")
            fields = sum(struct.unpack_from("<HHH", header, 28))
            consumed += 46 + fields
            count += 1
            if consumed > size or count > max_entries:
                raise ValueError("Truncated or oversized ZIP directory")
            source.seek(fields, io.SEEK_CUR)
        if count != entries:
            raise ValueError("Inconsistent ZIP directory count")
    except (AttributeError, OSError, IndexError, zipfile.BadZipFile, struct.error, ValueError):
        raise RecoveryError("Повреждённый ZIP или превышен предел его каталога.") from None
    finally:
        source.seek(original_position)


@contextmanager
def media_source(path, *, member=None, budget=64 * 1024**2):
    """Read a local file or an individual verified ZIP member without extraction."""
    path = local(Path(path))
    with path.open("rb") as source:
        if member is None:
            import os

            yield BoundedReader(source, os.fstat(source.fileno()).st_size, budget)
            return
        if "/".join(safe_parts(member)) != member:
            raise RecoveryError("Некорректный путь вложения в ZIP.")
        preflight_zip(source)
        with zipfile.ZipFile(source) as archive:
            records = [item for item in archive.infolist() if item.filename == member]
            if (len(records) != 1 or records[0].flag_bits & 1 or records[0].is_dir()
                    or records[0].file_size > min(budget, 64 * 1024**2)):
                raise RecoveryError("Запись ZIP неоднозначна или превышает предел предпросмотра.")
            with archive.open(records[0]) as selected:
                yield BoundedReader(selected, records[0].file_size, budget)
