"""Bounded local thumbnails. Optional media tools receive only a checked local file."""

import io
import struct
import wave
from pathlib import Path
from typing import Protocol

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize, Qt
from PySide6.QtGui import QColor, QImage, QImageReader, QPainter, QPen

from .files import local
from .media_engine import HEADER_BYTES, PICTURE_TYPES, inspect
from .model import RecoveryError, quiet
from .video import DEMUXERS, _input, _run, gge_probe

MAX_PIXELS = 16_000_000
MAX_FRAME_BYTES = 1024 * 1024
VIDEO_MIME = {mime for mime in DEMUXERS if mime.startswith("video/")}


class VideoThumbnailProvider(Protocol):
    """Optional gFL or local-tool adapter. Return one PNG within the byte budget."""

    def frame(self, source: Path, detected_mime: str) -> bytes | None: ...


class FFmpegThumbnail:
    def __init__(self, executable: Path):
        self.executable = local(Path(executable))
        if self.executable.name.casefold() not in {"ffmpeg.exe", "ffmpeg"} or not self.executable.is_file():
            raise RecoveryError("Выберите установленную программу ffmpeg для локального предпросмотра.")

    def frame(self, source: Path, detected_mime: str) -> bytes | None:
        if detected_mime not in VIDEO_MIME:
            return None
        source = local(source)
        return _run(self.executable, [
            "-nostdin", "-v", "error", "-threads", "1", *_input(source, detected_mime),
            "-map", "0:v:0", "-an", "-sn", "-dn", "-frames:v", "1", "-filter_threads", "1",
            "-vf", "scale=480:300:force_original_aspect_ratio=decrease", "-threads", "1",
            "-vcodec", "png", "-f", "image2pipe", "pipe:1",
        ], 12, quiet)



def _scaled_image(reader):
    reader.setDecideFormatFromContent(True)
    reader.setAutoTransform(True)
    size = reader.size()
    if not size.isValid() or not 0 < size.width() * size.height() <= MAX_PIXELS:
        return QImage()
    reader.setScaledSize(size.scaled(QSize(640, 420), Qt.AspectRatioMode.KeepAspectRatio))
    return reader.read()


def _decode_preview(data):
    if not data or len(data) > MAX_FRAME_BYTES or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return QImage()
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    return _scaled_image(QImageReader(buffer)) if buffer.open(QIODevice.OpenModeFlag.ReadOnly) else QImage()


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
        if picture.isNull():
            picture = _decode_preview(gge_probe(path, "image", progress))
    elif facts.mime == "audio/wav" and "corrupt_container" not in facts.issues:
        picture = _waveform(path)
    elif facts.mime in VIDEO_MIME and "corrupt_container" not in facts.issues:
        data = gge_probe(path, "poster", progress)
        if data is None and provider:
            data = provider.frame(path, facts.mime)
        picture = _decode_preview(data)
    progress("inspect_media", 1, 1)
    return facts, picture
