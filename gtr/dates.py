"""Parse exported timestamps without changing their wall time or precision."""

import re
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

DAY_FIRST = re.compile(
    r"(?P<wall>\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}(?::\d{2})?)"
    r"(?: *(?:(?P<zone>UTC|GMT) *)?(?P<offset>[+-]\d{2}:?\d{2})?)?",
    re.IGNORECASE,
)
CLOCK_ONLY = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?")
PARSE_ERRORS = (ValueError, TypeError, OverflowError, OSError)
DATE_ISSUES = {
    "invalid_wall_date",
    "invalid_epoch",
    "invalid_date",
    "date_conflict",
    "ambiguous_local_time",
    "nonexistent_local_time",
    "assumed_timezone",
    "service_no_time",
}


def wall_time(raw: str, *, require_clock=False) -> datetime:
    clean = " ".join(raw.replace("\N{MINUS SIGN}", "-").split())
    if match := DAY_FIRST.fullmatch(clean):
        wall, zone, offset = match.group("wall", "zone", "offset")
        fmt = "%d.%m.%Y %H:%M:%S" if wall.count(":") == 2 else "%d.%m.%Y %H:%M"
        offset = offset or ("+00:00" if zone else "")
        return datetime.strptime(wall + offset, fmt + ("%z" if offset else ""))
    if require_clock and not re.match(r"\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}", clean):
        raise ValueError("A full date and clock are required")
    return datetime.fromisoformat(clean)


def export_offset(wall: datetime, moment: datetime) -> datetime | None:
    offset = wall - moment.astimezone(UTC).replace(tzinfo=None)
    if abs(offset) <= timedelta(hours=14) and offset.total_seconds() % 60 == 0:
        return wall.replace(tzinfo=timezone(offset))
    return None


def in_zone(wall: datetime, zone: ZoneInfo, flags: list[str]) -> datetime | None:
    choices = [wall.replace(tzinfo=zone, fold=fold) for fold in (0, 1)]
    choices = [c for c in choices if c.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == wall]
    if not choices:
        flags.append("nonexistent_local_time")
        return None
    if len({c.utcoffset() for c in choices}) > 1:
        flags.append("ambiguous_local_time")
        return None
    flags.append("assumed_timezone")
    return choices[0]


def timestamp(raw, epoch, zone, flags):
    """Keep the exported offset, including for Telegram's pair of wall time and Unix time."""
    moment, wall = None, None
    if epoch not in (None, ""):
        try:
            moment = datetime.fromtimestamp(int(epoch), UTC)
        except PARSE_ERRORS:
            flags.append("invalid_epoch")
    if raw:
        try:
            wall = wall_time(raw)
        except (*PARSE_ERRORS, AttributeError):
            flags.append("invalid_wall_date")
    try:
        if moment is not None:
            if wall is None:
                return moment.isoformat()
            if wall.tzinfo is None:
                wall = export_offset(wall, moment)
            if wall is None or wall != moment:
                flags.append("date_conflict")
                return moment.isoformat()
        if wall is None:
            raise ValueError
        result = wall if wall.tzinfo is not None else in_zone(wall, zone, flags)
        return result.isoformat() if result is not None else ""
    except PARSE_ERRORS:
        flags.append("invalid_date")
        return ""


def _html_value(raw: str, kind: str) -> datetime:
    if kind != "epoch":
        return wall_time(raw, require_clock=True)
    if not re.fullmatch(r"[+-]?\d{1,13}", raw.strip()):
        raise ValueError("Expected a Unix timestamp")
    value = int(raw)
    # Contemporary milliseconds are distinct from Unix seconds in HTML metadata.
    return datetime.fromtimestamp(value / 1000 if 10**12 <= abs(value) < 10**13 else value, UTC)


def _resolve_html(candidates, zone, flags):
    if not candidates:
        flags.append("invalid_date")
        return "", None, "missing"
    field, wall = candidates[0]
    explicit = [dt.astimezone(UTC) for _, dt in candidates if dt.tzinfo is not None]
    local = {dt for _, dt in candidates if dt.tzinfo is None}
    if len(set(explicit)) > 1 or len(local) > 1:
        flags.append("date_conflict")
        return "", None, "conflict"
    if wall.tzinfo is not None:
        if local and export_offset(next(iter(local)), wall) is None:
            flags.append("date_conflict")
            return "", None, "conflict"
        return wall.isoformat(), field, "exported"
    if explicit:
        resolved = export_offset(wall, explicit[0])
        if resolved is None:
            flags.append("date_conflict")
            return "", None, "conflict"
        return resolved.isoformat(), field, "derived_offset"
    resolved = in_zone(wall, zone, flags)
    return (resolved.isoformat(), field, "configured_zone") if resolved else ("", None, flags[-1])


def html_timestamp(fields: list[tuple[str, str, str]], zone: ZoneInfo, flags: list[str]):
    """Resolve direct timestamp fields and keep their individual parse results for reports."""
    inputs, candidates, visible = [], [], []
    for field, kind, raw in fields:
        entry = {"field": field, "raw": raw, "parsed": None, "status": "empty"}
        inputs.append(entry)
        if not raw.strip():
            continue
        try:
            value = _html_value(raw, kind)
        except PARSE_ERRORS:
            code = "invalid_epoch" if kind == "epoch" else "invalid_wall_date"
            if kind != "display":
                flags.append(code)
            entry["status"] = (
                "clock_only" if kind == "display" and CLOCK_ONLY.fullmatch(raw.strip()) else code
            )
        else:
            entry.update(parsed=value.isoformat(), status="parsed")
            (visible if kind == "display" else candidates).append((field, value))
    try:
        iso, used, resolution = _resolve_html(candidates or visible, zone, flags)
    except PARSE_ERRORS:
        flags.append("invalid_date")
        iso, used, resolution = "", None, "invalid_date"
    raw = next((value for _, _, value in fields if value.strip()), "")
    return raw, iso, {"fields": inputs, "selected": used, "resolution": resolution}
