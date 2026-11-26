"""Selected PyAV/GGE algorithms for bounded, source-independent local previews."""

import io
import json
import math
import struct
import sys
from contextlib import contextmanager
from fractions import Fraction
from pathlib import Path

from .gge_io import media_source
from .gge_metadata import capture_date
from .media import identify

FORMATS = {
    "video/mp4": "mov", "video/quicktime": "mov", "video/webm": "matroska",
    "video/x-matroska": "matroska", "video/x-msvideo": "avi",
    "audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/aac": "aac",
    "audio/mp4": "mov", "audio/wav": "wav", "audio/flac": "flac",
}
WIDTH_LIMIT = 8192
PIXEL_LIMIT = 40_000_000


@contextmanager
def open_media(source, name=""):
    import av

    original = source.tell()
    mime, recognized = identify(name, source.read(4096))
    source.seek(original)
    if not recognized or mime not in FORMATS:
        raise ValueError("container_mismatch")
    options = {
        "protocol_whitelist": "pipe", "probesize": "2097152",
        "analyzeduration": "2000000", "max_probe_packets": "512", "max_streams": "16",
    }
    if FORMATS[mime] == "mov":
        options.update(enable_drefs="0", use_absolute_path="0")
    with av.open(source, "r", format=FORMATS[mime], buffer_size=65536, options=options) as container:
        if not 0 < len(container.streams) <= 16:
            raise ValueError("stream_limit")
        for stream in (*container.streams.video, *container.streams.audio):
            stream.codec_context.thread_count = 2
        yield container, mime


def metadata(container, mime, frame=None):
    videos, audio = container.streams.video, container.streams.audio
    video = videos[0] if videos else None
    duration = container.duration / 1_000_000 if container.duration else None
    if duration is not None and (not math.isfinite(duration) or not 0 < duration <= 7 * 86400):
        duration = None
    result = {"container": mime, "streams": len(container.streams), "duration": duration,
              "audio": [stream.codec_context.name for stream in audio],
              "capture_date": capture_date(container.metadata.get("creation_time"))}
    if video is None:
        return result
    context = video.codec_context
    width, height = context.width, context.height
    if not (0 < width <= WIDTH_LIMIT and 0 < height <= WIDTH_LIMIT and width * height <= PIXEL_LIMIT):
        raise ValueError("video_dimensions_limit")
    sar = video.sample_aspect_ratio or Fraction(1)
    if not Fraction(1, 16) <= sar <= 16:
        raise ValueError("aspect_ratio_limit")
    rotated = int(frame.rotation) % 360 if frame is not None else None
    mirrored = False
    if frame is not None:
        for side in frame.side_data.values():
            if side.type.name == "DISPLAYMATRIX" and len(bytes(side)) == 36:
                matrix = struct.unpack("=9i", bytes(side))
                mirrored = matrix[0] * matrix[4] - matrix[1] * matrix[3] < 0
    display_width, display_height = round(width * float(sar)), height
    if rotated in {90, 270}:
        display_width, display_height = display_height, display_width
    pixel = context.format.name if context.format else None
    extra = context.extradata or b""
    codec_tag = "avc1." + extra[1:4].hex().upper() if context.name == "h264" and len(extra) > 4 and extra[0] == 1 else None
    return {
        **result, "codec": context.name, "codec_tag": codec_tag, "width": width, "height": height,
        "rotation": rotated, "mirrored": mirrored, "sar": str(sar),
        "display_width": display_width, "display_height": display_height,
        "pixel_format": pixel, "color_space": int(context.colorspace),
        "hdr": context.color_trc in {16, 18},
        "alpha": (pixel or "").startswith(("yuva", "rgba", "bgra", "argb", "abgr", "gbrap"))
        or video.metadata.get("alpha_mode") == "1",
        "rate": str(video.average_rate) if video.average_rate else None,
        "decode": "sample_ok" if frame is not None else "unverified",
    }


def oriented_image(frame, details, max_side=480):
    from PIL import Image, ImageOps

    if not 64 <= max_side <= 1024 or (details["rotation"] or 0) % 90:
        raise ValueError("unsupported_transform")
    sar = float(Fraction(details["sar"]))
    factor = min(1, max_side / max(frame.width * sar, frame.height))
    height = max(2, int(frame.height * factor))
    width = max(2, int(frame.width * sar * factor))
    image = frame.reformat(width=width, height=height, format="rgb24").to_image()
    if details["mirrored"]:
        image = ImageOps.mirror(image)
    if details["rotation"]:
        image = image.rotate(details["rotation"], expand=True)
    image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return image


def inspect(path, *, member=None, poster=False):
    with media_source(path, member=member) as stream, open_media(stream, name=member or Path(path).name) as opened:
        container, mime = opened
        details = metadata(container, mime)
        if container.streams.video:
            frame = next(container.decode(video=0), None)
            if frame is None:
                raise ValueError("video_decode_failed")
            details = metadata(container, mime, frame)
            if poster:
                output = io.BytesIO()
                oriented_image(frame, details).save(output, "PNG")
                if output.tell() > 1024**2:
                    raise ValueError("preview_output_limit")
                return output.getvalue()
        return details


def image_preview(source, max_side=480):
    """GGE image resizing and orientation, with alpha preserved in PNG."""
    from PIL import Image, ImageOps

    if not 64 <= max_side <= 1024:
        raise ValueError("preview_dimensions")
    with Image.open(source, formats=("JPEG", "PNG", "WEBP", "GIF")) as picture:
        if not 0 < picture.width * picture.height <= PIXEL_LIMIT:
            raise ValueError("pixel_budget")
        if picture.format == "JPEG":
            picture.draft("RGB", (max_side, max_side))
        picture.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        prepared = ImageOps.exif_transpose(picture)
        output = io.BytesIO()
        prepared.convert("RGBA" if "A" in prepared.getbands() or "transparency" in prepared.info else "RGB").save(
            output, "PNG"
        )
        if output.tell() > 1024**2:
            raise ValueError("preview_output_limit")
        return output.getvalue()


if __name__ == "__main__":
    kind, filename = sys.argv[1:3]
    if kind == "image":
        with media_source(Path(filename), budget=40 * 1024**2) as stream:
            sys.stdout.buffer.write(image_preview(stream))
    elif kind == "poster":
        sys.stdout.buffer.write(inspect(Path(filename), poster=True))
    elif kind == "probe":
        sys.stdout.buffer.write(json.dumps(inspect(Path(filename)), ensure_ascii=False).encode("utf-8"))
    else:
        raise SystemExit(2)
