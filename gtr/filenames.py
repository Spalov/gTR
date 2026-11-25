"""Separate storage, transcript placeholders and document display names."""

import unicodedata
from pathlib import PurePosixPath

from .files import BAD_NAMES, name_key


def _fit(stem, suffix):
    # Bound UTF-8 bytes as well as Windows UTF-16 code units.
    budget = 180 - len(suffix.encode())
    return stem.encode()[:budget].decode("utf-8", "ignore").rstrip(" .") + suffix


def safe_filename(raw):
    if not isinstance(raw, str):
        return ""
    leaf = unicodedata.normalize("NFC", raw.replace("\\", "/").rsplit("/", 1)[-1])
    leaf = "".join(
        "_" if unicodedata.category(c).startswith("C") or c in '<>:"|?*\u2028\u2029' else c for c in leaf
    ).strip(" .")
    if not leaf:
        return ""
    if BAD_NAMES.match(leaf):
        leaf = "_" + leaf
    suffix = PurePosixPath(leaf).suffix
    if len(suffix.encode()) > 24:
        suffix = ""
    return _fit(leaf.removesuffix(suffix) if suffix else leaf, suffix)


def display_name(item):
    if item.get("display_name"):
        return item["display_name"]
    if name := safe_filename(item.get("source")):
        return name
    return "Комплект вложений.zip" if "members" in item else "Сведения о вложении.json"


def unique_name(display, used):
    marker = display
    suffix = PurePosixPath(display).suffix
    if len(suffix.encode()) > 24:
        suffix = ""
    stem = display.removesuffix(suffix) if suffix else display
    index = used.get(name_key(display), 0) + 1
    while name_key(marker) in used:
        marker = _fit(stem, f" ({index}){suffix}")
        index += 1
    used[name_key(display)] = max(1, index - 1)
    used.setdefault(name_key(marker), 1)
    return marker


def assign_names(item, meta, used):
    labels = meta.get("file_names", {})
    if not isinstance(labels, dict):
        labels = {}
    original = safe_filename(labels.get(item["source"]) or meta.get("file_name")) if item["source"] else ""
    display = original or display_name(item)
    if item.get("transform"):
        display = safe_filename(PurePosixPath(display).stem + ".mp4")
    provenance = "export_metadata" if original else "export_basename" if item["source"] else "generated"
    item.update(
        storage_name=item["name"],
        name=unique_name(display, used),
        display_name=display,
        filename_source=provenance,
    )
    if "members" in item:
        entries = {"exported-details.json": 1}
        for member in item["members"]:
            name = labels.get(member["source"])
            if not name and member["kind"] != "photo":
                name = meta.get("file_name")
            member["stored_as"] = unique_name(safe_filename(name) or display_name(member), entries)
