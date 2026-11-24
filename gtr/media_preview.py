"""Bounded local thumbnails. Optional media tools receive only a checked local file."""

import io
import struct
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Protocol

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize, Qt
from PySide6.QtGui import QColor, QImage, QImageReader, QPainter, QPen

from .files import local, private_directory
from .media_engine import HEADER_BYTES, PICTURE_TYPES, inspect
from .model import RecoveryError, quiet

MAX_PIXELS = 16_000_000
MAX_FRAME_BYTES = 1024 * 1024
DEMUXERS = {
    "video/mp4": "mov",
    "video/quicktime": "mov",
    "video/webm": "matroska",
    "video/x-matroska": "matroska",
}


class VideoThumbnailProvider(Protocol):
    """Optional gFL or local-tool adapter. Return one PNG within the byte budget."""

    def frame(self, source: Path, detected_mime: str) -> bytes | None: ...


class FFmpegThumbnail:
    def __init__(self, executable: Path):
        self.executable = local(Path(executable))
        if self.executable.name.casefold() not in {"ffmpeg.exe", "ffmpeg"} or not self.executable.is_file():
            raise RecoveryError("Выберите установленную программу ffmpeg для локального предпросмотра.")

    def frame(self, source: Path, detected_mime: str) -> bytes | None:
        format_name = DEMUXERS.get(detected_mime)
        if not format_name:
            return None
        source = local(source)
        with tempfile.TemporaryDirectory(prefix="gtr-frame-") as tmp:
            root = Path(tmp)
            private_directory(root)
            frame = root / "frame.png"
            cmd = [
                str(self.executable),
                "-nostdin",
                "-loglevel",
                "error",
                "-protocol_whitelist",
                "file",
                "-f",
                format_name,
                "-threads",
                "1",
            ]
            if format_name == "mov":
                cmd.extend(("-enable_drefs", "0", "-use_absolute_path", "0"))
            cmd.extend(
                (
                    "-i",
                    str(source),
                    "-map",
                    "0:v:0",
                    "-an",
                    "-sn",
                    "-dn",
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=480:300:force_original_aspect_ratio=decrease",
                    "-threads",
                    "1",
                    "-fs",
                    str(MAX_FRAME_BYTES),
                    str(frame),
                )
            )
            try:
                result = subprocess.run(
                    cmd,
                    cwd=root,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=12,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if result.returncode == 0 and frame.is_file() and frame.stat().st_size <= MAX_FRAME_BYTES:
                    return frame.read_bytes()
            except (OSError, subprocess.TimeoutExpired):
                pass
        return None


def _scaled_image(reader):
    reader.setDecideFormatFromContent(True)
    reader.setAutoTransform(True)
    size = reader.size()
    if not size.isValid() or not 0 < size.width() * size.height() <= MAX_PIXELS:
        return QImage()
    reader.setScaledSize(size.scaled(QSize(640, 420), Qt.AspectRatioMode.KeepAspectRatio))
    return reader.read()


def _waveform(path):
    try:
        with path.open("rb") as source:
            header = source.read(HEADER_BYTES)
        with wave.open(io.BytesIO(header)) as stream:
            if stream.getsampwidth() != 2:
                return QImage()
            data = stream.readframes(min(stream.getnframes(), stream.getframerate() * 2))
        if len(data) % 2 or not data:
            return QImage()
    except (EOFError, OSError, wave.Error):
        return QImage()
    samples = [abs(v) for (v,) in struct.iter_unpack("<h", data)]
    stride = max(1, len(samples) // 120)
    image = QImage(480, 120, QImage.Format.Format_RGB32)
    image.fill(QColor("#202a2e"))
    painter = QPainter(image)
    try:
        painter.setPen(QPen(QColor("#e6bb75"), 2))
        for x in range(120):
            block = samples[x * stride : (x + 1) * stride]
            peak = max(1, int(max(block, default=0) / 32768 * 54))
            painter.drawLine(2 + x * 4, 60 - peak, 2 + x * 4, 60 + peak)
    finally:
        painter.end()
    return image


def preview(path, expected_hash, declared_mime=None, *, provider=None, progress=quiet):
    path = local(Path(path))
    facts = inspect(
        path,
        expected_hash=expected_hash,
        declared_mime=declared_mime,
        checkpoint=lambda: progress("inspect_media", 0, 1),
    )
    if not facts.intact:
        raise RecoveryError("Файл недоступен или изменился после анализа.")
    picture = QImage()
    if facts.mime in PICTURE_TYPES and "corrupt_container" not in facts.issues:
        picture = _scaled_image(QImageReader(str(path)))
    elif facts.mime == "audio/wav" and "corrupt_container" not in facts.issues:
        picture = _waveform(path)
    elif provider and facts.mime in DEMUXERS and "corrupt_container" not in facts.issues:
        data = provider.frame(path, facts.mime)
        if data and len(data) <= MAX_FRAME_BYTES and data.startswith(b"\x89PNG\r\n\x1a\n"):
            buffer = QBuffer()
            buffer.setData(QByteArray(data))
            if buffer.open(QIODevice.OpenModeFlag.ReadOnly):
                picture = _scaled_image(QImageReader(buffer))
    progress("inspect_media", 1, 1)
    return facts, picture
