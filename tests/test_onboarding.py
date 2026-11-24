import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient
from telethon import errors

from tgsearch_mcp import onboarding as ob
from tgsearch_mcp.config import config_path, load_config
from tgsearch_mcp.errors import TgSearchError

API_ID = 12345678
API_HASH = "0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("TGSEARCH_HOME", str(tmp_path))
    for name in ("TG_API_ID", "TG_API_HASH", "TG_SESSION_STRING"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeUser:
    id = 42
    first_name = "Test"
    last_name = "User"
    username = "test_user"


class FakeSession:
    def save(self) -> str:
        return "fake-session"


class FakeQR:
    def __init__(self, client):
        self.client = client
        self.version = 0
        self.url = "tg://login?token=0"
        self.expires = datetime.now(timezone.utc) + timedelta(seconds=30)

    async def recreate(self) -> None:
        self.version += 1
        self.url = f"tg://login?token={self.version}"
        self.expires = datetime.now(timezone.utc) + timedelta(seconds=30)

    async def wait(self, timeout=None):
        action = self.client.actions.pop(0) if self.client.actions else "hang"
        if action == "timeout":
            raise asyncio.TimeoutError
        if action == "password":
            raise errors.SessionPasswordNeededError(request=None)
        if action == "scan":
            return FakeUser()
        await asyncio.Event().wait()  # block until cancelled
        return None


class FakeClient:
    def __init__(self, actions=("scan",), password="secret"):
        self.actions = list(actions)
        self.password = password
        self.connected = False
        self.authorized = False
        self.disconnects = 0
        self.passwords = []
        self.qr = FakeQR(self)
        self.session = FakeSession()

    async def connect(self) -> None:
        self.connected = True

    async def qr_login(self):
        return self.qr

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def sign_in(self, password=None):
        self.passwords.append(password)
        if password != self.password:
            raise errors.PasswordHashInvalidError(request=None)
        return FakeUser()

    async def get_me(self):
        return FakeUser()

    async def disconnect(self) -> None:
        self.disconnects += 1
        self.connected = False


def make_controller(actions=("scan",), **kwargs):
    clients: list[FakeClient] = []
    saved: list[tuple[int, str, str]] = []

    def factory(api_id, api_hash):
        client = FakeClient(actions)
        clients.append(client)
        return client

    controller = ob.LoginController(
        client_factory=factory,
        save_session=lambda api_id, api_hash, session: saved.append((api_id, api_hash, session)),
        **kwargs,
    )
    return controller, clients, saved


async def wait_state(controller, *states, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        pending = controller.pending
        if pending is not None and pending.state in states:
            return pending
        await asyncio.sleep(0.01)
    current = controller.pending
    raise AssertionError(f"state never became {states}, got {current and current.state}")


async def wait_until(predicate, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not met")


# --------------------------------------------------------------------------- #
# State machine
# --------------------------------------------------------------------------- #


async def test_scan_saves_session_and_closes_client():
    controller, clients, saved = make_controller()
    await controller.start(API_ID, API_HASH)
    pending = await wait_state(controller, "done")

    assert saved == [(API_ID, API_HASH, "fake-session")]
    assert pending.account == {"id": 42, "name": "Test User", "username": "test_user"}
    assert clients[0].disconnects == 1


async def test_expired_qr_is_recreated_and_version_grows():
    controller, clients, saved = make_controller(actions=("timeout", "scan"))
    await controller.start(API_ID, API_HASH)
    pending = await wait_state(controller, "done")

    assert pending.version == 1
    assert clients[0].qr.url == "tg://login?token=1"
    assert len(saved) == 1


async def test_password_step_accepts_password_after_a_typo():
    controller, clients, saved = make_controller(actions=("password",))
    await controller.start(API_ID, API_HASH)
    pending = await wait_state(controller, "password")

    await controller.submit_password("wrong")
    await wait_until(lambda: pending.attempts == 1 and pending.code == "password_invalid")
    assert pending.state == "password"

    await controller.submit_password("secret")
    pending = await wait_state(controller, "done")
    assert clients[0].passwords == ["wrong", "secret"]
    assert saved == [(API_ID, API_HASH, "fake-session")]


async def test_three_wrong_passwords_stop_the_login():
    controller, clients, saved = make_controller(actions=("password",))
    await controller.start(API_ID, API_HASH)
    pending = await wait_state(controller, "password")

    for attempt in range(1, 4):
        await controller.submit_password("wrong")
        if attempt < 3:
            await wait_until(lambda: pending.attempts == attempt)

    pending = await wait_state(controller, "error")
    assert pending.code == "password_rejected"
    assert saved == []


async def test_login_expires_when_ttl_is_zero():
    controller, clients, saved = make_controller(ttl=0)
    await controller.start(API_ID, API_HASH)
    pending = await wait_state(controller, "expired")

    assert pending.code == "expired"
    assert saved == []


async def test_only_one_login_at_a_time():
    controller, clients, _ = make_controller(actions=())
    await controller.start(API_ID, API_HASH)
    with pytest.raises(TgSearchError) as excinfo:
        await controller.start(API_ID, API_HASH)
    assert excinfo.value.code == "login_in_progress"
    assert len(clients) == 1
    await controller.stop()


async def test_cancel_closes_the_client_and_keeps_no_session():
    controller, clients, saved = make_controller(actions=())
    await controller.start(API_ID, API_HASH)
    await controller.cancel()

    assert controller.pending.state == "cancelled"
    assert clients[0].disconnects >= 1
    assert saved == []


def test_telegram_rejection_is_reported_to_the_page():
    class BadClient(FakeClient):
        async def qr_login(self):
            raise errors.ApiIdInvalidError(request=None)

    def factory(api_id, api_hash):
        return BadClient()

    controller = ob.LoginController(client_factory=factory, save_session=lambda *args: None)
    with http_client(controller) as client:
        response = client.post(
            "/api/session",
            headers={"X-Setup-Key": "setup-key"},
            json={"api_id": API_ID, "api_hash": API_HASH},
        )
        assert response.status_code == 400
        assert response.json()["error"] == "api_id_invalid"
        assert load_config().api_configured is False


async def test_finished_login_survives_stop():
    controller, _, _ = make_controller()
    await controller.start(API_ID, API_HASH)
    pending = await wait_state(controller, "done")

    await controller.stop()
    assert pending.state == "done"


# --------------------------------------------------------------------------- #
# Credential parsing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("raw", ["12345678", " 12345678 ", '"12345678"', 12345678])
def test_normalize_api_id_accepts_common_typos(raw):
    assert ob.normalize_api_id(raw) == API_ID


@pytest.mark.parametrize("raw", ["", "abc", "12 34", 99])
def test_normalize_api_id_rejects_garbage(raw):
    with pytest.raises(TgSearchError) as excinfo:
        ob.normalize_api_id(raw)
    assert excinfo.value.code == "invalid_api_id"


@pytest.mark.parametrize(
    "raw", [API_HASH, f" {API_HASH}\n", f'"{API_HASH}"', API_HASH.upper()]
)
def test_normalize_api_hash_accepts_common_typos(raw):
    assert ob.normalize_api_hash(raw) == raw.strip().strip('"').strip()


@pytest.mark.parametrize("raw", ["", "abc", API_HASH[:-1], API_HASH + "0", "z" * 32])
def test_normalize_api_hash_rejects_garbage(raw):
    with pytest.raises(TgSearchError) as excinfo:
        ob.normalize_api_hash(raw)
    assert excinfo.value.code == "invalid_api_hash"


# --------------------------------------------------------------------------- #
# Snippets
# --------------------------------------------------------------------------- #


def test_snippets_are_valid_json_and_mention_the_binary():
    snippets = ob.client_snippets("/opt/bin/tgsearch-mcp")
    by_id = {item["id"]: item for item in snippets}

    opencode = json.loads(by_id["opencode"]["code"])
    assert opencode["mcp"]["servers"]["telegram"]["command"] == [
        "/opt/bin/tgsearch-mcp",
        "serve",
    ]

    generic = json.loads(by_id["claude-desktop"]["code"])
    assert generic["mcpServers"]["telegram"]["command"] == "/opt/bin/tgsearch-mcp"

    assert "claude mcp add" in by_id["claude-code"]["code"]

    codex = by_id["codex"]["code"]
    assert "[mcp_servers.telegram]" in codex
    assert 'command = \'/opt/bin/tgsearch-mcp\'' in codex
    assert 'args = ["serve"]' in codex


def test_qr_svg_is_responsive_and_well_formed():
    svg = ob.qr_svg("tg://login?token=abc")
    assert svg.startswith("<svg")
    assert 'width="100%"' in svg
    assert "viewBox" in svg


def test_page_script_only_touches_elements_that_exist():
    import re

    referenced = set(re.findall(r"\$\('([\w-]+)'\)", ob.PAGE))
    assert referenced
    missing = {name for name in referenced if f'id="{name}"' not in ob.PAGE}
    assert not missing, f"script references missing ids: {sorted(missing)}"


def translation_keys(page: str) -> tuple[set[str], set[str]]:
    import re

    english = page.split("  en: {", 1)[1].split("\n  ru: {", 1)[0]
    russian = page.split("\n  ru: {", 1)[1].split("\n};", 1)[0]
    pattern = re.compile(r"^    ([a-z0-9_]+):", re.MULTILINE)
    return set(pattern.findall(english)), set(pattern.findall(russian))


def test_both_languages_define_the_same_keys():
    english, russian = translation_keys(ob.PAGE)
    assert len(english) > 40, "the dictionary looks truncated"
    assert english == russian, (
        f"missing in en: {sorted(russian - english)}, missing in ru: {sorted(english - russian)}"
    )


def test_every_key_used_by_the_markup_is_defined():
    import re

    english, _ = translation_keys(ob.PAGE)
    used = set(re.findall(r'data-i18n(?:-html)?="([a-z0-9_]+)"', ob.PAGE))
    assert used
    assert used <= english, f"undefined keys: {sorted(used - english)}"


def test_language_defaults_to_the_browser_and_is_switchable():
    import re

    script = re.search(r"<script>(.*)</script>", ob.PAGE, re.S).group(1)
    assert "navigator.languages" in script
    assert "localStorage.setItem('tgsearch.lang'" in script
    assert 'data-lang="ru"' in ob.PAGE and 'data-lang="en"' in ob.PAGE
    assert script.rindex("lang = detectLang()") < script.rindex("boot();")


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #


def http_client(controller) -> TestClient:
    app = ob.create_app(key="setup-key", controller=controller, binary="/opt/bin/tgsearch-mcp")
    return TestClient(app)


def test_api_requires_the_setup_key():
    controller, _, _ = make_controller()
    with http_client(controller) as client:
        response = client.get("/api/config")
        assert response.status_code == 403
        assert response.json()["error"] == "forbidden"

        assert client.get("/api/config?k=wrong").status_code == 403
        ok = client.get("/api/config?k=setup-key")
        assert ok.status_code == 200
        assert ok.json()["has_api"] is False


def test_cross_origin_writes_are_refused():
    controller, _, _ = make_controller()
    with http_client(controller) as client:
        response = client.post(
            "/api/session",
            headers={"X-Setup-Key": "setup-key", "Origin": "https://evil.example"},
            json={"api_id": API_ID, "api_hash": API_HASH},
        )
        assert response.status_code == 403


def test_web_flow_writes_the_config_and_never_leaks_the_session():
    clients: list[FakeClient] = []

    def factory(api_id, api_hash):
        client = FakeClient(("scan",))
        clients.append(client)
        return client

    controller = ob.LoginController(client_factory=factory)  # default save_session
    with http_client(controller) as client:
        started = client.post(
            "/api/session",
            headers={"X-Setup-Key": "setup-key", "Origin": "http://127.0.0.1:8765"},
            json={"api_id": str(API_ID), "api_hash": API_HASH},
        )
        assert started.status_code == 200
        payload = started.json()
        assert payload["state"] == "pending"
        assert payload["svg"].startswith("<svg")
        assert "session" not in payload

        status = {}
        for _ in range(200):
            status = client.get("/api/session", headers={"X-Setup-Key": "setup-key"}).json()
            if status["state"] == "done":
                break
            time.sleep(0.01)

        assert status["state"] == "done"
        assert status["account"]["username"] == "test_user"
        assert status["snippets"]
        assert "session" not in status
        assert "fake-session" not in str(status)

    cfg = load_config()
    assert cfg.api_id == API_ID
    assert cfg.api_hash == API_HASH
    assert cfg.session == "fake-session"
    assert oct(config_path().stat().st_mode)[-3:] == "600"
    assert clients[0].disconnects == 1


def test_bad_credentials_keep_the_page_on_step_one():
    controller, _, _ = make_controller()
    with http_client(controller) as client:
        response = client.post(
            "/api/session",
            headers={"X-Setup-Key": "setup-key"},
            json={"api_id": "abc", "api_hash": "short"},
        )
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_api_id"

        response = client.post(
            "/api/session",
            headers={"X-Setup-Key": "setup-key"},
            json={"api_id": API_ID, "api_hash": "short"},
        )
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_api_hash"
        assert load_config().api_configured is False


def test_corrupt_stored_credentials_send_the_user_back_to_the_form():
    from tgsearch_mcp.config import Config, save_config

    save_config(Config(api_id="not-a-number", api_hash="short"))
    controller, _, _ = make_controller()
    with http_client(controller) as client:
        config_view = client.get("/api/config", headers={"X-Setup-Key": "setup-key"}).json()
        assert config_view["has_api"] is False

        response = client.post(
            "/api/session",
            headers={"X-Setup-Key": "setup-key"},
            json={"api_id": API_ID, "api_hash": API_HASH},
        )
        assert response.status_code == 200
        assert response.json()["state"] == "pending"

    cfg = load_config()
    assert cfg.api_id == API_ID
    assert cfg.api_hash == API_HASH


def test_password_endpoint_requires_a_password():
    controller, _, _ = make_controller(actions=("password",))
    with http_client(controller) as client:
        client.post(
            "/api/session",
            headers={"X-Setup-Key": "setup-key"},
            json={"api_id": API_ID, "api_hash": API_HASH},
        )
        response = client.post(
            "/api/session/password",
            headers={"X-Setup-Key": "setup-key"},
            json={"password": ""},
        )
        assert response.status_code == 400
        assert response.json()["error"] == "password_required"


def test_idle_status_is_reported_without_a_login():
    controller, _, _ = make_controller()
    with http_client(controller) as client:
        response = client.get("/api/session", headers={"X-Setup-Key": "setup-key"})
        assert response.json() == {"ok": True, "state": "idle"}


def test_server_binds_loopback_only():
    assert ob.BIND_HOST == "127.0.0.1"


def test_port_finder_skips_busy_ports():
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind((ob.BIND_HOST, 0))
        busy.listen(1)
        taken = busy.getsockname()[1]
        if taken + ob.PORT_SPAN > 65535:
            pytest.skip("ephemeral port is too close to the top of the range")
        assert ob._find_port(taken) != taken
