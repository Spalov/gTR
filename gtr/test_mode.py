"""Verify both views of a private dialog before opening a new local test cycle."""

from dataclasses import dataclass

from .account import Target
from .journal import Endpoint, State, _terminal_unknown, timestamp
from .model import RecoveryError


@dataclass(frozen=True)
class ResetResult:
    id: int
    account_id: int
    target: Target


def _reset_snapshot(journal, db):
    peer = journal.endpoint.key
    active = db.execute(
        """SELECT o.state,o.outcome FROM destination_locks l
           LEFT JOIN operations o ON o.id=l.operation AND o.peer=l.peer WHERE l.peer=?""",
        (peer,),
    ).fetchone()
    if active is not None and not _terminal_unknown(active):
        if active["state"] == State.STAGED_NOT_STARTED:
            raise RecoveryError(
                "Закройте прежнюю подготовку через «Восстановить прерванную подготовку» в диагностике."
            )
        raise RecoveryError("Дождитесь завершения импорта или подготовки в этом диалоге.")
    latest_event = db.execute(
        """SELECT max(e.seq) FROM operation_events e JOIN operations o ON o.id=e.operation
           WHERE o.peer=?""",
        (peer,),
    ).fetchone()[0]
    return journal.latest_test_reset(db), latest_event


async def _check_empty(manager, target):
    from telethon import functions, types

    peer = target.input_peer
    if target.kind != "pm" or not isinstance(peer, types.InputPeerUser) or peer.user_id != target.id:
        raise RecoveryError("Выберите личный диалог между двумя тестовыми аккаунтами.")
    history = await manager.rpc(
        manager.client(
            functions.messages.GetHistoryRequest(
                peer=peer, offset_id=0, offset_date=None, add_offset=0, limit=1, max_id=0, min_id=0, hash=0
            )
        )
    )
    if not isinstance(history, (types.messages.Messages, types.messages.MessagesSlice)):
        raise RecoveryError("Telegram не подтвердил пустую историю. Повторите проверку.")
    if history.messages or getattr(history, "count", 0):
        raise RecoveryError(f"В истории аккаунта {manager.account.id} ещё есть сообщения.")
    if history.messages != [] or getattr(history, "count", 0) != 0 or getattr(history, "inexact", False):
        raise RecoveryError("Telegram вернул неточный результат. Повторите проверку истории.")


async def reset_test_registry(journal, primary, partner, other_id, title="") -> ResetResult:
    first, second = primary.account, partner.account
    if (
        primary is partner
        or primary.client is None
        or partner.client is None
        or first is None
        or second is None
        or first.id == second.id
        or second.id != other_id
        or first.id != journal.endpoint.account_id
        or journal.endpoint.key != Endpoint.telegram(first.id, second.id, "pm").key
    ):
        raise RecoveryError("Войдите в два разных аккаунта выбранного личного диалога.")

    journal.classify_interrupted()
    with journal.connect() as db:
        before = _reset_snapshot(journal, db)
    target = await primary.test_target(second.id, title)
    reverse = await partner.test_target(first.id, first.name)
    if target.id != second.id or reverse.id != first.id:
        raise RecoveryError("Telegram вернул другой диалог. Тестовый сброс остановлен.")
    await _check_empty(primary, target)
    await _check_empty(partner, reverse)

    with journal.connect(lock=True) as db:
        if _reset_snapshot(journal, db) != before:
            raise RecoveryError("За время проверки изменилась история операций. Повторите проверку.")
        reset_id = db.execute(
            "INSERT INTO test_import_resets(peer,account,other_account,verified_at) VALUES (?,?,?,?)",
            (journal.endpoint.key, first.id, second.id, timestamp()),
        ).lastrowid
        db.execute(
            """INSERT INTO test_reset_exclusions(reset_id,operation)
               SELECT ?,id FROM operations WHERE peer=? AND outcome IN ('accepted','uncertain')""",
            (reset_id, journal.endpoint.key),
        )
    return ResetResult(reset_id, first.id, target)
