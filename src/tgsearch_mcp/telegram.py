from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from telethon import TelegramClient, errors, functions, types, utils
from telethon.sessions import StringSession

from . import __version__
from .config import Config, index_path
from .errors import TgSearchError, error_payload
from .formatting import (
    chat_entry,
    chat_id,
    chat_kind,
    chat_title,
    chat_username,
    entity_maps,
    is_message,
    iso,
    message_entry,
    parse_date,
    sender_name,
    sender_peer_id,
    topic_entry,
    topic_id_of,
    user_entry,
)
from .index import MessageIndex

log = logging.getLogger(__name__)

FILTERS: dict[str, type] = {
    "any": types.InputMessagesFilterEmpty,
    "text": types.InputMessagesFilterEmpty,
    "photos": types.InputMessagesFilterPhotos,
    "videos": types.InputMessagesFilterVideo,
    "documents": types.InputMessagesFilterDocument,
    "links": types.InputMessagesFilterUrl,
    "music": types.InputMessagesFilterMusic,
    "voice": types.InputMessagesFilterVoice,
    "pinned": types.InputMessagesFilterPinned,
}


def clamp(value: int, low: int, high: int) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = low
    return max(low, min(high, value))


def _participant_role(participant: Any) -> str | None:
    if isinstance(participant, (types.ChannelParticipantCreator, types.ChatParticipantCreator)):
        return "creator"
    if isinstance(participant, (types.ChannelParticipantAdmin, types.ChatParticipantAdmin)):
        return "admin"
    return None


def normalize_chat_ref(ref: str) -> str | int:
    text = str(ref).strip()
    if not text:
        raise TgSearchError("invalid_chat", "Empty chat reference.")
    lowered = text.lower()
    if lowered.startswith("tg://resolve"):
        marker = "domain="
        if marker in lowered:
            text = text[lowered.index(marker) + len(marker) :].split("&", 1)[0]
            lowered = text.lower()
    if "t.me/" in lowered:
        tail = text[lowered.index("t.me/") + len("t.me/") :]
        tail = tail.split("?", 1)[0].strip("/")
        parts = [part for part in tail.split("/") if part]
        if not parts:
            raise TgSearchError("invalid_chat", f"Cannot parse chat link: {ref!r}")
        if parts[0] == "c" and len(parts) >= 2 and parts[1].isdigit():
            return int(f"-100{parts[1]}")
        if parts[0] == "s" and len(parts) >= 2:
            return parts[1]
        if parts[0] in {"joinchat", "addstickers", "addemoji"} or parts[0].startswith("+"):
            raise TgSearchError(
                "invite_link_unsupported",
                "Invite links cannot be used as a chat reference.",
                "Join the chat in a Telegram app, then use its @username or numeric id.",
            )
        return parts[0]
    if text.startswith("@"):
        return text
    if text.lstrip("-").isdigit():
        return int(text)
    return text


def _rank(query: str, title: str, username: str, chat_id_value: int | None) -> int:
    if not query:
        return 0
    title_cf = (title or "").casefold()
    username_cf = (username or "").casefold()
    if title_cf == query or username_cf == query or str(chat_id_value) == query:
        return 0
    if title_cf.startswith(query) or username_cf.startswith(query):
        return 1
    if any(word.startswith(query) for word in title_cf.replace("-", " ").split()):
        return 2
    return 3


def _window_around(messages: list[Any], around_id: int, limit: int) -> list[Any]:
    """Keep a symmetric window of `limit` messages centred on `around_id`."""
    ordered = sorted(messages, key=lambda message: message.id)
    if len(ordered) <= limit:
        return ordered
    pivot = min(range(len(ordered)), key=lambda index: abs(ordered[index].id - around_id))
    start = max(0, pivot - limit // 2)
    end = start + limit
    if end > len(ordered):
        end = len(ordered)
        start = max(0, end - limit)
    return ordered[start:end]


async def _drain(iterator) -> list[Any]:
    return [item async for item in iterator]


class TelegramService:
    def __init__(self, cfg: Config, index: MessageIndex | None = None) -> None:
        self.cfg = cfg
        self._index = index
        self._index_path = index_path()
        self._client: TelegramClient | None = None
        self._connect_lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(3)
        self._entity_cache: dict[str, tuple[float, Any]] = {}
        self._dialogs: list[tuple[Any, bool]] | None = None
        self._dialogs_ts = 0.0
        self._dialog_ids: set[int] = set()
        self._me = None

    @property
    def index(self) -> MessageIndex:
        if self._index is None:
            self._index = MessageIndex(self._index_path)
        return self._index

    async def client(self) -> TelegramClient:
        if self._client is not None and self._client.is_connected():
            return self._client
        async with self._connect_lock:
            if self._client is not None and self._client.is_connected():
                return self._client
            self.cfg.require_api()
            if not self.cfg.session_configured:
                raise TgSearchError(
                    "not_authorized",
                    "No Telegram session is configured.",
                    "Run `tgsearch-mcp login` in a terminal (QR or phone code).",
                )
            if self._client is None:
                try:
                    session = StringSession(self.cfg.session or "")
                except Exception as exc:
                    raise TgSearchError(
                        "session_invalid",
                        f"Cannot parse the stored session: {exc}",
                        "Run `tgsearch-mcp login` again.",
                    ) from exc
                self._client = TelegramClient(
                    session,
                    self.cfg.api_id,
                    self.cfg.api_hash,
                    device_model=self.cfg.device_model,
                    system_version=self.cfg.system_version,
                    app_version=self.cfg.app_version,
                    receive_updates=False,
                    flood_sleep_threshold=2,
                    connection_retries=3,
                    retry_delay=1,
                    timeout=self.cfg.timeout,
                )
            await self._client.connect()
            if not self._client.is_connected():
                raise TgSearchError("connection_error", "Could not connect to Telegram.")
            if not await self._client.is_user_authorized():
                raise TgSearchError(
                    "not_authorized",
                    "The stored Telegram session is not authorized.",
                    "Run `tgsearch-mcp login` again.",
                )
            self._me = await self._client.get_me()
            return self._client

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None
        if self._index is not None:
            self._index.close()
            self._index = None

    async def _run(self, factory: Callable[[TelegramClient], Awaitable[Any]], timeout: float | None = None):
        client = await self.client()
        limit = timeout or self.cfg.timeout
        async with self._sem:
            try:
                return await asyncio.wait_for(factory(client), limit)
            except errors.FloodWaitError as exc:
                if exc.seconds > 2:
                    raise
                await asyncio.sleep(exc.seconds + 0.5)
                return await asyncio.wait_for(factory(client), limit)

    async def _collect(self, iterator) -> list[Any]:
        async with self._sem:
            return await asyncio.wait_for(_drain(iterator), self.cfg.timeout)

    async def get_me(self):
        if self._me is not None:
            return self._me
        client = await self.client()
        self._me = await client.get_me()
        return self._me

    async def dialogs(self, refresh: bool = False) -> list[tuple[Any, bool]]:
        now = time.monotonic()
        if not refresh and self._dialogs is not None and now - self._dialogs_ts < self.cfg.dialog_ttl:
            return self._dialogs
        limit = self.cfg.dialog_limit if self.cfg.dialog_limit > 0 else None
        items: list[tuple[Any, bool]] = []
        main = await self._run(lambda client: client.get_dialogs(limit=limit))
        items.extend((dialog, False) for dialog in main)
        if self.cfg.include_archived:
            try:
                archived = await self._run(lambda client: client.get_dialogs(limit=limit, archived=True))
                items.extend((dialog, True) for dialog in archived)
            except Exception as exc:
                log.warning("archived dialogs unavailable: %s", exc)
        seen: set[int] = set()
        unique: list[tuple[Any, bool]] = []
        for dialog, archived in items:
            if dialog.id in seen:
                continue
            seen.add(dialog.id)
            unique.append((dialog, archived))
        self._dialogs = unique
        self._dialogs_ts = time.monotonic()
        self._dialog_ids = seen
        return unique

    async def resolve(self, ref: str, refresh: bool = False):
        key = str(ref).strip()
        if not key:
            raise TgSearchError("invalid_chat", "Empty chat reference.")
        now = time.monotonic()
        cached = self._entity_cache.get(key)
        if cached and not refresh and now - cached[0] < 600:
            return cached[1]
        target = normalize_chat_ref(key)
        try:
            entity = await self._run(lambda client: client.get_entity(target))
        except (errors.PeerIdInvalidError, errors.UsernameNotOccupiedError, errors.UsernameInvalidError, ValueError):
            await self.dialogs()
            try:
                entity = await self._run(lambda client: client.get_entity(target))
            except Exception as exc:
                raise TgSearchError(
                    "chat_not_found",
                    f"Chat not found: {ref!r}",
                    "Use `tg_find_chats` to discover a valid @username or id.",
                ) from exc
        if entity is None:
            raise TgSearchError(
                "chat_not_found",
                f"Chat not found: {ref!r}",
                "Use `tg_find_chats` to discover a valid @username or id.",
            )
        self._entity_cache[key] = (time.monotonic(), entity)
        return entity

    def _is_member(self, entity) -> bool | None:
        if not self._dialog_ids:
            return None
        return chat_id(entity) in self._dialog_ids

    def _add_entity(self, users: dict[int, Any], chats: dict[int, Any], entity) -> None:
        key = chat_id(entity)
        if key is None:
            return
        if isinstance(entity, types.User):
            users.setdefault(key, entity)
        else:
            chats.setdefault(key, entity)

    async def _sender_maps(self, messages, users=None, chats=None):
        user_map, chat_map = entity_maps(users, chats)
        missing: list[int] = []
        seen: set[int] = set()
        for message in messages:
            if getattr(message, "_sender", None) is not None:
                continue
            peer_id = sender_peer_id(message)
            if peer_id is None or peer_id in user_map or peer_id in chat_map or peer_id in seen:
                continue
            seen.add(peer_id)
            missing.append(peer_id)
        for peer_id in missing[:20]:
            try:
                entity = await self._run(lambda client, pid=peer_id: client.get_entity(pid))
            except Exception:
                continue
            self._add_entity(user_map, chat_map, entity)
        return user_map, chat_map

    def _entries(self, messages, entity, users, chats, max_text: int) -> list[dict[str, Any]]:
        return [
            message_entry(message, chat_entity=entity, users=users, chats=chats, max_text=max_text)
            for message in messages
        ]

    async def find_chats(
        self,
        query: str = "",
        scope: str = "all",
        limit: int = 20,
        refresh: bool = False,
        active_within_days: int | None = None,
        sort: str = "relevance",
    ) -> dict[str, Any]:
        if scope not in {"all", "dialogs", "public"}:
            raise TgSearchError("invalid_argument", "scope must be one of: all, dialogs, public.")
        if sort not in {"relevance", "activity"}:
            raise TgSearchError(
                "invalid_argument", "sort must be one of: relevance, activity."
            )
        limit = clamp(limit, 1, 50)
        days = None if active_within_days is None else clamp(active_within_days, 0, 3650)
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=days) if days is not None else None
        )
        needle = (query or "").strip()
        folded = needle.casefold()
        dialogs = await self.dialogs(refresh=refresh)
        ranked = []
        if scope in {"all", "dialogs"}:
            for dialog, archived in dialogs:
                last_date = dialog.date
                if last_date is not None and last_date.tzinfo is None:
                    last_date = last_date.replace(tzinfo=timezone.utc)
                if cutoff is not None and (last_date is None or last_date < cutoff):
                    continue
                entity = dialog.entity
                title = chat_title(entity)
                username = chat_username(entity) or ""
                if folded and not (
                    folded in title.casefold()
                    or folded in username.casefold()
                    or folded in str(dialog.id)
                ):
                    continue
                rank = _rank(folded, title, username, dialog.id)
                last_message = getattr(dialog, "message", None)
                entry = chat_entry(
                    entity,
                    source="dialogs",
                    is_member=True,
                    archived=archived,
                    last_message_date=iso(last_date),
                    last_message_id=getattr(last_message, "id", None),
                )
                if dialog.unread_count:
                    entry["unread"] = dialog.unread_count
                sort_date = last_date or datetime.min.replace(tzinfo=timezone.utc)
                ranked.append((rank, sort_date.timestamp(), entry))
        if sort == "activity":
            ranked.sort(key=lambda item: (-item[1], item[0]))
        else:
            ranked.sort(key=lambda item: (item[0], -item[1]))
        results = [item[2] for item in ranked[:limit]]

        public_error = None
        if scope in {"all", "public"} and needle:
            try:
                public = await self._public_search(needle, limit)
            except Exception as exc:
                public = []
                public_error = error_payload(exc)
            existing = {item["chat_id"] for item in results}
            for entry in public:
                if entry["chat_id"] in existing:
                    continue
                existing.add(entry["chat_id"])
                results.append(entry)
                if len(results) >= limit * 2:
                    break

        response: dict[str, Any] = {
            "ok": True,
            "query": query or "",
            "count": len(results),
            "sort": sort,
            "chats": results,
        }
        if days is not None:
            response["active_within_days"] = days
        if public_error:
            response["public_search_error"] = public_error
        if scope == "public" and not needle:
            response["note"] = (
                "scope='public' needs a non-empty query: the public directory is searched by "
                "name or @username, not by message content."
            )
        elif not results:
            response["note"] = (
                "No chats matched. Try another keyword, or scope='public' to search the public directory."
            )
        return response

    async def _public_search(self, query: str, limit: int) -> list[dict[str, Any]]:
        found = await self._run(
            lambda client: client(functions.contacts.SearchRequest(q=query, limit=limit))
        )
        order: dict[int, int] = {}
        for index, peer in enumerate(found.results or []):
            try:
                order[utils.get_peer_id(peer)] = index
            except Exception:
                continue
        entities = list(found.chats or []) + list(found.users or [])
        entities.sort(key=lambda entity: order.get(chat_id(entity), 999))
        return [
            chat_entry(entity, source="public", is_member=chat_id(entity) in self._dialog_ids)
            for entity in entities[:limit]
        ]

    async def chat_info(self, chat: str) -> dict[str, Any]:
        entity = await self.resolve(chat)
        entry = chat_entry(entity, is_member=self._is_member(entity))
        if isinstance(entity, types.Channel):
            full = await self._run(
                lambda client: client(functions.channels.GetFullChannelRequest(channel=entity))
            )
            full_chat = full.full_chat
            extra = {
                "about": getattr(full_chat, "about", None),
                "members": getattr(full_chat, "participants_count", None),
                "pinned_msg_id": getattr(full_chat, "pinned_msg_id", None),
                "linked_chat_id": getattr(full_chat, "linked_chat_id", None),
                "slowmode_seconds": getattr(full_chat, "slowmode_seconds", None),
                "forum": bool(getattr(entity, "forum", False)) or None,
            }
            entry.update({key: value for key, value in extra.items() if value not in (None, False, 0)})
        elif isinstance(entity, types.Chat):
            full = await self._run(
                lambda client: client(functions.messages.GetFullChatRequest(chat_id=entity.id))
            )
            full_chat = full.full_chat
            participants = getattr(getattr(full_chat, "participants", None), "participants", None)
            extra = {
                "about": getattr(full_chat, "about", None),
                "members": len(participants) if participants else None,
                "pinned_msg_id": getattr(full_chat, "pinned_msg_id", None),
            }
            entry.update({key: value for key, value in extra.items() if value not in (None, False, 0)})
        elif isinstance(entity, types.User):
            extra = {
                "bot": bool(getattr(entity, "bot", False)) or None,
                "verified": bool(getattr(entity, "verified", False)) or None,
                "premium": bool(getattr(entity, "premium", False)) or None,
            }
            entry.update({key: value for key, value in extra.items() if value})
        return {"ok": True, "chat": entry}

    def _members_hidden(self, entity: Any, reason: str) -> dict[str, Any]:
        return {
            "ok": True,
            "chat": chat_entry(entity),
            "count": 0,
            "members": [],
            "participants_hidden": True,
            "note": f"{reason} tg_get_chat still reports the member count.",
        }

    async def chat_members(
        self,
        chat: str,
        query: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        entity = await self.resolve(chat)
        limit = clamp(limit, 1, 200)
        offset = max(0, int(offset or 0))
        needle = (query or "").strip()
        next_offset = None
        total = None
        if isinstance(entity, types.Channel):
            try:
                result = await self._run(
                    lambda client: client(
                        functions.channels.GetParticipantsRequest(
                            channel=entity,
                            filter=types.ChannelParticipantsSearch(q=needle),
                            offset=offset,
                            limit=limit,
                            hash=0,
                        )
                    )
                )
            except (errors.ChatAdminRequiredError, errors.ChannelPrivateError):
                return self._members_hidden(
                    entity,
                    "Telegram does not expose the member list of this chat to this account.",
                )
            users, _ = entity_maps(result.users, result.chats)
            participants = list(result.participants or [])
            members = []
            for participant in participants:
                user_id = getattr(participant, "user_id", None)
                if user_id is None:
                    peer = getattr(participant, "peer", None)
                    user_id = chat_id(peer) if isinstance(peer, types.PeerUser) else None
                if user_id is None:
                    continue
                members.append(
                    user_entry(
                        users.get(int(user_id)),
                        user_id=int(user_id),
                        role=_participant_role(participant),
                    )
                )
            total = getattr(result, "count", None)
            if total is not None:
                if offset + len(participants) < total:
                    next_offset = offset + len(participants)
            elif len(participants) >= limit:
                next_offset = offset + len(participants)
        elif isinstance(entity, types.Chat):
            full = await self._run(
                lambda client: client(functions.messages.GetFullChatRequest(chat_id=entity.id))
            )
            full_chat = full.full_chat
            items = getattr(getattr(full_chat, "participants", None), "participants", None)
            if items is None:
                return self._members_hidden(
                    entity,
                    "Telegram does not expose the member list of this group to this account.",
                )
            users, _ = entity_maps(getattr(full, "users", None), getattr(full, "chats", None))
            folded = needle.casefold()
            selected = []
            for item in items:
                user_id = int(getattr(item, "user_id", 0) or 0)
                user = users.get(user_id)
                if folded:
                    haystack = " ".join(
                        part
                        for part in (
                            chat_title(user) if user is not None else "",
                            chat_username(user) if user is not None else "",
                            str(user_id),
                        )
                        if part
                    ).casefold()
                    if folded not in haystack:
                        continue
                selected.append((item, user_id, user))
            total = len(selected)
            page = selected[offset : offset + limit]
            members = [
                user_entry(user, user_id=user_id, role=_participant_role(item))
                for item, user_id, user in page
            ]
            if offset + len(page) < total:
                next_offset = offset + len(page)
        else:
            return {
                "ok": True,
                "chat": chat_entry(entity),
                "count": 0,
                "members": [],
                "note": "Only groups and supergroups have member lists.",
            }
        response: dict[str, Any] = {
            "ok": True,
            "chat": chat_entry(entity),
            "count": len(members),
            "members": members,
        }
        if total is not None:
            response["total"] = total
        if next_offset is not None:
            response["next_offset"] = next_offset
        if not members:
            response["note"] = (
                "No members matched. Telegram only exposes the users it shows to this account."
            )
        return response

    async def list_contacts(
        self,
        query: str | None = None,
        limit: int = 50,
        offset: int = 0,
        include_phones: bool = False,
    ) -> dict[str, Any]:
        result = await self._run(
            lambda client: client(functions.contacts.GetContactsRequest(hash=0))
        )
        users = [user for user in result.users if isinstance(user, types.User)]
        needle = (query or "").strip().casefold()
        if needle:
            users = [
                user
                for user in users
                if needle in (chat_title(user) or "").casefold()
                or needle in (chat_username(user) or "").casefold()
                or needle in str(user.id)
            ]
        users.sort(key=lambda user: (chat_title(user) or "").casefold())
        limit = clamp(limit, 1, 500)
        offset = max(0, int(offset or 0))
        total = len(users)
        page = users[offset : offset + limit]
        contacts = [user_entry(user, include_phone=include_phones) for user in page]
        response: dict[str, Any] = {
            "ok": True,
            "query": query or "",
            "total": total,
            "returned": len(contacts),
            "contacts": contacts,
        }
        if offset + len(page) < total:
            response["next_offset"] = offset + len(page)
        if not contacts:
            response["note"] = (
                "No contacts matched. This is the account's contact list, not chat members."
            )
        return response

    async def list_topics(
        self,
        chat: str,
        query: str | None = None,
        limit: int = 50,
        offset_topic: int = 0,
    ) -> dict[str, Any]:
        entity = await self.resolve(chat)
        if not isinstance(entity, types.Channel) or not getattr(entity, "forum", False):
            return {
                "ok": True,
                "chat": chat_entry(entity),
                "count": 0,
                "topics": [],
                "note": "This chat is not a forum with topics.",
            }
        limit = clamp(limit, 1, 100)
        result = await self._run(
            lambda client: client(
                functions.messages.GetForumTopicsRequest(
                    peer=entity,
                    offset_date=None,
                    offset_id=0,
                    offset_topic=int(offset_topic or 0),
                    limit=limit,
                    q=query,
                )
            )
        )
        topics = [topic_entry(topic, entity) for topic in result.topics]
        response: dict[str, Any] = {
            "ok": True,
            "chat": chat_entry(entity),
            "count": len(topics),
            "total": getattr(result, "count", None),
            "topics": topics,
        }
        if len(topics) >= limit and topics:
            response["next_offset_topic"] = topics[-1]["topic_id"]
        if not topics:
            response["note"] = "No topics matched."
        return response

    async def search_messages(
        self,
        chat: str,
        query: str = "",
        limit: int = 20,
        offset_id: int = 0,
        kind: str = "any",
        topic_id: int | None = None,
        sender: str | None = None,
        min_date: str | None = None,
        max_date: str | None = None,
        max_text_chars: int = 400,
    ) -> dict[str, Any]:
        query = (query or "").strip()
        if not query and kind == "any" and not topic_id:
            raise TgSearchError(
                "invalid_argument",
                "Provide a non-empty query, or use kind/topic_id for a filter-only search.",
            )
        if kind not in FILTERS:
            raise TgSearchError(
                "invalid_argument",
                f"Unknown kind {kind!r}. Use one of: {', '.join(FILTERS)}.",
            )
        entity = await self.resolve(chat)
        peer = utils.get_input_peer(entity)
        from_peer = None
        if sender:
            from_peer = utils.get_input_peer(await self.resolve(sender))
        limit = clamp(limit, 1, 50)
        kwargs: dict[str, Any] = {
            "peer": peer,
            "q": query,
            "filter": FILTERS[kind](),
            "min_date": parse_date(min_date),
            "max_date": parse_date(max_date, end_of_day=True),
            "offset_id": int(offset_id or 0),
            "add_offset": 0,
            "limit": limit,
            "max_id": 0,
            "min_id": 0,
            "hash": 0,
            "from_id": from_peer,
        }
        if topic_id:
            kwargs["top_msg_id"] = int(topic_id)
        result = await self._run(
            lambda client: client(functions.messages.SearchRequest(**kwargs))
        )
        messages = [message for message in result.messages if is_message(message)]
        users, chats = entity_maps(result.users, result.chats)
        self._add_entity(users, chats, entity)
        entries = self._entries(messages, entity, users, chats, max_text_chars)
        response: dict[str, Any] = {
            "ok": True,
            "chat": chat_entry(entity, is_member=self._is_member(entity)),
            "query": query,
            "returned": len(entries),
            "order": "newest_first",
            "messages": entries,
        }
        total = getattr(result, "count", None)
        if total is not None:
            response["total"] = total
        if len(messages) >= limit and messages:
            response["next_offset_id"] = messages[-1].id
        if not entries:
            response["note"] = (
                "No messages matched. Try a shorter query, another kind, or remove date/topic filters."
            )
        return response

    async def search_global(
        self,
        query: str,
        limit: int = 20,
        kind: str = "any",
        min_date: str | None = None,
        max_date: str | None = None,
        offset_rate: int = 0,
        offset_peer: str | None = None,
        offset_id: int = 0,
        broadcasts_only: bool | None = None,
        groups_only: bool | None = None,
    ) -> dict[str, Any]:
        query = (query or "").strip()
        if not query:
            raise TgSearchError("invalid_argument", "tg_search_global needs a non-empty query.")
        if kind not in FILTERS:
            raise TgSearchError(
                "invalid_argument",
                f"Unknown kind {kind!r}. Use one of: {', '.join(FILTERS)}.",
            )
        peer = types.InputPeerEmpty()
        if offset_peer:
            peer = utils.get_input_peer(await self.resolve(offset_peer))
        limit = clamp(limit, 1, 50)
        result = await self._run(
            lambda client: client(
                functions.messages.SearchGlobalRequest(
                    q=query,
                    filter=FILTERS[kind](),
                    min_date=parse_date(min_date),
                    max_date=parse_date(max_date, end_of_day=True),
                    offset_rate=int(offset_rate or 0),
                    offset_peer=peer,
                    offset_id=int(offset_id or 0),
                    limit=limit,
                    broadcasts_only=broadcasts_only,
                    groups_only=groups_only,
                )
            )
        )
        messages = [message for message in result.messages if is_message(message)]
        users, chats = entity_maps(result.users, result.chats)
        entries: list[dict[str, Any]] = []
        last_chat = None
        for message in messages:
            try:
                peer_id = utils.get_peer_id(message.peer_id)
            except Exception:
                peer_id = None
            chat_entity = chats.get(peer_id) or users.get(peer_id) if peer_id is not None else None
            entry = message_entry(message, chat_entity=chat_entity, users=users, chats=chats, max_text=400)
            if chat_entity is not None:
                entry["chat_id"] = chat_id(chat_entity)
                entry["chat_title"] = chat_title(chat_entity)
                entry["chat_ref"] = chat_entry(chat_entity)["ref"]
                entry["chat_type"] = chat_kind(chat_entity)
                last_chat = chat_entity
            elif peer_id is not None:
                entry["chat_id"] = peer_id
            entries.append(entry)
        response: dict[str, Any] = {
            "ok": True,
            "query": query,
            "returned": len(entries),
            "order": "newest_first",
            "messages": entries,
        }
        if len(messages) >= limit and messages:
            response["next"] = {
                "offset_rate": int(getattr(result, "next_rate", 0) or 0),
                "offset_peer": chat_entry(last_chat)["ref"] if last_chat is not None else None,
                "offset_id": messages[-1].id,
            }
        if not entries:
            response["note"] = (
                "No messages matched across your chats. Public channels you have not joined are not included; "
                "open the channel and use tg_search_messages instead."
            )
        return response

    async def get_messages(
        self,
        chat: str,
        limit: int = 50,
        ids: list[int] | None = None,
        before_id: int | None = None,
        after_id: int | None = None,
        around_id: int | None = None,
        topic_id: int | None = None,
        min_date: str | None = None,
        max_date: str | None = None,
        max_text_chars: int = 800,
    ) -> dict[str, Any]:
        limit = clamp(limit, 1, 200)
        if ids and any(value is not None for value in (before_id, after_id, around_id)):
            raise TgSearchError("invalid_argument", "Use either ids or before_id/after_id/around_id, not both.")
        chosen = [value for value in (before_id, after_id, around_id) if value is not None]
        if len(chosen) > 1:
            raise TgSearchError("invalid_argument", "Use only one of before_id, after_id or around_id.")

        entity = await self.resolve(chat)
        min_dt = parse_date(min_date)
        max_dt = parse_date(max_date, end_of_day=True)
        users: dict[int, Any] = {}
        chats: dict[int, Any] = {}
        messages: list[Any] = []

        if ids:
            requested = [int(value) for value in ids]
            result = await self._run(lambda client: client.get_messages(entity, ids=requested))
            if not isinstance(result, list):
                result = [result]
            messages = [message for message in result if message is not None and is_message(message)]
        elif around_id is not None:
            half = max(1, limit // 2)
            older = await self._collect(
                (await self.client()).iter_messages(
                    entity,
                    limit=half + 1,
                    offset_id=int(around_id) + 1,
                    reply_to=topic_id,
                )
            )
            newer = await self._collect(
                (await self.client()).iter_messages(
                    entity,
                    limit=limit - half,
                    min_id=int(around_id),
                    reverse=True,
                    reply_to=topic_id,
                )
            )
            merged = {message.id: message for message in older + newer}
            messages = sorted(merged.values(), key=lambda message: message.id)
        elif after_id is not None:
            fetch = limit * 4 if (min_dt or max_dt) else limit
            messages = await self._collect(
                (await self.client()).iter_messages(
                    entity,
                    limit=min(fetch, 400),
                    min_id=int(after_id),
                    reverse=True,
                    reply_to=topic_id,
                )
            )
        else:
            fetch = limit * 4 if min_dt else limit
            peer = utils.get_input_peer(entity)
            if topic_id:
                result = await self._run(
                    lambda client: client(
                        functions.messages.GetRepliesRequest(
                            peer=peer,
                            msg_id=int(topic_id),
                            offset_id=int(before_id or 0),
                            offset_date=max_dt,
                            add_offset=0,
                            limit=min(fetch, 400),
                            max_id=0,
                            min_id=0,
                            hash=0,
                        )
                    )
                )
            else:
                result = await self._run(
                    lambda client: client(
                        functions.messages.GetHistoryRequest(
                            peer=peer,
                            offset_id=int(before_id or 0),
                            offset_date=max_dt,
                            add_offset=0,
                            limit=min(fetch, 400),
                            max_id=0,
                            min_id=0,
                            hash=0,
                        )
                    )
                )
            messages = [message for message in result.messages if is_message(message)]
            users, chats = entity_maps(result.users, result.chats)

        if min_dt:
            messages = [message for message in messages if message.date and message.date >= min_dt]
        if max_dt and (after_id is not None or around_id is not None):
            messages = [message for message in messages if message.date and message.date <= max_dt]
        if around_id is not None:
            messages = _window_around(messages, int(around_id), limit)
        elif after_id is not None or ids:
            messages = sorted(messages, key=lambda message: message.id)[:limit]
        else:
            messages = sorted(messages, key=lambda message: message.id, reverse=True)[:limit]
            messages.reverse()
        self._add_entity(users, chats, entity)
        user_map, chat_map = await self._sender_maps(messages, users, chats)
        entries = self._entries(messages, entity, user_map, chat_map, max_text_chars)

        response: dict[str, Any] = {
            "ok": True,
            "chat": chat_entry(entity, is_member=self._is_member(entity)),
            "returned": len(entries),
            "order": "oldest_first",
            "messages": entries,
        }
        if ids:
            response["requested_ids"] = [int(value) for value in ids]
        if messages:
            next_hint: dict[str, int] = {"older_than_id": messages[0].id}
            if after_id is not None or around_id is not None:
                next_hint["newer_than_id"] = messages[-1].id
            response["next"] = next_hint
        else:
            response["note"] = "No messages found in the requested range."
        return response

    async def index_chat(
        self,
        chat: str,
        limit: int = 5000,
        since_id: int | None = None,
        reset: bool = False,
    ) -> dict[str, Any]:
        limit = clamp(limit, 1, 20000)
        entity = await self.resolve(chat)
        chat_id_value = chat_id(entity)
        if chat_id_value is None:
            raise TgSearchError("invalid_chat", f"Cannot determine the id of {chat!r}.")
        index = self.index
        if reset:
            index.delete_chat(chat_id_value)
        min_id = int(since_id) if since_id is not None else 0
        if not reset and not min_id:
            min_id = index.last_msg_id(chat_id_value)
        index.upsert_chat(chat_id_value, chat_title(entity), chat_username(entity), chat_kind(entity))

        peer = utils.get_input_peer(entity)
        text_limit = int(os.environ.get("TGSEARCH_INDEX_TEXT_LIMIT", "10000") or 10000)
        offset_id = 0
        scanned = 0
        upserted = 0
        max_seen = index.last_msg_id(chat_id_value)
        stopped = None

        while scanned < limit:
            chunk = min(100, limit - scanned)
            try:
                result = await self._run(
                    lambda client: client(
                        functions.messages.GetHistoryRequest(
                            peer=peer,
                            offset_id=offset_id,
                            offset_date=None,
                            add_offset=0,
                            limit=chunk,
                            max_id=0,
                            min_id=0,
                            hash=0,
                        )
                    )
                )
            except errors.FloodWaitError as exc:
                stopped = {"reason": "flood_wait", "retry_after": int(exc.seconds)}
                break
            messages = [message for message in result.messages if is_message(message)]
            if not messages:
                break
            users, chats = entity_maps(result.users, result.chats)
            self._add_entity(users, chats, entity)
            rows = []
            for message in messages:
                if message.id <= min_id:
                    continue
                text = (message.message or "")[:text_limit]
                topic = topic_id_of(message)
                reply = getattr(message, "reply_to", None)
                reply_id = getattr(reply, "reply_to_msg_id", None) if reply is not None else None
                if reply_id and reply_id == topic:
                    reply_id = None
                rows.append(
                    {
                        "chat_id": chat_id_value,
                        "msg_id": message.id,
                        "date": int(message.date.timestamp()) if message.date else 0,
                        "sender_id": sender_peer_id(message),
                        "sender_name": sender_name(message, users, chats, chat_title(entity)),
                        "text": text,
                        "has_media": 1 if message.media is not None else 0,
                        "topic_id": topic,
                        "reply_to_msg_id": reply_id,
                    }
                )
            if rows:
                upserted += index.upsert_messages(rows)
                max_seen = max(max_seen, max(row["msg_id"] for row in rows))
            scanned += len(messages)
            offset_id = messages[-1].id
            if len(messages) < chunk or messages[-1].id <= min_id:
                break

        index.set_state(chat_id_value, max_seen)
        response: dict[str, Any] = {
            "ok": True,
            "chat": chat_entry(entity),
            "scanned": scanned,
            "upserted": upserted,
            "indexed_total": index.count_chat(chat_id_value),
            "last_msg_id": max_seen,
            "since_id": min_id,
            "reset": reset,
            "index": index.stats(),
        }
        if stopped:
            response["stopped"] = stopped
            response["hint"] = "Telegram asked to wait. Re-run tg_index_chat later to continue from the saved state."
        elif scanned == 0 or upserted == 0:
            response["note"] = "Nothing new to index for this chat."
        return response

    async def index_search(
        self,
        query: str,
        chat: str | None = None,
        chats: list[str] | None = None,
        limit: int = 20,
        mode: str = "auto",
        min_date: str | None = None,
        max_date: str | None = None,
        sender: str | None = None,
        has_media: bool = False,
        topic_id: int | None = None,
        sort: str = "relevance",
    ) -> dict[str, Any]:
        index = self.index
        refs = [ref for ref in ([chat] if chat else []) + list(chats or []) if ref]
        chat_ids: list[int] = []
        unresolved: list[str] = []
        chat_entity = None
        for ref in refs:
            resolved = None
            try:
                entity = await self.resolve(ref)
                resolved = chat_id(entity)
                if resolved is not None and chat_entity is None:
                    chat_entity = entity
            except Exception:
                # Offline, rate-limited or unknown ref: fall back to the local index.
                pass
            if resolved is None:
                resolved = index.find_chat(str(ref))
            if resolved is None:
                unresolved.append(str(ref))
            elif resolved not in chat_ids:
                chat_ids.append(resolved)
        if refs and not chat_ids:
            raise TgSearchError(
                "chat_not_found",
                f"No indexed chat matches: {', '.join(unresolved)}",
                "Run tg_index_chat for that chat first, or omit `chats` to search every indexed chat.",
            )
        try:
            min_dt = parse_date(min_date)
            max_dt = parse_date(max_date, end_of_day=True)
        except ValueError as exc:
            raise TgSearchError("invalid_argument", str(exc)) from exc
        options: dict[str, Any] = {
            "chat_ids": chat_ids or None,
            "limit": clamp(limit, 1, 100),
            "min_ts": int(min_dt.timestamp()) if min_dt else None,
            "max_ts": int(max_dt.timestamp()) if max_dt else None,
            "sender": sender,
            "has_media": bool(has_media),
            "topic_id": topic_id,
            "sort": sort,
        }
        try:
            results = index.search(query, mode=mode, **options)
        except ValueError as exc:
            raise TgSearchError("invalid_query", str(exc)) from exc
        relaxed = False
        if not results and mode == "auto":
            results = index.search(query, mode="any", **options)
            relaxed = bool(results)
        response: dict[str, Any] = {
            "ok": True,
            "query": query,
            "returned": len(results),
            "results": results,
            "index": index.stats(),
        }
        if chat_entity is not None and len(refs) == 1:
            response["chat"] = chat_entry(chat_entity)
        if len(refs) > 1:
            response["chat_ids"] = chat_ids
        if unresolved:
            response["unresolved_chats"] = unresolved
        if relaxed:
            response["relaxed"] = True
            response["note"] = (
                "No message matched every word: results are OR-matched, so check them "
                "before relying on them."
            )
        elif not results:
            response["note"] = (
                "No local matches. This chat may not be indexed yet: run tg_index_chat, "
                "or use tg_search_messages for server-side Telegram search."
            )
        return response

    async def status(self) -> dict[str, Any]:
        response: dict[str, Any] = {
            "ok": True,
            "version": __version__,
            "read_only": True,
            "configured": self.cfg.api_configured,
            "session_present": self.cfg.session_configured,
            "logged_in": False,
        }
        try:
            stats = self.index.stats()
            stats["chats_indexed"] = self.index.chat_stats(5)
            response["index"] = stats
        except Exception as exc:
            response["index_error"] = error_payload(exc)
        if not self.cfg.api_configured:
            response["hint"] = (
                "Create an app at https://my.telegram.org, then run `tgsearch-mcp login` "
                "or set TG_API_ID and TG_API_HASH."
            )
            return response
        if not self.cfg.session_configured:
            response["hint"] = "Run `tgsearch-mcp login` in a terminal to authorize this account."
            return response
        try:
            me = await asyncio.wait_for(self.get_me(), min(self.cfg.timeout, 20))
        except Exception as exc:
            response["error"] = error_payload(exc)
            response["hint"] = "Run `tgsearch-mcp login` if the session is missing or revoked."
            return response
        response["logged_in"] = True
        response["account"] = {
            "id": me.id,
            "name": " ".join(part for part in (me.first_name, me.last_name) if part) or None,
            "username": me.username,
        }
        return response
