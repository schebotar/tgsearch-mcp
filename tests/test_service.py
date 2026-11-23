from datetime import datetime, timezone

import pytest
from telethon import types, utils
from telethon.tl.types import messages

from tgsearch_mcp.config import Config
from tgsearch_mcp.errors import TgSearchError
from tgsearch_mcp.index import MessageIndex
from tgsearch_mcp.telegram import TelegramService


def make_channel(**kwargs):
    defaults = dict(
        id=111,
        title="Quant Channel",
        photo=types.ChatPhotoEmpty(),
        date=datetime(2024, 1, 1, tzinfo=timezone.utc),
        username="quant",
        access_hash=99,
    )
    defaults.update(kwargs)
    return types.Channel(**defaults)


def make_user(**kwargs):
    defaults = dict(id=555, first_name="Bob", access_hash=7)
    defaults.update(kwargs)
    return types.User(**defaults)


def make_message(msg_id, text="hello", **kwargs):
    defaults = dict(
        id=msg_id,
        peer_id=types.PeerChannel(111),
        date=datetime(2025, 1, 1, tzinfo=timezone.utc),
        message=text,
        from_id=types.PeerUser(555),
    )
    defaults.update(kwargs)
    return types.Message(**defaults)


def collection(msg_list, channel=None, user=None):
    return messages.Messages(
        messages=list(msg_list),
        topics=[],
        chats=[channel] if channel else [],
        users=[user] if user else [],
    )


class FakeService(TelegramService):
    """TelegramService with the network layer replaced by canned responses."""

    def __init__(self, index, entity, responses=None):
        super().__init__(Config(api_id=1, api_hash="hash"), index=index)
        self.entity = entity
        self.responses = list(responses or [])
        self.calls = 0

    async def resolve(self, ref, refresh=False):
        return self.entity

    async def _run(self, factory, timeout=None):
        self.calls += 1
        if not self.responses:
            raise AssertionError("unexpected Telegram call")
        return self.responses.pop(0)


@pytest.fixture
def index(tmp_path):
    idx = MessageIndex(tmp_path / "i.sqlite3")
    yield idx
    idx.close()


async def test_search_messages_maps_entries_and_sender(index):
    channel = make_channel()
    user = make_user()
    response = collection(
        [make_message(2, "sharpe ratio 2"), make_message(1, "sharpe ratio 1")],
        channel=channel,
        user=user,
    )
    service = FakeService(index, channel, [response])

    result = await service.search_messages("@quant", "sharpe", limit=20)

    assert result["ok"] is True
    assert result["query"] == "sharpe"
    assert result["returned"] == 2
    assert result["order"] == "newest_first"
    first = result["messages"][0]
    assert first["id"] == 2
    assert first["sender"] == "Bob"
    assert first["link"] == "https://t.me/quant/2"
    assert "next_offset_id" not in result


async def test_search_messages_requires_query_or_filter(index):
    service = FakeService(index, make_channel(), [])
    with pytest.raises(TgSearchError) as excinfo:
        await service.search_messages("@quant", "")
    assert excinfo.value.code == "invalid_argument"


async def test_search_messages_rejects_unknown_kind(index):
    service = FakeService(index, make_channel(), [])
    with pytest.raises(TgSearchError) as excinfo:
        await service.search_messages("@quant", "x", kind="stickers")
    assert excinfo.value.code == "invalid_argument"


async def test_get_messages_by_ids_keeps_order(index):
    channel = make_channel()
    user = make_user()
    service = FakeService(
        index,
        channel,
        [[make_message(5, "five"), make_message(4, "four")], user],
    )

    result = await service.get_messages("@quant", ids=[4, 5])

    assert result["ok"] is True
    assert [message["id"] for message in result["messages"]] == [4, 5]
    assert result["requested_ids"] == [4, 5]
    assert result["messages"][0]["sender"] == "Bob"


async def test_get_messages_rejects_mixed_cursors(index):
    service = FakeService(index, make_channel(), [])
    with pytest.raises(TgSearchError) as excinfo:
        await service.get_messages("@quant", before_id=1, around_id=2)
    assert excinfo.value.code == "invalid_argument"


async def test_index_chat_then_incremental_update(index):
    channel = make_channel()
    user = make_user()
    first = FakeService(
        index,
        channel,
        [collection([make_message(3, "third"), make_message(2, "second"), make_message(1, "first")], channel, user)],
    )

    result = await first.index_chat("@quant", limit=10)

    assert result["ok"] is True
    assert result["scanned"] == 3
    assert result["upserted"] == 3
    assert result["indexed_total"] == 3
    assert index.last_msg_id(utils.get_peer_id(channel)) == 3
    assert len(index.search("second")) == 1

    second = FakeService(index, channel, [collection([make_message(4, "fourth")], channel, user)])
    result = await second.index_chat("@quant", limit=10)

    assert result["upserted"] == 1
    assert result["since_id"] == 3
    assert result["indexed_total"] == 4


async def test_index_search_without_chat_uses_local_index(index):
    channel = make_channel()
    index.upsert_chat(-100111, "Quant Channel", "quant", "channel")
    index.upsert_messages(
        [
            dict(
                chat_id=-100111,
                msg_id=1,
                date=1735689600,
                sender_id=None,
                sender_name=None,
                text="sharpe ratio explained",
                has_media=0,
                topic_id=None,
            )
        ]
    )
    service = FakeService(index, channel, [])

    result = await service.index_search("sharpe")

    assert result["ok"] is True
    assert result["returned"] == 1
    assert result["results"][0]["link"] == "https://t.me/quant/1"
    assert result["index"]["messages"] == 1


class FakeDialogsService(FakeService):
    def __init__(self, index, entity, responses=None, dialogs=None):
        super().__init__(index, entity, responses)
        self._fake_dialogs = dialogs or []

    async def dialogs(self, refresh=False):
        return self._fake_dialogs


class FakeDialog:
    def __init__(self, entity, date=None, unread_count=0):
        self.entity = entity
        self.id = utils.get_peer_id(entity)
        self.date = date or datetime(2025, 1, 1, tzinfo=timezone.utc)
        self.unread_count = unread_count


async def test_find_chats_scope_public_requires_query(index):
    channel = make_channel()
    service = FakeDialogsService(index, channel, [], dialogs=[(FakeDialog(channel), False)])

    result = await service.find_chats("", scope="public")

    assert result["chats"] == []
    assert "non-empty query" in result["note"]


async def test_find_chats_scope_public_adds_non_member_channels(index):
    channel = make_channel()
    found = types.contacts.Found(
        my_results=[],
        results=[channel],
        chats=[channel],
        users=[],
    )
    service = FakeDialogsService(index, channel, [found], dialogs=[])

    result = await service.find_chats("quant", scope="public")

    assert result["count"] == 1
    assert result["chats"][0]["source"] == "public"
    assert result["chats"][0]["is_member"] is False


async def test_find_chats_scope_dialogs_matches_title(index):
    channel = make_channel()
    service = FakeDialogsService(
        index, channel, [], dialogs=[(FakeDialog(channel, unread_count=3), False)]
    )

    result = await service.find_chats("quant", scope="dialogs")

    assert result["count"] == 1
    assert result["chats"][0]["ref"] == "@quant"
    assert result["chats"][0]["unread"] == 3
    assert result["chats"][0]["source"] == "dialogs"


async def test_find_chats_scope_dialogs_filters_out_non_matches(index):
    channel = make_channel()
    service = FakeDialogsService(index, channel, [], dialogs=[(FakeDialog(channel), False)])

    result = await service.find_chats("bitcoin", scope="dialogs")

    assert result["chats"] == []
    assert "No chats matched" in result["note"]
