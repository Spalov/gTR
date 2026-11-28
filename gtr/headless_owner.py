"""Owner workflow for the native window. Secrets and peer handles stay in the engine."""

import asyncio
import base64
import json
import re
from contextlib import closing, suppress
from sqlite3 import Row, connect
from threading import Event, Thread
from uuid import uuid4

from .account import Login, Target
from .conversion import load_bundle, prepare, prepare_eligible
from .faults import ImportFault
from .files import local
from .incremental_import import (
    VERIFY_PREVIOUS_OPERATIONS,
    discover_for_package,
    remember_profile,
    save_verification_preference,
)
from .journal import Endpoint, Journal, RecoveryRequired
from .model import Cancelled, RecoveryError
from .observation import CaptureWindow
from .owner import api_vault_path, app_home, vault_path
from .profiles import PROFILES
from .server import OwnerSession


class OwnerFlow:
    def __init__(self, owner=None):
        self.owner = owner or OwnerSession()
        self.targets = {}
        self.lease = ""
        self.test = None
        self.save_api = False
        self.save_session = False

    def target(self, key):
        if not isinstance(key, str) or key not in self.targets:
            raise ImportFault("PEER_ID_INVALID")
        return self.targets[key]

    def reset_scope(self, target):
        if self.test and self.test.target.id == target.id and self.test.target.kind == target.kind:
            return self.test.id
        return None

    def invalidate(self):
        self.lease = ""
        self.owner.discard()

    def saved_dialogs(self):
        account = self.owner.manager.account
        path = local(app_home() / "imports.sqlite")
        if account is None or not path.is_file():
            return
        with closing(connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = Row
            db.execute("PRAGMA trusted_schema=OFF")
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='import_profiles'").fetchone():
                return
            for row in db.execute("SELECT target_id,title FROM import_profiles WHERE account=? AND kind='pm' LIMIT 1500",
                                  (account.id,)):
                if type(row["target_id"]) is int and account.id != row["target_id"] > 0:
                    self.targets.setdefault(str(row["target_id"]),
                                            Target(row["target_id"], row["title"], "pm", None))

    async def login(self, result):
        if not result.account:
            return {"challenge": result.challenge}
        await self.owner.remember(session=self.save_session, api=self.save_api)
        return {"account": {"id": str(result.account.id), "name": result.account.name, "premium": result.account.premium}}

    async def run(self, method, p, progress):
        from .ipc import ProtocolError, number, request_path, string

        if method == "account.status":
            account = self.owner.manager.account
            return {"saved_api": api_vault_path().is_file(), "saved_session": vault_path().is_file(),
                    "account": {"id": str(account.id), "name": account.name} if account else None}
        if method == "account.connect":
            self.targets.clear()
            self.test = None
            self.lease = ""
            saved = p.get("saved_api") is True
            self.save_session, self.save_api = p.get("remember_session") is True, p.get("remember_api") is True
            a, h, phone = p.get("api_id", 0), p.get("api_hash", ""), p.get("phone", "")
            if not saved and (not number(a) or not isinstance(h, str) or not re.fullmatch(r"[a-fA-F0-9]{32}", h)):
                raise ProtocolError
            if phone != "" and not string(phone, 16):
                raise ProtocolError
            return await self.login(await self.owner.connect(a, h, phone, p.get("saved_session") is True, saved_api=saved))
        if method in ("account.code", "account.password", "test.code", "test.password"):
            value = p["value"]
            if not string(value, 256):
                raise ProtocolError
            password = method.endswith(".password")
            if method.startswith("test."):
                return self.test_login(await self.owner.answer_test_login(value, password=password))
            manager = self.owner.manager
            return await self.login(await (manager.submit_password(value) if password else manager.submit_code(value)))
        if method in ("account.disconnect", "account.forget"):
            self.targets.clear()
            self.test = None
            self.lease = ""
            await self.owner.disconnect()
            if method == "account.forget":
                if p.get("confirmed") is not True:
                    raise ProtocolError
                local(vault_path()).unlink(missing_ok=True)
                if p.get("api") is True:
                    local(api_vault_path()).unlink(missing_ok=True)
            return {}
        if method == "account.profile":
            profile = await self.owner.profile()
            return {"username": profile.username, "age": profile.age, "last_seen": profile.last_seen,
                    "avatar": base64.b64encode(profile.avatar).decode("ascii") if len(profile.avatar) <= 512 * 1024 else ""}
        if method == "account.dialogs":
            if p.get("refresh", False) is True:
                self.targets = {str(target.id): target for target in await self.owner.manager.dialogs()}
                self.saved_dialogs()
            offset = p.get("offset", 0)
            if not number(offset, minimum=0):
                raise ProtocolError
            query = p.get("query", "")
            if query != "" and not string(query, 120):
                raise ProtocolError
            found = [t for t in self.targets.values() if query.casefold() in t.title.casefold()]
            return {"dialogs": [{"id": str(t.id), "name": t.title[:300], "kind": t.kind}
                                for t in found[offset:offset + 150]], "total": len(found)}
        if method == "test.cancel":
            await self.owner.cancel_test_reset()
            return {}
        if method == "test.begin":
            account = self.owner.manager.account
            if account is None or p.get("confirmed") is not True or not string(p["phone"], 16):
                raise ProtocolError
            target = self.target(p["target"])
            self.invalidate()
            self.test = None
            return self.test_login(await self.owner.begin_test_reset(account.id, target, p["phone"], consented=True))
        if method == "import.discard":
            self.invalidate()
            return {}
        if method == "import.execute":
            if p.get("confirmed") is not True or not self.lease or p["lease"] != self.lease:
                raise ProtocolError
            self.lease = ""
            return await self.owner.execute(self.owner.review, progress)

        target = self.target(p["target"])
        account = self.owner.manager.account
        if account is None:
            raise RecoveryError("Сначала подключите аккаунт Telegram.")
        root = request_path(p["bundle"]) if "bundle" in p else None
        project = request_path(p["project"]) if "project" in p else None
        test = self.reset_scope(target) if p.get("test") is True else None
        if p.get("test") is True and test is None:
            raise ImportFault("TEST_RESET_REQUIRED", stage="test_scope")
        journal = self.owner.journal_factory(account, target)
        if target.input_peer is None and method in {"import.validate", "import.baseline", "import.observe"}:
            target = await self.owner.manager.test_target(target.id, target.title)
            self.targets[str(target.id)] = target
        if method == "import.validate":
            if p.get("consented") is not True:
                raise ProtocolError
            self.invalidate()
            expected = p.get("manifest")
            if expected is not None and (not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected)):
                raise ProtocolError
            review = await self.owner.validate(root, target, project, progress, expected=expected,
                                               consented=True, test_reset_id=test)
            self.lease = uuid4().hex
            return {"lease": self.lease, "account": review.account.name, "target": review.target.title,
                    "target_id": str(target.id), "messages": review.check.messages, "attachments": review.check.media,
                    "operation_id": review.check.operation_id, "confirmation": review.check.confirmation[:1800]}
        if method == "import.baseline":
            return await self.owner.baseline(target, root=root, path=request_path(p["path"]),
                                             protected=self.protected(project))
        if method == "import.observe":
            window = CaptureWindow(request_path(p["path"]), p["after"], p.get("through"), p.get("probe") is True,
                                   protected=self.protected(project), historical_from=p.get("first_date"),
                                   historical_through=p.get("last_date"),
                                   baseline=request_path(p["baseline"]) if p.get("baseline") else None,
                                   media_hash_limit=128 * 1024**2 if p.get("probe") is True else 0)
            result = await self.owner.observe(root, target, window, progress)
            return {"state": result["state"], "count": result["messages"]}
        if method == "profile.use":
            with journal.connect() as db:
                row = db.execute("SELECT * FROM import_profiles WHERE account=? AND peer=?",
                                 (account.id, journal.endpoint.key)).fetchone()
            if row is None:
                return {"verify_previous": True}
            return {"verify_previous": json.loads(row["settings"]).get(VERIFY_PREVIOUS_OPERATIONS, True),
                    "project": row["project"], "mode": row["mode"]}
        if method == "profile.preference":
            if type(p["verify_previous"]) is not bool:
                raise ProtocolError
            save_verification_preference(journal, {"account": account.id, "peer": journal.endpoint.key,
                "target_id": target.id, "kind": target.kind, "title": f"{account.name} ↔ {target.title}",
                "project": "", "archive": "", "mode": "text_media"}, p["verify_previous"])
            return {}
        if method == "import.recover":
            if p.get("confirmed") is not True or not string(p["operation_id"], 64):
                raise ProtocolError
            operation = journal.operation(p["operation_id"])
            if operation is None or operation["peer"] != journal.endpoint.key or operation["account"] != account.id:
                raise ImportFault("PEER_ID_INVALID")
            return {"state": journal.recover_before_start(p["operation_id"], acknowledged=True)}
        if method == "import.repair":
            if p.get("confirmed") is not True or not isinstance(p["decisions"], dict):
                raise ProtocolError
            self.invalidate()
            bundle = load_bundle(root, progress)
            dest = local(app_home() / "packages" / uuid4().hex)
            repaired = prepare_eligible(project, bundle, dest, bundle.record_intervals(), progress,
                                        video_decisions=p["decisions"], repair=True)
            return {"path": str(repaired.root), "manifest": repaired.manifest_hash(), "included": repaired.data["included"]}
        if method == "import.new":
            if p.get("confirmed") is not True or project is None:
                raise ProtocolError
            self.invalidate()
            bundle = load_bundle(root, progress)
            discover_for_package(journal, bundle, progress)
            summary = journal.review_bundle(bundle)
            if not summary.eligible:
                raise RecoveryRequired(summary)
            dest = local(app_home() / "packages" / uuid4().hex)
            new = prepare_eligible(project, bundle, dest, summary.eligible, progress)
            remember_profile(journal, account, target, project, None, bundle.data.get("media_mode", "text_media"), {})
            return {"path": str(new.root), "included": new.data["included"], "manifest": new.manifest_hash()}
        if method == "import.test_package":
            if p.get("confirmed") is not True or test is None:
                raise ProtocolError
            self.invalidate()
            bundle = load_bundle(root, progress)
            data, selection = bundle.data, bundle.data.get("selection", {})
            dest = local(app_home() / "packages" / uuid4().hex)
            new = prepare(project, data["chat_id"], dest, data["timezone"], progress,
                          record_ranges=bundle.record_intervals(),
                          profile=next(key for key, value in PROFILES.items() if value == data["profile"]),
                          media_mode=data.get("media_mode", "text_media"), test_reset_id=test,
                          video_policy=data.get("video_policy"), video_decisions=data.get("video_decisions"),
                          first_record=selection.get("first_record", 1), last_record=selection.get("last_record"),
                          annotate=data.get("annotate", False), annotation_mode=data.get("annotation_mode", "detailed"),
                          annotation_language=data.get("annotation_language", "ru"),
                          selection_snapshot=data.get("selection_snapshot"))
            return {"path": str(new.root), "included": new.data["included"], "manifest": new.manifest_hash()}
        raise ProtocolError

    def test_login(self, result):
        if isinstance(result, Login):
            return {"challenge": result.challenge}
        self.test = result
        self.targets[str(result.target.id)] = result.target
        return {"reset": result.id, "target": str(result.target.id)}

    @staticmethod
    def protected(project):
        if project is None:
            return ()
        from pathlib import Path

        from .core import catalogue, properties

        with catalogue(project) as db:
            original = properties(db).get("original")
        return (project, Path(original)) if original else (project,)


class OwnerLoop:
    """Keep the MTProto client's event loop alive between IPC requests."""

    def __init__(self, factory=OwnerFlow):
        self.flow = factory()
        self.loop = asyncio.new_event_loop()
        self.started = Event()
        self.worker = Thread(target=self._serve, name="gtr-telegram", daemon=True)
        self.worker.start()
        self.started.wait(5)

    def _serve(self):
        asyncio.set_event_loop(self.loop)
        self.started.set()
        self.loop.run_forever()
        self.loop.close()

    def run(self, method, params, progress, cancelled):
        from concurrent.futures import TimeoutError

        async def request():
            task = asyncio.create_task(self.flow.run(method, params, progress))
            try:
                while not task.done():
                    if cancelled.is_set():
                        task.cancel()
                        break
                    await asyncio.wait((task,), timeout=0.15)
                return await task
            except asyncio.CancelledError as error:
                raise Cancelled() from error

        future = asyncio.run_coroutine_threadsafe(request(), self.loop)
        try:
            return future.result(timeout=6 * 60 * 60)
        except TimeoutError:
            cancelled.set()
            raise ImportFault("TIMEOUT") from None

    def close(self):
        future = asyncio.run_coroutine_threadsafe(self.flow.owner.disconnect(), self.loop)
        with suppress(Exception):
            future.result(timeout=2)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.worker.join(1)


OWNER_ROUTES = {
    "account.status": (set(), set()),
    "account.connect": (set(), {"api_id", "api_hash", "phone", "saved_session", "saved_api", "remember_session", "remember_api"}),
    "account.code": ({"value"}, set()),
    "account.password": ({"value"}, set()),
    "account.profile": (set(), set()),
    "account.dialogs": (set(), {"refresh", "offset", "query"}),
    "account.disconnect": (set(), set()),
    "account.forget": ({"confirmed"}, {"api"}),
    "profile.use": ({"target"}, set()),
    "profile.preference": ({"target", "verify_previous"}, set()),
    "test.begin": ({"target", "phone", "confirmed"}, set()),
    "test.code": ({"value"}, set()),
    "test.password": ({"value"}, set()),
    "test.cancel": (set(), set()),
    "import.discard": (set(), set()),
    "import.validate": ({"target", "bundle", "consented"}, {"project", "test", "manifest"}),
    "import.execute": ({"lease", "confirmed"}, set()),
    "import.baseline": ({"target", "bundle", "path"}, {"project"}),
    "import.observe": ({"target", "bundle", "path", "after"}, {"project", "through", "probe", "first_date", "last_date", "baseline"}),
    "import.new": ({"target", "bundle", "project", "confirmed"}, set()),
    "import.test_package": ({"target", "bundle", "project", "confirmed", "test"}, set()),
    "import.recover": ({"target", "operation_id", "confirmed"}, set()),
    "import.repair": ({"target", "bundle", "project", "confirmed", "decisions"}, set()),
}


def operation_report(job, private):
    if not re.fullmatch(r"[a-f0-9]{32}", job):
        raise RecoveryError("Операция отсутствует в журнале.")
    database = local(app_home() / "imports.sqlite")
    if not database.is_file():
        raise RecoveryError("Операция отсутствует в журнале.")
    return Journal(database, Endpoint("local-review")).diagnostic_report(job, private=private)


def operation_open(job, progress):
    from .incremental_import import recover_package

    if not re.fullmatch(r"[a-f0-9]{32}", job):
        raise RecoveryError("Операция отсутствует в журнале.")
    path = local(app_home() / "imports.sqlite")
    if not path.is_file():
        raise RecoveryError("Операция отсутствует в журнале.")
    initial = Journal(path, Endpoint("local-review"))
    row = initial.operation(job)
    if not row:
        raise RecoveryError("Операция отсутствует в журнале.")
    journal = Journal(path, Endpoint(row["peer"], row["account"]))
    bundle = recover_package(journal, job, progress)
    return {"path": str(bundle.root)}
