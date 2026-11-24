"""Local web onboarding: QR login in a browser plus a copy-paste client config.

Everything here runs on the user's own machine and binds to loopback only.
There is no token, no database and no third-party account: the page exists
because a QR code in a terminal is the hardest part of the setup for someone
who is not a developer.

The QR state machine is deliberately separated from HTTP so it can be tested
against a fake client without touching the network.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import secrets
import shlex
import shutil
import socket
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route
from telethon import TelegramClient, errors
from telethon.sessions import StringSession

from . import __version__
from .config import config_path, load_config, save_config
from .errors import TgSearchError, error_payload

BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PORT_SPAN = 10

#: How long a half-finished login is kept before it is dropped. Telegram QR
#: tokens themselves expire every 30 seconds; the loop refreshes them.
LOGIN_TTL_SECONDS = 600
PASSWORD_ATTEMPTS = 3

_ACTIVE_STATES = ("pending", "password")
_API_HASH_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_SVG_SIZE_RE = re.compile(r'width="[\d.]+mm"\s+height="[\d.]+mm"')


# --------------------------------------------------------------------------- #
# QR rendering
# --------------------------------------------------------------------------- #


def qr_svg(url: str) -> str:
    """Render a `tg://login?token=...` URL as a responsive inline SVG."""
    import qrcode
    import qrcode.image.svg

    qr = qrcode.QRCode(border=2, image_factory=qrcode.image.svg.SvgPathImage)
    qr.add_data(url)
    qr.make(fit=True)
    raw = qr.make_image().to_string(encoding="unicode")
    return _SVG_SIZE_RE.sub('width="100%" height="100%"', raw, count=1)


# --------------------------------------------------------------------------- #
# Credential parsing
# --------------------------------------------------------------------------- #


def normalize_api_id(value: Any) -> int:
    text = str(value if value is not None else "").strip().strip("\"'")
    if not text:
        raise TgSearchError(
            "invalid_api_id",
            "api_id is empty.",
            "Copy it from https://my.telegram.org - API development tools.",
        )
    try:
        api_id = int(text)
    except ValueError as exc:
        raise TgSearchError(
            "invalid_api_id",
            "api_id must be a number, without spaces or letters.",
            "Copy the App api_id from https://my.telegram.org - API development tools.",
        ) from exc
    if not 1000 <= api_id <= 2_147_483_647:
        raise TgSearchError(
            "invalid_api_id",
            "api_id looks wrong: it is usually 7-9 digits.",
            "Copy it again from https://my.telegram.org - API development tools.",
        )
    return api_id


def normalize_api_hash(value: Any) -> str:
    text = str(value if value is not None else "").strip().strip("\"'").strip()
    if not text:
        raise TgSearchError(
            "invalid_api_hash",
            "api_hash is empty.",
            "Copy it from https://my.telegram.org - API development tools.",
        )
    if not _API_HASH_RE.match(text):
        raise TgSearchError(
            "invalid_api_hash",
            f"api_hash must be exactly 32 hex characters, got {len(text)}.",
            "Copy the whole App api_hash - no spaces, no quotes.",
        )
    return text


# --------------------------------------------------------------------------- #
# Login state machine
# --------------------------------------------------------------------------- #


def _display_name(me: Any) -> str:
    name = " ".join(
        part
        for part in (getattr(me, "first_name", None), getattr(me, "last_name", None))
        if part
    )
    return name or getattr(me, "username", None) or str(getattr(me, "id", ""))


async def _disconnect(client: Any) -> None:
    try:
        await client.disconnect()
    except Exception:  # noqa: BLE001 - best effort on teardown
        pass


def _default_client(api_id: int, api_hash: str) -> TelegramClient:
    # Updates must stay on: `qr.wait()` completes on `UpdateLoginToken`.
    # The long-running search client disables them on purpose - this one is
    # short-lived and needs the update to see the scan.
    return TelegramClient(
        StringSession(),
        api_id,
        api_hash,
        device_model="tgsearch-mcp",
        system_version="Linux",
        app_version=__version__,
        receive_updates=True,
    )


def _save_to_config(api_id: int, api_hash: str, session: str) -> None:
    cfg = load_config()
    cfg.api_id = api_id
    cfg.api_hash = api_hash
    cfg.session = session
    save_config(cfg)


@dataclass
class PendingLogin:
    client: Any = field(repr=False)
    qr: Any = field(repr=False)
    api_id: int
    api_hash: str
    created: float
    version: int = 0
    state: str = "pending"
    account: dict[str, Any] | None = None
    code: str | None = None
    message: str | None = None
    hint: str | None = None
    attempts: int = 0
    session: str | None = field(default=None, repr=False)
    task: asyncio.Task | None = field(default=None, repr=False)
    passwords: asyncio.Queue = field(default_factory=asyncio.Queue, repr=False)


class LoginController:
    """One in-flight QR login per process, driven by the web page."""

    def __init__(
        self,
        *,
        client_factory: Callable[[int, str], Any] | None = None,
        save_session: Callable[[int, str, str], None] | None = None,
        ttl: float = LOGIN_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client_factory = client_factory or _default_client
        self._save_session = save_session or _save_to_config
        self._ttl = ttl
        self._clock = clock
        self._pending: PendingLogin | None = None
        self._lock = asyncio.Lock()

    @property
    def pending(self) -> PendingLogin | None:
        return self._pending

    async def start(self, api_id: int, api_hash: str) -> PendingLogin:
        async with self._lock:
            current = self._pending
            if current is not None and current.state in _ACTIVE_STATES:
                raise TgSearchError(
                    "login_in_progress",
                    "A QR login is already waiting for a scan.",
                    "Finish or cancel it in the browser, then try again.",
                )
            client = self._client_factory(api_id, api_hash)
            try:
                await asyncio.wait_for(client.connect(), 30)
                qr = await asyncio.wait_for(client.qr_login(), 30)
            except BaseException:
                # Includes TgSearchError raised by wait_for/timeout paths.
                await _disconnect(client)
                raise
            pending = PendingLogin(
                client=client,
                qr=qr,
                api_id=api_id,
                api_hash=api_hash,
                created=self._clock(),
            )
            self._pending = pending
            pending.task = asyncio.create_task(self._watch(pending), name="tgsearch-login")
            return pending

    async def submit_password(self, password: str) -> PendingLogin:
        pending = self._pending
        if pending is None or pending.state != "password":
            raise TgSearchError(
                "no_password_step",
                "This login is not waiting for a password.",
                "Start the QR login again.",
            )
        pending.passwords.put_nowait(password)
        return pending

    async def cancel(self) -> None:
        pending = self._pending
        if pending is None:
            return
        task = pending.task
        if task is not None and task.done():
            return
        pending.state = "cancelled"
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await _disconnect(pending.client)

    async def stop(self) -> None:
        await self.cancel()

    # -- internals --------------------------------------------------------- #

    def _expired(self, pending: PendingLogin) -> bool:
        return self._clock() - pending.created > self._ttl

    async def _watch(self, pending: PendingLogin) -> None:
        try:
            while True:
                if pending.state != "pending":
                    return
                # `wait()` blocks for the token's own lifetime and returns on
                # `UpdateLoginToken`. This extra check covers a scan that raced
                # with a token refresh.
                if await pending.client.is_user_authorized():
                    await self._finish(pending)
                    return
                if self._expired(pending):
                    self._fail(
                        pending,
                        "expired",
                        "The QR login timed out.",
                        "Start again: nothing was saved.",
                    )
                    return
                try:
                    await pending.qr.wait()
                except asyncio.TimeoutError:
                    if self._expired(pending):
                        self._fail(
                            pending,
                            "expired",
                            "The QR login timed out.",
                            "Start again: nothing was saved.",
                        )
                        return
                    await asyncio.sleep(0.2)
                    await pending.qr.recreate()
                    pending.version += 1
                    continue
                except errors.SessionPasswordNeededError:
                    await self._ask_password(pending)
                    return
                await self._finish(pending)
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - shown in the browser
            payload = error_payload(exc)
            self._fail(
                pending,
                str(payload.get("error") or "error"),
                payload.get("message"),
                payload.get("hint"),
            )
        finally:
            await _disconnect(pending.client)

    async def _ask_password(self, pending: PendingLogin) -> None:
        pending.state = "password"
        pending.code = None
        while pending.state == "password":
            remaining = self._ttl - (self._clock() - pending.created)
            if remaining <= 0:
                self._fail(
                    pending,
                    "expired",
                    "The QR login timed out.",
                    "Start again: nothing was saved.",
                )
                return
            try:
                password = await asyncio.wait_for(pending.passwords.get(), timeout=remaining)
            except asyncio.TimeoutError:
                self._fail(
                    pending,
                    "expired",
                    "The QR login timed out.",
                    "Start again: nothing was saved.",
                )
                return
            try:
                await asyncio.wait_for(pending.client.sign_in(password=password), 60)
            except errors.PasswordHashInvalidError:
                pending.attempts += 1
                if pending.attempts >= PASSWORD_ATTEMPTS:
                    self._fail(
                        pending,
                        "password_rejected",
                        "Too many wrong two-step verification passwords.",
                        "Start again; Telegram may limit password attempts for a while.",
                    )
                    return
                pending.code = "password_invalid"
                pending.message = "Wrong password."
                pending.hint = None
                continue
            except Exception as exc:  # noqa: BLE001 - shown in the browser
                payload = error_payload(exc)
                self._fail(
                    pending,
                    str(payload.get("error") or "error"),
                    payload.get("message"),
                    payload.get("hint"),
                )
                return
            pending.code = None
            pending.message = None
            pending.hint = None
            await self._finish(pending)
            return

    async def _finish(self, pending: PendingLogin) -> None:
        me = await pending.client.get_me()
        pending.session = pending.client.session.save()
        pending.account = {
            "id": getattr(me, "id", None),
            "name": _display_name(me),
            "username": getattr(me, "username", None),
        }
        # Saving before flipping to `done` makes a write failure an error, not
        # a false "you are logged in".
        self._save_session(pending.api_id, pending.api_hash, pending.session)
        pending.state = "done"
        pending.code = None
        pending.message = None
        pending.hint = None

    @staticmethod
    def _fail(pending: PendingLogin, code: str, message: str | None, hint: str | None) -> None:
        pending.state = "error" if code != "expired" else "expired"
        pending.code = code
        pending.message = message
        pending.hint = hint


# --------------------------------------------------------------------------- #
# Client config snippets
# --------------------------------------------------------------------------- #


def _stored_credentials(cfg: Any) -> tuple[int, str] | None:
    """Parsed pair from config.json, or None when it is missing or malformed."""
    if not cfg.api_configured:
        return None
    try:
        return normalize_api_id(cfg.api_id), normalize_api_hash(cfg.api_hash)
    except TgSearchError:
        return None


def client_snippets(binary: str | None = None) -> list[dict[str, str]]:
    binary = binary or shutil.which("tgsearch-mcp") or "tgsearch-mcp"
    command = shlex.join([binary, "serve"])
    opencode = json.dumps(
        {
            "$schema": "https://opencode.ai/config.json",
            "mcp": {
                "servers": {
                    "telegram": {"type": "local", "command": [binary, "serve"]},
                },
            },
        },
        indent=2,
    )
    generic = json.dumps(
        {"mcpServers": {"telegram": {"command": binary, "args": ["serve"]}}},
        indent=2,
    )
    codex = "\n".join(
        [
            f"# or: codex mcp add telegram -- {command}",
            "",
            "[mcp_servers.telegram]",
            # A TOML literal string keeps Windows backslashes intact.
            f"command = '{binary}'",
            'args = ["serve"]',
        ]
    )
    return [
        {
            "id": "opencode",
            "label": "OpenCode",
            "language": "json",
            "code": opencode,
        },
        {
            "id": "claude-code",
            "label": "Claude Code",
            "language": "sh",
            "code": f"claude mcp add telegram -- {command}",
        },
        {
            "id": "codex",
            "label": "Codex",
            "language": "toml",
            "code": codex,
        },
        {
            "id": "claude-desktop",
            "label": "Claude Desktop / VS Code",
            "language": "json",
            "code": generic,
        },
        {
            "id": "cursor",
            "label": "Cursor",
            "language": "json",
            "code": generic,
        },
    ]


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #


def _is_local_origin(origin: str) -> bool:
    match = re.match(r"^http://(127\.0\.0\.1|localhost|\[::1\])(?::\d+)?$", origin.strip())
    return bool(match)


class GuardMiddleware:
    """Require the one-time key and reject cross-origin writes.

    The server listens on loopback, but any page open in the same browser could
    otherwise POST to it. The key in the URL stops that.
    """

    def __init__(self, app, key: str) -> None:
        self.app = app
        self.key = key

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api"):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        token = request.query_params.get("k") or request.headers.get("x-setup-key", "")
        if not token or not secrets.compare_digest(token, self.key):
            await _deny(
                scope,
                receive,
                send,
                "Open the URL that tgsearch-mcp printed in the terminal.",
            )
            return
        origin = request.headers.get("origin")
        if request.method != "GET" and origin and not _is_local_origin(origin):
            await _deny(scope, receive, send, "Cross-origin request refused.")
            return
        await self.app(scope, receive, send)


async def _deny(scope, receive, send, message: str) -> None:
    response = JSONResponse(
        {"ok": False, "error": "forbidden", "message": message},
        status_code=403,
    )
    await response(scope, receive, send)


def _public(pending: PendingLogin, binary: str) -> dict[str, Any]:
    data: dict[str, Any] = {"ok": True, "state": pending.state, "version": pending.version}
    if pending.state == "pending":
        data["svg"] = qr_svg(pending.qr.url)
        expires = getattr(pending.qr, "expires", None)
        if expires is not None:
            left = int((expires - datetime.now(timezone.utc)).total_seconds())
            data["expires_in"] = max(0, left)
    if pending.state == "done":
        data["account"] = pending.account
        data["snippets"] = client_snippets(binary)
    if pending.state == "password":
        data["attempts_left"] = max(0, PASSWORD_ATTEMPTS - pending.attempts)
    if pending.code:
        data["error"] = pending.code
        data["message"] = pending.message
        data["hint"] = pending.hint
    return data


def create_app(
    *,
    key: str,
    controller: LoginController | None = None,
    binary: str | None = None,
) -> Starlette:
    controller = controller or LoginController()
    binary = binary or shutil.which("tgsearch-mcp") or "tgsearch-mcp"

    async def index(_request: Request) -> Response:
        return HTMLResponse(PAGE, headers={"Cache-Control": "no-store"})

    async def config_view(_request: Request) -> Response:
        cfg = load_config()
        stored = _stored_credentials(cfg)
        return JSONResponse(
            {
                "ok": True,
                "has_api": stored is not None,
                "has_session": cfg.session_configured,
                "api_id": stored[0] if stored else None,
                "config_path": str(config_path()),
            }
        )

    async def snippets(_request: Request) -> Response:
        cfg = load_config()
        return JSONResponse(
            {
                "ok": True,
                "has_session": cfg.session_configured,
                "snippets": client_snippets(binary),
            }
        )

    async def session_start(request: Request) -> Response:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - empty or malformed body is fine
            body = {}
        if not isinstance(body, dict):
            body = {}
        cfg = load_config()
        supplied = False
        stored = _stored_credentials(cfg)
        if stored is not None:
            api_id, api_hash = stored
        else:
            try:
                api_id = normalize_api_id(body.get("api_id"))
                api_hash = normalize_api_hash(body.get("api_hash"))
            except TgSearchError as exc:
                return JSONResponse(error_payload(exc), status_code=400)
            supplied = True
        try:
            pending = await controller.start(api_id, api_hash)
        except TgSearchError as exc:
            status = 409 if exc.code == "login_in_progress" else 400
            return JSONResponse(error_payload(exc), status_code=status)
        except Exception as exc:  # noqa: BLE001 - shown in the browser
            return JSONResponse(error_payload(exc), status_code=400)
        if supplied:
            # Reaching this point means Telegram accepted the pair.
            cfg.api_id = api_id
            cfg.api_hash = api_hash
            save_config(cfg)
        return JSONResponse(_public(pending, binary))

    async def session_status(_request: Request) -> Response:
        pending = controller.pending
        if pending is None:
            return JSONResponse({"ok": True, "state": "idle"})
        return JSONResponse(_public(pending, binary))

    async def session_password(request: Request) -> Response:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        password = str((body or {}).get("password") or "")
        if not password:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "password_required",
                    "message": "Two-step verification password is required.",
                },
                status_code=400,
            )
        try:
            pending = await controller.submit_password(password)
        except TgSearchError as exc:
            return JSONResponse(error_payload(exc), status_code=400)
        return JSONResponse(_public(pending, binary))

    async def session_cancel(_request: Request) -> Response:
        await controller.cancel()
        return JSONResponse({"ok": True, "state": "cancelled"})

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            await controller.stop()

    routes = [
        Route("/", index),
        Route("/api/config", config_view),
        Route("/api/snippets", snippets),
        Route("/api/session", session_start, methods=["POST"]),
        Route("/api/session", session_status, methods=["GET"]),
        Route("/api/session", session_cancel, methods=["DELETE"]),
        Route("/api/session/password", session_password, methods=["POST"]),
    ]
    return Starlette(
        routes=routes,
        middleware=[Middleware(GuardMiddleware, key=key)],
        lifespan=lifespan,
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def _find_port(preferred: int, span: int = PORT_SPAN) -> int:
    for port in range(preferred, preferred + span):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((BIND_HOST, port))
            except OSError:
                continue
            return port
    raise TgSearchError(
        "port_in_use",
        f"Ports {preferred}-{preferred + span - 1} are busy.",
        "Pass another one: tgsearch-mcp setup --port 8790",
    )


def run_setup(*, port: int = DEFAULT_PORT, open_browser: bool = True) -> int:
    key = secrets.token_urlsafe(24)
    app = create_app(key=key)
    chosen = _find_port(port)
    url = f"http://{BIND_HOST}:{chosen}/?k={key}"

    print("tgsearch-mcp setup", file=sys.stderr)
    print(f"  open: {url}", file=sys.stderr)
    if _looks_like_ssh():
        print(
            "  over SSH: ssh -L {port}:127.0.0.1:{port} <host>, then open the same URL".format(
                port=chosen
            ),
            file=sys.stderr,
        )
    print("  Ctrl+C stops the server; nothing is written until login finishes.", file=sys.stderr)

    if open_browser:
        threading.Timer(0.7, _open_browser, args=(url,)).start()

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=BIND_HOST,
            port=chosen,
            log_level="warning",
            access_log=False,
            lifespan="on",
        )
    )
    try:
        server.run()
    except KeyboardInterrupt:
        return 130
    return 0


def _open_browser(url: str) -> None:
    with contextlib.suppress(Exception):
        webbrowser.open(url)


def _looks_like_ssh() -> bool:
    import os

    return bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"))


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #

PAGE = """<!doctype html>
<html lang="en" class="boot">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>tgsearch-mcp</title>
<style>
  :root {
    /* Catppuccin Mocha */
    color-scheme: dark;
    --bg: #1e1e2e;       /* base */
    --card: #181825;     /* mantle */
    --fg: #cdd6f4;       /* text */
    --muted: #a6adc8;    /* subtext0 */
    --line: #313244;     /* surface0 */
    --accent: #b4befe;   /* lavender */
    --accent-fg: #1e1e2e;
    --link: #89b4fa;     /* blue */
    --ok: #a6e3a1;       /* green */
    --bad: #f38ba8;      /* red */
    --code-bg: #11111b;  /* crust */
    --shadow: 0 1px 2px rgba(17, 17, 27, .55);
  }
  ::selection { background: #585b70; color: #cdd6f4; }
  * { box-sizing: border-box; }
  html.boot body { visibility: hidden; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--fg);
    font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  }
  main { max-width: 680px; margin: 0 auto; padding: 32px 20px 56px; }
  header { display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; }
  header .titles { min-width: 0; }
  header h1 { font-size: 22px; margin: 0 0 4px; letter-spacing: -0.01em; }
  header p { margin: 0; color: var(--muted); }
  .lang {
    display: flex; flex: none; padding: 2px; gap: 2px;
    border: 1px solid var(--line); border-radius: 999px; background: var(--card);
  }
  .lang button {
    padding: 4px 10px; font-size: 12px; font-weight: 700; letter-spacing: .05em;
    background: transparent; border: 0; border-radius: 999px; color: var(--muted);
  }
  .lang button[aria-pressed="true"] { background: var(--accent); color: var(--accent-fg); }
  .steps {
    display: flex; gap: 8px; list-style: none; padding: 0; margin: 24px 0 20px;
    font-size: 13px; color: var(--muted);
  }
  .steps li { flex: 1; padding-top: 8px; border-top: 3px solid var(--line); }
  .steps li[data-active="true"] { border-color: var(--accent); color: var(--fg); font-weight: 600; }
  .steps li[data-done="true"] { border-color: var(--ok); color: var(--ok); }
  .card {
    background: var(--card); border: 1px solid var(--line); border-radius: 14px;
    padding: 22px; box-shadow: var(--shadow);
  }
  .card + .card { margin-top: 16px; }
  h2 { font-size: 17px; margin: 0 0 6px; }
  p.lead { margin: 0 0 16px; color: var(--muted); }
  label { display: block; font-size: 13px; font-weight: 600; margin: 14px 0 6px; }
  input[type=text], input[type=password] {
    width: 100%; padding: 10px 12px; font: inherit; color: var(--fg);
    background: var(--bg); border: 1px solid var(--line); border-radius: 9px;
  }
  input:focus { outline: 2px solid var(--accent); outline-offset: 1px; }
  .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-top: 18px; }
  button {
    font: inherit; font-weight: 600; padding: 10px 16px; border-radius: 9px;
    border: 1px solid var(--line); background: var(--card); color: var(--fg); cursor: pointer;
  }
  button.primary { background: var(--accent); border-color: var(--accent); color: var(--accent-fg); }
  button.ghost { background: transparent; color: var(--muted); }
  button:disabled { opacity: .55; cursor: default; }
  a { color: var(--link); }
  ol.how { margin: 0 0 4px; padding-left: 20px; }
  ol.how li { margin: 6px 0; }
  ol.how code, code.inline {
    background: var(--code-bg); padding: 1px 5px; border-radius: 5px;
    font: 13px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  details { margin-top: 14px; font-size: 14px; }
  details summary { cursor: pointer; color: var(--muted); }
  details ul { margin: 10px 0 0; padding-left: 20px; color: var(--muted); }
  details li { margin: 6px 0; }
  .qr {
    display: grid; place-items: center; margin: 18px auto 12px; padding: 16px;
    width: 300px; height: 300px; background: #fff; border-radius: 12px; border: 1px solid var(--line);
  }
  .qr svg { width: 100%; height: 100%; display: block; }
  .qr-hint { text-align: center; color: var(--muted); font-size: 14px; }
  .timer { text-align: center; font-size: 13px; color: var(--muted); margin-top: 10px; }
  .spinner {
    width: 22px; height: 22px; border: 2px solid var(--line); border-top-color: var(--accent);
    border-radius: 50%; animation: spin .8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .banner { border-radius: 10px; padding: 12px 14px; font-size: 14px; margin-bottom: 16px; }
  .banner.bad { background: color-mix(in srgb, var(--bad) 12%, transparent); color: var(--bad); }
  .banner .detail { color: var(--muted); font-size: 13px; margin-top: 6px; word-break: break-word; }
  .account { display: flex; align-items: center; gap: 10px; margin: 4px 0 18px; }
  .account .name { font-weight: 600; }
  .account .user { color: var(--muted); font-size: 14px; }
  .tabs { display: flex; gap: 6px; flex-wrap: wrap; margin: 18px 0 10px; }
  .tabs button {
    padding: 6px 12px; font-size: 13px; font-weight: 600; border-radius: 999px;
    background: transparent; color: var(--muted);
  }
  .tabs button[aria-selected="true"] { background: var(--code-bg); color: var(--fg); border-color: var(--line); }
  pre {
    margin: 0; padding: 14px; overflow-x: auto; background: var(--code-bg);
    border: 1px solid var(--line); border-radius: 10px;
    font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  .snippet-hint { color: var(--muted); font-size: 13px; margin: 8px 0 0; }
  footer { margin-top: 26px; color: var(--muted); font-size: 13px; }
  footer p { margin: 6px 0; }
  [hidden] { display: none !important; }
</style>
</head>
<body>
<main>
  <header>
    <div class="titles">
      <h1>tgsearch-mcp</h1>
      <p class="sub" data-i18n="subtitle"></p>
    </div>
    <div class="lang" role="group" aria-label="Language">
      <button type="button" data-lang="ru" aria-pressed="false">RU</button>
      <button type="button" data-lang="en" aria-pressed="false">EN</button>
    </div>
  </header>

  <ol class="steps" id="steps">
    <li data-step="1" data-i18n="step1"></li>
    <li data-step="2" data-i18n="step2"></li>
    <li data-step="3" data-i18n="step3"></li>
  </ol>

  <section class="card" id="creds" hidden>
    <h2 data-i18n="creds_title"></h2>
    <p class="lead" data-i18n="creds_lead"></p>
    <ol class="how">
      <li data-i18n-html="creds_li1"></li>
      <li data-i18n-html="creds_li2"></li>
      <li data-i18n-html="creds_li3"></li>
      <li data-i18n-html="creds_li4"></li>
    </ol>
    <details>
      <summary data-i18n="creds_help"></summary>
      <ul>
        <li data-i18n-html="help1"></li>
        <li data-i18n-html="help2"></li>
        <li data-i18n-html="help3"></li>
        <li data-i18n-html="help4"></li>
        <li data-i18n-html="help5"></li>
        <li data-i18n-html="help6"></li>
      </ul>
    </details>
    <div id="creds-error"></div>
    <label for="api-id">api_id</label>
    <input id="api-id" type="text" inputmode="numeric" autocomplete="off" placeholder="12345678">
    <label for="api-hash">api_hash</label>
    <input id="api-hash" type="text" autocomplete="off" spellcheck="false" placeholder="0123456789abcdef0123456789abcdef">
    <div class="row">
      <button class="primary" id="start" data-i18n="creds_button"></button>
      <span class="timer" id="creds-status"></span>
    </div>
  </section>

  <section class="card" id="qr" hidden>
    <h2 data-i18n="qr_title"></h2>
    <p class="lead" data-i18n-html="qr_lead"></p>
    <div class="qr" id="qr-box"><div class="spinner"></div></div>
    <p class="qr-hint" data-i18n="qr_hint"></p>
    <p class="timer" id="qr-timer"></p>
    <div class="row">
      <button class="ghost" id="cancel" data-i18n="qr_cancel"></button>
    </div>
  </section>

  <section class="card" id="password" hidden>
    <h2 data-i18n="pw_title"></h2>
    <p class="lead" data-i18n="pw_lead"></p>
    <div id="password-error"></div>
    <label for="password-input" data-i18n="pw_label"></label>
    <input id="password-input" type="password" autocomplete="current-password">
    <div class="row">
      <button class="primary" id="password-submit" data-i18n="pw_submit"></button>
      <button class="ghost" id="password-cancel" data-i18n="qr_cancel"></button>
    </div>
  </section>

  <section class="card" id="done" hidden>
    <h2 id="done-title"></h2>
    <div id="done-error"></div>
    <div class="account" id="account" hidden>
      <div>
        <div class="name" id="account-name"></div>
        <div class="user" id="account-user"></div>
      </div>
    </div>
    <p class="lead" id="done-lead"></p>
    <div class="tabs" id="tabs" role="tablist"></div>
    <pre id="snippet"></pre>
    <p class="snippet-hint" id="snippet-hint"></p>
    <div class="row">
      <button class="primary" id="copy" data-i18n="done_copy"></button>
      <button class="ghost" id="again" data-i18n="done_again"></button>
    </div>
  </section>

  <section class="card" id="error" hidden>
    <h2 data-i18n="error_title"></h2>
    <div id="error-body"></div>
    <div class="row">
      <button class="primary" id="retry" data-i18n="error_retry"></button>
    </div>
  </section>

  <footer>
    <p data-i18n="footer1"></p>
    <p data-i18n-html="footer2"></p>
  </footer>
</main>

<script>
const KEY = new URLSearchParams(location.search).get('k') || '';
const $ = (id) => document.getElementById(id);
const SECTIONS = ['creds', 'qr', 'password', 'done', 'error'];

const I18N = {
  en: {
    title: 'tgsearch-mcp — Telegram setup',
    subtitle: 'Search your Telegram from an AI agent. Read-only.',
    step1: '1. App credentials',
    step2: '2. QR login',
    step3: '3. Connect your agent',

    creds_title: 'Telegram app credentials',
    creds_lead: 'The server works with your own account, so Telegram requires an app. It is free and done once.',
    creds_li1: 'Open <a href="https://my.telegram.org" target="_blank" rel="noreferrer">my.telegram.org</a> and sign in with your phone number.',
    creds_li2: 'The code arrives <strong>as a Telegram message</strong>, not by SMS.',
    creds_li3: 'Open <code>API development tools</code> and create an app (any name, platform — Other).',
    creds_li4: 'Copy <code>api_id</code> and <code>api_hash</code> from that page.',
    creds_help: 'Not working?',
    help1: '<strong>The site does not open.</strong> my.telegram.org is periodically blocked in some countries and by some ISPs — that is a network block, you need a VPN. Telegram itself still works.',
    help2: '<strong>No SMS code.</strong> The code arrives as a Telegram message. You need any phone or computer where you are already signed in.',
    help3: '<strong>This is not a bot.</strong> BotFather is not involved; only my.telegram.org issues these keys.',
    help4: '<strong>The app form fails.</strong> It happens with new and virtual numbers. Try a different Short name or come back later.',
    help5: '<strong>The keys belong to the account</strong> you signed in with.',
    help6: '<strong>Check the input:</strong> api_id is digits only, api_hash is exactly 32 characters without spaces or quotes.',
    creds_button: 'Get the QR code',

    qr_title: 'Scan the code',
    qr_lead: 'Telegram → Settings → Devices → <em>Link Desktop Device</em>.',
    qr_hint: 'The code refreshes on its own — scan the one on screen.',
    qr_cancel: 'Cancel',

    pw_title: 'Two-step verification password',
    pw_lead: 'The account is protected by a password you set in Telegram.',
    pw_label: 'Password',
    pw_submit: 'Sign in',

    done_title: 'Done',
    done_lead: 'Copy the config into your agent — it will see the <code class="inline">tg_*</code> tools.',
    done_copy: 'Copy',
    done_again: 'Sign in with another account',
    already_title: 'Telegram is already connected',
    already_lead: 'Just add the server to your agent. Use the button below to sign in with another account.',

    error_title: 'That did not work',
    error_retry: 'Try again',

    footer1: 'The server only reads: it never sends, deletes or joins anything.',
    footer2: 'The session is stored locally in <code class="inline" id="config-path">config.json</code>. Remove it with <code class="inline">tgsearch-mcp logout</code>.',

    timer: 'Code refreshes in {n}s',
    refreshing: 'Refreshing the code…',
    connecting: 'Connecting to Telegram…',
    copied: 'Copied',
    copy_manual: 'Select and copy manually',
    cancelled: 'Login cancelled. Nothing was saved — you can start over.',
    server_down: 'The server is not responding. Is it still running in the terminal?',
    config_failed: 'Could not read the configuration.',

    hint_opencode: 'opencode.json (project) or ~/.config/opencode/opencode.json',
    hint_claude_code: 'One command in the terminal',
    hint_codex: '~/.codex/config.toml (or .codex/config.toml in a project)',
    hint_claude_desktop: 'claude_desktop_config.json or mcp.json',
    hint_cursor: '~/.cursor/mcp.json',

    err_generic: 'Something went wrong.',
    err_login_in_progress: 'A login is already in progress. Finish or cancel it first.',
    err_invalid_api_id: 'api_id must be digits only.',
    err_invalid_api_hash: 'api_hash is 32 characters without spaces or quotes.',
    err_api_id_invalid: 'Telegram rejected these keys. Make sure api_id and api_hash come from the same app at my.telegram.org.',
    err_api_id_published: 'This api_id is public. Create a new app at my.telegram.org.',
    err_timeout: 'Telegram did not answer in time. Check the connection and try again.',
    err_connection_error: 'Cannot reach Telegram. Check the network, VPN or proxy.',
    err_flood_wait: 'Telegram asks to wait: too many attempts. Try again in a few minutes.',
    err_password_invalid: 'Wrong password. Attempts left: {n}.',
    err_password_rejected: 'Too many wrong passwords. Start over later.',
    err_expired: 'The code expired before it was scanned. Start over.',
    err_no_password_step: 'The password step is no longer active. Start over.',
    err_internal_error: 'Internal error. Please try again.',
  },
  ru: {
    title: 'tgsearch-mcp — подключение Telegram',
    subtitle: 'Поиск по вашему Telegram для ИИ-агента. Только чтение.',
    step1: '1. Ключи приложения',
    step2: '2. Вход по QR',
    step3: '3. Подключение агента',

    creds_title: 'Ключи приложения Telegram',
    creds_lead: 'Сервер работает с вашим аккаунтом, поэтому Telegram требует ключи приложения. Это бесплатно и делается один раз.',
    creds_li1: 'Откройте <a href="https://my.telegram.org" target="_blank" rel="noreferrer">my.telegram.org</a> и войдите по номеру телефона.',
    creds_li2: 'Код придёт <strong>сообщением в Telegram</strong>, а не по SMS.',
    creds_li3: 'Раздел <code>API development tools</code> → создайте приложение (название любое, platform — Other).',
    creds_li4: 'Скопируйте <code>api_id</code> и <code>api_hash</code> с этой страницы.',
    creds_help: 'Не получается?',
    help1: '<strong>Сайт не открывается.</strong> my.telegram.org периодически недоступен из некоторых стран и у некоторых провайдеров — это блокировка сайта, нужен VPN. Сам Telegram при этом работает.',
    help2: '<strong>Код не приходит по SMS.</strong> Он приходит в Telegram. Нужен любой телефон или компьютер, где вы уже вошли.',
    help3: '<strong>Это не бот.</strong> BotFather не нужен, ключи выдаёт только my.telegram.org.',
    help4: '<strong>Ошибка при создании приложения.</strong> Бывает на новых и виртуальных номерах. Попробуйте другое <code>Short name</code> или позже.',
    help5: '<strong>Ключи привязаны к аккаунту</strong>, с которого вы входили на сайт.',
    help6: '<strong>Проверьте ввод:</strong> <code>api_id</code> — только цифры, <code>api_hash</code> — ровно 32 символа без пробелов и кавычек.',
    creds_button: 'Получить QR-код',

    qr_title: 'Отсканируйте код',
    qr_lead: 'Telegram → Настройки → Устройства → <em>Подключить устройство</em>.',
    qr_hint: 'Код обновляется сам — сканируйте тот, что на экране.',
    qr_cancel: 'Отмена',

    pw_title: 'Пароль двухэтапной проверки',
    pw_lead: 'Аккаунт защищён паролем, который вы задали в Telegram.',
    pw_label: 'Пароль',
    pw_submit: 'Войти',

    done_title: 'Готово',
    done_lead: 'Скопируйте конфиг в свой агент — и он увидит инструменты <code class="inline">tg_*</code>.',
    done_copy: 'Скопировать',
    done_again: 'Войти другим аккаунтом',
    already_title: 'Telegram уже подключён',
    already_lead: 'Осталось добавить сервер в агент. Войти другим аккаунтом можно кнопкой ниже.',

    error_title: 'Не получилось',
    error_retry: 'Попробовать снова',

    footer1: 'Сервер только читает: он не отправляет, не удаляет и не вступает в чаты.',
    footer2: 'Сессия хранится локально: <code class="inline" id="config-path">config.json</code>. Удалить её можно командой <code class="inline">tgsearch-mcp logout</code>.',

    timer: 'Код обновится через {n} с',
    refreshing: 'Обновляю код…',
    connecting: 'Соединяюсь с Telegram…',
    copied: 'Скопировано',
    copy_manual: 'Выделите и скопируйте вручную',
    cancelled: 'Вход отменён. Ничего не сохранено — можно начать заново.',
    server_down: 'Сервер не отвечает. Он ещё запущен в терминале?',
    config_failed: 'Не удалось прочитать конфигурацию.',

    hint_opencode: 'opencode.json (в проекте) или ~/.config/opencode/opencode.json',
    hint_claude_code: 'Одна команда в терминале',
    hint_codex: '~/.codex/config.toml (или .codex/config.toml в проекте)',
    hint_claude_desktop: 'claude_desktop_config.json или mcp.json',
    hint_cursor: '~/.cursor/mcp.json',

    err_generic: 'Что-то пошло не так.',
    err_login_in_progress: 'Вход уже идёт. Завершите или отмените его, потом попробуйте снова.',
    err_invalid_api_id: 'api_id должен состоять только из цифр.',
    err_invalid_api_hash: 'api_hash — это 32 символа без пробелов и кавычек.',
    err_api_id_invalid: 'Telegram не принял эти ключи. Проверьте, что api_id и api_hash взяты из одного приложения на my.telegram.org.',
    err_api_id_published: 'Этот api_id засвечен в открытых источниках. Создайте новое приложение на my.telegram.org.',
    err_timeout: 'Telegram не ответил вовремя. Проверьте интернет и попробуйте снова.',
    err_connection_error: 'Нет связи с Telegram. Проверьте интернет, VPN или прокси.',
    err_flood_wait: 'Telegram просит подождать: слишком много попыток. Попробуйте через несколько минут.',
    err_password_invalid: 'Неверный пароль. Осталось попыток: {n}.',
    err_password_rejected: 'Слишком много неверных паролей. Начните заново позже.',
    err_expired: 'Код устарел — вы не успели отсканировать. Начните заново.',
    err_no_password_step: 'Шаг с паролем уже неактуален. Начните заново.',
    err_internal_error: 'Внутренняя ошибка. Попробуйте ещё раз.',
  },
};

const ERROR_KEYS = {
  login_in_progress: 'err_login_in_progress',
  invalid_api_id: 'err_invalid_api_id',
  invalid_api_hash: 'err_invalid_api_hash',
  api_id_invalid: 'err_api_id_invalid',
  api_id_published: 'err_api_id_published',
  timeout: 'err_timeout',
  connection_error: 'err_connection_error',
  flood_wait: 'err_flood_wait',
  password_invalid: 'err_password_invalid',
  password_rejected: 'err_password_rejected',
  expired: 'err_expired',
  no_password_step: 'err_no_password_step',
  internal_error: 'err_internal_error',
};

let lang = 'en';
let configPath = '';
let doneVariant = 'fresh';
let lastData = null;
let pollTimer = null;
let deadline = 0;
let timerHandle = null;
let snippets = [];
let activeSnippet = 0;
let busy = false;

function t(key, vars) {
  const table = I18N[lang] || I18N.en;
  let text = table[key];
  if (text === undefined) text = I18N.en[key];
  if (text === undefined) text = key;
  if (vars) {
    for (const name of Object.keys(vars)) {
      text = text.split('{' + name + '}').join(String(vars[name]));
    }
  }
  return text;
}

function applyLang() {
  document.documentElement.lang = lang;
  document.title = t('title');
  for (const el of document.querySelectorAll('[data-i18n]')) {
    el.textContent = t(el.getAttribute('data-i18n'));
  }
  for (const el of document.querySelectorAll('[data-i18n-html]')) {
    el.innerHTML = t(el.getAttribute('data-i18n-html'));
  }
  for (const el of document.querySelectorAll('[data-lang]')) {
    el.setAttribute('aria-pressed', String(el.getAttribute('data-lang') === lang));
  }
  if (configPath && $('config-path')) $('config-path').textContent = configPath;
  if (lastData) renderDoneText();
}

function detectLang() {
  try {
    const stored = localStorage.getItem('tgsearch.lang');
    if (stored === 'ru' || stored === 'en') return stored;
  } catch (err) {}
  const list = navigator.languages && navigator.languages.length
    ? navigator.languages
    : [navigator.language || 'en'];
  return String(list[0] || 'en').toLowerCase().startsWith('ru') ? 'ru' : 'en';
}

function setLang(next) {
  lang = next === 'ru' ? 'ru' : 'en';
  try { localStorage.setItem('tgsearch.lang', lang); } catch (err) {}
  applyLang();
  if (lastData) render(lastData);
}

function show(name) {
  for (const id of SECTIONS) $(id).hidden = id !== name;
  const active = { creds: 1, qr: 2, password: 2, done: 3, error: 3 }[name] || 1;
  for (const li of $('steps').children) {
    li.dataset.active = String(Number(li.dataset.step) === active);
    li.dataset.done = String(Number(li.dataset.step) < active);
  }
}

function banner(text, detail) {
  const box = document.createElement('div');
  box.className = 'banner bad';
  box.textContent = text;
  if (detail) {
    const small = document.createElement('div');
    small.className = 'detail';
    small.textContent = detail;
    box.appendChild(small);
  }
  return box;
}

function describe(data) {
  const key = ERROR_KEYS[data.error];
  let text = t(key || 'err_generic');
  if (data.error === 'password_invalid' && typeof data.attempts_left === 'number') {
    text = t('err_password_invalid', { n: data.attempts_left });
  }
  return banner(text, data.message || data.hint || '');
}

async function call(path, body) {
  const options = { headers: { 'X-Setup-Key': KEY } };
  if (body !== undefined) {
    options.method = 'POST';
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  let data;
  try {
    data = await response.json();
  } catch (err) {
    data = { ok: false, error: 'bad_response', message: 'Server closed or returned non-JSON.' };
  }
  data.status = response.status;
  return data;
}

function startTimer(seconds) {
  clearInterval(timerHandle);
  deadline = Date.now() + seconds * 1000;
  const tick = () => {
    const left = Math.max(0, Math.round((deadline - Date.now()) / 1000));
    $('qr-timer').textContent = left > 0 ? t('timer', { n: left }) : t('refreshing');
  };
  tick();
  timerHandle = setInterval(tick, 1000);
}

function stopTimer() {
  clearInterval(timerHandle);
  timerHandle = null;
}

function stopPolling() {
  clearInterval(pollTimer);
  pollTimer = null;
  stopTimer();
}

function render(data) {
  lastData = data;
  if (data.state === 'pending') {
    show('qr');
    if (data.version !== render.version) {
      render.version = data.version;
      $('qr-box').innerHTML = data.svg;
      if (typeof data.expires_in === 'number') startTimer(data.expires_in);
    }
    return;
  }
  if (data.state === 'password') {
    stopPolling();
    show('password');
    if (data.error) $('password-error').replaceChildren(describe(data));
    else $('password-error').replaceChildren();
    $('password-input').focus();
    return;
  }
  if (data.state === 'done') {
    stopPolling();
    renderDone(data);
    return;
  }
  if (data.state === 'expired' || data.state === 'error') {
    stopPolling();
    show('error');
    $('error-body').replaceChildren(describe(data));
    return;
  }
  show('creds');
}
render.version = -1;

function renderDoneText() {
  $('done-title').textContent = t(doneVariant === 'already' ? 'already_title' : 'done_title');
  $('done-lead').innerHTML = t(doneVariant === 'already' ? 'already_lead' : 'done_lead');
}

function renderDone(data) {
  if (data && data.account) doneVariant = 'fresh';
  show('done');
  if (data && data.account) {
    $('account').hidden = false;
    $('account-name').textContent = data.account.name || '';
    $('account-user').textContent = data.account.username ? '@' + data.account.username : String(data.account.id || '');
  }
  renderDoneText();
  const currentId = snippets[activeSnippet] ? snippets[activeSnippet].id : '';
  if (data && data.snippets) snippets = data.snippets;
  const index = snippets.findIndex((item) => item.id === currentId);
  activeSnippet = index >= 0 ? index : 0;
  drawSnippets();
}

function drawSnippets() {
  const tabs = $('tabs');
  tabs.replaceChildren();
  snippets.forEach((item, index) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = item.label;
    button.setAttribute('role', 'tab');
    button.setAttribute('aria-selected', String(index === activeSnippet));
    button.addEventListener('click', () => { activeSnippet = index; drawSnippets(); });
    tabs.appendChild(button);
  });
  const current = snippets[activeSnippet];
  $('snippet').textContent = current ? current.code : '';
  $('snippet-hint').textContent = current ? t('hint_' + current.id.replace(/-/g, '_')) : '';
}

async function poll() {
  if (busy) return;
  busy = true;
  try {
    const data = await call('/api/session');
    if (data.ok) render(data);
  } catch (err) {
    stopPolling();
    show('error');
    $('error-body').replaceChildren(banner(t('server_down'), String(err)));
  } finally {
    busy = false;
  }
}

function pollEvery(ms) {
  clearInterval(pollTimer);
  pollTimer = setInterval(poll, ms);
}

async function startLogin(withCredentials) {
  show('qr');
  $('qr-box').innerHTML = '<div class="spinner"></div>';
  $('creds-status').textContent = t('connecting');
  const payload = {};
  if (withCredentials) {
    payload.api_id = $('api-id').value;
    payload.api_hash = $('api-hash').value;
  }
  const data = await call('/api/session', payload);
  $('creds-status').textContent = '';
  if (!data.ok) {
    if (data.error === 'login_in_progress') {
      pollEvery(1200);
      poll();
      return;
    }
    show('creds');
    $('creds-error').replaceChildren(describe(data));
    return;
  }
  $('creds-error').replaceChildren();
  render(data);
  pollEvery(1400);
}

async function boot() {
  const cfg = await call('/api/config');
  if (cfg.config_path) {
    configPath = cfg.config_path;
    if ($('config-path')) $('config-path').textContent = configPath;
  }
  if (!cfg.ok) {
    show('error');
    $('error-body').replaceChildren(banner(t('config_failed'), cfg.message || ''));
    return;
  }
  if (cfg.has_session) {
    const info = await call('/api/snippets');
    doneVariant = 'already';
    snippets = info.snippets || [];
    activeSnippet = 0;
    show('done');
    renderDoneText();
    drawSnippets();
    return;
  }
  if (cfg.has_api) {
    startLogin(false);
    return;
  }
  show('creds');
  $('api-id').focus();
}

for (const button of document.querySelectorAll('[data-lang]')) {
  button.addEventListener('click', () => setLang(button.getAttribute('data-lang')));
}

$('start').addEventListener('click', () => startLogin(true));
$('cancel').addEventListener('click', async () => {
  stopPolling();
  await fetch('/api/session', { method: 'DELETE', headers: { 'X-Setup-Key': KEY } });
  lastData = { state: 'cancelled' };
  show('error');
  $('error-body').replaceChildren(banner(t('cancelled')));
});
$('password-cancel').addEventListener('click', async () => {
  await fetch('/api/session', { method: 'DELETE', headers: { 'X-Setup-Key': KEY } });
  lastData = { state: 'cancelled' };
  show('error');
  $('error-body').replaceChildren(banner(t('cancelled')));
});
$('password-submit').addEventListener('click', async () => {
  const password = $('password-input').value;
  if (!password) return;
  const data = await call('/api/session/password', { password });
  $('password-input').value = '';
  if (!data.ok) {
    $('password-error').replaceChildren(describe(data));
    return;
  }
  render(data);
  if (data.state !== 'done') pollEvery(1200);
});
$('password-input').addEventListener('keydown', (event) => {
  if (event.key === 'Enter') $('password-submit').click();
});
$('copy').addEventListener('click', async () => {
  const current = snippets[activeSnippet];
  if (!current) return;
  try {
    await navigator.clipboard.writeText(current.code);
    $('copy').textContent = t('copied');
    setTimeout(() => { $('copy').textContent = t('done_copy'); }, 1500);
  } catch (err) {
    $('copy').textContent = t('copy_manual');
  }
});
$('again').addEventListener('click', () => {
  doneVariant = 'fresh';
  $('account').hidden = true;
  startLogin(false);
});
$('retry').addEventListener('click', () => {
  $('error-body').replaceChildren();
  boot();
});

lang = detectLang();
try {
  applyLang();
} finally {
  document.documentElement.classList.remove('boot');
}
boot();
</script>
</body>
</html>
"""
