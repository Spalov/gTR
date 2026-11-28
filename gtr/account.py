"""User-authorized login, in-memory client lifetime, and eligible dialog selection."""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from io import BytesIO

from .faults import ImportFault
from .model import RecoveryError
from .mtproto import request, rpc_error


@dataclass(frozen=True)
class Account:
    id: int
    name: str
    premium: bool = False


@dataclass(frozen=True)
class Target:
    id: int
    title: str
    kind: str
    input_peer: object = field(repr=False)


@dataclass(frozen=True)
class AccountProfile:
    username: str = ""
    age: str = ""
    last_seen: str = ""
    avatar: bytes = field(default=b"", repr=False)


def registration_age(value, today):
    if not isinstance(value, str):
        return ""
    match = re.fullmatch(r"(0?[1-9]|1[0-2])\.([0-9]{4})", value)
    if not match:
        return ""
    month, year = map(int, match.groups())
    months = (today.year - year) * 12 + today.month - month
    if year < 2013 or months < 0:
        return ""
    years, remainder = divmod(months, 12)
    return f"≈ {years} г. {remainder} мес. · с {month:02d}.{year}"


def seen_text(status):
    from telethon import types

    if isinstance(status, types.UserStatusOffline) and isinstance(status.was_online, datetime):
        return status.was_online.astimezone().strftime("%d.%m.%Y, %H:%M")
    for constructor, text in (
        (types.UserStatusOnline, "В сети"),
        (types.UserStatusRecently, "Недавно"),
        (types.UserStatusLastWeek, "За последнюю неделю"),
        (types.UserStatusLastMonth, "За последний месяц"),
    ):
        if isinstance(status, constructor):
            return text
    return ""


class _AvatarBuffer(BytesIO):
    def write(self, data):
        if self.tell() + len(data) > 1024 * 1024:
            raise ValueError("Avatar exceeds the memory limit")
        return super().write(data)


@dataclass(frozen=True)
class Login:
    account: Account | None = None
    challenge: str = ""


def create_client(api_id, api_hash, session):
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError:
        raise RecoveryError('Установите зависимости владельца через pip install ".[owner]".') from None
    log = logging.getLogger("gtr.owner.transport")
    if not log.handlers:
        log.addHandler(logging.NullHandler())
    log.propagate = False
    return TelegramClient(
        StringSession(session),
        api_id,
        api_hash,
        request_retries=0,
        connection_retries=0,
        auto_reconnect=False,
        flood_sleep_threshold=0,
        receive_updates=False,
        raise_last_call_error=True,
        timeout=15,
        base_logger=log,
        lang_code="ru",
        system_lang_code="ru",
    )


class AccountManager:
    def __init__(self, client_factory=create_client, *, timed: bool = True):
        self.make_client, self.timed = client_factory, timed
        self.client = None
        self.account: Account | None = None
        self.phone, self.code_hash = "", ""
        self.challenge = ""

    async def rpc(self, awaitable, seconds=45):
        return await request(awaitable, seconds=seconds, timed=self.timed)

    async def connect(self, api_id: int, api_hash: str, phone: str = "", session: str = "") -> Login:
        if not 0 < api_id < 2**31 or not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
            raise RecoveryError("Проверьте API ID и API hash.")
        if phone and not re.fullmatch(r"\+[1-9]\d{5,14}", phone):
            raise RecoveryError("Введите номер в международном формате.")
        if not session and not phone:
            raise RecoveryError("Введите номер телефона для входа.")
        await self.disconnect()
        try:
            self.client = self.make_client(api_id, api_hash, session)
        except (ValueError, TypeError):
            raise RecoveryError("Не удалось открыть сессию Telegram.") from None
        try:
            await self.rpc(self.client.connect())
            if await self.rpc(self.client.is_user_authorized()):
                return await self._identity()
            if not phone:
                raise RecoveryError("Сохранённая сессия не авторизована. Войдите с номером телефона.")
            sent = await self.rpc(self.client.send_code_request(phone))
            self.phone, self.code_hash = phone, sent.phone_code_hash
            self.challenge = "code"
            return Login(challenge=self.challenge)
        except (Exception, asyncio.CancelledError):
            try:
                await self.disconnect()
            except (OSError, RecoveryError):
                pass
            raise

    async def submit_code(self, code: str) -> Login:
        from telethon.errors import RPCError, SessionPasswordNeededError

        if not self.client or self.challenge != "code" or not re.fullmatch(r"\d{3,10}", code):
            raise ImportFault("PHONE_CODE_INVALID")
        try:
            # SessionPasswordNeeded is handled before generic RPC classification.
            call = self.client.sign_in(self.phone, code, phone_code_hash=self.code_hash)
            if self.timed:
                await asyncio.wait_for(call, 45)
            else:
                await call
        except SessionPasswordNeededError:
            self.phone = self.code_hash = ""
            self.challenge = "password"
            return Login(challenge=self.challenge)
        except (RPCError, OSError) as error:
            raise rpc_error(error) from None
        return await self._identity()

    async def submit_password(self, password: str) -> Login:
        if not self.client or self.challenge != "password" or not password:
            raise ImportFault("PASSWORD_HASH_INVALID")
        await self.rpc(self.client.sign_in(password=password))
        return await self._identity()

    async def _identity(self) -> Login:
        from telethon import utils

        user = await self.rpc(self.client.get_me())
        if not user or user.bot:
            raise RecoveryError("Для импорта требуется личная пользовательская сессия Telegram.")
        self.account = Account(
            user.id, utils.get_display_name(user) or str(user.id), bool(getattr(user, "premium", False))
        )
        self.phone = self.code_hash = self.challenge = ""
        return Login(account=self.account)

    async def profile(self) -> AccountProfile:
        from telethon import functions, types

        self._assert_connected()
        user = await self.rpc(self.client.get_me(), seconds=12)
        if user is None or user.id != self.account.id:
            raise RecoveryError("Telegram вернул данные другого аккаунта.")
        details = None
        if callable(self.client):
            try:
                details = await self.rpc(
                    self.client(functions.users.GetFullUserRequest(types.InputUserSelf())), seconds=12
                )
            except ImportFault:
                pass
        settings = getattr(getattr(details, "full_user", None), "settings", None)
        month = getattr(settings, "registration_month", None)
        avatar = b""
        if isinstance(getattr(user, "photo", None), types.UserProfilePhoto):
            try:
                with _AvatarBuffer() as output:
                    await self.rpc(
                        self.client.download_profile_photo(user, file=output, download_big=False), seconds=12
                    )
                    avatar = output.getvalue()
            except (ImportFault, ValueError):
                pass
        return AccountProfile(
            getattr(user, "username", None) or "",
            registration_age(month, date.today()),
            seen_text(getattr(user, "status", None)),
            avatar,
        )

    async def forward_names(self, identities):
        """Resolve only explicit exported user IDs through authorized reads."""
        from telethon import functions, types, utils

        self._assert_connected()
        requested = {identity for identity in identities if type(identity) is int and 0 < identity < 2**63}
        if not requested or len(requested) > 40:
            return {}

        contact_list = await self.rpc(self.client(functions.contacts.GetContactsRequest(hash=0)), seconds=12)
        contacts = {contact.user_id for contact in getattr(contact_list, "contacts", [])}
        names = {
            user.id: (utils.get_display_name(user), "contacts")
            for user in getattr(contact_list, "users", [])
            if isinstance(user, types.User) and user.id in requested & contacts and not user.deleted
            and utils.get_display_name(user)
        }
        for identity in sorted(requested - names.keys()):
            try:
                user = await self.rpc(self.client.get_entity(types.PeerUser(identity)), seconds=6)
            except (ImportFault, ValueError):
                continue
            if (isinstance(user, types.User) and user.id == identity and not user.deleted
                    and (name := utils.get_display_name(user))):
                names[identity] = (name, "contacts" if identity in contacts else "public")
        return names

    def session(self) -> str:
        if not self.account or not self.client:
            raise RecoveryError("Сначала войдите в свой аккаунт.")
        return self.client.session.save()

    def _assert_connected(self):
        if self.account is None or self.client is None:
            raise RecoveryError("Сначала подключите свой Telegram-аккаунт.")

    async def _target(self, entity) -> Target | None:
        from telethon import types, utils

        if isinstance(entity, types.User) and not entity.bot and not entity.is_self and not entity.deleted:
            kind = "pm"
        elif isinstance(entity, types.Channel) and entity.megagroup:
            kind = "group"
        else:
            return None
        peer = await self.rpc(self.client.get_input_entity(entity))
        return Target(utils.get_peer_id(entity), utils.get_display_name(entity), kind, peer)

    async def dialogs(self) -> list[Target]:
        self._assert_connected()

        async def collect():
            targets = []
            async for dialog in self.client.iter_dialogs():
                if target := await self._target(dialog.entity):
                    targets.append(target)
            return targets

        return await self.rpc(collect(), seconds=120)

    async def resolve(self, peer_name: str) -> Target:
        self._assert_connected()
        if re.fullmatch(r"-?\d+", peer_name):
            match = next((target for target in await self.dialogs() if target.id == int(peer_name)), None)
        elif re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{2,31}", peer_name):
            match = await self._target(await self.rpc(self.client.get_entity(peer_name)))
        else:
            match = None
        if not match:
            raise ImportFault("PEER_ID_INVALID")
        return match

    async def test_target(self, user_id: int, title: str = "") -> Target:
        """Resolve the peer even after the cleared conversation leaves the dialog list."""
        from telethon import functions, types

        self._assert_connected()
        if type(user_id) is not int or user_id <= 0 or user_id == self.account.id:
            raise RecoveryError("Выберите личный диалог с другим тестовым аккаунтом.")
        try:
            peer = await self.rpc(self.client.get_input_entity(types.PeerUser(user_id)))
        except ValueError:
            contacts = await self.rpc(self.client(functions.contacts.GetContactsRequest(hash=0)))
            user = next((u for u in getattr(contacts, "users", ()) if u.id == user_id), None)
            target = await self._target(user) if user is not None else None
            if target is None or target.id != user_id or target.kind != "pm":
                raise ImportFault("SAVED_DIALOG_UNAVAILABLE", stage="resolve_target") from None
            return target
        if not isinstance(peer, types.InputPeerUser) or peer.user_id != user_id:
            raise RecoveryError("Telegram вернул другой диалог. Тестовый сброс остановлен.")
        return Target(user_id, title or f"Диалог {user_id}", "pm", peer)

    async def disconnect(self):
        client, self.client = self.client, None
        self.account = None
        self.phone = self.code_hash = self.challenge = ""
        if client:
            await self.rpc(client.disconnect(), seconds=15)
