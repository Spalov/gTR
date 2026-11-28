"""Structured, local validation errors shared by package writers and readers."""

from math import isfinite

from .model import RecoveryError

PACKAGE_ERRORS = {
    "SELECTION_STALE": (
        "Подтверждённая выборка архива изменилась или недоступна.",
        "Откройте исходный проект и подготовьте пакет из текущей выборки.",
    ),
    "PACKAGE_SCHEMA_INVALID": (
        "Поле пакета не соответствует схеме.",
        "Подготовьте пакет заново из открытого исходного проекта.",
    ),
    "UNSUPPORTED_PACKAGE_VERSION": (
        "Версия формата пакета не поддерживается.",
        "Откройте пакет в подходящей версии gTR или подготовьте его заново из проекта.",
    ),
    "ATTACHMENT_BINDING_INVALID": (
        "Имя файла или маркер в тексте не соответствует привязке вложения.",
        "Подготовьте новый пакет из исходного проекта.",
    ),
    "MEDIA_ATTRIBUTES_INVALID": (
        "Параметры вложения расходятся с форматом или манифестом.",
        "Проверьте исходный файл и заново выберите способ обработки вложения.",
    ),
    "MEDIA_HASH_MISMATCH": (
        "Содержимое вложения изменилось после подготовки.",
        "Откройте исходный проект и подготовьте новую копию пакета.",
    ),
    "FILE_NOT_FOUND": (
        "В пакете отсутствует обязательный файл.",
        "Откройте полную папку пакета или восстановите его из исходного проекта.",
    ),
    "MANIFEST_MISMATCH": (
        "Содержимое пакета расходится с сохранённым манифестом.",
        "Откройте пакет заново и повторите проверку.",
    ),
    "PACKAGE_STORAGE_INSUFFICIENT": (
        "Недостаточно свободного места для защищённой копии пакета.",
        "Освободите указанное место на диске и повторите проверку.",
    ),
    "PACKAGE_IO_ERROR": (
        "Файл или папка пакета недоступны.",
        "Проверьте доступ к локальным файлам и повторите проверку.",
    ),
}


def _value(data):
    if type(data) is float:
        return data if isfinite(data) else str(data)
    if data is None or type(data) in (bool, int):
        return data
    if isinstance(data, str):
        return "".join(c if c.isprintable() else " " for c in data[:256])
    return type(data).__name__


def public_package_issue(issue):
    result = {key: value for key, value in issue.items() if key not in {"file", "source_id"}}
    if issue.get("field") != "free_bytes":
        result.pop("actual", None)
        result.pop("expected", None)
    return result


class PackageValidationError(RecoveryError):
    def __init__(self, code, stage, field, *, file=None, record=None, expected=None, actual=None, reason=None):
        message, action = PACKAGE_ERRORS[code]
        self.code = code
        self.stage = stage
        record = record or {}
        self.issue = {
            "code": code, "component": "recovery_package", "severity": "error",
            "retry": "after_review", "stage": stage, "field": _value(field),
            "file": _value(str(file).replace("\\", "/").rsplit("/", 1)[-1]) if file else None,
            "source_id": _value(record.get("source_id")),
            "source_ordinal": _value(record.get("source_ordinal")),
            "expected": _value(expected), "actual": _value(actual),
            "reason": reason or message, "action": action,
        }
        super().__init__(self.issue["reason"])

    def details(self, *, private=False):
        return dict(self.issue) if private else public_package_issue(self.issue)
