from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from . import __version__
from .config import load_config
from .errors import error_payload
from .telegram import TelegramService

Scope = Literal["all", "dialogs", "public"]
Kind = Literal[
    "any", "text", "photos", "videos", "documents", "links", "music", "voice", "pinned"
]
SearchMode = Literal["auto", "raw"]

ChatRef = Annotated[
    str,
    Field(description="Chat reference: @username, t.me link, or numeric chat id."),
]
MaxText = Annotated[
    int,
    Field(ge=80, le=4000, description="Maximum characters of message text per message."),
]

_service: TelegramService | None = None
_service_lock = asyncio.Lock()


async def get_service() -> TelegramService:
    global _service
    if _service is None:
        async with _service_lock:
            if _service is None:
                _service = TelegramService(load_config())
    return _service


async def close_service() -> None:
    global _service
    if _service is not None:
        await _service.close()
        _service = None


async def _safe(coro):
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001 - tools must return structured errors
        return error_payload(exc)


@asynccontextmanager
async def _lifespan(_server):
    try:
        yield {}
    finally:
        await close_service()


mcp = MCPServer(
    name="tgsearch",
    version=__version__,
    instructions=(
        "Read-only Telegram search over your own user account. "
        "Workflow: tg_find_chats -> tg_search_messages -> tg_get_messages(around_id=...) "
        "for context. Use tg_search_global for cross-chat search, and "
        "tg_index_chat + tg_search_index for fast local search over big histories. "
        "The server never sends, edits, joins or deletes anything."
    ),
    lifespan=_lifespan,
    log_level="WARNING",
)


@mcp.tool()
async def tg_status() -> dict[str, Any]:
    """Check Telegram configuration, login state, account and local index.

    Call this first. Never raises: when configuration or the session is
    missing it returns {ok:true, logged_in:false, hint}.
    """
    service = await get_service()
    return await service.status()


@mcp.tool()
async def tg_find_chats(
    query: Annotated[
        str, Field(description="Substring of the chat title, @username or id (case-insensitive).")
    ] = "",
    scope: Annotated[
        Scope, Field(description="dialogs = your chats, public = Telegram directory, all = both.")
    ] = "all",
    limit: Annotated[int, Field(ge=1, le=50, description="Max chats per source.")] = 20,
    refresh: Annotated[bool, Field(description="Bypass the 5-minute dialog cache.")] = False,
) -> dict[str, Any]:
    """Find chats and return the exact `ref` to use in other tools.

    Searches your dialogs by title/username/id and, for a non-empty query,
    the public Telegram directory. Public results include chats you have not
    joined; nothing is joined automatically.
    """
    service = await get_service()
    return await _safe(
        service.find_chats(query=query, scope=scope, limit=limit, refresh=refresh)
    )


@mcp.tool()
async def tg_get_chat(chat: ChatRef) -> dict[str, Any]:
    """Get one chat's details: about, member count, pinned message, forum flag,
    public link and access restrictions."""
    service = await get_service()
    return await _safe(service.chat_info(chat))


@mcp.tool()
async def tg_list_topics(
    chat: ChatRef,
    query: Annotated[str | None, Field(description="Optional topic title filter.")] = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 50,
    offset_topic: Annotated[
        int, Field(ge=0, description="Pagination cursor from a previous call.")
    ] = 0,
) -> dict[str, Any]:
    """List forum topics of a supergroup; use the returned topic_id as
    `topic_id` in tg_search_messages / tg_get_messages."""
    service = await get_service()
    return await _safe(
        service.list_topics(chat, query=query, limit=limit, offset_topic=offset_topic)
    )


@mcp.tool()
async def tg_search_messages(
    chat: ChatRef,
    query: Annotated[
        str, Field(description="Text query. Empty is allowed only with kind or topic_id.")
    ] = "",
    limit: Annotated[int, Field(ge=1, le=50)] = 20,
    offset_id: Annotated[
        int, Field(ge=0, description="Cursor: return messages older than this id.")
    ] = 0,
    kind: Annotated[Kind, Field(description="Message type filter.")] = "any",
    topic_id: Annotated[int | None, Field(description="Forum topic id.")] = None,
    sender: Annotated[
        str | None, Field(description="Only messages from this @username or user id.")
    ] = None,
    min_date: Annotated[
        str | None, Field(description="ISO date (2025-01-31) or unix timestamp.")
    ] = None,
    max_date: Annotated[
        str | None,
        Field(description="ISO date or unix timestamp; a date-only value means end of day."),
    ] = None,
    max_text_chars: MaxText = 400,
) -> dict[str, Any]:
    """Server-side full-text search inside one chat - the fast path for
    "find X in this channel". Results are newest first, with snippet, link and
    a `next_offset_id` cursor. For context around a hit use tg_get_messages
    with around_id."""
    service = await get_service()
    return await _safe(
        service.search_messages(
            chat,
            query=query,
            limit=limit,
            offset_id=offset_id,
            kind=kind,
            topic_id=topic_id,
            sender=sender,
            min_date=min_date,
            max_date=max_date,
            max_text_chars=max_text_chars,
        )
    )


@mcp.tool()
async def tg_search_global(
    query: Annotated[str, Field(description="Text query.")],
    limit: Annotated[int, Field(ge=1, le=50)] = 20,
    kind: Annotated[Kind, Field(description="Message type filter.")] = "any",
    min_date: Annotated[str | None, Field(description="ISO date or unix timestamp.")] = None,
    max_date: Annotated[str | None, Field(description="ISO date or unix timestamp.")] = None,
    offset_rate: Annotated[
        int, Field(ge=0, description="Cursor from a previous `next` object.")
    ] = 0,
    offset_peer: Annotated[
        str | None, Field(description="Cursor chat ref from a previous `next` object.")
    ] = None,
    offset_id: Annotated[
        int, Field(ge=0, description="Cursor message id from a previous `next` object.")
    ] = 0,
    broadcasts_only: Annotated[bool | None, Field(description="Only broadcast channels.")] = None,
    groups_only: Annotated[bool | None, Field(description="Only groups/supergroups.")] = None,
) -> dict[str, Any]:
    """Search across all chats your account has access to.

    Public channels you have not joined are NOT included: open such a channel
    with tg_search_messages instead.
    """
    service = await get_service()
    return await _safe(
        service.search_global(
            query,
            limit=limit,
            kind=kind,
            min_date=min_date,
            max_date=max_date,
            offset_rate=offset_rate,
            offset_peer=offset_peer,
            offset_id=offset_id,
            broadcasts_only=broadcasts_only,
            groups_only=groups_only,
        )
    )


@mcp.tool()
async def tg_get_messages(
    chat: ChatRef,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ids: Annotated[list[int] | None, Field(description="Fetch these exact message ids.")] = None,
    before_id: Annotated[
        int | None, Field(description="Messages older than this id.")
    ] = None,
    after_id: Annotated[int | None, Field(description="Messages newer than this id.")] = None,
    around_id: Annotated[
        int | None, Field(description="Context around this id (limit/2 on each side).")
    ] = None,
    topic_id: Annotated[int | None, Field(description="Forum topic id.")] = None,
    min_date: Annotated[str | None, Field(description="ISO date or unix timestamp.")] = None,
    max_date: Annotated[
        str | None, Field(description="ISO date or unix timestamp; date-only means end of day.")
    ] = None,
    max_text_chars: MaxText = 800,
) -> dict[str, Any]:
    """Read history or fetch specific messages.

    Use ids for exact messages, around_id for context around a search hit,
    before_id/after_id for pagination, and no cursor for the latest messages.
    Results are oldest first and include `next` cursors.
    """
    service = await get_service()
    return await _safe(
        service.get_messages(
            chat,
            limit=limit,
            ids=ids,
            before_id=before_id,
            after_id=after_id,
            around_id=around_id,
            topic_id=topic_id,
            min_date=min_date,
            max_date=max_date,
            max_text_chars=max_text_chars,
        )
    )


@mcp.tool()
async def tg_index_chat(
    chat: ChatRef,
    limit: Annotated[
        int, Field(ge=1, le=20000, description="Max history messages to scan in this call.")
    ] = 5000,
    since_id: Annotated[
        int | None, Field(description="Index only messages newer than this id.")
    ] = None,
    reset: Annotated[
        bool, Field(description="Drop the existing local index for this chat first.")
    ] = False,
) -> dict[str, Any]:
    """Build or update the local SQLite FTS5 index for one chat.

    Incremental by default: only messages newer than the indexed maximum are
    fetched. Use it for large histories you query repeatedly, then search it
    instantly with tg_search_index. On FloodWait the response carries
    `stopped.retry_after`; re-run later to continue.
    """
    service = await get_service()
    return await _safe(service.index_chat(chat, limit=limit, since_id=since_id, reset=reset))


@mcp.tool()
async def tg_search_index(
    query: Annotated[
        str,
        Field(
            description="Search query. The default mode treats words as prefixes and ANDs them."
        ),
    ],
    chat: Annotated[
        str | None, Field(description="Limit to one chat (ref or numeric id).")
    ] = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
    mode: Annotated[
        SearchMode, Field(description="auto = safe prefix query, raw = raw FTS5 MATCH syntax.")
    ] = "auto",
) -> dict[str, Any]:
    """Search the local FTS5 index built by tg_index_chat.

    Works without network when `chat` is a numeric id or a known username.
    Returns highlighted snippets and links. This is a cache, not a substitute
    for server-side search over unindexed chats.
    """
    service = await get_service()
    return await _safe(service.index_search(query, chat=chat, limit=limit, mode=mode))
