from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys

from telethon import TelegramClient, errors
from telethon.sessions import StringSession

from . import __version__
from .config import Config, load_config, save_config
from .errors import TgSearchError, error_payload

QR_REFRESH_SECONDS = 30


def _print_qr(url: str) -> None:
    try:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception:
        pass
    print(f"\nOr open this link on a logged-in device:\n  {url}\n")

def _build_client(cfg: Config, session: str | None = None) -> TelegramClient:
    return TelegramClient(
        StringSession(session or ""),
        cfg.api_id,
        cfg.api_hash,
        device_model=cfg.device_model,
        system_version=cfg.system_version,
        app_version=cfg.app_version,
        receive_updates=False,
    )

def _resolve_api_credentials(args: argparse.Namespace, cfg: Config) -> tuple[int, str]:
    api_id = args.api_id or cfg.api_id or os.environ.get("TG_API_ID")
    api_hash = args.api_hash or cfg.api_hash or os.environ.get("TG_API_HASH")
    if not api_id or not api_hash:
        print(
            "Telegram API credentials are required.\n"
            "Create an app at https://my.telegram.org -> API development tools,\n"
            "then pass --api-id/--api-hash or set TG_API_ID/TG_API_HASH.",
            file=sys.stderr,
        )
        if not sys.stdin.isatty():
            raise TgSearchError("not_configured", "Missing API credentials.")
        api_id = input("api_id: ").strip()
        api_hash = input("api_hash: ").strip()
    try:
        return int(str(api_id)), str(api_hash)
    except ValueError as exc:
        raise TgSearchError("not_configured", "api_id must be an integer.") from exc

async def _login_qr(client: TelegramClient) -> None:
    qr = await client.qr_login()
    while True:
        _print_qr(qr.url)
        try:
            await qr.wait(timeout=QR_REFRESH_SECONDS)
            return
        except asyncio.TimeoutError:
            await qr.recreate()
        except errors.SessionPasswordNeededError:
            password = getpass.getpass("Two-step verification password: ")
            await client.sign_in(password=password)
            return

async def _login_phone(client: TelegramClient, phone: str) -> None:
    await client.send_code_request(phone)
    code = input("Login code from Telegram: ").strip()
    try:
        await client.sign_in(phone=phone, code=code)
    except errors.SessionPasswordNeededError:
        password = getpass.getpass("Two-step verification password: ")
        await client.sign_in(password=password)

async def _cmd_login(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.api_id, cfg.api_hash = _resolve_api_credentials(args, cfg)
    client = _build_client(cfg, cfg.session)
    await client.connect()
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            cfg.session = client.session.save()
            save_config(cfg)
            print(f"Already logged in as {me.first_name or ''} (@{me.username or me.id}).")
            return 0
        if args.phone:
            await _login_phone(client, args.phone)
        else:
            print("Scan the QR code in Telegram: Settings -> Devices -> Link Desktop Device.")
            await _login_qr(client)
        if not await client.is_user_authorized():
            raise TgSearchError("not_authorized", "Authorization did not complete.")
        me = await client.get_me()
        cfg.session = client.session.save()
        save_config(cfg)
        print(f"Logged in as {me.first_name or ''} (@{me.username or me.id}). Session saved.")
        return 0
    finally:
        await client.disconnect()

async def _cmd_logout(_args: argparse.Namespace) -> int:
    cfg = load_config()
    if not cfg.session:
        print("No stored session.")
        return 0
    cfg.require_api()
    client = _build_client(cfg, cfg.session)
    await client.connect()
    try:
        try:
            await client.log_out()
            print("Logged out on the Telegram side.")
        except Exception as exc:  # noqa: BLE001 - best effort
            print(f"Telegram logout failed ({exc}); removing the local session anyway.")
    finally:
        await client.disconnect()
    cfg.session = None
    save_config(cfg)
    print("Local session removed.")
    return 0

async def _cmd_status(_args: argparse.Namespace) -> int:
    from .telegram import TelegramService

    service = TelegramService(load_config())
    try:
        result = await service.status()
    finally:
        await service.close()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1

def _cmd_serve(_args: argparse.Namespace) -> int:
    from .server import mcp

    mcp.run("stdio")
    return 0

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tgsearch-mcp",
        description="Read-only Telegram search MCP server (stdio).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    login = sub.add_parser("login", help="Authorize a Telegram account (QR or phone).")
    login.add_argument("--api-id", type=int, help="Telegram api_id (my.telegram.org).")
    login.add_argument("--api-hash", help="Telegram api_hash (my.telegram.org).")
    login.add_argument("--phone", help="Log in with a phone number instead of a QR code.")
    login.set_defaults(func=_cmd_login)

    logout = sub.add_parser("logout", help="Revoke the session and remove it locally.")
    logout.set_defaults(func=_cmd_logout)

    status = sub.add_parser("status", help="Print login and index status as JSON.")
    status.set_defaults(func=_cmd_status)

    serve = sub.add_parser("serve", help="Run the MCP server over stdio (default).")
    serve.set_defaults(func=_cmd_serve)
    return parser

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None) or args.command == "serve":
        return _cmd_serve(args)
    try:
        return asyncio.run(args.func(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        payload = error_payload(exc)
        print(json.dumps(payload, indent=2, ensure_ascii=False), file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
