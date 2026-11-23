from __future__ import annotations

import asyncio
from typing import Any

from telethon import errors


class TgSearchError(Exception):
    """An error with a stable machine-readable code and an agent-friendly hint."""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        hint: str | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.hint = hint
        self.retry_after = retry_after

    def payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ok": False,
            "error": self.code,
            "message": self.message,
        }
        if self.hint:
            out["hint"] = self.hint
        if self.retry_after:
            out["retry_after"] = self.retry_after
        return out


_ERRORS: tuple[tuple[type[BaseException], str, str | None], ...] = (
    (errors.FloodWaitError, "flood_wait", "Telegram rate limit hit. Wait and retry; reduce the request rate or the requested range."),
    (errors.FloodPremiumWaitError, "flood_wait", "Telegram rate limit hit for this peer. Wait and retry."),
    (errors.SessionPasswordNeededError, "2fa_required", "This account has two-step verification. Run `tgsearch-mcp login` and enter the password."),
    (errors.AuthKeyUnregisteredError, "session_invalid", "The session was revoked or is unknown to Telegram. Run `tgsearch-mcp login` again."),
    (errors.AuthKeyDuplicatedError, "session_invalid", "The session was used from another place and revoked. Run `tgsearch-mcp login` again."),
    (errors.SessionExpiredError, "session_invalid", "The session expired. Run `tgsearch-mcp login` again."),
    (errors.SessionRevokedError, "session_invalid", "The session was revoked. Run `tgsearch-mcp login` again."),
    (errors.UserDeactivatedError, "account_unavailable", "The Telegram account is deactivated."),
    (errors.UserDeactivatedBanError, "account_unavailable", "The Telegram account is banned."),
    (errors.UsernameNotOccupiedError, "chat_not_found", "No public chat or user has this username. Use `tg_find_chats` to discover the right reference."),
    (errors.UsernameInvalidError, "invalid_username", "The username is malformed."),
    (errors.ChannelPrivateError, "chat_private", "The chat is private or this account is banned there. This server never joins chats; join it in a Telegram app first."),
    (errors.ChannelInvalidError, "chat_not_found", "Unknown or inaccessible channel. Use `tg_find_chats`."),
    (errors.PeerIdInvalidError, "chat_not_found", "Unknown chat id for this account. Use `tg_find_chats` to get a valid reference."),
    (errors.MsgIdInvalidError, "message_not_found", "Message id not found in this chat."),
    (errors.MessageIdInvalidError, "message_not_found", "Message id not found in this chat."),
    (errors.TopicDeletedError, "topic_not_found", "The forum topic was deleted."),
    (errors.ChannelForumMissingError, "not_a_forum", "This chat has no forum topics."),
    (errors.ChatAdminRequiredError, "admin_required", "Telegram requires admin rights for this operation."),
    (errors.SlowModeWaitError, "slow_mode", "The chat has slow mode enabled."),
    (errors.InviteHashExpiredError, "invite_link_unsupported", "Invite link expired. Join the chat in a Telegram app, then use its @username or numeric id."),
    (errors.InviteHashInvalidError, "invite_link_unsupported", "Invalid invite link. Join the chat in a Telegram app, then use its @username or numeric id."),
)


def error_payload(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, TgSearchError):
        return exc.payload()

    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return TgSearchError(
            "timeout",
            "The Telegram request timed out.",
            "Retry with a smaller limit or a narrower range.",
        ).payload()

    if isinstance(exc, (ConnectionError, OSError)):
        return TgSearchError(
            "connection_error",
            f"Could not reach Telegram: {exc}",
            "Check the network/proxy settings and retry.",
        ).payload()

    for klass, code, hint in _ERRORS:
        if isinstance(exc, klass):
            retry_after = None
            if isinstance(exc, (errors.FloodWaitError, errors.FloodPremiumWaitError, errors.SlowModeWaitError)):
                retry_after = int(getattr(exc, "seconds", 0) or 0) or None
            return TgSearchError(code, str(exc), hint, retry_after).payload()

    if isinstance(exc, errors.RPCError):
        return TgSearchError(
            "telegram_error",
            f"{type(exc).__name__}: {exc}",
            "Telegram rejected the request. Check the chat reference and arguments.",
        ).payload()

    return TgSearchError(
        "internal_error",
        f"{type(exc).__name__}: {exc}",
    ).payload()
