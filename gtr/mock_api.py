"""A stateful in-process test double. It has no client, credentials or transport."""

import hashlib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .bindings import wire_name
from .importer import ImportFault, run_import
from .profiles import HEADERS, read_records
from .verification import compare

SCENARIOS = {
    "success": ("Без ошибок", "", ""),
    "format": ("Формат отклонён", "check_history", "IMPORT_FORMAT_UNRECOGNIZED"),
    "peer": ("Нет доступа к диалогу", "check_peer", "USER_NOT_MUTUAL_CONTACT"),
    "date": ("Неверная дата", "init_import", "IMPORT_FORMAT_DATE_INVALID"),
    "flood": ("Ограничение частоты", "init_import", "FLOOD_WAIT"),
    "media": ("Вложение отклонено", "upload", "MEDIA_INVALID"),
    "disconnect": ("Обрыв до запуска", "init_import", "CONNECTION_LOST"),
    "lost_reply": ("Потерян ответ на запуск", "start_import", "CONNECTION_LOST"),
    "one_sender": ("Серверный отправитель один", "", ""),
    "missing_on_second": ("Нет сообщения у второго участника", "", ""),
    "reordered": ("Нарушена последовательность", "", ""),
    "metadata_only": ("Дата осталась в метаданных", "", ""),
    "media_unavailable": ("Медиа нельзя открыть после импорта", "", ""),
    "active": ("Telegram сообщает об активном импорте", "init_import", "PREVIOUS_CHAT_IMPORT_ACTIVE_WAIT"),
    "timeout": ("Тайм-аут финального запроса", "start_import", "TIMEOUT"),
}


def offline_result(operation):
    """Complete immediate mock awaits without constructing a network-capable event loop.

    On Windows, even creating an asyncio loop can open a loopback socket pair.
    A mock that unexpectedly suspends is a test failure.
    """
    try:
        operation.send(None)
    except StopIteration as result:
        return result.value
    finally:
        operation.close()
    raise RuntimeError("Локальный адаптер запросил внешнее ожидание.")


class MockAPI:
    def __init__(self, scenario="success"):
        self.name = scenario
        self.scenario = SCENARIOS[scenario]
        self.calls = []
        self.peer = None
        self.expected, self.uploaded = set(), set()
        self.initialized = self.started = False

    def step(self, name):
        self.calls.append(name)
        if self.scenario[1] == name:
            raise ImportFault(
                self.scenario[2],
                30 if self.scenario[2] in {"FLOOD_WAIT", "PREVIOUS_CHAT_IMPORT_ACTIVE_WAIT"} else 0,
            )

    async def check_head(self, head):
        self.step("check_history")
        self.profile = next((key for key, header in HEADERS.items() if header.match(head)), None)
        if not self.profile or len(head.split("\n")[:-1]) > 100:
            raise ImportFault("IMPORT_FORMAT_UNRECOGNIZED")
        return "unknown"

    async def check_peer(self, peer, parsed_kind):
        self.step("check_peer")
        if not peer or "check_history" not in self.calls:
            raise ImportFault("PEER_ID_INVALID")
        self.peer = peer
        return "Подтвердите локальную имитацию импорта в тестовый диалог."

    async def initialize(self, peer, transcript: Path, media_count):
        self.step("init_import")
        if peer != self.peer or self.initialized:
            raise ImportFault("IMPORT_ID_INVALID")
        with transcript.open(encoding="utf-8", newline="\n") as stream:
            try:
                self.content = list(read_records(stream, self.profile))
                self.expected = {name for _, _, _, name in self.content if name}
            except ValueError:
                raise ImportFault("IMPORT_FILE_INVALID") from None
        if len(self.expected) != media_count:
            raise ImportFault("IMPORT_FILE_INVALID")
        self.initialized = True
        return 1

    def check_id(self, peer, import_id):
        if not self.initialized or self.started or import_id != 1 or peer != self.peer:
            raise ImportFault("IMPORT_ID_INVALID")

    async def upload_media(self, peer, import_id, item, path):
        self.step("upload")
        self.check_id(peer, import_id)
        name = wire_name(item)
        if name not in self.expected - self.uploaded or path.stat().st_size != item["size"]:
            raise ImportFault("MEDIA_INVALID")
        self.uploaded.add(name)

    async def start(self, peer, import_id):
        self.check_id(peer, import_id)
        if self.uploaded != self.expected:
            raise ImportFault("MEDIA_INVALID")
        self.started = True
        self.step("start_import")
        return True

    def observations(self, bundle):
        if not self.started:
            raise ImportFault("IMPORT_ID_INVALID")
        entries = [r for r in bundle.data["records"] if r["included"]]
        labels = list(dict.fromkeys(row["author"] for row in entries))
        mapping = {label: 100 + i % 2 for i, label in enumerate(labels)}
        media_hashes = {wire_name(item): item["sha256"] for item in bundle.data["media"]}
        zone = ZoneInfo("UTC" if bundle.data["timezone"] == "source" else bundle.data["timezone"])

        def capture(account):
            items = []
            for i, (row, (_, author, body, asset)) in enumerate(zip(entries, self.content), 1):
                original = mapping[author]
                sender = 100 if self.name == "one_sender" else original
                date = (
                    row.get("import_utc")
                    or datetime.fromisoformat(row["import_date"]).replace(tzinfo=zone).isoformat()
                )
                server = "2026-11-21T13:00:00+00:00"
                has = bool(asset and asset in self.uploaded)
                items.append(
                    {
                        "id": i,
                        "from_id": sender,
                        "out": sender == account,
                        "visual_out": sender == account,
                        "visual_name": author,
                        "visual_date": server if self.name == "metadata_only" else date,
                        "visual_jump": self.name != "metadata_only",
                        "visual_visible": True,
                        "visual_rank": i,
                        "visual_media": has and self.name != "media_unavailable",
                        "imported": True,
                        "imported_name": author,
                        "text_sha256": hashlib.sha256(body.encode()).hexdigest(),
                        "has_media": has,
                        "media_accessible": has and self.name != "media_unavailable",
                        "media_sha256": media_hashes.get(asset)
                        if has and self.name != "media_unavailable"
                        else None,
                        "media_probe_scope": "full" if has and self.name != "media_unavailable" else "none",
                        "original_date": date,
                        "server_date": server,
                    }
                )
            if account == 101 and self.name == "missing_on_second" and items:
                items.pop()
            if self.name == "reordered" and len(items) > 1:
                items[0], items[1] = items[1], items[0]
                for i, item in enumerate(items, 1):
                    item["id"] = i
            return {
                "schema": 2,
                "package": bundle.fingerprint(),
                "manifest_sha256": bundle.manifest_hash(),
                "account_id": account,
                "peer_id": 201 - account,
                "mode": "mock",
                "after_restart": True,
                "client": "Mock",
                "messages": items,
            }

        return capture(100), capture(101), {name: mapping[name] for name in labels[:2]}


def rehearse(bundle, scenario, progress, *, project=None):
    api = MockAPI(scenario)
    result = offline_result(run_import(bundle, api, "local", True, lambda _: True, progress))
    if bundle.data["schema"] >= 2:
        result["verification"] = compare(
            bundle, *api.observations(bundle), project=project, progress=progress
        )
        progress("complete", 1, 1)
    result["mode"] = "mock"
    return result
