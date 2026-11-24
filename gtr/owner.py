"""Interactive owner CLI. Telegram calls follow an explicit owner action."""

import asyncio
import json
import os
import re
import sys
import tempfile
from getpass import getpass
from pathlib import Path

from .account import AccountManager
from .conversion import LOSS, load_bundle
from .core import catalogue, properties
from .files import local
from .importer import ImportController, ImportFault
from .integrity import check_local
from .journal import Endpoint, Journal, State
from .model import Cancelled, RecoveryError
from .mtproto import TelegramAPI, rpc_error
from .observation import CaptureWindow, capture, last_id

__all__ = ["TelegramAPI", "owner_run", "rpc_error"]


def app_home():
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share")) / "gAssets" / "gTR"


def vault_path():
    return local(app_home() / "owner.dpapi")


def api_vault_path():
    return local(app_home() / "telegram-api.dpapi")


def _write_secret(path, payload, description):
    import win32crypt

    data = win32crypt.CryptProtectData(payload, description, None, None, None, 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
            temp_path = Path(tmp.name)
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path:
            temp_path.unlink(missing_ok=True)


def _read_secret(path, error_message):
    import pywintypes
    import win32crypt

    try:
        with path.open("rb") as source:
            encrypted = source.read(65537)
        if len(encrypted) > 65536:
            raise ValueError
        return win32crypt.CryptUnprotectData(encrypted, None, None, None, 1)[1]
    except (pywintypes.error, OSError, ValueError):
        raise RecoveryError(error_message) from None


def save_session(session):
    _write_secret(vault_path(), session.encode("ascii"), "gTR owner")


def read_session():
    message = "Не удалось прочитать сессию под текущей учётной записью Windows."
    try:
        return _read_secret(vault_path(), message).decode("ascii")
    except UnicodeError:
        raise RecoveryError(message) from None


def save_api_credentials(api_id, api_hash):
    if (
        type(api_id) is not int
        or not 0 < api_id < 2**31
        or not isinstance(api_hash, str)
        or not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash)
    ):
        raise RecoveryError("Проверьте API ID и API hash Telegram.")
    payload = json.dumps([api_id, api_hash], separators=(",", ":")).encode("ascii")
    _write_secret(api_vault_path(), payload, "gTR Telegram API")


def read_api_credentials():
    message = "Не удалось загрузить данные API под текущей учётной записью Windows."
    try:
        api_id, api_hash = json.loads(_read_secret(api_vault_path(), message))
        if type(api_id) is not int or not 0 < api_id < 2**31 or not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
            raise ValueError
        return api_id, api_hash
    except (ValueError, TypeError, RecursionError):
        raise RecoveryError(message) from None


def journal_for(account, target):
    return Journal(
        local(app_home() / "imports.sqlite"), Endpoint.telegram(account.id, target.id, target.kind)
    )


def owner_run(
    root,
    peer_name,
    *,
    observation=None,
    baseline=False,
    baseline_path=None,
    probe_media=False,
    project=None,
    capture_options=None,
):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RecoveryError("Режим владельца доступен в интерактивном окне.")
    bundle = load_bundle(root)
    if not bundle.data["included"]:
        raise RecoveryError("Пакет не содержит сообщений для переноса.")
    protected = ()
    if project:
        with catalogue(project) as db:
            protected = (project, Path(properties(db)["original"]))
        if not observation and not baseline and check_local(bundle, project)["status"] == "differences":
            raise RecoveryError("Пакет расходится с выбранным проектом. Проверьте исходники и отчёт.")
    window = (
        CaptureWindow(*observation, probe_media=probe_media, protected=protected, **(capture_options or {}))
        if observation
        else None
    )
    if window:
        window.validate(bundle)
    print(f"Цель {peer_name} · Сообщений {bundle.data['included']} · Вложений {len(bundle.data['media'])}")
    for code, count in bundle.data["losses"].items():
        print(f"  {LOSS.get(code, code)} — {count}")
    print(
        "Следующий шаг подключит ваш аккаунт к Telegram."
        if window or baseline
        else "Проверка формата отправит Telegram до 100 первых строк. Для загрузки пакета будет отдельное подтверждение."
    )
    if input("Введите LIVE для разрешения подключения: ").strip() != "LIVE":
        raise Cancelled()
    return asyncio.run(
        _owner_connected(
            bundle,
            peer_name,
            window=window,
            baseline=baseline,
            baseline_path=baseline_path,
            protected=protected,
            project=project,
        )
    )


async def _owner_connected(
    bundle, peer_name, *, window=None, baseline=False, baseline_path=None, protected=(), project=None
):
    manager = AccountManager()
    controller = None
    try:
        saved = (
            read_session()
            if vault_path().exists() and input("Использовать сессию Windows? [y/N] ").strip().lower() == "y"
            else ""
        )
        if api_vault_path().is_file() and input("Использовать сохранённые данные API? [Y/n] ").strip().lower() != "n":
            api_id, api_hash = read_api_credentials()
        else:
            try:
                api_id = int(getpass("API ID: "))
            except ValueError:
                raise RecoveryError("Некорректный API ID.") from None
            api_hash = getpass("API hash: ").strip()
        phone = "" if saved else getpass("Телефон с кодом страны: ").strip()
        auth = await manager.connect(api_id, api_hash, phone, saved)
        if auth.challenge == "code":
            auth = await manager.submit_code(getpass("Код Telegram: ").strip())
        if auth.challenge == "password":
            auth = await manager.submit_password(getpass("Пароль двухэтапной проверки: "))
        account = auth.account
        if (
            os.name == "nt"
            and input("Сохранить сессию с шифрованием Windows DPAPI? [y/N] ").strip().lower() == "y"
        ):
            save_session(manager.session())
        target = await manager.resolve(peer_name)
        print(f"\nАккаунт {account.name} (ID {account.id}). Цель {target.title} (ID {target.id}).")
        if input("Введите числовой ID выбранного чата: ").strip() != str(target.id):
            raise Cancelled()
        if window or baseline:
            if target.kind not in {"pm", "group"}:
                raise RecoveryError("Выберите поддерживаемый диалог для сверки.")
            expected = "BASELINE" if baseline else "OBSERVE"
            if input(f"Для чтения введите {expected}: ").strip() != expected:
                raise Cancelled()
            return (
                await last_id(manager, target, bundle=bundle, path=baseline_path, protected=protected)
                if baseline
                else await capture(manager, target, bundle, window)
            )

        if (
            input(
                "Если все затрагиваемые участники явно согласились на импорт этого пакета в выбранный чат, введите СОГЛАСИЕ: "
            ).strip()
            != "СОГЛАСИЕ"
        ):
            raise Cancelled()
        api = TelegramAPI(manager.client, target.kind, premium=account.premium, timed=True)
        journal = journal_for(account, target)
        last = ""
        titles = {
            "local_check": "Локальная проверка",
            "check_history": "Распознавание формата",
            "check_peer": "Проверка диалога",
            "check_limits": "Проверка лимитов файлов",
            "await_confirmation": "Ожидание владельца",
            "init_import": "Загрузка текста",
            "transcript_uploaded": "Текст истории принят. Импорт ещё не запущен.",
            "start_import": "Запуск обработки",
            "verifying": "Запуск принят. Ожидаются снимки результата.",
        }

        def progress(stage, done, total):
            nonlocal last
            if stage == "upload" and (done % 10 == 0 or done == total):
                print(f"Вложения {done}/{total}")
            if stage != last and stage in titles:
                print(titles[stage])
            last = stage

        controller = ImportController(
            api, target.input_peer, journal=journal, mode="owner", progress=progress, limits=api.upload_limit
        )
        from .incremental_import import discover_for_package, remember_profile

        controller.source_project = project
        discover_for_package(journal, bundle, progress)
        remember_profile(
            journal,
            account,
            target,
            project,
            protected[-1] if protected else None,
            bundle.data.get("media_mode", "text_media"),
            {"timezone": bundle.data["timezone"]},
        )
        checked = await controller.validate(bundle)
        print(
            f"\nАккаунт {account.name} · Цель {target.title} · Сообщений {checked.messages} · Медиа {checked.media}"
        )
        print("Пакет добавит записи в историю выбранного чата.")
        print(f"\nПодтверждение Telegram\n{checked.confirmation}")
        approved = input("Введите IMPORT, чтобы подтвердить загрузку и запуск: ").strip() == "IMPORT"
        result = await controller.execute(checked, approved=approved)
        return {**result, "operation_id": checked.operation_id, "phase": "VERIFYING"}
    finally:
        if controller:
            controller.cancel()
        had_client = manager.client is not None
        try:
            await manager.disconnect()
        except ImportFault:
            pass
        else:
            if had_client and controller and controller.state == State.STAGED_NOT_STARTED and controller.job:
                controller.journal.owner_disconnected(controller.job)
