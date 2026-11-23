from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any

from telethon import types, utils


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def iso_from_ts(value: int | float | None) -> str | None:
    if value is None:
        return None
    return iso(datetime.fromtimestamp(float(value), tz=timezone.utc))


def parse_date(value: Any, *, end_of_day: bool = False) -> datetime | None:
    """Accept ISO 8601 strings, date-only strings and unix timestamps."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime.combine(
            value, time.max if end_of_day else time.min, tzinfo=timezone.utc
        )
    elif isinstance(value, (int, float)):
        result = datetime.fromtimestamp(float(value), tz=timezone.utc)
    elif isinstance(value, str):
        raw = value.strip()
        try:
            result = datetime.fromtimestamp(float(raw), tz=timezone.utc)
        except ValueError:
            try:
                result = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(
                    f"invalid date {value!r}; use ISO 8601 (2025-01-31) or a unix timestamp"
                ) from exc
            if result.tzinfo is None:
                result = result.replace(tzinfo=timezone.utc)
            if end_of_day and len(raw) <= 10:
                result = datetime.combine(result.date(), time.max, tzinfo=timezone.utc)
    else:
        raise ValueError(f"invalid date {value!r}")
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def truncate(text: str | None, limit: int | None) -> tuple[str, bool]:
    text = text or ""
    if not limit or limit <= 0 or len(text) <= limit:
        return text, False
    return text[:limit].rstrip() + "…", True


def chat_username(entity: Any) -> str | None:
    username = getattr(entity, "username", None)
    if username:
        return str(username)
    for item in getattr(entity, "usernames", None) or []:
        if getattr(item, "active", True) and getattr(item, "username", None):
            return str(item.username)
    return None


def chat_kind(entity: Any) -> str:
    if isinstance(entity, types.User):
        return "bot" if getattr(entity, "bot", False) else "user"
    if isinstance(entity, types.Chat):
        return "group"
    if isinstance(entity, types.Channel):
        return "channel" if getattr(entity, "broadcast", False) else "supergroup"
    return "unknown"


def chat_title(entity: Any) -> str:
    try:
        name = utils.get_display_name(entity)
    except Exception:
        name = None
    return name or chat_username(entity) or str(getattr(entity, "id", ""))


def chat_id(entity: Any) -> int | None:
    try:
        return utils.get_peer_id(entity)
    except Exception:
        value = getattr(entity, "id", None)
        return int(value) if isinstance(value, int) else None


def chat_ref(entity: Any) -> str:
    username = chat_username(entity)
    if username:
        return f"@{username}"
    return str(chat_id(entity))


def private_internal_id(chat_id_value: int | None) -> str | None:
    try:
        text = str(int(chat_id_value))
    except (TypeError, ValueError):
        return None
    if text.startswith("-100"):
        return text[4:]
    return None


def message_link(
    chat_id_value: int | None,
    username: str | None,
    msg_id: int | None,
    topic_id: int | None = None,
) -> str | None:
    if not msg_id:
        return None
    username = (username or "").lstrip("@")
    if username:
        if topic_id and topic_id != msg_id:
            return f"https://t.me/{username}/{topic_id}/{msg_id}"
        return f"https://t.me/{username}/{msg_id}"
    internal = private_internal_id(chat_id_value)
    if internal:
        if topic_id and topic_id != msg_id:
            return f"https://t.me/c/{internal}/{topic_id}/{msg_id}"
        return f"https://t.me/c/{internal}/{msg_id}"
    return None


def chat_link(entity: Any) -> str | None:
    username = chat_username(entity)
    if username:
        return f"https://t.me/{username}"
    internal = private_internal_id(chat_id(entity))
    if internal:
        return f"https://t.me/c/{internal}"
    return None


def topic_id_of(message: Any) -> int | None:
    reply = getattr(message, "reply_to", None)
    if reply is None:
        return None
    top = getattr(reply, "reply_to_top_id", None)
    return int(top) if top else None


def sender_peer_id(message: Any) -> int | None:
    from_id = getattr(message, "from_id", None)
    if from_id is None:
        return None
    try:
        return utils.get_peer_id(from_id)
    except Exception:
        return None


def entity_maps(users: Any, chats: Any) -> tuple[dict[int, Any], dict[int, Any]]:
    user_map: dict[int, Any] = {}
    for entity in users or []:
        key = chat_id(entity)
        if key is not None:
            user_map[key] = entity
    chat_map: dict[int, Any] = {}
    for entity in chats or []:
        key = chat_id(entity)
        if key is not None:
            chat_map[key] = entity
    return user_map, chat_map


def is_message(message: Any) -> bool:
    return isinstance(message, types.Message)


def sender_name(
    message: Any,
    users: dict[int, Any] | None = None,
    chats: dict[int, Any] | None = None,
    fallback_chat_title: str | None = None,
) -> str | None:
    sender = getattr(message, "_sender", None)
    if sender is not None:
        return chat_title(sender)
    peer_id = sender_peer_id(message)
    if peer_id is not None:
        entity = (users or {}).get(peer_id) or (chats or {}).get(peer_id)
        if entity is not None:
            return chat_title(entity)
    if getattr(message, "post", False) and fallback_chat_title:
        return fallback_chat_title
    return None


def message_entry(
    message: Any,
    *,
    chat_entity: Any = None,
    users: dict[int, Any] | None = None,
    chats: dict[int, Any] | None = None,
    max_text: int = 400,
) -> dict[str, Any]:
    if chat_entity is not None:
        chat_id_value = chat_id(chat_entity)
        username = chat_username(chat_entity)
    else:
        try:
            chat_id_value = utils.get_peer_id(message.peer_id)
        except Exception:
            chat_id_value = None
        username = None

    topic_id = topic_id_of(message)
    text, was_truncated = truncate(getattr(message, "message", None), max_text)
    entry: dict[str, Any] = {
        "id": message.id,
        "date": iso(getattr(message, "date", None)),
    }
    if text:
        entry["text"] = text
    if was_truncated:
        entry["text_truncated"] = True
    sender = sender_name(message, users, chats, chat_title(chat_entity) if chat_entity is not None else None)
    if sender:
        entry["sender"] = sender
    if getattr(message, "media", None) is not None:
        entry["has_media"] = True
    if topic_id:
        entry["topic_id"] = topic_id
    link = message_link(chat_id_value, username, message.id, topic_id)
    if link:
        entry["link"] = link
    views = getattr(message, "views", None)
    if views is not None:
        entry["views"] = views
    if getattr(message, "edited", False):
        entry["edited"] = iso(getattr(message, "edit_date", None))
    reply = getattr(message, "reply_to", None)
    if reply is not None:
        reply_to = getattr(reply, "reply_to_msg_id", None)
        if reply_to and reply_to != topic_id:
            entry["reply_to"] = reply_to
    return entry


def chat_entry(
    entity: Any,
    *,
    source: str | None = None,
    is_member: bool | None = None,
    archived: bool | None = None,
    about: str | None = None,
    members: int | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "chat_id": chat_id(entity),
        "type": chat_kind(entity),
        "title": chat_title(entity),
        "ref": chat_ref(entity),
    }
    username = chat_username(entity)
    if username:
        entry["username"] = username
    link = chat_link(entity)
    if link:
        entry["link"] = link
    member_count = members if members is not None else getattr(entity, "participants_count", None)
    if member_count is not None:
        entry["members"] = member_count
    for flag in ("verified", "scam", "fake", "restricted", "join_request"):
        if getattr(entity, flag, None):
            entry[flag] = True
    if getattr(entity, "forum", False):
        entry["forum"] = True
    if is_member is not None:
        entry["is_member"] = is_member
    if archived:
        entry["archived"] = True
    if source:
        entry["source"] = source
    if about:
        entry["about"] = about
    return entry


def topic_entry(topic: Any, chat_entity: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "topic_id": topic.id,
        "title": topic.title,
        "date": iso(getattr(topic, "date", None)),
        "top_message": getattr(topic, "top_message", None),
    }
    if getattr(topic, "closed", None):
        entry["closed"] = True
    if getattr(topic, "pinned", None):
        entry["pinned"] = True
    unread = getattr(topic, "unread_count", None)
    if unread:
        entry["unread"] = unread
    link = message_link(chat_id(chat_entity), chat_username(chat_entity), topic.id)
    if link:
        entry["link"] = link
    return entry
