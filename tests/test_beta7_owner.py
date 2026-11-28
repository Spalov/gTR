import pytest

from gtr.account import Account, Login, Target
from gtr.headless_owner import OwnerFlow
from gtr.ipc import ProtocolError, recovery
from gtr.journal import Endpoint, Journal, RecoveryRequired
from gtr.mock_api import MockAPI, offline_result
from gtr.model import quiet
from gtr.server import OwnerSession


class SyntheticAccount:
    account = None
    client = None
    timed = False

    async def connect(self, _id, _hash, _phone, _session):
        self.client = object()
        return Login(challenge="code")

    async def submit_code(self, _code):
        return Login(challenge="password")

    async def submit_password(self, _pass):
        self.account = Account(671, "Synthetic")
        return Login(account=self.account)

    async def dialogs(self):
        return [Target(674, "Synthetic dialog", "pm", "synthetic")]

    async def disconnect(self):
        self.client = self.account = None


def test_native_owner_consent_lease_and_duplicate_guard(project, tmp_path):
    manager = SyntheticAccount()
    transport = MockAPI()
    async def limit():
        return 2 * 1024**3

    transport.upload_limit = limit
    journal = Journal(tmp_path / "operation.sqlite", Endpoint.telegram(671, 674, "pm"))
    owner = OwnerSession(manager, api_factory=lambda *a, **k: transport, journal_factory=lambda *_: journal)
    flow = OwnerFlow(owner)

    def call(method, **params):
        return offline_result(flow.run(method, params, quiet))

    assert call("account.connect", api_id=10, api_hash="0" * 32, phone="+10000000000")["challenge"] == "code"
    assert call("account.code", value="123456")["challenge"] == "password"
    assert call("account.password", value="test-placeholder")["account"]["id"] == "671"
    assert call("account.dialogs", refresh=True)["dialogs"][0]["id"] == "674"
    params = {"project": str(project), "chat": 1, "preset": "", "media": "text_only"}
    plan = recovery(params, quiet)
    prepared = recovery({**params, "confirmed": True, "snapshot": plan["snapshot"]}, quiet, build=True)
    target = {"target": "674", "bundle": prepared["path"], "project": str(project), "manifest": prepared["manifest"]}
    with pytest.raises(ProtocolError):
        call("import.validate", **target, consented=False)
    assert not transport.calls
    review = call("import.validate", **target, consented=True)
    assert review["messages"] == prepared["summary"]["included"] == 3
    assert transport.calls == ["check_history", "check_peer"]
    with pytest.raises(ProtocolError):
        call("import.execute", lease="unconfirmed", confirmed=True)
    response = call("import.execute", lease=review["lease"], confirmed=True)
    assert response["state"] == "accepted" and transport.calls.count("start_import") == 1
    with pytest.raises(ProtocolError):
        call("import.execute", lease=review["lease"], confirmed=True)
    with pytest.raises(RecoveryRequired):
        call("import.validate", **target, consented=True)
    assert transport.calls.count("start_import") == 1
    report = journal.diagnostic_report(response["operation_id"], private=False)
    encoded = repr(report)
    assert "test-placeholder" not in encoded and "+10000000000" not in encoded
    assert str(project) not in encoded
