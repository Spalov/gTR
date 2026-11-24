import io
import json
import sys
from datetime import UTC, date, datetime
from hashlib import sha256
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from telethon import functions, types

from gtr import BETA_NUMBER, updates
from gtr.account import Account, AccountManager, create_client, registration_age
from gtr.mock_api import offline_result
from gtr.model import RecoveryError
from gtr.owner import (
    api_vault_path,
    app_home,
    read_api_credentials,
    read_session,
    save_api_credentials,
    save_session,
)
from gtr.server import OwnerSession

NEXT = BETA_NUMBER + 1
TEST_HASH = "a" * 32


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI")
def test_saved_api_and_session_are_separate_and_encrypted():
    save_session("invented-session")
    save_api_credentials(1234, TEST_HASH)
    assert TEST_HASH.encode() not in api_vault_path().read_bytes()
    assert read_api_credentials() == (1234, TEST_HASH)
    assert read_session() == "invented-session"
    api_vault_path().write_bytes(b"not encrypted")
    with pytest.raises(RecoveryError, match="данные API"):
        read_api_credentials()
    assert read_session() == "invented-session"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI")
def test_reconnect_uses_the_saved_api_and_session():
    save_api_credentials(1234, TEST_HASH)
    save_session("invented-session")

    class Client:
        async def connect(self):
            pass

        async def is_user_authorized(self):
            return True

        async def get_me(self):
            return types.User(123, first_name="Тест")

    def client(api_id, api_hash, session):
        assert (api_id, api_hash, session) == (1234, TEST_HASH, "invented-session")
        return Client()

    session = OwnerSession(AccountManager(client, timed=False))
    login = offline_result(session.connect(0, "", "", True, saved_api=True))
    assert login.account.id == 123


@pytest.mark.parametrize(
    ("month", "expected"),
    [
        ("04.2024", "≈ 2 г. 7 мес. · с 04.2024"),
        ("11.2026", "≈ 0 г. 0 мес. · с 11.2026"),
        ("12.2026", ""),
        ("13.2018", ""),
        ("2020", ""),
        (None, ""),
    ],
)
def test_registration_age_uses_only_telegram_month(month, expected):
    assert registration_age(month, date(2026, 11, 24)) == expected


def test_account_transport_uses_in_memory_session_without_proxy(monkeypatch):
    from telethon.sessions import StringSession

    options = {}

    def telegram(session, api_id, api_hash, **kwargs):
        assert isinstance(session, StringSession) and not session.save()
        assert (api_id, api_hash) == (1234, TEST_HASH)
        options.update(kwargs)
        return "direct transport"

    monkeypatch.setattr("telethon.TelegramClient", telegram)
    assert create_client(1234, TEST_HASH, "") == "direct transport"
    assert options.get("proxy") is None and not options["auto_reconnect"]
    assert not app_home().exists()


def test_self_profile_with_avatar_and_last_seen(monkeypatch):
    monkeypatch.setattr("gtr.account.date", SimpleNamespace(today=lambda: date(2026, 11, 24)))
    stamp = datetime(2026, 11, 22, 12, 30, tzinfo=UTC)
    user = types.User(
        123,
        username="fictional",
        photo=types.UserProfilePhoto(photo_id=42, dc_id=2),
        status=types.UserStatusOffline(was_online=stamp),
    )

    class ProfileClient:
        async def get_me(self):
            return user

        async def __call__(self, query):
            assert isinstance(query, functions.users.GetFullUserRequest)
            assert isinstance(query.id, types.InputUserSelf)
            settings = types.PeerSettings(registration_month="04.2024")
            return SimpleNamespace(full_user=SimpleNamespace(settings=settings))

        async def download_profile_photo(self, who, *, file, download_big):
            assert who is user and not download_big
            file.write(b"sample-avatar")

    manager = AccountManager(timed=False)
    manager.client = ProfileClient()
    manager.account = Account(123, "Пример")
    result = offline_result(manager.profile())
    assert result.username == "fictional"
    assert result.last_seen == stamp.astimezone().strftime("%d.%m.%Y, %H:%M")
    assert result.age == "≈ 2 г. 7 мес. · с 04.2024"
    assert result.avatar == b"sample-avatar"
    assert not app_home().exists()

    user.photo = user.status = None
    user.username = None
    manager.client = SimpleNamespace(get_me=manager.client.get_me)
    result = offline_result(manager.profile())
    assert (result.username, result.age, result.last_seen, result.avatar) == ("", "", "", b"")


def update_file(folder, beta, *, extra=None):
    root = f"gTR-Beta-{beta}"
    filename = folder / f"{root}-Windows-x64.zip"
    with ZipFile(filename, "w") as package:
        for name in updates.REQUIRED:
            package.writestr(f"{root}/{name}", b"sample content")
        if extra:
            package.writestr(f"{root}/{extra}", b"test")
    return filename


def asset_data(update, *, digest=None):
    raw = update.path.read_bytes()
    return {
        "tag_name": f"beta-{update.number}",
        "draft": False,
        "assets": [
            {
                "name": update.path.name,
                "state": "uploaded",
                "digest": "sha256:" + (digest or sha256(raw).hexdigest()),
                "size": len(raw),
            }
        ],
    }


def mock_release(monkeypatch, data):
    class Response(io.BytesIO):
        def geturl(self):
            return f"https://api.github.com/repos/{updates.OFFICIAL_REPOSITORY}/releases/tags/{data['tag_name']}"

    def response(request, timeout):
        assert request.get_method() == "GET" and timeout > 0
        answer = Response(json.dumps(data).encode())
        assert request.full_url == answer.geturl()
        return answer

    monkeypatch.setattr(updates, "urlopen", response)


def test_official_update_installs_separately_and_uses_existing_data(tmp_path, monkeypatch):
    archive = update_file(tmp_path, NEXT)
    update_file(tmp_path, BETA_NUMBER)
    previous = app_home() / "versions" / f"gTR-Beta-{BETA_NUMBER}"
    previous.mkdir(parents=True)
    original = previous / "gTR.cmd"
    original.write_text("previous build")
    project = app_home() / "projects" / "test-project"
    project.mkdir(parents=True)
    config = project / "settings.txt"
    config.write_text("existing data")

    found = updates.find_download(tmp_path)
    assert found == updates.Update(archive, NEXT)
    mock_release(monkeypatch, asset_data(found))
    stages = set()
    installed = updates.install(found, lambda stage, *_: stages.add(stage))
    assert installed.parent == previous.parent
    assert installed.name == f"gTR-Beta-{NEXT}"
    assert updates.installed_update() == installed
    assert updates.installed_update(current=NEXT) is None
    assert (installed / "gTR.cmd").read_bytes() == b"sample content"
    assert original.read_text() == "previous build"
    assert config.read_text() == "existing data"
    assert stages == {"verify_update", "extract_update"}


def test_update_leaves_no_installation_when_digest_differs(tmp_path, monkeypatch):
    found = updates.candidate(update_file(tmp_path, NEXT))
    mock_release(monkeypatch, asset_data(found, digest="0" * 64))
    with pytest.raises(RecoveryError, match="Контрольная сумма"):
        updates.install(found)
    assert not list((app_home() / "versions").iterdir())


def test_update_rejects_duplicate_windows_paths(tmp_path, monkeypatch):
    found = updates.candidate(update_file(tmp_path, NEXT, extra="GTR.CMD"))
    mock_release(monkeypatch, asset_data(found))
    with pytest.raises(RecoveryError, match="недопустимый путь"):
        updates.install(found)
    assert updates.installed_update() is None
