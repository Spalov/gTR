"""Local evidence about attribution and relationships in Telegram Desktop exports."""

import re
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime

PEER = re.compile(r"(user|chat|channel)([1-9]\d{0,18})")
SOURCE_ID = re.compile(r"[1-9]\d{0,18}")
SURROGATE = re.compile(r"[\ud800-\udfff]")


def unicode_text(text):
    if not isinstance(text, str):
        return ""
    return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace") if SURROGATE.search(text) else text


@dataclass(frozen=True)
class Forward:
    is_forwarded: bool = False
    original_sender_id: str = ""
    historical_sender_name: str = ""
    resolved_display_name: str = ""
    original_peer_type: str = "unknown"
    original_message_id: str = ""
    original_message_date: str = ""
    privacy_state: str = "unknown"
    attribution_source: str = "unknown"
    attribution_confidence: str = "unknown"
    resolution_status: str = "unknown"
    resolution_source: str = "unknown"

    @property
    def user_id(self):
        match = PEER.fullmatch(self.original_sender_id)
        if match and match[1] == "user" and self.privacy_state != "hidden":
            return int(match[2])
        return None

    def resolve(self, known):
        """Names returned from Telegram are kept outside source metadata."""
        identity = self.user_id
        if identity is None:
            return self
        evidence = known.get(identity)
        if evidence is None:
            return replace(self, privacy_state="unavailable", resolution_status="unavailable")
        name, source = evidence
        return replace(self, resolved_display_name=name, resolution_source=source,
                       resolution_status="resolved", privacy_state="available")


def forward_from_fields(meta, format_name):
    nested = meta.get("forward_info")
    nested = nested if isinstance(nested, dict) else {}
    if not nested and not any(key in meta for key in (
        "forwarded_from", "forwarded_from_id", "forwarded_date", "forwarded_from_hidden", "forward_privacy",
    )):
        return Forward()
    historical = unicode_text(meta.get("forwarded_from") or nested.get("from_name")).strip()
    typed_id = unicode_text(meta.get("forwarded_from_id") or nested.get("from_id"))
    privacy = meta.get("forward_privacy") or nested.get("privacy_state")
    hidden = meta.get("forwarded_from_hidden") is True or privacy == "hidden"
    match = PEER.fullmatch(typed_id) if not hidden else None
    original_id = str(nested.get("message_id") or meta.get("forwarded_message_id") or "")
    return Forward(
        is_forwarded=True, original_sender_id=typed_id if match else "",
        historical_sender_name=historical,
        original_peer_type=match[1] if match else "unknown",
        original_message_id=original_id if SOURCE_ID.fullmatch(original_id) else "",
        original_message_date=unicode_text(meta.get("forwarded_date") or nested.get("date")),
        privacy_state="hidden" if hidden else "unavailable" if privacy == "unavailable" else "unknown",
        attribution_source=format_name,
        attribution_confidence="exported_id" if match else "historical_label" if historical else "unknown",
        resolution_status="unresolved" if match else "historical" if historical else "unknown",
    )


def forward_from_meta(meta):
    evidence = meta.get("forward")
    if isinstance(evidence, dict) and evidence.get("is_forwarded") is True:
        ids = {key: value for key, value in evidence.items() if key in Forward.__dataclass_fields__}
        if ids.get("privacy_state") == "hidden":
            ids["original_sender_id"] = ""
        return Forward(**ids)
    return forward_from_fields(meta, "legacy_export")


def enrich(meta, raw, iso, format_name, zone):
    forward = forward_from_fields(meta, format_name)
    if forward.is_forwarded:
        meta["forward"] = asdict(forward)
    quote = meta.get("reply_to_quote") or meta.get("quote_text")
    if isinstance(quote, dict):
        quote = quote.get("text")
    if isinstance(quote, str) and quote:
        meta["quote"] = unicode_text(quote)
    album = meta.get("grouped_id", meta.get("album_id"))
    if type(album) is int or isinstance(album, str) and album.isdecimal():
        meta["album_id"] = str(album)
    if meta.get("edited") or meta.get("edited_unixtime") or meta.get("edit_date"):
        meta["edited_available"] = True
    value = datetime.fromisoformat(iso) if iso else None
    precision = (
        "fraction" if re.search(r"\d{1,2}:\d{2}:\d{2}[.,]\d+", raw or "") else
        "second" if re.search(r"\d{1,2}:\d{2}:\d{2}", raw or "") or meta.get("date_unixtime") else
        "minute" if re.search(r"\d{1,2}:\d{2}", raw or "") else "unknown"
    )
    meta["historical_time"] = {
        "raw": raw, "offset": value.strftime("%z") if value else "",
        "utc": value.astimezone(UTC).isoformat() if value else "", "zone": str(zone),
        "precision": precision,
    }


def local_structure(meta, rich):
    entities = meta.get("text_entities")
    entities = entities if isinstance(entities, list) else []
    return {
        "forward": forward_from_meta(meta).is_forwarded,
        "album": bool(meta.get("album_id")),
        "quote": bool(meta.get("quote")),
        "reactions": bool(meta.get("reactions")),
        "editing": bool(meta.get("edited_available") or meta.get("edited")),
        "formatting": bool(re.search(r"<(?!br\b)[a-z]+", rich)),
        "links": "href=" in rich or any(
            isinstance(e, dict) and e.get("type") in {"link", "text_link", "mention", "mention_name"}
            for e in entities
        ),
    }


def match_secondary_evidence(chats, secondary):
    """An exact ID and text anchor rule out guesses based only on a chat title."""
    from collections import Counter

    primary_count = Counter(c.title for c in chats)
    secondary_count = Counter(c.title for c in secondary)
    by_title = {c.title: c for c in secondary if secondary_count[c.title] == 1}
    for chat in chats:
        if primary_count[chat.title] != 1 or not (other := by_title.get(chat.title)):
            continue
        counts = Counter(msg.source_id for msg in other.messages)
        by_id = {msg.source_id: msg for msg in other.messages if msg.source_id and counts[msg.source_id] == 1}
        for message in chat.messages:
            if not (alternate := by_id.get(message.source_id)) or message.text != alternate.text:
                continue
            first, second = forward_from_meta(message.meta), forward_from_meta(alternate.meta)
            date_diff = bool(message.date_iso and alternate.date_iso and
                             datetime.fromisoformat(message.date_iso) != datetime.fromisoformat(alternate.date_iso))
            date_gap = bool(message.date_iso) != bool(alternate.date_iso)
            forward_diff = first != second and (first.is_forwarded or second.is_forwarded)
            if date_diff or date_gap or forward_diff:
                message.meta["format_comparison"] = {
                    "preferred": "json", "basis": "unique_chat_source_id_and_exact_text",
                    "date_conflict": date_diff, "json_date": message.date_raw,
                    "json_parsed": message.date_iso, "html_date": alternate.date_raw, "html_parsed": alternate.date_iso,
                    "json_forward": asdict(first), "html_forward": asdict(second),
                }
                if date_diff:
                    message.flags.append("date_conflict")


def safe_note(value):
    text = " ".join(unicode_text(value).split())[:320]
    return text.translate(str.maketrans({"<": "‹", ">": "›", "[": "［", "]": "］", "(": "（", ")": "）", "\x00": ""}))


def historical_notes(meta):
    """Explicitly selected plaintext. Resolved/current names and IDs are never used."""
    result = {}
    source = forward_from_meta(meta)
    if source.is_forwarded and (author := safe_note(source.historical_sender_name)):
        privacy = " (профиль скрыт)" if source.privacy_state == "hidden" else ""
        result["forward"] = f"[Историческая пометка gTR] Переслано от {author}{privacy}"
    if quote := safe_note(meta.get("quote")):
        result["quote"] = f"[Историческая цитата gTR] {quote}"
    return result


def annotated_text(text, annotations):
    return "\n".join([text, *annotations.values()]) if annotations else text
