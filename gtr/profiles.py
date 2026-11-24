"""Candidate text profiles for the documented History Import handshake."""

import re

PROFILES = {"ios": "whatsapp-ios-day-first-seconds-v1", "android": "whatsapp-android-day-first-seconds-v1"}
HEADERS = {
    PROFILES["ios"]: re.compile(
        r"^\[(?P<date>\d{2}/\d{2}/\d{4}, \d{2}:\d{2}:\d{2})\] (?P<author>[^:\r\n]+): "
    ),
    PROFILES["android"]: re.compile(
        r"^(?P<date>\d{2}/\d{2}/\d{2}, \d{2}:\d{2}:\d{2}) - (?P<author>[^:\r\n]+): "
    ),
}
MEDIA_NAME = re.compile(r"gtr-\d{7,}\.[a-z0-9]{1,10}\Z")
MARKER = re.compile(r"<attached:\s*([^>\r\n]+)>", re.IGNORECASE)
ANDROID_MARKER = re.compile(r"([^<>\r\n]+) \(file attached\)")


def wire_line(profile, date, author, body, asset=None):
    if profile == PROFILES["android"]:
        if not 2000 <= date.year <= 2099:
            from .model import RecoveryError

            raise RecoveryError("Для даты вне 2000–2099 выберите профиль iOS.")
        prefix = f"{date:%d/%m/%y, %H:%M:%S} - {author}: "
        marker = f"{asset} (file attached)" if asset else ""
    else:
        prefix = f"[{date:%d/%m/%Y, %H:%M:%S}] {author}: "
        marker = f"<attached: {asset}>" if asset else ""
    return prefix + (marker + ("\n" if body else "") if marker else "") + body + "\n"


def read_records(stream, profile):
    """Read one logical record per exactly parsed header, including multiline captions."""
    header, attached = HEADERS[profile], MARKER if profile == PROFILES["ios"] else ANDROID_MARKER
    date, author, body = "", "", []
    for line in stream:
        match = header.match(line)
        if match:
            if author:
                yield _record(date, author, body, attached)
            date, author, body = match["date"], match["author"], [line[match.end() :]]
        elif author:
            body.append(line)
        else:
            raise ValueError("Unexpected header")
    if author:
        yield _record(date, author, body, attached)


def _record(date, author, parts, attached):
    body = "".join(parts).removesuffix("\n")
    first, _, rest = body.partition("\n")
    marker = attached.fullmatch(first)
    return date, author, rest if marker else body, marker[1] if marker else ""
