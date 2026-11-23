import pytest
from telethon import types

from tgsearch_mcp.errors import TgSearchError, error_payload
from tgsearch_mcp.telegram import _window_around, clamp, normalize_chat_ref


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("@quant", "@quant"),
        ("quant", "quant"),
        ("-1001234567890", -1001234567890),
        ("https://t.me/quant_channel", "quant_channel"),
        ("https://t.me/quant_channel/1234", "quant_channel"),
        ("https://t.me/c/1234567890/42", -1001234567890),
        ("https://t.me/s/quant_channel", "quant_channel"),
        ("tg://resolve?domain=quant_channel", "quant_channel"),
    ],
)
def test_normalize_chat_ref(ref, expected):
    assert normalize_chat_ref(ref) == expected


def test_normalize_chat_ref_rejects_invite_links():
    with pytest.raises(TgSearchError) as excinfo:
        normalize_chat_ref("https://t.me/+AbCdEf")
    assert excinfo.value.code == "invite_link_unsupported"


def test_normalize_chat_ref_rejects_empty():
    with pytest.raises(TgSearchError) as excinfo:
        normalize_chat_ref("  ")
    assert excinfo.value.code == "invalid_chat"


def test_clamp():
    assert clamp(0, 1, 50) == 1
    assert clamp(999, 1, 50) == 50
    assert clamp("7", 1, 50) == 7
    assert clamp("nope", 1, 50) == 1


def test_window_around_centres_on_id():
    messages = [types.Message(id=i, peer_id=types.PeerChannel(1), date=None, message=str(i)) for i in range(1, 21)]
    window = _window_around(messages, around_id=10, limit=5)
    assert [message.id for message in window] == [8, 9, 10, 11, 12]


def test_window_around_clamps_at_history_start():
    messages = [types.Message(id=i, peer_id=types.PeerChannel(1), date=None, message=str(i)) for i in range(1, 4)]
    window = _window_around(messages, around_id=1, limit=5)
    assert [message.id for message in window] == [1, 2, 3]


def test_error_payload_maps_known_and_unknown_errors():
    mapped = error_payload(TgSearchError("chat_not_found", "nope", "use tg_find_chats"))
    assert mapped == {
        "ok": False,
        "error": "chat_not_found",
        "message": "nope",
        "hint": "use tg_find_chats",
    }

    from telethon import errors

    flood = error_payload(errors.FloodWaitError(request=None, capture=30))
    assert flood["error"] == "flood_wait"
    assert flood["retry_after"] == 30

    unknown = error_payload(RuntimeError("boom"))
    assert unknown["error"] == "internal_error"
    assert "boom" in unknown["message"]
