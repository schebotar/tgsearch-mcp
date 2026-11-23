from datetime import datetime, timezone

import pytest
from telethon import types

from tgsearch_mcp.formatting import (
    chat_entry,
    chat_kind,
    chat_ref,
    message_entry,
    message_link,
    parse_date,
    private_internal_id,
    truncate,
)


def make_channel(**kwargs):
    defaults = dict(
        id=1234567890,
        title="Quant Channel",
        photo=types.ChatPhotoEmpty(),
        date=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    defaults.update(kwargs)
    return types.Channel(**defaults)


def make_message(**kwargs):
    defaults = dict(
        id=42,
        peer_id=types.PeerChannel(1234567890),
        date=datetime(2025, 2, 3, 12, 0, tzinfo=timezone.utc),
        message="hello world",
    )
    defaults.update(kwargs)
    return types.Message(**defaults)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2025-01-31", datetime(2025, 1, 31, tzinfo=timezone.utc)),
        ("2025-01-31T10:00:00Z", datetime(2025, 1, 31, 10, tzinfo=timezone.utc)),
        (1735689600, datetime(2025, 1, 1, tzinfo=timezone.utc)),
        ("1735689600", datetime(2025, 1, 1, tzinfo=timezone.utc)),
        (None, None),
        ("", None),
    ],
)
def test_parse_date(value, expected):
    assert parse_date(value) == expected


def test_parse_date_date_only_end_of_day():
    assert parse_date("2025-01-31", end_of_day=True) == datetime(
        2025, 1, 31, 23, 59, 59, 999999, tzinfo=timezone.utc
    )


def test_parse_date_rejects_garbage():
    with pytest.raises(ValueError):
        parse_date("not-a-date")


def test_truncate():
    assert truncate("short", 10) == ("short", False)
    text, cut = truncate("abcdefghijkl", 5)
    assert cut is True
    assert text.endswith("…")


def test_private_internal_id():
    assert private_internal_id(-1001234567890) == "1234567890"
    assert private_internal_id(-42) is None
    assert private_internal_id(None) is None


def test_message_link_prefers_username_then_private_prefix():
    assert message_link(-1001234567890, "quant_channel", 42) == "https://t.me/quant_channel/42"
    assert message_link(-1001234567890, None, 42) == "https://t.me/c/1234567890/42"
    assert message_link(-1001234567890, "quant_channel", 77, topic_id=42) == (
        "https://t.me/quant_channel/42/77"
    )
    assert message_link(None, None, 42) is None


def test_chat_kind_and_ref():
    channel = make_channel(username="quant_channel")
    assert chat_kind(channel) == "supergroup"
    assert chat_kind(make_channel(broadcast=True)) == "channel"
    assert chat_ref(channel) == "@quant_channel"
    assert chat_ref(make_channel()) == "-1001234567890"


def test_chat_entry_includes_counts_and_flags():
    entry = chat_entry(make_channel(username="quant_channel", participants_count=1234, verified=True))
    assert entry["chat_id"] == -1001234567890
    assert entry["username"] == "quant_channel"
    assert entry["members"] == 1234
    assert entry["verified"] is True
    assert entry["link"] == "https://t.me/quant_channel"


def test_message_entry_shape():
    entry = message_entry(make_message(post=True), chat_entity=make_channel(username="quant_channel"))
    assert entry == {
        "id": 42,
        "date": "2025-02-03T12:00:00+00:00",
        "text": "hello world",
        "sender": "Quant Channel",
        "link": "https://t.me/quant_channel/42",
    }


def test_message_entry_marks_media_and_topic():
    message = make_message(
        media=types.MessageMediaPhoto(photo=types.PhotoEmpty(id=1)),
        reply_to=types.MessageReplyHeader(reply_to_msg_id=7, reply_to_top_id=7, forum_topic=True),
    )
    entry = message_entry(message, chat_entity=make_channel())
    assert entry["has_media"] is True
    assert entry["topic_id"] == 7
