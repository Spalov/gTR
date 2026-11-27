"""Local media choices, recoverable conversion cache and progress."""

import errno
import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from time import monotonic, sleep

from .files import BLOCK, digest, local, private_directory
from .model import RecoveryError

DECISIONS = {"compatible", "original", "document", "skip", "local", "image", "reencode", "remux", "fidelity", "compact"}
CACHE_PROFILE = "mp4-h264-aac-remux-crf18-autorotate-sar-v1"
CACHE_KEY = re.compile(r"[a-f0-9]{64}")
CACHE_TEMP = ".gtr-media-"
CACHE_MARK = b"gTR owned media v1\n"
CACHE_FILES = {".gtr-media-owner", "video.mp4", "evidence.json"}
MIN_FREE = 32 * 1024**2


class MediaDecisionRequired(RecoveryError):
    def __init__(self, entries):
        self.entries = entries
        super().__init__(f"Выберите обработку для проблемных вложений ({len(entries)}).")


class MediaDiskSpace(RecoveryError):
    def __init__(self):
        super().__init__("Недостаточно свободного места для подготовки видео. Освободите место на диске.")


def validate_decisions(choices):
    if choices is None:
        return {}
    if not isinstance(choices, dict) or any(
        not isinstance(key, str) or not CACHE_KEY.fullmatch(key)
        or not isinstance(value, str) or value not in DECISIONS
        for key, value in choices.items()
    ):
        raise RecoveryError("Недопустимые решения по совместимости медиа.")
    return choices


def video_decision_key(source_sha256, chat_id, source_ordinal, original_digest, grouped=False):
    scope = f"{source_sha256}:{chat_id}:{source_ordinal}:{original_digest}:{int(grouped)}"
    return sha256(scope.encode("utf-8")).hexdigest()


def require_media_space(path, size):
    if shutil.disk_usage(local(Path(path))).free < size:
        raise MediaDiskSpace()


def _cache_root():
    from .owner import app_home

    return local(app_home() / "media-cache")


def _lock_file(stream, unlock=False):
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN if unlock else fcntl.LOCK_EX | fcntl.LOCK_NB)


@contextmanager
def _cache_lease(root, progress=None):
    with local(root / ".lease").open("a+b") as stream:
        while True:
            try:
                _lock_file(stream)
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                if progress is None:
                    raise RecoveryError("Кеш занят подготовкой медиа. Повторите после её завершения.") from None
                progress("media_cache_wait", 0, 0)
                sleep(0.15)
        try:
            yield
        finally:
            _lock_file(stream, unlock=True)


def _cache_entries(root):
    if root.is_dir():
        for path in root.iterdir():
            if not (CACHE_KEY.fullmatch(path.name) or path.name.startswith(CACHE_TEMP)):
                continue
            try:
                work = local(path / "work") if path.name.startswith(CACHE_TEMP) else local(path)
                if _owned(work):
                    yield work
            except RecoveryError:
                continue


def _owned(entry):
    try:
        marker = local(entry / ".gtr-media-owner")
        with marker.open("rb") as stream:
            return stream.read(len(CACHE_MARK) + 1) == CACHE_MARK
    except OSError:
        return False


def _remove_owned(entry):
    if not _owned(entry) or {path.name for path in entry.iterdir()} - CACHE_FILES:
        return False
    leaves = [local(entry / name) for name in CACHE_FILES]
    if any(path.exists() and not path.is_file() for path in leaves):
        return False
    for path in leaves:
        path.unlink(missing_ok=True)
    entry.rmdir()
    if entry.name == "work" and entry.parent.name.startswith(CACHE_TEMP) and not any(entry.parent.iterdir()):
        entry.parent.rmdir()
    return True


@contextmanager
def _new_cache_entry(entry):
    with tempfile.TemporaryDirectory(prefix=CACHE_TEMP, dir=entry.parent) as temporary:
        parent = Path(temporary)
        private_directory(parent)
        work = parent / "work"
        work.mkdir()
        (work / ".gtr-media-owner").write_bytes(CACHE_MARK)
        yield work
        if entry.exists():
            raise RecoveryError("Папка кеша уже занята.")
        work.rename(entry)


def cache_info():
    root = _cache_root()
    size = count = 0
    for path in _cache_entries(root):
        # An entry is published only after the media and its evidence are complete.
        prepared = local(path / "video.mp4")
        try:
            if prepared.is_file():
                size += prepared.stat().st_size
                count += bool(CACHE_KEY.fullmatch(path.name))
        except FileNotFoundError:
            continue
    return {"size": size, "files": count}


def clean_cache(progress=None):
    root = _cache_root()
    if not root.is_dir():
        return {"size": 0, "files": 0}
    with _cache_lease(root, progress):
        result = cache_info()
        for path in list(_cache_entries(root)):
            if progress:
                progress("media_cache_clean", 0, 0)
            _remove_owned(path)
        remaining = cache_info()
        return {key: result[key] - remaining[key] for key in result}


def _tool_state(path):
    if path is None:
        return None
    try:
        info = path.stat()
    except OSError:
        return str(path)
    return str(path), info.st_size, info.st_mtime_ns


def _toolchain(tools):
    return _tool_state(tools.ffmpeg), _tool_state(tools.ffprobe)


def _try_cache(root, tools, kind, original, progress, profile):
    from .video import compatible_copy_matches

    if not _owned(root):
        return None
    path = local(root / "video.mp4")
    meta = local(root / "evidence.json")
    try:
        if meta.stat().st_size > 16384:
            return None
        claim = json.loads(meta.read_text(encoding="utf-8"))
        if not CACHE_KEY.fullmatch(claim["sha256"]) or type(claim["size"]) is not int:
            return None
        actual = tools.analyze(path, kind, expected_size=claim["size"], expected_hash=claim["sha256"], progress=progress)
        if claim.get("method") in {"remux", "transcode"} and compatible_copy_matches(actual, original, profile=profile):
            return path, claim, replace(actual, method=claim["method"])
    except (OSError, KeyError, ValueError, TypeError):
        pass
    return None


def _copy_checked(source, dest, expected, progress):
    require_media_space(dest.parent, expected["size"])
    copied, sha = 0, sha256()
    try:
        with source.open("rb") as original, dest.open("xb") as output:
            while chunk := original.read(BLOCK):
                progress("media_cache_copy", copied, expected["size"])
                copied += len(chunk)
                if copied > expected["size"]:
                    break
                output.write(chunk)
                sha.update(chunk)
            if copied != expected["size"] or sha.hexdigest() != expected["sha256"]:
                raise RecoveryError("Контрольная сумма совместимой копии изменилась.")
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            raise MediaDiskSpace() from None
        raise


class MediaQueue:
    def __init__(self, callback=None):
        self.callback = callback
        self._probes = {}
        self.begin(0, announce=False)

    def begin(self, total, *, announce=True):
        self.total = total
        self.done = self.errors = self.converted = self.reused = 0
        self.entries = []
        self._current = ""
        self._started = monotonic()
        if announce and total:
            self.emit("analysis")

    def emit(self, phase, *, fraction=None, speed=None, eta=None):
        if self.callback is None:
            return
        elapsed = max(0.001, monotonic() - self._started)
        remaining = max(0, self.total - self.done)
        self.callback({
            "phase": phase, "total": self.total, "done": self.done, "current": self._current,
            "fraction": fraction, "speed": speed,
            "eta": eta if eta is not None else (elapsed * remaining / self.done if self.done else None),
            "errors": self.errors, "converted": self.converted, "reused": self.reused,
        })

    def start(self, name):
        self._current = name
        self.total = max(self.total, self.done + 1)
        self.emit("analysis")

    def finish(self, entry=None):
        self.done += 1
        if entry is not None:
            self.entries.append(entry)
            self.errors += entry["reason"] in {"conversion_failed", "disk_space"}
        self.emit("analysis", fraction=1)

    def settle(self, *, ready=False):
        self.total = self.done
        self._current = ""
        self.emit("waiting" if self.entries else "ready" if ready else "continue")

    def probe(self, tools, path, item, progress):
        try:
            info = local(path).stat()
        except FileNotFoundError:
            return tools.analyze(path, item["kind"], expected_size=item["size"], expected_hash=item["sha256"], progress=progress)
        token = (path, info.st_mtime_ns, info.st_size, item["sha256"], item["kind"], _toolchain(tools))
        if token not in self._probes:
            self._probes[token] = tools.analyze(
                path, item["kind"], expected_size=item["size"], expected_hash=item["sha256"], progress=progress,
            )
        return self._probes[token]

    def copy(self, tools, source, target, item, original, progress, *, force_transcode=False, profile="compatible"):
        root = _cache_root()
        try:
            root.mkdir(parents=True)
        except FileExistsError:
            pass
        else:
            private_directory(root)
        keys = (CACHE_PROFILE, item["sha256"], item["kind"], _toolchain(tools), force_transcode, profile)
        cache_name = sha256(json.dumps(keys).encode("utf-8")).hexdigest()
        entry = local(root / cache_name)
        source = local(source)
        target = local(target)
        require_media_space(target.parent, MIN_FREE)
        checkpoint = lambda: progress("verify_video_source", 0, item["size"])
        if digest(source, checkpoint) != item["sha256"]:
            raise RecoveryError("Оригинал видео изменился после анализа.")
        with _cache_lease(root, progress):
            self.emit("verify")
            result = _try_cache(entry, tools, item["kind"], original, progress, profile)
            if result is None:
                if entry.exists() and not _remove_owned(entry):
                    raise RecoveryError("Папка кеша занята файлами другого приложения.")
                require_media_space(root, min(2 * 1024**3, item["size"] + MIN_FREE))
                self.emit("convert", fraction=0)

                def report(stage, done, total):
                    progress(stage, done, total)
                    if stage == "convert_video" and total:
                        fraction = min(1, done / total)
                        elapsed = max(0.001, monotonic() - started)
                        speed = done / (elapsed * 1000)
                        self.emit("convert", fraction=fraction, speed=speed,
                                  eta=(total - done) / (1000 * speed) if speed else None)

                started = monotonic()
                with _new_cache_entry(entry) as work:
                    output = work / "video.mp4"
                    actual = tools.compatibility_copy(
                        source, output, item["kind"], original, item["sha256"], report,
                        force_transcode=force_transcode, **({"profile": profile} if profile != "compatible" else {}),
                    )
                    self.emit("verify")
                    claim = {"sha256": digest(output, checkpoint), "size": output.stat().st_size, "method": actual.method}
                    (work / "evidence.json").write_text(json.dumps(claim), encoding="utf-8")
                result = entry / "video.mp4", claim, actual
                self.converted += 1
            else:
                self.reused += 1
            cached, claim, actual = result
            _copy_checked(cached, target, claim, progress)
        if digest(source, checkpoint) != item["sha256"]:
            target.unlink(missing_ok=True)
            raise RecoveryError("Оригинал видео изменился при подготовке.")
        return {"output": actual.summary(), **claim}
