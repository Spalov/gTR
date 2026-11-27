"""Offline, printable project and import reports. Reports never contain message bodies."""

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from html import escape
from pathlib import Path

from . import __version__
from .conversion import LOSS
from .core import ISSUES, catalogue, properties, summary, verify
from .dates import DATE_ISSUES
from .files import local, publish_text
from .history import UNRELIABLE
from .media import KIND_NAMES
from .model import RecoveryError, quiet
from .verification import EVIDENCE_STATES, FIELD_NAMES, VERDICTS, VERIFICATION_NAMES

FILE_STATES = {
    "ok": "Совпадает SHA-256",
    "changed": "Изменён",
    "missing": "Отсутствует",
    "unreadable": "Недоступен",
}
LOCAL = "В локальном проекте хранятся исходные записи, доступные идентификаторы, даты и связи. В пакет входят текст, метки авторов и байты доступных вложений."
SERVER = "Telegram определяет итоговое авторство, представление и время. Серверные ID и статусы прочтения не переносятся. Отдельные потери перечислены ниже."
DATE_RESOLUTIONS = {
    "exported": "Смещение или метка из экспорта",
    "derived_offset": "Смещение определено по двум исходным полям",
    "configured_zone": "Применён часовой пояс проекта",
    "parsed": "Разобрано",
    "unresolved": "Время не определено",
}
DATE_FIELD_STATES = {
    "empty": "поле пустое",
    "clock_only": "только показания часов, без даты",
    "invalid_epoch": ISSUES["invalid_epoch"],
    "invalid_wall_date": ISSUES["invalid_wall_date"],
}


def date_diagnostics(db, progress=quiet):
    codes = sorted(DATE_ISSUES)
    fields = ",".join("?" for _ in codes)
    flags = defaultdict(set)
    for mid, code in db.execute(f"SELECT msg,code FROM issues WHERE code IN ({fields})", codes):
        flags[mid].add(code)
    records = []
    for row in db.execute("""SELECT id,chat,source_id,source,date_raw,date_iso,meta FROM messages
        ORDER BY chat,id"""):
        if len(records) % 250 == 0:
            progress("report", 0, 1)
        issues = flags[row["id"]]
        meta = json.loads(row["meta"])
        evidence = meta.get("date_evidence", {})
        comparison = meta.get("format_comparison", {})
        alternate = {
            key: comparison[key] for key in ("preferred", "date_conflict", "html_date", "html_parsed",
                                             "json_date", "json_parsed") if key in comparison
        }
        moment = datetime.fromisoformat(row["date_iso"]) if row["date_iso"] else None
        usable = bool(moment and not (issues & UNRELIABLE))
        status = "service_no_time" if "service_no_time" in issues else "parsed" if usable else "unresolved"
        records.append(
            {
                "chat": row["chat"],
                "source_id": row["source_id"],
                "file": row["source"],
                "raw": row["date_raw"],
                "parsed": row["date_iso"] or None,
                "utc": moment.astimezone(UTC).isoformat() if moment else None,
                "status": status,
                "resolution": evidence.get("resolution", status),
                "selected": evidence.get("selected"),
                "precision": meta.get("historical_time", {}).get("precision", "unknown"),
                **({"alternate_format": alternate} if alternate else {}),
                "fields": evidence.get("fields", []),
                "issues": [{"code": code, "reason": ISSUES[code]} for code in sorted(issues)],
            }
        )
    return {
        "timezone": properties(db).get("timezone", ""),
        "counts": dict(Counter(r["status"] for r in records)),
        "records": records,
    }


def project_report(project, progress=quiet):
    stats = summary(project)
    problems = []
    checked = verify(project, progress, problems=problems)
    with catalogue(project) as db:
        chats = [
            dict(row)
            for row in db.execute("""SELECT c.id,c.title,
            count(CASE WHEN m.service=0 THEN 1 END) AS messages,
            count(DISTINCT CASE WHEN m.service=0 THEN nullif(m.sender_key,'') END) AS participants
            FROM chats c LEFT JOIN messages m ON m.chat=c.id GROUP BY c.id ORDER BY c.id""")
        ]
        issues = [
            dict(row)
            for row in db.execute("""SELECT i.chat,m.source_id,i.code,i.level
            FROM issues i LEFT JOIN messages m ON m.id=i.msg ORDER BY i.chat,i.msg,i.code""")
        ]
        dates = date_diagnostics(db, progress)
    return {
        "kind": "project",
        "version": __version__,
        "created": datetime.now().astimezone().isoformat(timespec="seconds"),
        "project": Path(project).name,
        "summary": stats,
        "chats": chats,
        "observations": issues,
        "date_diagnostics": dates,
        "verification": {"counts": checked, "problems": problems},
    }


EMPTY_TEXT = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def plan_summary(data):
    records = data["records"]
    statuses = Counter(r.get("video_recovery", {}).get("action") for r in records if r.get("video_recovery"))
    media_bytes = sum(item["size"] for item in data["media"])
    return {
        "source_records": data.get("source_records", len(data["records"])),
        "selected": data.get("selected_records", len(data["records"])),
        "selection": data.get("selection"),
        "included": data["included"],
        "skipped": data["skipped"],
        "attachments": len(data["media"]),
        "omitted_assets": data.get("omitted_assets", 0),
        "media_mode": data.get("media_mode", "text_media"),
        "media_bytes": media_bytes,
        "text_records": sum(bool(r.get("included")) and r.get("original_text_sha256", EMPTY_TEXT) != EMPTY_TEXT for r in records),
        "forwards": sum(bool(r.get("structure", {}).get("forward")) for r in records),
        "replies": sum(bool(r.get("reply_source_id")) for r in records),
        "album_elements": sum(bool(r.get("structure", {}).get("album")) for r in records),
        "albums": data.get("album_count"),
        "missing_assets": sum(a["status"] in {"missing", "empty"} for r in records for a in r.get("original_media", [])),
        "video_actions": dict(statuses),
        "attachment_actions": dict(Counter(
            r["media_recovery"]["action"] for r in records if r.get("media_recovery")
        )),
        "video_incompatible": sum(
            r.get("video_recovery", {}).get("original", {}).get("status") not in {None, "compatible"}
            for r in records
        ),
        "annotated_records": sum(bool(r.get("annotations")) for r in records),
        "estimated_seconds_at_10_mbps": round(media_bytes * 8 / 10_000_000, 2),
        "timezone": data["timezone"],
        "date_policy": data.get("date_policy", "ordered"),
        "timeline": data.get("timeline", {}),
        "by_kind": dict(Counter(item["kind"] for item in data["media"])),
        "losses": data["losses"],
    }


def _table(headings, rows):
    rows = list(rows)
    if not rows:
        return "<p>Замечаний нет.</p>"
    head = "".join(f"<th>{escape(t)}</th>" for t in headings)
    body = "".join(
        "<tr>" + "".join(f"<td>{escape(str(v if v is not None else '—'))}</td>" for v in r) + "</tr>"
        for r in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _document(title, parts):
    title = escape(title)
    style = """body{font:16px/1.55 'Segoe UI',sans-serif;max-width:960px;margin:36px auto;padding:0 24px;color:#1d2c38}
    h1{margin:4px 0 12px;font-size:32px}h2{margin-top:28px;font-size:21px}header{border-bottom:3px solid #dbad63}
    header small{color:#54697c}table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:6px 10px;
    text-align:left;border-bottom:1px solid #d6dfe4;overflow-wrap:anywhere}th{background:#edf2f5}
    code{overflow-wrap:anywhere}p{margin:8px 0}@media print{body{margin:0}tr{break-inside:avoid}thead{display:table-header-group}}"""
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta http-equiv="Content-Security-Policy" content="default-src &#39;none&#39;; style-src &#39;unsafe-inline&#39;">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>'
        f"<style>{style}</style></head><body><header><small>gTR {__version__} · gAssets</small><h1>{title}</h1>"
        f"</header>{''.join(parts)}</body></html>"
    )


def _dates_html(dates):
    if not dates["records"]:
        return ""
    rows = []
    labels = {**DATE_RESOLUTIONS, "service_no_time": ISSUES["service_no_time"]}
    for record in dates["records"]:
        inputs = " · ".join(
            f"{item['field']} = {item['raw']} → {item['parsed'] or DATE_FIELD_STATES[item['status']]}"
            for item in record["fields"]
        )
        if alternate := record.get("alternate_format"):
            raw = alternate["html_date"] or "—"
            moment = alternate["html_parsed"] or "—"
            inputs = f"{inputs or record['raw']} · HTML {raw} → {moment}"
        notes = [labels.get(record["resolution"], labels[record["status"]])]
        notes.extend(item["reason"] for item in record["issues"] if item["reason"] != notes[0])
        rows.append(
            (
                f"{record['chat']} / {record['source_id'] or '—'}",
                record["file"],
                record["selected"],
                inputs or record["raw"],
                record["parsed"],
                record["utc"],
                " · ".join(notes),
            )
        )
    counts = " · ".join(f"{labels[key]} {count}" for key, count in dates["counts"].items())
    formats = " HTML" if all(row["file"].lower().endswith(".html") for row in dates["records"]) else ""
    return (
        f"<h2>Диагностика дат{formats}</h2><p>{escape(counts)}. Часовой пояс проекта {escape(dates['timezone'])}.</p>"
        + _table(
            ("Диалог / ID", "Файл", "Выбрано", "Исходные поля", "Разобрано со смещением", "UTC", "Итог"), rows
        )
    )


def project_html(report):
    s = report["summary"]
    return _document(
        "Отчёт о восстановлении",
        [
            f"<p>{escape(report['project'])} · {escape(report['created'])}</p>",
            _table(
                ("Показатель", "Значение"),
                [
                    ("Тип экспорта", s["export_format"].upper()),
                    ("Источник", s["source_type"]),
                    ("Сообщения", s["messages"]),
                    ("Служебные события", s["service_events"]),
                    ("Участники", s["participants"]),
                    ("Период", f"{s['first'] or '—'} — {s['last'] or '—'}"),
                    ("Ответы с найденной целью", f"{s['resolved_replies']} / {s['replies']}"),
                    ("Повторы исходных ID", s["duplicates"]),
                    ("Конфликтующие ID", s["conflicting_ids"]),
                    ("Файлы", s["files"]),
                    ("Объём файлов в байтах", s["extracted_bytes"]),
                    ("SHA-256 источника", s["sha256"]),
                ],
            ),
            "<h2>Диалоги</h2>",
            _table(
                ("Диалог", "Сообщения", "Участники"),
                [(c["title"], c["messages"], c["participants"]) for c in report["chats"]],
            ),
            _dates_html(report["date_diagnostics"]),
            "<h2>Вложения</h2>",
            _table(("Тип", "Записи"), [(name, s["attachments"].get(k, 0)) for k, name in KIND_NAMES.items()]),
            "<h2>Повторная проверка файлов</h2>",
            _table(
                ("Состояние", "Файлы"),
                [(FILE_STATES[k], v) for k, v in report["verification"]["counts"].items()],
            ),
            _table(
                ("Относительный путь", "Проблема"),
                [(p["path"], FILE_STATES[p["status"]]) for p in report["verification"]["problems"]],
            )
            if report["verification"]["problems"]
            else "",
            "<h2>Наблюдения по записям</h2>",
            _table(
                ("Диалог №", "Исходный ID", "Наблюдение"),
                [
                    (i["chat"], i["source_id"], ISSUES.get(i["code"], i["code"]))
                    for i in report["observations"]
                ],
            ),
        ],
    )


VIDEO_ACTIONS = {
    "blocked": "Требуется решение", "keep": "Оригинал", "convert": "Совместимая копия",
    "document": "Документ", "skip": "Пропуск", "local": "Только локально",
}
VIDEO_RESULTS = {
    "compatible": "Параметры подходят", "transcode_required": "Нужна совместимая копия",
    "multiple_attachments": "Несколько вложений в одной записи", "no_video": "Нет видеопотока",
    "missing_metadata": "Не определены длительность или размеры", "decode_failed": "Ошибка декодирования",
    "probe_failed": "Не удалось прочитать потоки", "ffprobe_unavailable": "FFprobe не найден",
    "unknown_container": "Контейнер не распознан", "corrupt_container": "Повреждённый контейнер",
    "changed": "Файл изменился при анализе",
}


def _video_html(records):
    rows = []
    for record in records:
        if not (video := record.get("video_recovery")):
            continue
        original = video.get("original", {})
        output = video.get("output", {})
        state = original.get("status")
        dimensions = f"{original['width']} × {original['height']}" if original.get("width") else "—"
        rows.append((
            record["source_id"], original.get("container"), original.get("codec"),
            ", ".join(original.get("audio") or ()), original.get("duration"), dimensions,
            original.get("fps"), original.get("rotation"), original.get("streaming"),
            VIDEO_RESULTS.get(state, state), VIDEO_ACTIONS[video["action"]], output.get("codec"),
            video.get("original_sha256"), video.get("sha256"),
        ))
    return "<h2>Решения по видео</h2>" + _table(
        ("ID", "Контейнер", "Видео", "Аудио", "Секунды", "Размер", "Кадры/с", "Поворот",
         "Потоковое", "Проверка", "Решение", "Подготовленный кодек", "SHA-256 исходника", "SHA-256 копии"),
        rows,
    ) if rows else ""


def _attachment_recovery_html(records):
    actions = {
        **VIDEO_ACTIONS, "sticker": "Исходный стикер", "image": "Совместимое изображение PNG",
    }
    types = {
        **KIND_NAMES, "webp_sticker": "Стикер WebP", "tgs_sticker": "Стикер TGS",
        "webm_sticker": "Стикер WebM", "webp_image": "Изображение WebP",
    }
    rows = [
        (record["source_ordinal"], types.get(decision.get("source_kind")), actions.get(decision["action"]),
         decision.get("original_sha256"), decision.get("sha256"))
        for record in records if (decision := record.get("media_recovery"))
    ]
    return "<h2>Решения по вложениям</h2>" + _table(
        ("Запись", "Исходный тип", "Обработка", "SHA-256 оригинала", "SHA-256 копии"), rows,
    ) if rows else ""


def package_html(data, *, planned=False):
    stats = plan_summary(data)
    dates = "Исходное время" if stats["date_policy"] == "historical" else "Альтернативные даты"
    media = {
        "text_only": "Только текст",
        "document": "Медиа в виде документов",
        "text_media": "Полная история",
    }
    profile = "Контрольный тест" if data.get("fixture") else media.get(stats["media_mode"], "Архивный пакет")
    timeline = stats["timeline"]
    return _document(
        "План переноса" if planned else "Отчёт о пакете импорта",
        [
            f"<p>{escape(data.get('chat_title') or 'Диалог')} · {profile} · {dates} · {escape(data['timezone'])}</p>",
            _table(
                ("Состав", "Количество"),
                [
                    ("Записи в источнике", stats["source_records"]),
                    ("Записи в выборке", stats["selected"]),
                    ("Включённые сообщения", stats["included"]),
                    ("Пропущенные записи", stats["skipped"]),
                    ("Вложения", stats["attachments"]),
                    ("Записи с текстом к отправке", stats["text_records"]),
                    ("Пересланные записи", stats["forwards"]),
                    ("Ответы", stats["replies"]),
                    ("Альбомы", stats["albums"]),
                    ("Элементы альбомов", stats["album_elements"]),
                    ("Записей с историческими пометками", stats["annotated_records"]),
                    ("Отсутствующие или пустые вложения", stats["missing_assets"]),
                    ("Видео с неподтверждённой совместимостью", stats["video_incompatible"]),
                    ("Совместимых копий", stats["video_actions"].get("convert", 0)),
                    ("Оценка загрузки медиа при 10 Мбит/с, сек", stats["estimated_seconds_at_10_mbps"]),
                    ("Вложения, исключённые режимом пакета", stats["omitted_assets"]),
                    ("Байты вложений", stats["media_bytes"]),
                    ("Совпавшие временные метки", timeline.get("equal")),
                    ("Убывающее время в экспорте", timeline.get("regression")),
                    ("Последующие сдвиги", timeline.get("cascade")),
                    ("Максимальный сдвиг в секундах", timeline.get("max_shift_seconds")),
                ],
            ),
            "<h2>Сохранность данных</h2>",
            f"<p>{LOCAL}</p><p>{SERVER}</p>",
            (
                "<p>Оценка рассчитана по каталогу проекта. При создании пакета байты включённых вложений проверяются повторно.</p>"
                if planned
                else "<p>Контрольные суммы текста и включённых вложений сверены при подготовке пакета.</p>"
            ),
            "<h2>Вложения по типам</h2>",
            _table(
                ("Тип", "Количество"), [(name, stats["by_kind"].get(k, 0)) for k, name in KIND_NAMES.items()]
            ),
            "<h2>Подготовка медиа</h2>",
            _table(
                (
                    "Файл в пакете",
                    "Тип",
                    "MIME по содержимому",
                    "MIME для запроса",
                    "Конструктор",
                    "Серверная проверка",
                ),
                [
                    (
                        item["name"],
                        KIND_NAMES.get(item["kind"], item["kind"]),
                        item.get("detected_mime") or "—",
                        item["mime"],
                        item.get("input_media", item["upload_as"]),
                        "Ожидается",
                    )
                    for item in data["media"]
                ],
            ),
            _video_html(data["records"]),
            _attachment_recovery_html(data["records"]),
            "<h2>Ограничения и пропуски</h2>",
            _table(("Наблюдение", "Записи"), [(LOSS.get(k, k), v) for k, v in stats["losses"].items()]),
            "<h2>Записи с потерями</h2>",
            _table(
                ("Исходный ID", "Включено", "Наблюдения"),
                [
                    (
                        r["source_id"],
                        "Да" if r["included"] else "Нет",
                        ", ".join(LOSS.get(k, k) for k in r["losses"]),
                    )
                    for r in data["records"]
                    if r["losses"]
                ],
            ),
            f"<p>SHA-256 текста <code>{escape(data['text_sha256'])}</code></p>",
        ],
    )


def redact_verification(report):
    """Share verification outcomes without labels, identifiers or private date ranges."""
    return {
        **{
            k: value
            for k, value in report.items()
            if k not in {"package", "manifest_sha256", "views", "operation_id", "confirmed_source_ordinals"}
        },
        "privacy": "redacted",
        "views": [
            {
                **{
                    k: value
                    for k, value in view.items()
                    if k not in {"account_id", "peer_id", "client", "matched", "scope", "differences"}
                },
                "scope": {
                    k: value
                    for k, value in view.get("scope", {}).items()
                    if k in {"kind", "scan_limit", "scanned", "returned", "truncated", "media_hash_limit"}
                },
                "differences": [
                    {"ordinal": item["ordinal"], "fields": item["fields"]} for item in view["differences"]
                ],
            }
            for view in report["views"]
        ],
    }


def verification_html(report):
    mode = "Имитация" if report["mode"] == "mock" else "Снимки владельца"
    levels = report["levels"]
    verdict = VERIFICATION_NAMES.get(report.get("verification_status"), VERDICTS[report["state"]])
    statuses = {"verified": "Совпало", "differences": "Расхождения", "unknown": "Нет подтверждения"}
    parts = [
        f"<p>{mode} · {verdict}</p>",
        f"<p>{EVIDENCE_STATES[report['evidence_status']]}</p>",
        _table(
            ("Уровень", "Состояние"),
            (
                ("A · Локальная целостность", statuses[levels["A"]["status"]]),
                ("B1 · Историческое содержимое на сервере", statuses[levels["B1"]]),
                ("B2 · Исходные серверные свойства", "Возможность отсутствует в API"),
                ("C · Клиент после перезапуска", statuses[levels["C"]]),
                ("Наблюдаемая связь ответа", statuses[report["replies"]]),
                ("Записи, найденные с двух сторон", report["server_messages_observed"]),
                ("Записи, проверенные в двух клиентах", report["client_messages_verified"]),
                ("Вложения без подтверждённой привязки", report.get("unresolved_attachments", 0)),
            ),
        ),
        "<p>Импортная подпись хранит историческую метку. Принадлежность записи исходному Telegram-аккаунту определяется отдельно по серверным полям. History Import не назначает исходные ID, отправителя, направление, реакции, связи ответов и состояния прочтения.</p>",
        "<p>Полное совпадение SHA-256 подтверждает одинаковые байты. Расхождение возможно, например, после обработки медиа Telegram. Чтение заголовка подтверждает доступ к первым байтам.</p>",
        "<p>Личные идентификаторы, имена и точные границы окна скрыты.</p>"
        if report.get("privacy") == "redacted"
        else "",
        '<p data-code="VISUAL_RECONSTRUCTION_UNCONFIRMED">Отображение истории после перезапуска пока не подтверждено.</p>'
        if report["visual_status"] == "VISUAL_RECONSTRUCTION_UNCONFIRMED"
        else "",
        "<p>Историческая дата совпала в метаданных. Совпадение даты в клиентах не подтверждено.</p>"
        if report["historical_metadata_only"]
        else "",
    ]
    if levels["A"]["differences"]:
        parts.append(
            _table(
                ("Локальная проблема", "Позиция"),
                ((item["code"], item.get("ordinal")) for item in levels["A"]["differences"]),
            )
        )
    for i, view in enumerate(report["views"], 1):
        scope = view.get("scope", {})
        kind = scope.get("kind")
        description = {
            "id_window": "Диапазон серверных ID",
            "historical_date": "Ограниченный поиск по историческим датам",
            "legacy": "Снимок предыдущей версии",
        }.get(kind)
        bounds = ""
        if kind == "id_window" and "after_id" in scope:
            bounds = f" · после ID {scope['after_id']} · до ID {scope['through_id'] or 'конца'}"
        if kind == "historical_date" and "historical_from" in scope:
            bounds = f" · {scope['historical_from']} — {scope['historical_through']} UTC"
        readback = (
            f" · просмотрено {scope['scanned']} из предела {scope['scan_limit']}"
            if "scan_limit" in scope
            else ""
        )
        truncated = " · достигнут предел чтения" if scope.get("truncated") else ""
        parts += [
            f"<h2>Аккаунт {i}</h2><p>{view['observed']} из {view['expected']} записей</p>",
            f"<p>{escape(description + bounds + readback + truncated)}</p>" if description else "",
            _table(
                (
                    "Без совпадения в снимке",
                    "Лишние в снимке",
                    "Неоднозначные",
                    "Повторяющиеся серверные ID",
                    "После перезапуска",
                    "Клиент",
                ),
                [
                    (
                        view["missing"],
                        view["extra"],
                        view["ambiguous"],
                        view["duplicate_ids"],
                        "Да" if view["after_restart"] else "—",
                        view.get("client", "—"),
                    )
                ],
            ),
            _table(
                ("Поле", "Совпало", "Расхождение", "Нет данных"),
                [
                    (
                        FIELD_NAMES[key],
                        values.get("same", 0),
                        values.get("different", 0),
                        values.get("unknown", 0),
                    )
                    for key, values in view["metrics"].items()
                ],
            ),
        ]
        if view["differences"]:
            parts.append(
                _table(
                    ("Порядок", "Исходный ID", "Серверный ID", "Поля"),
                    [
                        (
                            r["ordinal"],
                            r.get("source_id"),
                            r.get("server_id"),
                            ", ".join(FIELD_NAMES[x] for x in r["fields"]),
                        )
                        for r in view["differences"]
                    ],
                )
            )
    return _document("Верификация результата импорта", parts)


def save_report(
    dest,
    *,
    project=None,
    package=None,
    verification=None,
    planned=False,
    progress=quiet,
    replace=False,
    protected=(),
    private_details=False,
):
    dest = local(Path(dest))
    if dest.suffix.lower() not in {".html", ".json"}:
        raise RecoveryError("Выберите формат отчёта HTML или JSON.")
    roots = list(protected)
    if project is not None:
        with catalogue(project) as db:
            roots.extend([Path(project), Path(properties(db)["original"])])
    if any(dest.is_relative_to(local(Path(root))) for root in roots):
        raise RecoveryError("Сохраните отчёт за пределами источника, проекта и пакета.")
    if dest.exists() and not replace:
        raise RecoveryError("Файл отчёта уже существует.")
    if verification is not None:
        report = verification if private_details else redact_verification(verification)
        html = verification_html(report) if dest.suffix.lower() == ".html" else ""
    elif package is not None:
        report = {
            "kind": "import_plan" if planned else "import_package",
            "version": __version__,
            "summary": plan_summary(package),
            "local": LOCAL,
            "telegram": SERVER,
            "source_sha256": package["source_sha256"],
            "text_sha256": package["text_sha256"],
            "records": package["records"],
            "media": package["media"],
        }
        html = package_html(package, planned=planned) if dest.suffix.lower() == ".html" else ""
    else:
        report = project_report(project, progress)
        html = project_html(report) if dest.suffix.lower() == ".html" else ""
    text = html or json.dumps(report, ensure_ascii=False, indent=2)
    progress("report", 0, 1)
    publish_text(dest, text, replace=replace)
    return dest
