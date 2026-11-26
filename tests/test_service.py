from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from telethon import errors, types, utils
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


async def test_find_chats_exposes_activity_and_filters_quiet_chats(index):
    channel = make_channel()
    quiet = make_channel(id=222, title="Quiet", username="quiet")
    now = datetime.now(timezone.utc)
    service = FakeDialogsService(
        index,
        channel,
        [],
        dialogs=[
            (FakeDialog(channel, date=now - timedelta(days=1)), False),
            (FakeDialog(quiet, date=now - timedelta(days=120)), False),
        ],
    )

    active = await service.find_chats("", scope="dialogs", active_within_days=30)
    assert [chat["ref"] for chat in active["chats"]] == ["@quant"]
    assert active["chats"][0]["last_message_date"]
    assert active["active_within_days"] == 30

    recent = await service.find_chats("", scope="dialogs", sort="activity")
    assert [chat["ref"] for chat in recent["chats"]] == ["@quant", "@quiet"]
    assert recent["sort"] == "activity"


async def test_find_chats_rejects_unknown_sort(index):
    service = FakeDialogsService(index, make_channel(), [], dialogs=[])
    with pytest.raises(TgSearchError) as excinfo:
        await service.find_chats("", sort="newest")
    assert excinfo.value.code == "invalid_argument"


async def test_chat_members_lists_supergroup_participants(index):
    channel = make_channel()
    user = make_user()
    creator = make_user(id=777, first_name="Alice", username="alice")
    participants = types.channels.ChannelParticipants(
        count=2,
        participants=[
            types.ChannelParticipant(user_id=555, date=None),
            types.ChannelParticipantCreator(user_id=777, admin_rights=types.ChatAdminRights()),
        ],
        chats=[],
        users=[user, creator],
    )
    service = FakeService(index, channel, [participants])

    result = await service.chat_members("@quant", limit=10)

    assert result["ok"] is True
    assert result["count"] == 2
    assert result["total"] == 2
    assert result["members"][0]["name"] == "Bob"
    assert "role" not in result["members"][0]
    assert result["members"][1]["role"] == "creator"
    assert result["members"][1]["username"] == "alice"
    assert "next_offset" not in result


async def test_chat_members_reports_hidden_list(index):
    service = FailingService(
        index, make_channel(), errors.ChatAdminRequiredError(request=None)
    )

    result = await service.chat_members("@quant")

    assert result["ok"] is True
    assert result["participants_hidden"] is True
    assert result["members"] == []


class FakeFullChat:
    """Minimal stand-in for messages.ChatFull responses."""

    def __init__(self, participants, users=None):
        self.full_chat = SimpleNamespace(participants=participants)
        self.users = users or []
        self.chats = []


class FailingService(FakeService):
    def __init__(self, index, entity, exc):
        super().__init__(index, entity, [])
        self.exc = exc

    async def _run(self, factory, timeout=None):
        raise self.exc


async def test_chat_members_lists_basic_group(index):
    group = types.Chat(
        id=222,
        title="Team",
        photo=types.ChatPhotoEmpty(),
        participants_count=2,
        date=None,
        version=1,
    )
    full = FakeFullChat(
        types.ChatParticipants(
            chat_id=222,
            participants=[
                types.ChatParticipant(user_id=555, inviter_id=777, date=None),
                types.ChatParticipantCreator(user_id=777),
            ],
            version=1,
        ),
        users=[make_user(), make_user(id=777, first_name="Alice")],
    )
    service = FakeService(index, group, [full])

    result = await service.chat_members("222", query="bob")

    assert result["total"] == 1
    assert result["members"] == [{"user_id": 555, "name": "Bob"}]


async def test_list_contacts_filters_and_hides_phones(index):
    user = make_user(phone="+10000000000", username="bobby")

    service = FakeService(
        index,
        make_channel(),
        [
            types.contacts.Contacts(
                contacts=[types.Contact(user_id=555, mutual=False)],
                saved_count=1,
                users=[user],
            )
        ],
    )
    result = await service.list_contacts(query="bob")
    assert result["total"] == 1
    assert result["contacts"][0]["name"] == "Bob"
    assert "phone" not in result["contacts"][0]

    with_phones = FakeService(
        index,
        make_channel(),
        [types.contacts.Contacts(contacts=[], saved_count=1, users=[user])],
    )
    result = await with_phones.list_contacts(include_phones=True)
    assert result["contacts"][0]["phone"] == "+10000000000"


async def test_index_search_relaxes_to_or_and_reports_it(index):
    index.upsert_messages(
        [
            dict(
                chat_id=-100111,
                msg_id=1,
                date=1735689600,
                sender_id=None,
                sender_name=None,
                text="alpha beta",
                has_media=0,
                topic_id=None,
            )
        ]
    )
    service = FakeService(index, make_channel(), [])

    result = await service.index_search("alpha gamma")

    assert result["relaxed"] is True
    assert result["returned"] == 1
    assert "OR-matched" in result["note"]


class OfflineService(FakeService):
    async def resolve(self, ref, refresh=False):
        raise RuntimeError("offline")


async def test_index_search_filters_by_chats_offline(index):
    index.upsert_chat(-100111, "A", "a", "channel")
    index.upsert_chat(-100222, "B", "b", "channel")
    rows = [
        dict(
            chat_id=chat,
            msg_id=1,
            date=1735689600,
            sender_id=None,
            sender_name=None,
            text="needle",
            has_media=0,
            topic_id=None,
        )
        for chat in (-100111, -100222)
    ]
    index.upsert_messages(rows)
    service = OfflineService(index, make_channel(), [])

    result = await service.index_search("needle", chats=["-100222", "missing"])

    assert [item["chat_id"] for item in result["results"]] == [-100222]
    assert result["unresolved_chats"] == ["missing"]
    assert "chat" not in result


async def test_index_search_rejects_unknown_chats(index):
    service = OfflineService(index, make_channel(), [])
    with pytest.raises(TgSearchError) as excinfo:
        await service.index_search("needle", chats=["telegram"])
    assert excinfo.value.code == "chat_not_found"
