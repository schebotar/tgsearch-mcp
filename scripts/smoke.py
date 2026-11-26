#!/usr/bin/env python3
"""Offline smoke test for tgsearch-mcp.

Boots the server over stdio exactly like a real MCP host, from an empty
temporary working directory and with every TG_*/TGSEARCH_* variable stripped:
no credentials, no login, no network. Asserts the tool catalog and that
`tg_status` degrades softly. This is what CI runs; for the live find → search →
context flow after `tgsearch-mcp login` use `scripts/verify.py`.

    python scripts/smoke.py

Exit codes: 0 = ok, 1 = catalog/status mismatch.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = 11


def clean_env(home: str) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("TG_", "TGSEARCH_"))
    }
    env["TGSEARCH_HOME"] = home
    pythonpath = [str(ROOT / "src"), env.get("PYTHONPATH", "")]
    env["PYTHONPATH"] = os.pathsep.join(part for part in pythonpath if part)
    return env


async def run() -> int:
    with tempfile.TemporaryDirectory(prefix="tgsearch-smoke-") as tmp:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "tgsearch_mcp", "serve"],
            env=clean_env(tmp),
            cwd=tmp,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                print(f"connected: {init.server_info.name} {init.server_info.version}")

                tools = await session.list_tools()
                names = sorted(tool.name for tool in tools.tools)
                print(f"tools ({len(names)}): {', '.join(names)}")
                if len(names) != EXPECTED_TOOLS:
                    print(f"FAIL: expected {EXPECTED_TOOLS} tools", file=sys.stderr)
                    return 1

                result = await session.call_tool("tg_status", {})
                status = result.structured_content or {}
                if status.get("ok") is not True:
                    print(f"FAIL: tg_status returned {status!r}", file=sys.stderr)
                    return 1
                if status.get("configured") or status.get("logged_in"):
                    print(
                        "FAIL: smoke must run without credentials, but the server is configured",
                        file=sys.stderr,
                    )
                    return 1

                print("tg_status: ok, configured=false, logged_in=false (offline)")
                return 0


def main() -> int:
    try:
        return asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
