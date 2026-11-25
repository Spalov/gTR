"""Telegram Desktop HTML pages and single-chat / account JSON exports."""

import html
import json
import re
from collections import defaultdict
from pathlib import PurePosixPath
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from .dates import html_timestamp, timestamp
from .export_json import MultipleChats
from .export_json import fields as json_fields
from .identity import html_author, json_author
from .model import (
    Asset,
    Cancelled,
    Chat,
    Message,
    Progress,
    RecoveryError,
    Snapshot,
    quiet,
)
from .structure import enrich, match_secondary_evidence, unicode_text

PAGE = re.compile(r"messages(\d*)\.html$", re.IGNORECASE)
MESSAGE_ID = re.compile(r"message(-?\d+)$")
REPLY_ID = re.compile(r"#(?:go_to_)?message(-?\d+)$")
MAX_PAGE = 256 * 1024**2
MEDIA_TYPES = {
    "video_file": "video",
    "video_message": "round_video",
    "voice_message": "voice",
    "sticker": "sticker",
    "animation": "animation",
    "audio_file": "audio",
}
LABELS = {
    "photo": "photo",
    "video file": "video",
    "video message": "round_video",
    "voice message": "voice",
    "sticker": "sticker",
    "animation": "animation",
    "audio file": "audio",
    "file": "document",
}
WRAPS = {
    "photo_wrap": "photo",
    "sticker_wrap": "sticker",
    "video_file_wrap": "video",
    "animated_wrap": "animation",
    "media_voice_message": "voice",
    "media_audio_file": "audio",
    "media_file": "document",
    "media_video": "video",
    "media_photo": "photo",
}
MARKS = {
    "b": "b",
    "strong": "b",
    "i": "i",
    "em": "i",
    "u": "u",
    "ins": "u",
    "s": "s",
    "del": "s",
    "strike": "s",
    "code": "code",
    "pre": "pre",
    "blockquote": "blockquote",
    "a": "u",
}
ENTITY = {
    "bold": "b",
    "italic": "i",
    "underline": "u",
    "strikethrough": "s",
    "code": "code",
    "pre": "pre",
    "blockquote": "blockquote",
    "text_link": "u",
    "link": "u",
    "mention": "u",
    "spoiler": "span",
}


def read_text(snap, name):
    if snap.files[name].size > MAX_PAGE:
        raise RecoveryError("Страница экспорта превышает лимит 256 МиБ.")
    try:
        return (snap.root / name).read_text(encoding="utf-8-sig")
    except UnicodeError:
        raise RecoveryError("Страница экспорта содержит недопустимые данные UTF-8.") from None


def html_content(node):
    if node is None or not node.contents:
        return "", ""
    first, last = node.contents[0], node.contents[-1]

    def render(item):
        if isinstance(item, Comment):
            return "", ""
        if isinstance(item, NavigableString):
            value = str(item)
            # Strip the writer's two layout newlines, never a user's explicit <br>.
            if item is first:
                value = value.removeprefix("\n")
            if item is last:
                value = re.sub(r"\n[ \t]*\Z", "", value)
            return value, html.escape(value)
        if item.name == "br":
            return "\n", "<br>"
        if item.name in {"script", "style", "iframe", "object"}:
            return "", ""
        if item.name == "img":
            value = item.get("alt", "")
            return value, html.escape(value)
        children = [render(child) for child in item.children]
        plain = "".join(p for p, _ in children)
        rich = "".join(r for _, r in children)
        mark = MARKS.get(item.name)
        if mark:
            rich = f"<{mark}>{rich}</{mark}>"
        elif "spoiler" in item.get("class", []):
            rich = f'<span title="Спойлер">{rich}</span>'
        return plain, rich

    return render(node)


def direct(node, cls):
    return node.find(class_=cls, recursive=False) if node else None


def html_date_fields(node: Tag, body: Tag) -> list[tuple[str, str, str]]:
    """Only inspect the message's timestamp, outside forwarded or quoted content."""
    date = direct(body, "date")
    anchors = [("date", date)] if date else []
    time_tags = body.find_all("time", recursive=False)
    if date:
        time_tags.extend(date.find_all("time", recursive=False))
    anchors.extend(("time", t) for t in time_tags if t is not date)
    fields = [
        (f"{name}.{attr}", "wall", tag[attr])
        for attr in ("title", "datetime", "data-date")
        for name, tag in anchors
        if tag.has_attr(attr)
    ]
    fields.extend(
        (f"{name}.{attr}", "epoch", tag[attr])
        for name, tag in [*anchors, ("message", node), ("body", body)]
        for attr in ("data-timestamp", "data-unixtime")
        if tag.has_attr(attr)
    )
    fields.extend((f"{name}.text", "display", tag.get_text(" ", strip=True)) for name, tag in anchors)
    return fields


def html_assets(body, base, meta):
    assets = []
    for wrap in body.find_all(class_="media_wrap"):
        box = wrap.find(recursive=False)
        if not isinstance(box, Tag):
            continue
        label = box.select_one(".title")
        label = label.get_text(strip=True) if label else ""
        classes = box.get("class", [])
        if "media_contact" in classes or set(classes) & {
            "media_location",
            "media_live_location",
            "media_venue",
        }:
            kind = "contact" if "media_contact" in classes else "location"
            key = "contact_information" if kind == "contact" else "location_information"
            meta[key] = {"title": label, "display": box.get_text("\n", strip=True)}
            href = box.get("href", "")
            if href and kind == "contact":
                assets.append(Asset(kind, href, base, url_encoded=True))
            else:
                if href:
                    meta[key]["exported_link"] = href
                assets.append(Asset(kind, "", base, metadata=True))
            continue
        kind = LABELS.get(label.lower()) or next((WRAPS[c] for c in classes if c in WRAPS), None)
        if not kind:
            meta.setdefault("other_media", []).append(wrap.get_text("\n", strip=True))
            continue
        href = box.get("href", "")
        if kind == "document" and label and "." in label and href:
            from urllib.parse import unquote

            source = (PurePosixPath(base) / unquote(href)).as_posix()
            meta.setdefault("file_names", {})[source] = label
        assets.append(Asset(kind, href, base, url_encoded=True))
    return assets


def parse_html(snap, folder, pages, zone, progress):
    chat = Chat("html:" + (folder or "."), folder or "Диалог", pages=len(pages))
    chat.source = min(pages)[1] if pages else ""
    previous, last_page, day = "", 0, ""
    for page_number, name in sorted(pages):
        progress("parse", last_page, len(pages))
        if page_number != last_page + 1:
            previous = ""
        last_page = page_number
        soup = BeautifulSoup(read_text(snap, name), "html.parser")
        nodes = soup.select(".message")
        if not nodes and not soup.select_one(".history, .page_body.chat_page"):
            raise RecoveryError("Страница не содержит историю Telegram Desktop.")
        heading = soup.select_one(".page_header .text")
        if heading:
            chat.title = heading.get_text(strip=True) or chat.title
        for node in nodes:
            match = MESSAGE_ID.fullmatch(node.get("id", ""))
            sid = match[1] if match else ""
            service = "service" in node.get("class", [])
            body = direct(node, "body")
            if body is None:
                raise RecoveryError("В HTML обнаружена запись без тела сообщения.")
            fields = html_date_fields(node, body)
            if service and sid.startswith("-") and not fields:
                # Synthetic day headers have negative IDs and no timestamp.
                next_day = body.get_text(strip=True)
                if day and next_day != day:
                    previous = ""
                day = next_day
                continue
            flags, meta = [], {}
            sender_tag = direct(body, "from_name")
            joined = "joined" in node.get("class", [])
            sender, basis = html_author(
                sender_tag.get_text(strip=True) if sender_tag else None, joined, previous
            )
            meta["identity_basis"] = basis
            previous = "" if service else sender
            if joined:
                meta["joined"] = True
            if service and not any(value.strip() for _, _, value in fields):
                raw, iso = day, ""
                flags.append("service_no_time")
            else:
                raw, iso, meta["date_evidence"] = html_timestamp(fields, zone, flags)
            payload = body
            forward_body = body.select_one(":scope > .forwarded.body")
            if forward_body:
                payload = forward_body
                source = direct(payload, "from_name")
                meta["forwarded_from"] = (
                    "".join(str(t) for t in source.children if isinstance(t, NavigableString)).strip()
                    if source
                    else ""
                )
                if source:
                    for key in ("forwarded-from-id", "forward-privacy", "forwarded-date"):
                        if value := source.get("data-" + key):
                            meta[key.replace("-", "_")] = value
            content = direct(payload, "text")
            plain, rich = html_content(body if service else content)
            reply_tag = direct(body, "reply_to") or direct(payload, "reply_to")
            link = reply_tag.find("a", href=True) if reply_tag else None
            reply = None
            if link:
                page_ref, _, anchor = link["href"].partition("#")
                if not page_ref or PAGE.fullmatch(page_ref.removeprefix("./")):
                    reply = REPLY_ID.fullmatch("#" + anchor)
                meta["reply_href"] = link["href"]
            if reply_tag and not reply:
                flags.append("unknown_reply")
                meta["unresolved_reply"] = True
            if direct(body, "reactions"):
                meta["reactions"] = direct(body, "reactions").get_text(" ", strip=True)
            if quote := direct(body, "reply_quote"):
                meta["reply_to_quote"] = quote.get_text(" ", strip=True)
            if node.get("data-grouped-id"):
                meta["grouped_id"] = node["data-grouped-id"]
            if edited := direct(body, "edited"):
                meta["edited"] = edited.get("title") or edited.get_text(strip=True)
            assets = html_assets(payload, folder, meta)
            enrich(meta, raw, iso, "html", zone)
            chat.messages.append(
                Message(
                    sid,
                    sender,
                    sender,
                    raw,
                    iso,
                    plain,
                    rich,
                    name,
                    reply=reply[1] if reply else "",
                    service=service,
                    assets=assets,
                    meta=meta,
                    flags=flags,
                )
            )
    return chat


def json_text(value):
    if isinstance(value, str):
        value = unicode_text(value)
        return value, html.escape(value).replace("\n", "<br>")
    plain, rich = [], []
    for entity in value or []:
        text = unicode_text(entity if isinstance(entity, str) else entity.get("text", ""))
        formatted = html.escape(text).replace("\n", "<br>")
        tag = ENTITY.get(entity.get("type")) if isinstance(entity, dict) else None
        plain.append(text)
        rich.append(f"<{tag}>{formatted}</{tag}>" if tag else formatted)
    return "".join(plain), "".join(rich)


PEER_PREFIX = {
    "personal_chat": "user",
    "bot_chat": "user",
    "saved_messages": "user",
    "replies": "user",
    "verification_codes": "user",
    "private_group": "chat",
    "private_supergroup": "channel",
    "public_supergroup": "channel",
    "private_channel": "channel",
    "public_channel": "channel",
}


def json_chat(item, name, index):
    chat = Chat(
        f"json:{name}:{item.get('type', '')}:{item.get('id', index)}:{index}",
        item.get("name") or "Диалог",
    )
    prefix = PEER_PREFIX.get(item.get("type"))
    if prefix and re.fullmatch(r"-?[1-9]\d{0,18}", str(item.get("id", ""))):
        chat.peer_key = prefix + str(item["id"])
    return chat


def author_label(labels, message):
    source_id = message.get("from_id") or message.get("actor_id")
    label = message.get("from") or message.get("actor")
    if source_id and label:
        labels[str(source_id)].add(label)


def json_messages(rows, name, zone, chat, labels, total, progress):
    for offset, msg in enumerate(rows):
        if offset % 250 == 0:
            progress("parse", offset, total)
        flags = []
        raw = msg.get("date", "")
        iso = timestamp(raw, msg.get("date_unixtime"), zone, flags)
        plain, rich = json_text(msg.get("text", ""))
        if msg.get("text_entities"):
            entity_text, entity_rich = json_text(msg["text_entities"])
            if entity_text == plain:
                rich = entity_rich
            else:
                flags.append("entity_mismatch")
        sender, sender_key, identity = json_author(msg, labels)
        raw_id = str(msg["id"]) if msg.get("id") is not None else ""
        sid = raw_id if re.fullmatch(r"-?\d+", raw_id) else ""
        assets = []
        base = str(PurePosixPath(name).parent)
        for key in ("photo", "file", "contact_vcard"):
            if key in msg:
                kind = {"photo": "photo", "contact_vcard": "contact"}.get(key) or MEDIA_TYPES.get(
                    msg.get("media_type"), "document"
                )
                size = msg.get(key + "_file_size" if key != "file" else "file_size")
                assets.append(Asset(kind, msg[key] or "", base, size, msg.get("mime_type", "")))
        if msg.get("contact_information") is not None and "contact_vcard" not in msg:
            assets.append(Asset("contact", "", base, metadata=True))
        if msg.get("location_information") is not None:
            assets.append(Asset("location", "", base, metadata=True))
        meta = {k: v for k, v in msg.items() if k not in {"text", "photo", "file", "from", "date", "id"}}
        meta.update(identity)
        if not meta.get("text_entities") and isinstance(msg.get("text"), list):
            meta["text_entities"] = msg["text"]
        enrich(meta, raw, iso, "json", zone)
        if raw_id and not sid:
            meta["source_id_raw"] = raw_id
        reply_peer = str(msg.get("reply_to_peer_id") or "")
        if reply_peer == chat.peer_key:
            reply_peer = ""
        yield Message(
            sid,
            sender,
            sender_key,
            raw,
            iso,
            plain,
            rich,
            name,
            str(msg.get("reply_to_message_id") or ""),
            reply_peer,
            msg.get("type", "message") != "message",
            assets,
            meta,
            flags,
        )


class MessageRows:
    """Repeatable streaming rows for catalogue creation and attachment discovery."""

    def __init__(self, reader, count):
        self.reader = reader
        self.count = count

    def __iter__(self):
        return iter(self.reader())

    def __len__(self):
        return self.count


def stream_json(snap, name, zone, progress):
    path = snap.root / name
    header, count, found = {}, 0, False
    labels = defaultdict(set)
    for key, value in json_fields(path):
        if key == "messages":
            if found:
                raise ValueError("More than one message array")
            found = True
        elif key is None:
            if count % 250 == 0:
                progress("parse", count, 0)
            author_label(labels, value)
            count += 1
        else:
            header[key] = value
    if not found:
        raise RecoveryError("В JSON отсутствует список сообщений.")

    def read_rows():
        return (value for key, value in json_fields(path) if key is None)

    chat = json_chat(header, name, 0)
    chat.messages = MessageRows(lambda: json_messages(read_rows(), name, zone, chat, labels, count, progress), count)
    chat.source = name
    return [chat]


def parse_json(snap, name, zone, progress, *, stream=False):
    if stream and 1024**2 <= snap.files[name].size <= MAX_PAGE:
        try:
            return stream_json(snap, name, zone, progress)
        except MultipleChats:
            pass
    data = json.loads(read_text(snap, name))
    if not isinstance(data, dict):
        raise RecoveryError("Неизвестная структура JSON-экспорта.")
    if "messages" in data:
        items = [data]
    else:
        items = [c for group in ("chats", "left_chats") for c in data.get(group, {}).get("list", [])]
    chats = []
    for index, item in enumerate(items):
        chat = json_chat(item, name, index)
        chat.source = name
        rows = item["messages"]
        labels = defaultdict(set)
        for msg in rows:
            author_label(labels, msg)
        chat.messages.extend(json_messages(rows, name, zone, chat, labels, len(rows), progress))
        chats.append(chat)
    return chats


def discover(snap: Snapshot):
    """Locate chat sources inside wrappers, excluding documents attached to another export."""
    groups, json_files = {}, []
    paths = [PurePosixPath(name) for name in sorted(snap.files)]
    roots = {p.parent for p in paths if PAGE.fullmatch(p.name) or p.name.lower() == "result.json"}
    media_dirs = {
        "files",
        "photos",
        "video_files",
        "round_video_messages",
        "voice_messages",
        "stickers",
        "music",
        "audio_files",
        "profile_pictures",
        "images",
        "js",
        "css",
    }
    other_json = []
    for path in paths:
        name = path.as_posix()
        if any(
            part.casefold() in media_dirs
            for root in path.parents
            if root in roots
            for part in path.relative_to(root).parts[:-1]
        ):
            continue
        page = PAGE.fullmatch(path.name)
        if page:
            folder = path.parent.as_posix() if path.parent.parts else ""
            groups.setdefault(folder, []).append((int(page[1] or 1), name))
        elif path.name.lower() == "result.json":
            json_files.append(name)
        elif path.suffix.lower() == ".json":
            other_json.append(name)
    if not json_files:
        for name in other_json:
            if snap.files[name].size > MAX_PAGE:
                continue
            try:
                data = json.loads(read_text(snap, name))
            except (ValueError, RecoveryError, RecursionError):
                continue
            if isinstance(data, dict) and (
                (isinstance(data.get("messages"), list) and ("id" in data or "type" in data))
                or any(
                    isinstance(data.get(k), dict) and isinstance(data[k].get("list"), list)
                    for k in ("chats", "left_chats")
                )
            ):
                json_files.append(name)
    return groups, json_files


def parse(
    snap: Snapshot, timezone_name: str, progress: Progress = quiet, *, export_format="auto", sources=None, stream=False
) -> list[Chat]:
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise RecoveryError("Неизвестный часовой пояс IANA.") from None
    groups, json_files = sources if sources is not None else discover(snap)
    if export_format not in {"auto", "html", "json"}:
        raise RecoveryError("Выберите HTML, JSON или автоматическое определение.")
    chosen = ("json" if json_files else "html") if export_format == "auto" else export_format
    if chosen == "json" and not json_files or chosen == "html" and not groups:
        raise RecoveryError("Экспорт выбранного формата не найден. Проверьте папку и формат источника.")
    chats = []
    try:
        if chosen == "json":
            for name in json_files:
                progress("parse", 0, 0)
                chats.extend(parse_json(snap, name, zone, progress, stream=stream and (not groups or export_format == "json")))
        else:
            for folder, pages in groups.items():
                chats.append(parse_html(snap, folder, pages, zone, progress))
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise RecoveryError("Не удалось разобрать структуру экспорта Telegram Desktop.") from None
    if not chats:
        raise RecoveryError("В источнике не найден диалог Telegram Desktop.")
    if export_format == "auto" and chosen == "json" and groups:
        secondary = []
        for folder, pages in groups.items():
            try:
                secondary.append(parse_html(snap, folder, pages, zone, progress))
            except Cancelled:
                raise
            except (RecoveryError, ValueError, TypeError, AttributeError, KeyError, RecursionError):
                continue
        match_secondary_evidence(chats, secondary)
    return chats
