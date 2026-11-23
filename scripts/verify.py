#!/usr/bin/env python3
"""Live smoke test for tgsearch-mcp.

Starts the server over stdio (exactly like a real MCP host), lists the tool
catalog, checks tg_status and, when the session is authorized, runs a real
chat lookup + search + context read.

    python scripts/verify.py
    python scripts/verify.py --find quant --chat @some_channel --query "sharpe ratio"

Exit codes: 0 = protocol checks passed, 1 = protocol check failed,
2 = server is not authorized yet (run `tgsearch-mcp login`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

EXPECTED_TOOLS = 9


def show(title: str, payload) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, indent=2, ensure_ascii=False)[:2000])


def payload_of(result) -> dict:
    return result.structured_content or {}


async def run(args: argparse.Namespace) -> int:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "tgsearch_mcp", "serve"],
        env=dict(os.environ),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"connected: {init.server_info.name} {init.server_info.version}")

            tools = await session.list_tools()
            names = [tool.name for tool in tools.tools]
            print(f"tools ({len(names)}): {', '.join(names)}")
            if len(names) != EXPECTED_TOOLS:
                print(f"FAIL: expected {EXPECTED_TOOLS} tools", file=sys.stderr)
                return 1

            status = payload_of(await session.call_tool("tg_status", {}))
            show("tg_status", status)

            if not status.get("logged_in"):
                print("\nSKIP live checks: not authorized.")
                print(f"hint: {status.get('hint') or status.get('error')}")
                return 2

            found = payload_of(await session.call_tool("tg_find_chats", {"query": args.find}))
            show(f"tg_find_chats({args.find!r})", {k: found.get(k) for k in ("count", "note")})
            chats = found.get("chats") or []
            for chat in chats[:5]:
                print(f"  - {chat.get('ref')}  {chat.get('type')}  {chat.get('title')}")

            chat = args.chat or (chats[0]["ref"] if chats else None)
            if not chat:
                print("\nSKIP search: no chat matched --find and no --chat given.")
                return 0

            result = payload_of(
                await session.call_tool(
                    "tg_search_messages", {"chat": chat, "query": args.query, "limit": 5}
                )
            )
            show(f"tg_search_messages({chat}, {args.query!r})", result)
            if result.get("ok") is False:
                return 1

            hits = result.get("messages") or []
            if not hits:
                print("\nNo hits; try another --query or --chat.")
                return 0

            context = payload_of(
                await session.call_tool(
                    "tg_get_messages",
                    {"chat": chat, "around_id": hits[0]["id"], "limit": 5},
                )
            )
            show(f"tg_get_messages(around_id={hits[0]['id']})", context)
            return 0 if context.get("ok") else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--find", default="", help="substring used for tg_find_chats (empty = list dialogs)"
    )
    parser.add_argument("--chat", help="chat ref for the search step (default: first match)")
    parser.add_argument("--query", default="test", help="query for tg_search_messages")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
