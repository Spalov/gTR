"""Compare source, package and independently observed properties without optimistic defaults."""

from collections import Counter
from hashlib import sha256

from .model import quiet

CONFIRMED = "confirmed"
PREPARED = "prepared"
PARTIAL = "partial"
LOCAL = "local_only"
UNSUPPORTED = "unsupported"
ABSENT = "absent"
UNKNOWN = "unknown"
ERROR = "error"

STATUS = {
    CONFIRMED: "Восстановлено и подтверждено",
    PREPARED: "Подготовлено, ожидает проверки",
    PARTIAL: "Восстановлено частично",
    LOCAL: "Сохранено локально",
    UNSUPPORTED: "Не поддерживается History Import",
    ABSENT: "Отсутствует в архиве",
    UNKNOWN: "Не удалось проверить",
    ERROR: "Ошибка или расхождение",
}
DIMENSIONS = {
    "text": ("text", "line_breaks", "unicode", "emoji"),
    "attachments": ("attachments", "caption", "filename"),
    "date": ("date", "time"),
    "order": ("order",),
    "media_classification": ("media_type",),
    "authorship": ("author",),
    "relationships": ("reply", "forward", "album", "quote"),
}
WEIGHTS = {
    "text": 25, "attachments": 20, "date": 15, "order": 10,
    "media_classification": 10, "authorship": 10, "relationships": 5, "independent": 5,
}
EMPTY = sha256(b"").hexdigest()


def _supported(source_present, included, observed, key, paired):
    if not source_present:
        return ABSENT
    if not included:
        return LOCAL
    evidence = [row.get("fields", {}).get(key, "unknown") for row in observed]
    if "different" in evidence:
        return ERROR
    if paired and len(evidence) == 2 and all(item == "same" for item in evidence):
        return CONFIRMED
    if "same" in evidence:
        return PARTIAL
    return UNKNOWN if evidence else PREPARED


def fidelity_record(record, item=None, observed=(), paired=False):
    included = record["included"]
    structure = record.get("structure", {})
    text = record.get("original_text_sha256", record.get("text_sha256", EMPTY)) != EMPTY
    original_media = record.get("original_media", [])
    supplied = bool(original_media)
    carried = bool(item)
    date = record.get("source_identity", {}).get("date_reliable", False)
    fields = {}
    for field, key, available in (
        ("text", "text", text),
        ("line_breaks", "text", structure.get("line_breaks", False)),
        ("unicode", "text", structure.get("unicode", False)),
        ("emoji", "text", structure.get("emoji", False)),
        ("date", "date", date),
        ("time", "date", date),
        ("order", "order", True),
        ("author", "label", bool(record["author"])),
        ("attachments", "attachment", supplied),
        ("media_type", "media_kind", supplied),
        ("filename", "document_name", any(a.get("original") for a in original_media)),
        ("caption", "text", text and supplied),
    ):
        transferred = included and (carried or field not in {"attachments", "media_type", "filename"})
        fields[field] = _supported(available, transferred, observed, key, paired)
    for field in ("forward", "reply", "quote", "album", "formatting", "editing", "reactions", "links"):
        exists = bool(record["reply_source_id"]) if field == "reply" else structure.get(field, False)
        fields[field] = (
            PARTIAL if exists and field in record.get("annotations", {}) and included
            else UNSUPPORTED if exists and included else LOCAL if exists else ABSENT
        )
    fields["timezone"] = LOCAL if record.get("source_identity", {}).get("original_timezone") else ABSENT
    if "date_conflict" in record["losses"] or "date" in record["losses"]:
        fields["date"] = fields["time"] = ERROR
    if any(a["status"] not in {"ok", "unconfirmed_type", "metadata"} for a in original_media):
        fields["attachments"] = ERROR
    if any(row.get("binding_error") for row in observed):
        fields["attachments"] = ERROR
    if carried and fields["attachments"] in {CONFIRMED, PARTIAL}:
        exact = _supported(True, True, observed, "media_bytes", paired)
        if item["upload_as"] == "photo" and exact == ERROR:
            exact = PARTIAL
        fields["attachments"] = exact
    if item and item.get("transform") and fields["attachments"] in {CONFIRMED, PREPARED}:
        fields["attachments"] = PARTIAL
    if structure.get("forward") and record.get("annotations"):
        fields["text"] = PARTIAL if fields["text"] != ERROR else ERROR
    existing = set(fields.values()) - {ABSENT, UNSUPPORTED, LOCAL}
    overall = (
        ERROR if ERROR in existing else CONFIRMED if existing == {CONFIRMED}
        else PARTIAL if CONFIRMED in existing or PARTIAL in existing else PREPARED if PREPARED in existing
        else UNKNOWN if existing else LOCAL
    )
    return {
        "source_ordinal": record["source_ordinal"], "source_id": record["source_id"],
        "historical_date": record["original_date"], "source_kinds": [a["kind"] for a in original_media],
        "prepared_kind": (item["kind"] if item.get("attributes") or item["upload_as"] == "photo" else "document")
        if item else None,
        "prepared_file": item.get("display_name", item["name"]) if item else None,
        "prepared_date": record.get("import_date"), "included": bool(included),
        "original_filenames": [a.get("display_name") or a.get("original") for a in original_media],
        "video_decision": record.get("video_recovery", {}).get("action"),
        "observed": [
            {"server_id": row["imported_message_id"], "kind": row.get("media_kind"),
             "date": row.get("imported_original_date"), "server_date": row.get("imported_server_date"),
             "file": row.get("document_name"), "constructor": row.get("media_constructor")}
            for row in observed
        ],
        "fields": fields, "status": overall, "reasons": list(record["losses"]),
    }


def evidence_lookup(comparison):
    if not comparison:
        return (), False
    views = comparison["views"]
    paired = (
        comparison.get("mode") == "telegram_observation" and len(views) == 2
        and views[0]["account_id"] != views[1]["account_id"]
        and all(not v["scope"].get("truncated") and not v["ambiguous"] and not v["duplicate_ids"] for v in views)
    )
    return tuple({row["ordinal"]: row for row in view["matched"]} for view in views), paired


def quality(rows, independently_confirmed):
    groups = {name: Counter() for name in DIMENSIONS}
    totals = Counter()
    message_errors = 0
    for row in rows:
        message_errors += row["status"] == ERROR
        fields = row["fields"]
        totals.update(fields.values())
        for name, keys in DIMENSIONS.items():
            groups[name].update(fields[key] for key in keys)
    details = {}
    weighted = active_weight = 0.0
    for name, counts in groups.items():
        eligible = sum(v for k, v in counts.items() if k not in {ABSENT, UNSUPPORTED, LOCAL})
        ratio = counts[CONFIRMED] / eligible if eligible else None
        details[name] = {"confirmed_fraction": ratio, "states": dict(counts)}
        if ratio is not None:
            weighted += WEIGHTS[name] * ratio
            active_weight += WEIGHTS[name]
    progress = independently_confirmed / len(rows) if rows else None
    if progress is not None:
        weighted += WEIGHTS["independent"] * progress
        active_weight += WEIGHTS["independent"]
    score = round(100 * weighted / active_weight, 2) if active_weight else None
    if score == 100 and (message_errors or totals[UNKNOWN] or totals[PREPARED] or totals[PARTIAL]):
        score = 99.99
    return {"index": score, "weights": WEIGHTS, "independent_fraction": progress,
            "messages_with_errors": message_errors, "states": dict(totals), "dimensions": details}


def fidelity(data, comparison=None, *, progress=quiet):
    media = {item["name"]: item for item in data["media"]}
    views, paired = evidence_lookup(comparison)
    rows, total = [], len(data["records"])
    for index, record in enumerate(data["records"], 1):
        if index % 100 == 1:
            progress("verify", index, total)
        observed = tuple(view[record["ordinal"]] for view in views if record.get("ordinal") in view)
        rows.append(fidelity_record(record, media.get(record.get("media")), observed, paired))
    return {"kind": "recovery_fidelity", "quality": quality(rows, (comparison or {}).get("client_messages_verified", 0)
                                                            if paired else 0), "records": rows}


PRIVATE_FIELDS = {"source_id", "historical_date", "prepared_date", "prepared_file", "original_filenames"}


def public_fidelity(report):
    from .conversion import LOSS
    from .media import KIND_NAMES

    constructors = {"MessageMediaPhoto", "MessageMediaDocument", "MessageMediaEmpty"}
    safe_rows = []
    for row in report["records"]:
        safe = {k: v for k, v in row.items() if k not in PRIVATE_FIELDS and k != "observed"}
        safe["reasons"] = [code for code in row["reasons"] if code in LOSS]
        safe["source_kinds"] = [kind for kind in row["source_kinds"] if kind in KIND_NAMES]
        safe["prepared_kind"] = row["prepared_kind"] if row["prepared_kind"] in KIND_NAMES else None
        safe["video_decision"] = (
            row["video_decision"] if row["video_decision"] in {"keep", "convert", "document", "skip", "blocked"} else None
        )
        safe["observed"] = [
            {
                "kind": view["kind"] if view["kind"] in KIND_NAMES else None,
                "constructor": view["constructor"] if view["constructor"] in constructors else None,
            }
            for view in row["observed"]
        ]
        safe_rows.append(safe)
    return {**report, "records": safe_rows}
