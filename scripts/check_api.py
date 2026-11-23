#!/usr/bin/env python3
"""Check Telegram api_id / api_hash without logging in.

Connects to Telegram over MTProto, reads `help.getConfig` (connectivity/DC)
and then asks for an `auth.exportLoginToken` QR token. That is a pre-login
call that validates the app credentials without sending any code, message or
notification; an invalid `api_id`/`api_hash` pair fails with `API_ID_INVALID`
(or `API_ID_PUBLISHED_FLOOD` for leaked credentials). The returned QR token
simply expires unused.

Credentials are looked up in this order (first non-empty wins):

    1. command line      --api-id / --api-hash
    2. environment       TG_API_ID / TG_API_HASH
    3. env file          ./.env (or --env-file PATH)
    4. config.json       $XDG_CONFIG_HOME/tgsearch-mcp/config.json

No account is touched and nothing is sent to anyone: the check only reads
Telegram's config and asks for an unused login token. Add `--save` to store a
valid pair in config.json so that `tgsearch-mcp login` / `serve` pick it up.

    python scripts/check_api.py
    python scripts/check_api.py --save
    python scripts/check_api.py --api-id <id> --api-hash <hash> --json

Exit codes: 0 = credentials accepted, 1 = rejected or network error,
2 = credentials are not configured anywhere.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from telethon import TelegramClient, errors, functions
from telethon.sessions import StringSession

# Allow running straight from a checkout without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tgsearch_mcp.config import config_path, load_config, save_config  # noqa: E402

DEFAULT_ENV_FILE = Path(".env")


def read_env_file(path: Path) -> dict[str, str]:
    """Parse a minimal KEY=VALUE .env file (comments, quotes, `export`)."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        name, sep, value = line.partition("=")
        if not sep:
            continue
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name:
            values[name] = value
    return values


def read_config_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _from_env(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else None


def resolve(args: argparse.Namespace) -> tuple[int | None, str | None, str | None]:
    """Return (api_id, api_hash, source) using the documented precedence."""
    env_file = read_env_file(Path(args.env_file).expanduser())
    file_cfg = read_config_file(config_path())

    api_id = None
    api_hash = None
    sources: list[str] = []

    if args.api_id is not None or args.api_hash is not None:
        api_id = args.api_id
        api_hash = args.api_hash
        sources.append("command line")
    if api_id is None:
        api_id = _from_env("TG_API_ID")
        if api_id is not None:
            sources.append("environment")
    if api_hash is None:
        api_hash = _from_env("TG_API_HASH")
        if api_hash is not None:
            sources.append("environment")
    if api_id is None and env_file.get("TG_API_ID"):
        api_id = env_file["TG_API_ID"]
        sources.append(str(args.env_file))
    if api_hash is None and env_file.get("TG_API_HASH"):
        api_hash = env_file["TG_API_HASH"]
        sources.append(str(args.env_file))
    if api_id is None and file_cfg.get("api_id") is not None:
        api_id = file_cfg["api_id"]
        sources.append(str(config_path()))
    if api_hash is None and file_cfg.get("api_hash"):
        api_hash = file_cfg["api_hash"]
        sources.append(str(config_path()))

    try:
        api_id = int(api_id) if api_id not in (None, "") else None
    except (TypeError, ValueError):
        raise SystemExit(f"api_id must be an integer, got {api_id!r}")

    api_hash = str(api_hash).strip() if api_hash not in (None, "") else None
    source = ", ".join(dict.fromkeys(sources)) or None
    return api_id, api_hash, source


def mask(secret: str) -> str:
    if len(secret) <= 8:
        return "…"
    return f"{secret[:4]}…{secret[-4:]}"


async def probe(api_id: int, api_hash: str, timeout: float) -> dict:
    """Round-trip to Telegram with the given credentials, no login needed."""
    client = TelegramClient(
        StringSession(),
        api_id,
        api_hash,
        receive_updates=False,
        connection_retries=1,
        retry_delay=1,
        timeout=timeout,
        device_model="tgsearch-mcp",
        system_version="Linux",
    )
    result: dict = {"ok": False, "api_id": api_id, "api_hash": mask(api_hash)}
    try:
        await asyncio.wait_for(client.connect(), timeout)
        if not client.is_connected():
            result["error"] = "could not connect to Telegram"
            return result
        config = await asyncio.wait_for(client(functions.help.GetConfigRequest()), timeout)
        # Pre-login method that carries api_id + api_hash: the server rejects
        # a bad pair here, and the token just expires if nobody scans it.
        await asyncio.wait_for(
            client(
                functions.auth.ExportLoginTokenRequest(
                    api_id=api_id, api_hash=api_hash, except_ids=[]
                )
            ),
            timeout,
        )
        result.update(
            {
                "ok": True,
                "api_hash_valid": True,
                "authorized": bool(await client.is_user_authorized()),
                "dc": getattr(config, "this_dc", None),
                "server_date": getattr(config, "date", None).isoformat()
                if getattr(config, "date", None)
                else None,
            }
        )
        return result
    except errors.ApiIdInvalidError:
        result["error"] = (
            "Telegram rejected the pair (API_ID_INVALID): api_id and api_hash "
            "must come from the same app at https://my.telegram.org"
        )
    except errors.ApiIdPublishedFloodError:
        result["error"] = (
            "This api_id is published/leaked (API_ID_PUBLISHED_FLOOD); create a new app."
        )
    except asyncio.TimeoutError:
        result["error"] = f"timed out after {timeout:g}s"
    except (ConnectionError, OSError) as exc:
        result["error"] = f"network error: {exc}"
    except errors.RPCError as exc:
        result["error"] = f"Telegram error: {type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001 - best effort
            pass
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate Telegram api_id/api_hash without logging in.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--api-id", type=int)
    parser.add_argument("--api-hash")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE), help="default: ./.env")
    parser.add_argument("--timeout", type=float, default=15.0, help="seconds (default: 15)")
    parser.add_argument("--save", action="store_true", help="write valid credentials to config.json")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    api_id, api_hash, source = resolve(args)
    if api_id is None or api_hash is None:
        missing = [name for name, value in (("api_id", api_id), ("api_hash", api_hash)) if value is None]
        print(
            "Missing " + " and ".join(missing) + ".\n"
            "Put them in ./.env (copy .env.example), export TG_API_ID/TG_API_HASH, "
            "pass --api-id/--api-hash, or run `tgsearch-mcp login` once.",
            file=sys.stderr,
        )
        return 2

    result = asyncio.run(probe(api_id, api_hash, args.timeout))
    result["source"] = source

    if result.get("ok"):
        cfg = load_config()
        cfg.api_id = api_id
        cfg.api_hash = api_hash
        if args.save:
            save_config(cfg)
            result["saved_to"] = str(config_path())

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif result.get("ok"):
        print(f"api_id {api_id} + api_hash {mask(api_hash)}: OK (source: {source})")
        print(f"Telegram reachable (dc {result.get('dc')}), server date {result.get('server_date')}.")
        print(
            "Account authorized: yes."
            if result.get("authorized")
            else "Account authorized: no (expected before `tgsearch-mcp login`)."
        )
        if result.get("saved_to"):
            print(f"Saved to {result['saved_to']} (mode 0600) - `tgsearch-mcp login` can use it.")
    else:
        print(f"api_id {api_id} + api_hash {mask(api_hash)}: FAILED (source: {source})", file=sys.stderr)
        print(result.get("error", "unknown error"), file=sys.stderr)

    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
