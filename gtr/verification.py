"""Independent local, Telegram readback and client-appearance evidence."""

import re
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from itertools import combinations
from zoneinfo import ZoneInfo

from .files import read_json
from .integrity import check_local
from .model import RecoveryError, quiet
from .observation import MAX_SCAN

FIELD_NAMES = {
    "text": "Текст",
    "order": "Порядок на сервере",
    "label": "Имя в метаданных",
    "server_author": "Серверный отправитель",
    "out": "Признак «исходящее»",
    "imported": "Маркировка импорта",
    "attachment": "Наличие вложения",
    "media_access": "Чтение медиа с сервера",
    "media_bytes": "Совпадение байтов полученного медиа",
    "document_name": "Имя документа на сервере",
    "document_mime": "Тип документа на сервере",
    "media_type": "Вид вложения на сервере",
    "media_kind": "Фактическое представление вложения",
    "reply": "Связь ответа",
    "date": "Историческая дата в метаданных",
    "wire_date": "Дата из плана в метаданных",
    "server_date": "Серверная дата присутствует",
    "visual_visible": "Видимость в клиенте",
    "visual_side": "Сторона в клиенте",
    "visual_name": "Имя в клиенте",
    "visual_date": "Историческая дата и время в клиенте",
    "visual_rank": "Место в хронологии клиента",
    "visual_jump": "Переход к исторической дате",
    "visual_media": "Вложения открываются в клиенте",
}
FIELDS = tuple(FIELD_NAMES)
VERDICTS = {
    "verified": "Подтверждены локальные данные, историческое содержимое и отображение",
    "differences": "Обнаружены расхождения",
    "inconclusive": "Не хватает данных для вывода",
}
VERIFICATION_NAMES = {
    "CONFIRMED": VERDICTS["verified"],
    "PARTIALLY_CONFIRMED": "Подтверждено частично",
    "NOT_CONFIRMED": "Не подтверждено: есть расхождения",
    "INCONCLUSIVE": "Не хватает данных для вывода",
}
SERVER_FIELDS = (
    "text",
    "order",
    "label",
    "imported",
    "attachment",
    "media_access",
    "date",
    "server_date",
)
UNSUPPORTED_BY_API = "UNSUPPORTED_BY_API"
EVIDENCE_STATES = {
    "NO_EVIDENCE_IN_WINDOW": "В выбранной области нет сопоставимых импортированных записей",
    "INCONCLUSIVE": "Для вывода нужно больше наблюдений",
    "COMPLETE_MATCH": "Сопоставление подтверждено",
    "MISMATCH": "В наблюдениях обнаружены расхождения",
}
VISUAL_FIELDS = tuple(k for k in FIELDS if k.startswith("visual_"))
QUALITY = {
    "Текст": ("text",),
    "Даты": ("date", "visual_date", "visual_jump"),
    "Авторы": ("label", "visual_name", "visual_side"),
    "Медиа": ("attachment", "media_access", "visual_media"),
    "Видимость": ("visual_visible", "visual_rank"),
}


def _valid_capture_scope(capture):
    scope = capture["scope"]
    kind = scope["kind"]
    after, through = scope["after_id"], scope["through_id"]
    rows = capture["messages"]
    if (
        kind not in {"id_window", "historical_date"}
        or type(after) is not int
        or not 0 <= after < 2**31 - 1
        or through is not None
        and (type(through) is not int or not after < through < 2**31 - 1)
        or not all(after < row["id"] and (through is None or row["id"] <= through) for row in rows)
        or scope.get("returned") != len(rows)
        or type(scope.get("truncated")) is not bool
        or type(scope.get("scanned")) is not int
        or type(scope.get("scan_limit")) is not int
        or not 0 <= len(rows) <= scope["scanned"] <= scope["scan_limit"] <= MAX_SCAN
        or capture["after_id"] != after
        or capture["through_id"] != through
        or not _stamp(capture["captured_at"])
    ):
        return False
    if kind == "historical_date":
        return date.fromisoformat(scope["historical_from"]) <= date.fromisoformat(
            scope["historical_through"]
        ) and not scope.get("baseline_captured_at")
    if scope.get("baseline_captured_at"):
        baseline = _stamp(scope["baseline_captured_at"])
        return bool(baseline and baseline <= _stamp(capture["captured_at"]))
    return True


def _captures(bundle, first, second):
    pair = (first,) if second is None else (first, second)
    try:
        if any(
            c["schema"] not in {1, 2, 3}
            or c["package"] != bundle.fingerprint()
            or not isinstance(c["messages"], list)
            for c in pair
        ):
            raise ValueError
        if any(c.get("manifest_sha256") != bundle.manifest_hash() for c in pair if c["schema"] >= 2):
            raise ValueError
        kind = first.get("peer_kind", "pm")
        accounts = [c["account_id"] for c in pair]
        if kind == "pm" and second is None:
            accounts.append(first["peer_id"])
        if len(set(accounts)) != len(accounts) or any(type(a) is not int or a <= 0 for a in accounts):
            raise ValueError
        if kind not in {"pm", "group"} or any(c.get("peer_kind", "pm") != kind for c in pair):
            raise ValueError
        if kind == "pm" and any(c["peer_id"] != p for c, p in zip(pair, reversed(accounts))):
            raise ValueError
        if kind == "group" and (
            type(first["peer_id"]) is not int
            or first["peer_id"] >= 0
            or any(c["peer_id"] != first["peer_id"] for c in pair)
        ):
            raise ValueError
        if first["mode"] not in {"mock", "telegram_observation"} or any(
            c["mode"] != first["mode"] for c in pair
        ):
            raise ValueError
        for c in pair:
            if any(
                type(row["id"]) is not int
                or row["id"] <= 0
                or row.get("text_sha256") is not None
                and not isinstance(row["text_sha256"], str)
                for row in c["messages"]
            ):
                raise ValueError
            if c["schema"] == 3 and not _valid_capture_scope(c):
                raise ValueError
    except (KeyError, ValueError, TypeError, AttributeError):
        raise RecoveryError(
            "Выберите снимки этого пакета из одного диалога, по одному с каждого аккаунта."
        ) from None
    return accounts


def _author_map(expected, accounts, explicit):
    if explicit is not None:
        if (
            not isinstance(explicit, dict)
            or any(type(v) is not int or v not in accounts for v in explicit.values())
            or len(set(explicit.values())) != len(explicit)
            or not set(explicit).issubset({row["author"] for row in expected})
        ):
            raise RecoveryError("Выберите разные аккаунты из снимков для исходных имён пакета.")
        return explicit
    found = {}
    for row in expected:
        match = re.fullmatch(r"user(\d+)", row.get("source_author_id", ""))
        if match:
            found.setdefault(row["author"], set()).add(int(match[1]))
    return {key: next(iter(ids)) for key, ids in found.items() if len(ids) == 1 and ids.issubset(accounts)}


def _stamp(value):
    if not value:
        return None
    try:
        date = datetime.fromisoformat(value)
        return date.astimezone(UTC).isoformat(timespec="seconds") if date.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def _original(row):
    source = row.get("source_identity", {})
    if source:
        return _stamp(source["original_utc"]) if source.get("date_reliable") else None
    return _stamp(row.get("original_date"))


def _wire(row, zone):
    if "import_utc" in row:
        return _stamp(row["import_utc"])
    try:
        return _stamp(datetime.fromisoformat(row["import_date"]).replace(tzinfo=zone).isoformat())
    except (ValueError, TypeError):
        return None


def _measure(actual, expected):
    return "unknown" if actual is None or expected is None else "same" if actual == expected else "different"


def _visible_time(row, original):
    value = _stamp(row.get("visual_date"))
    precision = row.get("visual_precision")
    if precision == "minute" and value and original:
        if datetime.fromisoformat(value).second != 0:
            return None, original
        return value, datetime.fromisoformat(original).replace(second=0).isoformat(timespec="seconds")
    return (value if precision is None or precision == "second" else None), original


def _positions(keys):
    result = defaultdict(list)
    for index, key in enumerate(keys):
        result[key].append(index)
    return result


def _projections(features):
    fields = tuple(i for i, value in enumerate(features) if value is not None)
    for size in range(1, len(fields) + 1):
        for shape in combinations(fields, size):
            yield shape, tuple(features[i] for i in shape)


def _align(expected, actual, zone, media_hashes):
    """Match unique text or unique metadata signatures, never native account fields."""
    want = [r["text_sha256"] for r in expected]
    got = [r.get("text_sha256") for r in actual]
    want_counts, got_counts = Counter(want), Counter(got)
    source_groups, observed_groups = _positions(want), _positions(got)
    pairs = [(None, None)] * len(expected)
    ambiguous = 0
    for digest, source in source_groups.items():
        received = observed_groups.get(digest, ())
        if len(source) == len(received) == 1:
            pairs[source[0]] = received[0], actual[received[0]]
        elif received:
            lookup = defaultdict(set)
            known_media = {media_hashes.get(expected[i].get("media")) for i in source} - {None}
            for i in source:
                record = expected[i]
                for stamp in {_original(record), _wire(record, zone)}:
                    for key in _projections(
                        (
                            bool(record.get("media")),
                            record["author"],
                            stamp,
                            media_hashes.get(record.get("media")),
                        )
                    ):
                        lookup[key].add(i)
            candidates = defaultdict(list)
            for i in received:
                record = actual[i]
                label, media_hash = record.get("imported_name"), record.get("media_sha256")
                features = (
                    record["has_media"] if type(record.get("has_media")) is bool else None,
                    label if isinstance(label, str) and label else None,
                    _stamp(record.get("original_date")),
                    media_hash if isinstance(media_hash, str) and media_hash in known_media else None,
                )
                shape = tuple(i for i, value in enumerate(features) if value is not None)
                matches = lookup.get((shape, tuple(features[i] for i in shape)), ())
                if len(matches) == 1:
                    candidates[next(iter(matches))].append(i)
            for pos, matches in candidates.items():
                if len(matches) == 1:
                    pairs[pos] = matches[0], actual[matches[0]]
            ambiguous += sum(pairs[i][0] is None for i in source)
    return (
        pairs,
        sum((want_counts - got_counts).values()),
        sum((got_counts - want_counts).values()),
        ambiguous,
    )


def _view(expected, capture, authors, zone, media_hashes, media):
    rows = capture["messages"]
    aligned, missing, extra, ambiguous = _align(expected, rows, zone, media_hashes)
    id_counts = Counter(row["id"] for row in rows)
    source_to_server = {
        e["source_id"]: a["id"]
        for e, (_, a) in zip(expected, aligned)
        if a is not None and id_counts[a["id"]] == 1 and e["source_id"]
    }
    metrics = {key: Counter() for key in FIELDS}
    mismatches, observations = [], []
    seen, verified, present = set(), set(), set()
    scope = (
        capture.get("scope")
        if capture["schema"] == 3
        else {
            "kind": "legacy",
            "after_id": capture.get("after_id"),
            "through_id": capture.get("through_id"),
            "returned": len(rows),
            "truncated": False,
        }
    )
    expected_hashes = {row["text_sha256"] for row in expected}
    evidence_candidates = sum(
        row.get("imported") is True and row.get("text_sha256") in expected_hashes for row in rows
    )
    for i, (want, (position, got)) in enumerate(zip(expected, aligned), 1):
        if got is None or id_counts[got["id"]] != 1:
            for item in metrics.values():
                item["unknown"] += 1
            continue
        identity = authors.get(want["author"])
        own = identity == capture["account_id"] if identity is not None else None
        has = bool(want.get("media"))
        attachment = media.get(want.get("media"), {})
        is_document = attachment.get("upload_as") == "document"
        from .filenames import display_name
        original = _original(want)
        server_id = got.get("from_id")
        if type(server_id) is not int:
            server_id = None

        def flag(key, got=got):
            value = got.get(key)
            return value if type(value) is bool else None

        reply = want.get("resolved_reply", want["reply_source_id"])
        reply_id = source_to_server.get(reply) if want["reply_source_id"] else 0
        fields = {
            "text": (got.get("text_sha256"), want["text_sha256"]),
            "order": (position + 1, i),
            "label": (got.get("imported_name"), want["author"]),
            "server_author": (server_id, identity),
            "out": (flag("out"), own),
            "imported": (flag("imported"), True),
            "attachment": (flag("has_media"), has),
            "media_access": (flag("media_accessible") if has else True, True),
            "media_bytes": (got.get("media_sha256"), media_hashes.get(want.get("media"))),
            "document_name": (got.get("document_name"), display_name(attachment) if is_document else None),
            "document_mime": (got.get("document_mime"), attachment.get("mime") if is_document else None),
            "media_type": (
                got.get("media_constructor"),
                ("MessageMediaDocument" if is_document else "MessageMediaPhoto") if has else None,
            ),
            "media_kind": (
                got.get("media_kind"),
                (attachment.get("kind") if attachment.get("attributes") or not is_document else "document")
                if has else None,
            ),
            "reply": (got.get("reply_to_id") or 0, reply_id),
            "date": (_stamp(got.get("original_date")), original),
            "wire_date": (_stamp(got.get("original_date")), _wire(want, zone)),
            "server_date": (True if _stamp(got.get("server_date")) else None, True),
            "visual_visible": (flag("visual_visible"), True),
            "visual_side": (flag("visual_out"), own),
            "visual_name": (got.get("visual_name") or None, want["author"]),
            "visual_date": _visible_time(got, original),
            "visual_rank": (got.get("visual_rank") if type(got.get("visual_rank")) is int else None, i),
            "visual_jump": (flag("visual_jump"), True),
            "visual_media": (flag("visual_media") if has else True, True),
        }
        found = []
        marks = {}
        for key, (actual, desired) in fields.items():
            mark = _measure(actual, desired)
            marks[key] = mark
            metrics[key][mark] += 1
            if mark == "different":
                found.append(key)
        if all(marks[key] == "same" for key in ("imported", "text")):
            seen.add(i)
            if marks["date"] == marks["label"] == marks["attachment"] == "same" and all(
                marks[k] != "different"
                for k in ("document_name", "document_mime", "media_type", "media_kind", "media_bytes")
            ):
                present.add(want["source_ordinal"])
            if all(marks[key] == "same" for key in VISUAL_FIELDS):
                verified.add(i)
        if found:
            mismatches.append(
                {"ordinal": i, "source_id": want["source_id"], "server_id": got["id"], "fields": found}
            )
        observations.append(
            {
                "source_id": want["source_id"],
                "ordinal": i,
                "fields": marks,
                "media_kind": got.get("media_kind"),
                "document_name": got.get("document_name"),
                "mapped_telegram_peer_id": identity,
                "imported_message_id": got["id"],
                "imported_sender_id": server_id,
                "effective_sender_id": got.get("effective_sender_id"),
                "imported_historical_peer_id": got.get("imported_from_id"),
                "imported_author_name": got.get("imported_name"),
                "imported_original_date": got.get("original_date"),
                "imported_server_date": got.get("server_date"),
                "imported_direction": flag("out"),
                "visual_date": got.get("visual_date"),
                "visual_precision": got.get("visual_precision"),
                "media_error": got.get("media_error"),
                "media_constructor": got.get("media_constructor"),
                "media_probe_scope": got.get("media_probe_scope", "none"),
                "media_bytes": marks["media_bytes"],
                "verification_status": "differences"
                if any(marks[key] == "different" for key in (*SERVER_FIELDS, *VISUAL_FIELDS, "media_kind"))
                else "unknown"
                if any(marks[key] == "unknown" for key in (*SERVER_FIELDS, *VISUAL_FIELDS))
                else "verified",
            }
        )
    view = {
        "account_id": capture["account_id"],
        "peer_id": capture["peer_id"],
        "peer_kind": capture.get("peer_kind", "pm"),
        "captured_at": capture.get("captured_at"),
        "observed": len(rows),
        "expected": len(expected),
        "missing": missing,
        "extra": extra,
        "ambiguous": ambiguous,
        "evidence_candidates": evidence_candidates,
        "scope": scope,
        "duplicate_ids": len(rows) - len(id_counts),
        "after_restart": capture.get("after_restart") is True,
        "client": capture.get("client", ""),
        "metrics": {key: dict(counts) for key, counts in metrics.items()},
        "differences": mismatches,
        "matched": observations,
    }
    return view, seen, verified, present


def _level(views, fields, count, *, confirmed=True):
    if any(v["metrics"][field].get("different") for v in views for field in fields):
        return "differences"
    if not confirmed or any(v["metrics"][field].get("same", 0) != count for v in views for field in fields):
        return "unknown"
    return "verified"


def compare(bundle, first, second=None, mapping=None, *, project=None, progress=quiet):
    if bundle.data["schema"] < 2:
        raise RecoveryError("Для проверки подготовьте новый пакет.")
    accounts = _captures(bundle, first, second)
    expected = [row for row in bundle.data["records"] if row["included"]]
    authors = _author_map(expected, accounts, mapping)
    identities = {row["source_author_id"] or row["author"] for row in expected}
    mapped = len(identities) == 2 and {authors.get(row["author"]) for row in expected} == set(accounts)
    count = len(expected)
    zone = ZoneInfo("UTC" if bundle.data["timezone"] == "source" else bundle.data["timezone"])
    local = check_local(bundle, project, progress)
    media_hashes = {item["name"]: item["sha256"] for item in bundle.data["media"]}
    snapshots = (first,) if second is None else (first, second)
    media = {item["name"]: item for item in bundle.data["media"]}
    inspected = [_view(expected, capture, authors, zone, media_hashes, media) for capture in snapshots]
    views = [result[0] for result in inspected]
    paired = len(views) == 2
    complete_read = paired and all(v["evidence_candidates"] and not v["scope"]["truncated"] for v in views)
    visibility = (
        bool(count)
        and complete_read
        and all(
            v["observed"] == count
            and not (v["missing"] or v["extra"] or v["duplicate_ids"] or v["ambiguous"])
            for v in views
        )
    )
    accepted = visibility and all(
        (
            v["scope"]["kind"] == "legacy"
            or v["scope"]["kind"] == "id_window"
            and _stamp(v["scope"].get("baseline_captured_at"))
        )
        and all(v["metrics"][key].get("same") == count for key in ("imported", "date", "label", "order"))
        for v in views
    )
    server = _level(views, SERVER_FIELDS, count, confirmed=visibility) if complete_read else "unknown"
    if complete_read and any(
        v["metrics"][k].get("different")
        for v in views
        for k in ("document_name", "document_mime", "media_type", "media_kind")
    ):
        server = "differences"
    if (
        complete_read
        and all(v["scope"]["kind"] != "historical_date" for v in views)
        and any(v["missing"] or v["extra"] or v["duplicate_ids"] for v in views)
    ):
        server = "differences"
    restart = paired and all(v["after_restart"] and v["client"] for v in views)
    visual = (
        _level(
            views,
            VISUAL_FIELDS,
            count,
            confirmed=mapped and visibility and restart,
        )
        if complete_read
        else "unknown"
    )
    replies = _level(views, ("reply",), count, confirmed=visibility)
    statuses = (local["status"], server, visual)
    state = (
        "differences"
        if complete_read and "differences" in statuses
        else "verified"
        if all(s == "verified" for s in statuses)
        else "inconclusive"
    )
    evidence_status = (
        "NO_EVIDENCE_IN_WINDOW"
        if not any(v["evidence_candidates"] for v in views)
        else {"verified": "COMPLETE_MATCH", "differences": "MISMATCH", "inconclusive": "INCONCLUSIVE"}[state]
    )
    server_observed = len(inspected[0][1] & inspected[1][1]) if paired else 0
    client_verified = len(inspected[0][2] & inspected[1][2]) if restart else 0
    verification_status = {
        "verified": "CONFIRMED",
        "differences": "NOT_CONFIRMED",
        "inconclusive": "PARTIALLY_CONFIRMED"
        if paired and (server_observed or client_verified)
        else "INCONCLUSIVE",
    }[state]
    quality = {
        label: _level(
            views,
            fields,
            count,
            confirmed=visibility and (restart if any(key in VISUAL_FIELDS for key in fields) else True),
        )
        for label, fields in QUALITY.items()
    }
    if not complete_read:
        quality = dict.fromkeys(quality, "unknown")
    if local["status"] != "verified":
        quality = {key: ("unknown" if value == "verified" else value) for key, value in quality.items()}
    if visual != "verified":
        quality["Видимость"] = "unknown" if quality["Видимость"] == "verified" else quality["Видимость"]
    quality["Ответы"] = UNSUPPORTED_BY_API
    confirmed_ordinals = (
        sorted(inspected[0][3] & inspected[1][3])
        if paired
        and first["mode"] == "telegram_observation"
        and all(
            (
                v["scope"]["kind"] == "id_window"
                and v["scope"].get("baseline_captured_at")
                or v["scope"]["kind"] == "historical_date"
                and not v["ambiguous"]
                and not v["duplicate_ids"]
            )
            and not v["scope"]["truncated"]
            for v in views
        )
        else []
    )
    return {
        "kind": "import_verification",
        "mode": first["mode"],
        "state": state,
        "verification_status": verification_status,
        "expected": count,
        "levels": {"A": local, "B": server, "B1": server, "B2": UNSUPPORTED_BY_API, "C": visual},
        "native": {
            "status": UNSUPPORTED_BY_API,
            "native_message_id": UNSUPPORTED_BY_API,
            "sender_id": UNSUPPORTED_BY_API,
            "direction": UNSUPPORTED_BY_API,
            "read_state": UNSUPPORTED_BY_API,
            "reply": UNSUPPORTED_BY_API,
            "reactions": UNSUPPORTED_BY_API,
            "sender_direction_observation": _level(
                views, ("server_author", "out"), count, confirmed=mapped and visibility
            ),
        },
        "quality": quality,
        "replies": replies,
        "evidence_status": evidence_status,
        "server_messages_observed": server_observed,
        "client_messages_verified": client_verified,
        "verified_records": client_verified,
        "visual_status": "VISUAL_RECONSTRUCTION_CONFIRMED"
        if visual == "verified"
        else "VISUAL_RECONSTRUCTION_UNCONFIRMED",
        "historical_metadata_only": paired
        and bool(count)
        and all(v["metrics"]["date"].get("same", 0) == count for v in views)
        and any(v["metrics"]["visual_date"].get("same", 0) != count for v in views),
        "views": views,
        "mapped_authors": len(authors),
        "package": bundle.fingerprint(),
        "manifest_sha256": bundle.manifest_hash(),
        "accepted_evidence": accepted,
        "confirmed_source_ordinals": confirmed_ordinals,
    }


def compare_files(bundle, paths, mapping=None, *, project=None, progress=quiet):
    if len(paths) not in {1, 2}:
        raise RecoveryError("Выберите от одного до двух JSON-снимков.")
    return compare(
        bundle,
        read_json(paths[0]),
        read_json(paths[1]) if len(paths) == 2 else None,
        read_json(mapping) if mapping else None,
        project=project,
        progress=progress,
    )
