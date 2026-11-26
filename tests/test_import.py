import hashlib
import socket
from types import SimpleNamespace

import pytest
from conftest import message, write_json

from gtr.conversion import load_bundle, prepare
from gtr.core import analyze
from gtr.importer import ImportFault, Journal, run_import
from gtr.mock_api import MockAPI, offline_result
from gtr.model import Cancelled, RecoveryError
from gtr.owner import TelegramAPI, rpc_error


def run(bundle, api, *, journal=None, consent=True, confirm=lambda _: True, progress=lambda *_: None):
    return offline_result(run_import(bundle, api, "target", consent, confirm, progress, journal))


def test_profile_includes_dates_original_bytes_and_losses(bundle, synthetic):
    b = load_bundle(bundle.root)
    text = b.transcript.read_text(encoding="utf-8")
    assert text.startswith("[02/01/2025, 08:04:05] Лея: До встречи 👋\nв 12:00.\n")
    assert "в 12:00.\n[02/01/2025, 09:04:05] Мирон: Договорились!\n" in text
    assert b.data["date_policy"] == "historical" and "ordered_time" not in b.data["losses"]
    assert ">\nПодпись\n" in text
    assert b.data["included"] == 10 and b.data["losses"]["rich_text"] == 1 and b.data["losses"]["reply"] == 1
    for item in b.data["media"]:
        assert b.media_path(item).read_bytes() == (synthetic / item["source"]).read_bytes()
        assert text.count("<attached: " + item["wire_name"] + ">") == 1
        assert hashlib.sha256(b.media_path(item).read_bytes()).hexdigest() == item["sha256"]


def test_ambiguous_names_and_message_boundaries_are_not_rewritten(tmp_path):
    src = write_json(
        tmp_path / "s",
        [
            message(1, "one"),
            message(2, "two", from_id="user22"),
            message(3, "body\n[03/12/2024, 12:00:00] Test: text", **{"from": "Мирон", "from_id": "user11"}),
            message(4, "safe", **{"from": "Z", "from_id": "user12"}),
        ],
    )
    p = analyze(src, tmp_path / "p")
    b = prepare(p, 1, tmp_path / "b")
    assert b.data["included"] == 1 and b.data["losses"] == {"identity": 2, "ambiguous_text": 1}
    assert b.transcript.read_text(encoding="utf-8").endswith("Z: safe\n")


def test_head_limits_physical_lines(tmp_path):
    src = write_json(tmp_path / "src", [message(1, "\n".join(str(i) for i in range(130)))])
    b = prepare(analyze(src, tmp_path / "p"), 1, tmp_path / "b")
    assert len(b.head().splitlines()) == 100
    assert b.head().splitlines()[-1] == "99"


def test_sequence_and_default_offline(bundle):
    with pytest.raises(AssertionError, match="network socket"):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    api = MockAPI()
    result = run(bundle, api)
    assert result == {"messages": 10, "media": 8, "state": "accepted"}
    assert api.calls == ["check_history", "check_peer", "init_import", *(["upload"] * 8), "start_import"]


def test_owner_confirmation_boundaries(bundle):
    api = MockAPI()
    with pytest.raises(RecoveryError):
        run(bundle, api, consent=False)
    assert not api.calls
    prompted = []
    with pytest.raises(Cancelled):
        run(bundle, api, confirm=lambda text: prompted.append(text) or False)
    assert api.calls == ["check_history", "check_peer"] and len(prompted) == 1


@pytest.mark.parametrize(
    ("scenario", "code"),
    [
        ("format", "IMPORT_FORMAT_UNRECOGNIZED"),
        ("peer", "USER_NOT_MUTUAL_CONTACT"),
        ("date", "IMPORT_FORMAT_DATE_INVALID"),
        ("media", "MEDIA_INVALID"),
        ("flood", "FLOOD_WAIT"),
        ("disconnect", "STAGING_UNCERTAIN"),
    ],
)
def test_mock_rpc_errors(bundle, scenario, code):
    api = MockAPI(scenario)
    with pytest.raises(ImportFault) as exc:
        run(bundle, api)
    assert exc.value.code == code and not api.started
    if scenario == "flood":
        assert exc.value.seconds == 30


def test_cancel_between_uploads_never_starts(bundle):
    api = MockAPI()

    def interrupt(stage, done, total):
        if stage == "upload" and done == 2:
            raise Cancelled()

    with pytest.raises(Cancelled):
        run(bundle, api, progress=interrupt)
    assert len(api.uploaded) == 2 and not api.started


@pytest.mark.parametrize("lost", [False, True])
def test_restart_blocks_duplicate_or_uncertain_start(tmp_path, bundle, lost):
    path = tmp_path / "journal.sqlite"
    journal = Journal(path, "account/destination")
    if lost:
        with pytest.raises(ImportFault) as exc:
            run(bundle, MockAPI("lost_reply"), journal=journal)
        assert exc.value.code == "START_UNCERTAIN"
    else:
        run(bundle, MockAPI(), journal=journal)
    api = MockAPI()
    with pytest.raises(RecoveryError):
        run(load_bundle(bundle.root), api, journal=Journal(path, "account/destination"))
    assert not api.calls
    assert run(bundle, MockAPI(), journal=Journal(path, "account/other"))["state"] == "accepted"


def test_real_tl_adapter_with_local_fake_client(bundle):
    telethon = pytest.importorskip("telethon")
    types = telethon.types
    calls = []

    class FakeClient:
        async def upload_file(self, path, file_name):
            calls.append(("file", file_name))
            return types.InputFile(id=7, parts=1, name=file_name, md5_checksum="")

        async def __call__(self, request):
            calls.append(request)
            match type(request).__name__:
                case "CheckHistoryImportRequest":
                    return SimpleNamespace(pm=True, group=False)
                case "CheckHistoryImportPeerRequest":
                    return SimpleNamespace(confirm_text="Test prompt")
                case "InitHistoryImportRequest":
                    return SimpleNamespace(id=42)
                case "UploadImportedMediaRequest":
                    if isinstance(request.media, types.InputMediaUploadedPhoto):
                        return types.MessageMediaPhoto(photo=types.Photo(10, 0, b"", None, [], 0))
                    return types.MessageMediaDocument(
                        document=types.Document(11, 0, b"", None, request.media.mime_type, 1, 0, request.media.attributes)
                    )
                case "StartHistoryImportRequest":
                    return True
                case _:
                    pytest.fail("Unexpected request")

    prompts = []
    piped = TelegramAPI(FakeClient(), "pm")
    assert (
        offline_result(
            run_import(bundle, piped, types.InputPeerSelf(), True, lambda t: prompts.append(t) or True)
        )["state"]
        == "accepted"
    )
    assert prompts == ["Test prompt"]
    requests = [c for c in calls if not isinstance(c, tuple)]
    assert requests[2].media_count == 8 and requests[-1].import_id == 42
    assert isinstance(requests[3].media, types.InputMediaUploadedPhoto)
    assert isinstance(requests[4].media, types.InputMediaUploadedDocument)
    assert not requests[4].media.force_file
    assert requests[-2].media.force_file
    assert all(r.file_name == r.media.file.name == f["wire_name"] for r, f in zip(requests[3:-1], bundle.data["media"]))


def test_telethon_error_classification():
    errors = pytest.importorskip("telethon.errors")
    assert rpc_error(errors.FloodWaitError(None, 12)).seconds == 12
    assert rpc_error(errors.PreviousChatImportActiveWaitMinError(None, 3)).seconds == 180
    assert rpc_error(errors.ImportFormatUnrecognizedError(None)).code == "IMPORT_FORMAT_UNRECOGNIZED"
    assert rpc_error(errors.ServerError(None, "INTERNAL", 500)).code == "SERVER_FAILURE"


def test_user_scoped_dpapi_roundtrip(tmp_path, monkeypatch):
    pytest.importorskip("win32crypt")
    from gtr import owner

    monkeypatch.setattr(owner, "app_home", lambda: tmp_path)
    owner.save_session("a-synthetic-value")
    assert b"a-synthetic-value" not in owner.vault_path().read_bytes()
    assert owner.read_session() == "a-synthetic-value"


def test_owner_cancels_before_network_setup(bundle, monkeypatch):
    from gtr import owner

    monkeypatch.setattr(owner.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(owner.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "no")
    with pytest.raises(Cancelled):
        owner.owner_run(bundle.root, "@synthetic")


def test_owner_uses_reviewed_bytes_across_prompts(bundle):
    from gtr.conversion import owner_copy

    original = bundle.transcript.read_bytes()
    with owner_copy(bundle) as reviewed:
        bundle.transcript.write_bytes(b"modified")
        assert reviewed.transcript.read_bytes() == original
    assert not reviewed.root.exists()
    with pytest.raises(RecoveryError), owner_copy(bundle):
        pytest.fail("Changed data must not reach transport")
