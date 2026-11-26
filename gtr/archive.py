"""Read-only sources. All archive formats pass through the same bounded copy."""

import ctypes as ct
import hashlib
import os
import shutil
import stat
import zipfile
from ctypes.util import find_library
from pathlib import Path

from .files import BLOCK, digest, is_link, local, name_key, safe_parts
from .gge_io import preflight_zip
from .media import identify
from .model import FileInfo, Progress, RecoveryError, Snapshot, quiet

MAX_FILES = 200_000
MAX_FILE_BYTES = 2 * 1024**3
MAX_TOTAL_BYTES = 8 * 1024**3


def stream_chunks(stream):
    while chunk := stream.read(BLOCK):
        yield chunk


def folder_entries(root):
    def walk(folder):
        for path in sorted(folder.iterdir(), key=lambda p: p.name):
            if is_link(path):
                raise RecoveryError("В исходной папке обнаружена ссылка.")
            name = path.relative_to(root).as_posix()
            if path.is_dir():
                yield name, None, ()
                yield from walk(path)
            elif path.is_file():
                with path.open("rb") as stream:
                    before = os.fstat(stream.fileno())
                    yield name, before.st_size, stream_chunks(stream)
                    after = os.fstat(stream.fileno())
                    if before.st_mtime_ns != after.st_mtime_ns or before.st_size != after.st_size:
                        raise RecoveryError("Исходный файл изменился при чтении.")
            else:
                raise RecoveryError("В исходной папке обнаружен специальный файл.")

    yield from walk(root)


def selected_entries(root, names):
    for name in names:
        path = local(root.joinpath(*safe_parts(name)))
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            yield name, before.st_size, stream_chunks(stream)
            after = os.fstat(stream.fileno())
            if before.st_mtime_ns != after.st_mtime_ns or before.st_size != after.st_size:
                raise RecoveryError("Файл экспорта изменился при чтении.")


def zip_entries(path):
    with path.open("rb") as source:
        preflight_zip(source, max_entries=MAX_FILES)
        with zipfile.ZipFile(source) as archive:
            yield from _zip_members(archive)


def _zip_members(archive):
    for entry in archive.infolist():
        mode = entry.external_attr >> 16
        if (
            entry.flag_bits & 1
            or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)
            or entry.external_attr & 0x400
        ):
            raise RecoveryError("Зашифрованные файлы, ссылки и специальные записи не поддерживаются.")
        if entry.is_dir():
            yield entry.filename.rstrip("/"), None, ()
        else:
            with archive.open(entry) as stream:
                yield entry.filename, entry.file_size, stream_chunks(stream)


def rar_entries(path):
    """Use the system libarchive reader. Never ask a native library to write paths."""
    if os.name == "nt":
        library = str(Path(os.environ["SystemRoot"]) / "System32" / "archiveint.dll")
    else:
        library = find_library("archive")
    try:
        if not library:
            raise OSError
        lib = ct.CDLL(library)
        ptr = ct.c_void_p

        def bind(name, result, *args):
            fn = getattr(lib, "archive_" + name)
            fn.restype, fn.argtypes = result, args
            return fn

        create = bind("read_new", ptr)
        close = bind("read_free", ct.c_int, ptr)
        rar = bind("read_support_format_rar", ct.c_int, ptr)
        rar5 = bind("read_support_format_rar5", ct.c_int, ptr)
        open_file = bind("read_open_filename_w", ct.c_int, ptr, ct.c_wchar_p, ct.c_size_t)
        next_entry = bind("read_next_header", ct.c_int, ptr, ct.POINTER(ptr))
        pathname = bind("entry_pathname_w", ct.c_wchar_p, ptr)
        filetype = bind("entry_filetype", ct.c_uint, ptr)
        hardlink = bind("entry_hardlink_w", ct.c_wchar_p, ptr)
        encrypted = bind("entry_is_encrypted", ct.c_int, ptr)
        size = bind("entry_size", ct.c_int64, ptr)
        read = bind("read_data", ct.c_ssize_t, ptr, ptr, ct.c_size_t)
    except (OSError, AttributeError):
        raise RecoveryError("Системный модуль RAR недоступен. Можно открыть распакованную папку.") from None
    handle = create()
    if not handle:
        raise RecoveryError("Не удалось открыть модуль RAR.")
    try:
        if min(rar(handle), rar5(handle), open_file(handle, str(path), BLOCK)) < 0:
            raise RecoveryError("Не удалось прочитать RAR. Проверьте целостность и шифрование.")
        entry, buffer = ptr(), ct.create_string_buffer(BLOCK)

        def chunks():
            while True:
                count = read(handle, buffer, BLOCK)
                if count < 0:
                    raise RecoveryError("Ошибка чтения данных RAR.")
                if count == 0:
                    break
                yield buffer.raw[:count]

        while True:
            rc = next_entry(handle, ct.byref(entry))
            if rc == 1:  # ARCHIVE_EOF
                break
            if rc != 0:
                raise RecoveryError("Повреждён заголовок RAR.")
            kind, name = filetype(entry), pathname(entry)
            if not name or hardlink(entry) or encrypted(entry) or kind not in (stat.S_IFDIR, stat.S_IFREG):
                raise RecoveryError("RAR содержит неподдерживаемую или зашифрованную запись.")
            if kind == stat.S_IFDIR:
                yield name.rstrip("/\\"), None, ()
            else:
                yield name, size(entry), chunks()
    finally:
        close(handle)


def snapshot(source: Path, target: Path, progress: Progress = quiet, *, selection=None) -> Snapshot:
    source = local(source)
    folder = source.is_dir()
    if not folder and not source.is_file():
        raise RecoveryError("Исходный файл не найден.")
    kind = "directory" if folder else source.suffix.lower().lstrip(".")
    readers = {"directory": folder_entries, "zip": zip_entries, "rar": rar_entries}
    if kind not in readers:
        raise RecoveryError("Выберите RAR, ZIP или папку экспорта.")
    progress("archive", 0, 0)
    source_hash = "" if folder else digest(source)
    target.mkdir()
    free = shutil.disk_usage(target).free
    files, seen, spelling = {}, set(), {}
    total = 0
    try:
        items = readers[kind](source) if selection is None else selected_entries(source, selection)
        for raw, size, chunks in items:
            raw = raw.replace("\\", "/")
            while raw.startswith("./"):
                raw = raw[2:]
            if raw in ("", ".") and size is None:
                continue
            parts = safe_parts(raw)
            name = "/".join(parts)
            key = name_key(name)
            if key in seen or len(seen) >= MAX_FILES:
                raise RecoveryError("Повторяющееся имя или превышен лимит записей архива.")
            seen.add(key)
            for i in range(1, len(parts) + 1):
                segment = "/".join(parts[:i])
                if spelling.setdefault(name_key(segment), segment) != segment:
                    raise RecoveryError("Имена путей неоднозначны в Windows.")
            out = target.joinpath(*parts)
            if size is None:
                out.mkdir(parents=True, exist_ok=True)
                continue
            if not 0 <= size <= MAX_FILE_BYTES or total + size > MAX_TOTAL_BYTES:
                raise RecoveryError("Превышен лимит размера распаковки.")
            if total + size > free:
                raise RecoveryError("Недостаточно места для рабочей копии архива.")
            out.parent.mkdir(parents=True, exist_ok=True)
            sha, written, header = hashlib.sha256(), 0, b""
            with out.open("xb") as sink:
                for chunk in chunks:
                    progress("archive", len(files), 0)
                    written += len(chunk)
                    if written > size:
                        raise RecoveryError("Размер файла превышает размер из заголовка.")
                    if len(header) < 128:
                        header += chunk[: 128 - len(header)]
                    sink.write(chunk)
                    sha.update(chunk)
            if written != size:
                raise RecoveryError("Неполные данные файла в архиве.")
            files[name] = FileInfo(size, sha.hexdigest(), *identify(name, header))
            total += written
            progress("archive", len(files), 0)
    except (OSError, zipfile.BadZipFile, EOFError, RuntimeError, NotImplementedError) as error:
        raise RecoveryError("Ошибка чтения архива или записи локальной копии.") from error
    if folder:
        tree = hashlib.sha256()
        for name, info in sorted(files.items()):
            tree.update(name.encode() + b"\0" + bytes.fromhex(info.sha256))
        source_hash = tree.hexdigest()
    elif source_hash != digest(source):
        raise RecoveryError("Контрольная сумма исходного архива изменилась во время чтения.")
    return Snapshot(target, files, source_hash, total if folder else source.stat().st_size, kind)
