from tgsearch_mcp.index import MessageIndex


def row(**kwargs):
    defaults = dict(
        chat_id=-100111,
        msg_id=1,
        date=1735689600,
        sender_id=None,
        sender_name="alice",
        text="",
        has_media=0,
        topic_id=None,
    )
    defaults.update(kwargs)
    return defaults


def test_search_finds_russian_words_by_prefix(tmp_path):
    index = MessageIndex(tmp_path / "i.sqlite3")
    try:
        index.upsert_chat(-100111, "Квант-канал", "quant", "channel")
        index.upsert_messages(
            [
                row(msg_id=1, text="Считаем коэффициент Шарпа для стратегии"),
                row(msg_id=2, text="Обзор рынка облигаций"),
                row(msg_id=3, text="Шарпирование портфеля"),
            ]
        )
        results = index.search("шарп")
        assert {item["msg_id"] for item in results} == {1, 3}
        assert results[0]["chat_title"] == "Квант-канал"
        assert results[0]["link"].startswith("https://t.me/quant/")
        assert "**" in results[0]["snippet"]

        exact = index.search("коэффициент шарпа")
        assert [item["msg_id"] for item in exact] == [1]
    finally:
        index.close()


def test_search_matches_all_terms_and_respects_chat_filter(tmp_path):
    index = MessageIndex(tmp_path / "i.sqlite3")
    try:
        index.upsert_chat(-100111, "A", "a", "channel")
        index.upsert_chat(-100222, "B", "b", "channel")
        index.upsert_messages(
            [
                row(chat_id=-100111, msg_id=1, text="sharpe ratio high"),
                row(chat_id=-100111, msg_id=2, text="sharpe only"),
                row(chat_id=-100222, msg_id=1, text="sharpe ratio low"),
            ]
        )
        assert len(index.search("sharpe ratio")) == 2
        only_b = index.search("sharpe ratio", chat_id=-100222)
        assert len(only_b) == 1
        assert only_b[0]["chat_id"] == -100222
    finally:
        index.close()


def test_upsert_is_idempotent_and_updates_text(tmp_path):
    index = MessageIndex(tmp_path / "i.sqlite3")
    try:
        index.upsert_messages([row(msg_id=5, text="old text")])
        index.upsert_messages([row(msg_id=5, text="new text")])
        assert index.count_chat(-100111) == 1
        assert index.search("old") == []
        assert len(index.search("new")) == 1
    finally:
        index.close()


def test_delete_chat_removes_messages_and_state(tmp_path):
    index = MessageIndex(tmp_path / "i.sqlite3")
    try:
        index.upsert_chat(-100111, "A", "a", "channel")
        index.upsert_messages([row(msg_id=1, text="hello")])
        index.set_state(-100111, 1)
        index.delete_chat(-100111)
        assert index.count_chat(-100111) == 0
        assert index.search("hello") == []
        assert index.stats()["chats"] == 0
    finally:
        index.close()


def test_find_chat_resolves_username_and_title(tmp_path):
    index = MessageIndex(tmp_path / "i.sqlite3")
    try:
        index.upsert_chat(-100111, "Quant Channel", "quant", "channel")
        assert index.find_chat("@Quant") == -100111
        assert index.find_chat("quant channel") == -100111
        assert index.find_chat("-100111") == -100111
        assert index.find_chat("missing") is None
    finally:
        index.close()


def test_set_state_records_totals(tmp_path):
    index = MessageIndex(tmp_path / "i.sqlite3")
    try:
        index.upsert_chat(-100111, "A", "a", "channel")
        index.upsert_messages([row(msg_id=1, text="a"), row(msg_id=2, text="b")])
        index.set_state(-100111, 2)
        stats = index.chat_stats()
        assert stats[0]["messages"] == 2
        assert stats[0]["last_msg_id"] == 2
    finally:
        index.close()


def test_index_files_are_private(tmp_path):
    import stat

    directory = tmp_path / "private"
    index = MessageIndex(directory / "i.sqlite3")
    try:
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE((directory / "i.sqlite3").stat().st_mode) == 0o600
    finally:
        index.close()
