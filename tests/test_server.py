import asyncio

import pytest

from tgsearch_mcp import server


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TGSEARCH_HOME", str(tmp_path))
    for name in ("TG_API_ID", "TG_API_HASH", "TG_SESSION_STRING", "TGSEARCH_INDEX"):
        monkeypatch.delenv(name, raising=False)
    yield
    asyncio.run(server.close_service())


def test_tool_catalog_is_read_only():
    tools = asyncio.run(server.mcp.list_tools())
    names = {tool.name for tool in tools}
    assert names == {
        "tg_status",
        "tg_find_chats",
        "tg_get_chat",
        "tg_get_chat_members",
        "tg_list_contacts",
        "tg_list_topics",
        "tg_search_messages",
        "tg_search_global",
        "tg_get_messages",
        "tg_index_chat",
        "tg_search_index",
    }
    assert not {"send", "delete", "edit", "join", "leave"} & names
    for tool in tools:
        assert tool.description, f"{tool.name} has no description"


def test_search_tools_expose_cross_chat_filters():
    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}

    index_schema = tools["tg_search_index"].input_schema["properties"]
    assert {"chat", "chats", "min_date", "max_date", "sender", "has_media", "topic_id", "sort"} <= set(
        index_schema
    )
    assert index_schema["mode"]["enum"] == ["auto", "any", "raw"]

    chat_schema = tools["tg_find_chats"].input_schema["properties"]
    assert {"active_within_days", "sort"} <= set(chat_schema)


def test_tg_status_without_credentials_is_soft_failure():
    result = asyncio.run(server.mcp.call_tool("tg_status", {}))
    payload = result.structured_content
    assert payload["ok"] is True
    assert payload["logged_in"] is False
    assert payload["configured"] is False
    assert "my.telegram.org" in payload["hint"]


def test_tg_search_index_on_empty_index_returns_no_results():
    result = asyncio.run(server.mcp.call_tool("tg_search_index", {"query": "sharpe"}))
    payload = result.structured_content
    assert payload["ok"] is True
    assert payload["returned"] == 0
    assert "tg_index_chat" in payload["note"]


def test_tg_search_index_rejects_empty_query():
    result = asyncio.run(server.mcp.call_tool("tg_search_index", {"query": "   "}))
    payload = result.structured_content
    assert payload["ok"] is False
    assert payload["error"] == "invalid_query"
