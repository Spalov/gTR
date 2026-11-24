"""Set the chosen artwork and generate the Windows sizes for it."""

import argparse
import os
import shutil
import struct
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRect, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QPainter
from PySide6.QtWidgets import QApplication


def icon(source):
    art = QImage(str(source))
    if art.isNull() or art.width() != art.height():
        raise ValueError("Выберите квадратное изображение логотипа.")
    width = art.width()
    closeup = art.copy(
        QRect(round(width * 0.22), round(width * 0.085), round(width * 0.60), round(width * 0.60))
    )
    sizes = (16, 32, 64, 128, 256)
    chunks = []
    for size in sizes:
        frame = (closeup if size <= 64 else art).scaled(
            size, size, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        if size >= 32:
            painter = QPainter(frame)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            badge = round(size * 0.28)
            rect = QRect(size - badge - 1, size - badge - 1, badge, badge)
            painter.setBrush(QColor("#172234"))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(rect)
            font = QFont("Segoe UI")
            font.setWeight(QFont.Weight.Bold)
            font.setPixelSize(round(badge * 0.78))
            painter.setFont(font)
            painter.setPen(QColor("#ffd48d"))
            painter.drawText(rect.adjusted(0, -1, 0, -1), Qt.AlignmentFlag.AlignCenter, "g")
            painter.end()
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not frame.save(buffer, "PNG"):
            raise ValueError("Не удалось записать размер иконки.")
        chunks.append(bytes(data))
    offset = 6 + 16 * len(chunks)
    entries = []
    for size, chunk in zip(sizes, chunks):
        entries.append(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(chunk), offset))
        offset += len(chunk)
    return struct.pack("<HHH", 0, 1, len(chunks)) + b"".join(entries + chunks)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication([])
    windows = Path(os.environ.get("SystemRoot", "C:/Windows")) / "Fonts"
    QFontDatabase.addApplicationFont(str(windows / "segoeuib.ttf"))
    assets = Path(__file__).resolve().parents[1] / "gtr" / "assets"
    frames = icon(args.source)
    if args.source.resolve() != (assets / "cat.png").resolve():
        shutil.copyfile(args.source, assets / "cat.png")
    (assets / "gtr.ico").write_bytes(frames)
