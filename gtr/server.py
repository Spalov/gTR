"""Persistent owner session and one preflight/confirmation lease."""

import sqlite3
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from .account import Account, AccountManager, Target
from .conversion import load_bundle
from .faults import ImportFault
from .importer import ImportController, Preflight
from .integrity import check_local
from .journal import Journal, State
from .model import RecoveryError, quiet
from .mtproto import TelegramAPI
from .observation import CaptureWindow, capture, last_id
from .owner import (
    journal_for,
    read_api_credentials,
    read_session,
    save_api_credentials,
    save_session,
)
from .package_errors import PackageValidationError
from .test_mode import reset_test_registry


@dataclass(frozen=True)
class Review:
    account: Account
    target: Target
    check: Preflight
    local: dict
    test_reset_id: int | None = None


def _review_package(root, project, expected, progress):
    bundle = load_bundle(root, progress)
    actual = bundle.manifest_hash()
    if expected is not None and actual != expected:
        raise PackageValidationError("MANIFEST_MISMATCH", "load_manifest", "manifest_sha256",
                                     file="manifest.json", expected=expected, actual=actual)
    verified = check_local(bundle, project, progress)
    if verified["status"] == "differences":
        conflict = verified["differences"][0]
        ordinal = conflict.get("ordinal", 0)
        record = bundle.data["records"][ordinal - 1] if 1 <= ordinal <= len(bundle.data["records"]) else None
        raise PackageValidationError(
            "MANIFEST_MISMATCH", "local_history", conflict["code"], file="manifest.json", record=record,
            expected=conflict.get("expected", "совпадение с исходным проектом"),
            actual=conflict.get("actual", "найдено расхождение"),
            reason="Пакет отличается от открытого исходного проекта.",
        )
    return bundle, verified


class OwnerSession:
    def __init__(self, manager=None, api_factory=TelegramAPI, journal_factory=journal_for):
        self.manager = manager or AccountManager()
        self.api_factory, self.journal_factory = api_factory, journal_factory
        self.controller = None
        self.review: Review | None = None
        self.staged_jobs = []
        self._test_partner = self._test_destination = self._api_credentials = None

    async def connect(self, api_id, api_hash, phone, use_saved, *, saved_api=False):
        await self.disconnect()
        if saved_api:
            api_id, api_hash = read_api_credentials()
        login = await self.manager.connect(api_id, api_hash, phone, read_session() if use_saved else "")
        self._api_credentials = api_id, api_hash
        return login

    async def remember(self, *, session=True, api=False):
        if session:
            save_session(self.manager.session())
        if api and self.manager.account and self._api_credentials:
            save_api_credentials(*self._api_credentials)

    async def profile(self):
        return await self.manager.profile()

    async def validate(
        self,
        root: Path,
        target: Target,
        project=None,
        progress=quiet,
        *,
        expected=None,
        consented=False,
        test_reset_id=None,
    ) -> Review:
        account = self.manager.account
        if account is None:
            raise RecoveryError("Сначала подключите аккаунт Telegram.")
        if consented is not True:
            raise RecoveryError(
                "Перед сетевой проверкой подтвердите согласие участников для этого пакета и чата."
            )
        self.discard()
        try:
            bundle, local = _review_package(root, project, expected, progress)
        except PackageValidationError as error:
            fault = ImportFault.from_package(error)
            with suppress(OSError, RecoveryError, sqlite3.Error):
                journal = self.journal_factory(account, target)
                fault.operation_id = journal.record_invalid_package(fault, test_reset_id)
            raise fault from None
        from .incremental_import import discover_for_package, remember_profile

        journal = self.journal_factory(account, target)
        if test_reset_id is not None:
            from telethon.types import InputPeerUser

            if target.kind != "pm" or target.id == account.id:
                raise RecoveryError("Тестовый импорт доступен в личном диалоге двух аккаунтов.")
            if bundle.data.get("test_reset_id") != test_reset_id:
                raise ImportFault("TEST_PACKAGE_REQUIRED", stage="test_scope")
            journal = Journal(journal.path, journal.endpoint, test_reset_id=test_reset_id)
            if target.input_peer is None:
                target = await self.manager.test_target(target.id, target.title)
            if not isinstance(target.input_peer, InputPeerUser) or target.input_peer.user_id != target.id:
                raise RecoveryError("Тестовый пакет связан с другим диалогом.")
        api = self.api_factory(
            self.manager.client, target.kind, premium=account.premium, timed=self.manager.timed
        )
        discover_for_package(journal, bundle, progress)
        self.controller = ImportController(
            api,
            target.input_peer,
            journal=journal,
            mode="owner",
            progress=progress,
            limits=api.upload_limit,
        )
        self.controller.source_project = project
        remember_profile(
            self.controller.journal,
            account,
            target,
            project,
            None,
            bundle.data.get("media_mode", "text_media"),
            {"timezone": bundle.data["timezone"]},
        )
        checked = await self.controller.validate(bundle)
        self.review = Review(account, target, checked, local, test_reset_id)
        return self.review

    async def execute(self, active_review, progress=quiet):
        if not self.controller or self.review is None or self.review is not active_review:
            raise RecoveryError("Проверьте текущий пакет и чат перед запуском.")
        if self.manager.account != self.review.account:
            raise RecoveryError("Аккаунт изменился. Повторите проверку.")
        review = self.review
        self.review = None
        self.controller.progress = progress
        try:
            result = await self.controller.execute(review.check, approved=True)
        finally:
            if self.controller.state == State.STAGED_NOT_STARTED and self.controller.job:
                journal, job = self.controller.journal, self.controller.job
                self.staged_jobs.append((journal, job))
                with suppress(OSError, sqlite3.Error):
                    journal.owner_disconnected(job)
        return {**result, "operation_id": review.check.operation_id, "phase": "VERIFYING"}

    def discard(self):
        if self.controller:
            self.controller.cancel()
        self.controller, self.review = None, None

    async def begin_test_reset(self, account_id, target, phone, *, consented=False):
        if (
            consented is not True
            or self.manager.account is None
            or self.manager.account.id != account_id
            or self._api_credentials is None
            or target.kind != "pm"
            or target.id == account_id
        ):
            raise RecoveryError("Подключите первый тестовый аккаунт и подтвердите сброс для этого диалога.")
        self.discard()
        await self.cancel_test_reset()
        self._test_destination = target
        self._test_partner = AccountManager(self.manager.make_client, timed=self.manager.timed)
        return await self._test_login(self._test_partner.connect(*self._api_credentials, phone=phone))

    async def answer_test_login(self, text, *, password=False):
        partner = self._test_partner
        if partner is None:
            raise RecoveryError("Откройте вход во второй тестовый аккаунт.")
        login = partner.submit_password(text) if password else partner.submit_code(text.strip())
        return await self._test_login(login)

    async def _test_login(self, login_call):
        more_steps = False
        try:
            login = await login_call
            if login.challenge:
                more_steps = True
                return login
            target = self._test_destination
            journal = self.journal_factory(self.manager.account, target)
            return await reset_test_registry(
                journal, self.manager, self._test_partner, target.id, target.title
            )
        except ImportFault as error:
            more_steps = error.code in {"PHONE_CODE_INVALID", "PASSWORD_HASH_INVALID"}
            raise
        finally:
            if not more_steps:
                await self.cancel_test_reset()

    async def cancel_test_reset(self):
        partner, self._test_partner = self._test_partner, None
        self._test_destination = None
        if partner:
            try:
                await partner.disconnect()
            except (OSError, RecoveryError):
                pass

    async def disconnect(self):
        self.discard()
        self._api_credentials = None
        await self.cancel_test_reset()
        staged, self.staged_jobs = self.staged_jobs, []
        had_client = self.manager.client is not None
        await self.manager.disconnect()
        if had_client:
            for journal, job in staged:
                journal.owner_disconnected(job)

    async def baseline(self, target, *, root=None, path=None, protected=()):
        bundle = load_bundle(root) if root is not None else None
        return await last_id(self.manager, target, bundle=bundle, path=path, protected=protected)

    async def observe(self, root, target, window: CaptureWindow, progress=quiet):
        return await capture(self.manager, target, load_bundle(root, progress), window, progress)
