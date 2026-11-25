"""Source sequence and deterministic transcript dates."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from sqlite3 import Row
from zoneinfo import ZoneInfo

SOURCE_ORDER = "id"
DATE_POLICIES = ("historical", "ordered")
UNRELIABLE = {
    "date_conflict",
    "ambiguous_local_time",
    "nonexistent_local_time",
    "invalid_date",
    "service_no_time",
}


@dataclass(frozen=True)
class HistoricalIdentity:
    original_message_id: str
    original_author_id: str
    original_author_name: str
    original_author_type: str
    original_timestamp: str
    original_timezone: str
    original_utc: str
    original_reply_to: str
    date_reliable: bool
    original_date_raw: str

    @classmethod
    def read(cls, row: Row, flags: set[str]) -> "HistoricalIdentity":
        meta = json.loads(row["meta"])
        source_id = meta.get("source_author_id") or (
            row["sender_key"] if row["sender_key"] != row["sender"] else ""
        )
        kind = next((kind for kind in ("user", "chat", "channel") if source_id.startswith(kind)), "")
        kind = kind or ("label" if row["sender"] else "unknown")
        moment = datetime.fromisoformat(row["date_iso"]) if row["date_iso"] else None
        offset = moment.strftime("%z") if moment else ""
        if len(offset) == 5:
            offset = offset[:3] + ":" + offset[3:]
        return cls(
            row["source_id"],
            source_id,
            row["sender"],
            kind,
            row["date_iso"] or row["date_raw"],
            offset,
            moment.astimezone(UTC).isoformat() if moment else "",
            row["reply"],
            bool(moment and not (flags & UNRELIABLE)),
            row["date_raw"],
        )


class WireClock:
    def __init__(self, anchor: Row | None, zone: ZoneInfo | None, policy: str = "ordered"):
        self.zone = zone
        self.policy = policy
        self.last = None
        self.last_source = None
        self.audit: dict[str, int] = {"equal": 0, "regression": 0, "cascade": 0, "max_shift_seconds": 0}
        self.fallback = self.local_time(anchor["date_iso"]) if anchor else datetime(2000, 1, 1)
        if anchor:
            self.fallback -= timedelta(
                seconds=min(anchor["position"] - 1, (self.fallback - datetime(1, 1, 1)).days * 86400)
            )

    def local_time(self, value: str) -> datetime:
        date = datetime.fromisoformat(value)
        return (date.astimezone(self.zone) if self.zone else date).replace(tzinfo=None, microsecond=0)

    def utc(self, date: datetime, original: str) -> str:
        if self.policy == "historical" and original:
            return datetime.fromisoformat(original).astimezone(UTC).isoformat(timespec="seconds")
        zone = self.zone or (datetime.fromisoformat(original).tzinfo if original else UTC)
        return date.replace(tzinfo=zone).astimezone(UTC).isoformat(timespec="seconds")

    def next(self, value: str, uncertain: bool) -> tuple[datetime | None, str]:
        source = self.local_time(value) if value and not uncertain else None
        if source is not None and self.last_source is not None:
            if source == self.last_source:
                self.audit["equal"] += 1
            elif source < self.last_source:
                self.audit["regression"] += 1
            elif self.policy == "ordered" and self.last and source <= self.last:
                self.audit["cascade"] += 1
        if source is not None:
            self.last_source = source
        if self.policy == "historical":
            return source, "" if source else "date"
        change = ""
        if source:
            date = source
        else:
            date = self.last + timedelta(seconds=1) if self.last else self.fallback
            change = "synthetic_time"
        if self.last and date <= self.last:
            date = self.last + timedelta(seconds=1)
            change = change or "ordered_time"
        if source is not None:
            self.audit["max_shift_seconds"] = max(
                self.audit["max_shift_seconds"], int((date - source).total_seconds())
            )
        self.last = date
        return date, change


def ordered_rows(db, chat_id):
    return db.execute(
        f"""SELECT m.*,t.source_id AS resolved_reply,t.chat AS resolved_reply_chat,
            row_number() OVER (ORDER BY m.{SOURCE_ORDER}) AS position
            FROM messages m LEFT JOIN messages t ON t.id=m.target WHERE m.chat=? ORDER BY m.{SOURCE_ORDER}""",
        (chat_id,),
    )


def first_reliable_time(db, chat_id):
    return db.execute(
        """SELECT date_iso,position FROM (
        SELECT id,date_iso,service,row_number() OVER (ORDER BY id) AS position FROM messages WHERE chat=?
        ) AS m WHERE service=0 AND date_iso<>'' AND NOT EXISTS (
          SELECT 1 FROM issues WHERE msg=m.id AND code IN ('date_conflict','ambiguous_local_time',
                                                         'nonexistent_local_time','invalid_date'))
        ORDER BY position LIMIT 1""",
        (chat_id,),
    ).fetchone()
