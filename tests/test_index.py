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


def test_search_filters_by_chats_dates_sender_and_media(tmp_path):
    index = MessageIndex(tmp_path / "i.sqlite3")
    try:
        index.upsert_chat(-100111, "A", "a", "channel")
        index.upsert_chat(-100222, "B", "b", "channel")
        index.upsert_messages(
            [
                row(chat_id=-100111, msg_id=1, date=1735689600, sender_name="Алиса", text="sharpe one"),
                row(chat_id=-100111, msg_id=2, date=1738368000, sender_name="Боб", text="sharpe two", has_media=1),
                row(chat_id=-100222, msg_id=1, date=1735689600, sender_name="Алиса", text="sharpe three"),
            ]
        )

        across = index.search("sharpe", chat_ids=[-100111, -100222])
        assert len(across) == 3

        only_b = index.search("sharpe", chat_ids=[-100222])
        assert [item["chat_id"] for item in only_b] == [-100222]

        by_date = index.search("sharpe", min_ts=1737000000)
        assert [item["msg_id"] for item in by_date] == [2]

        by_sender = index.search("sharpe", sender="алиса")
        assert {(item["chat_id"], item["msg_id"]) for item in by_sender} == {
            (-100111, 1),
            (-100222, 1),
        }

        with_media = index.search("sharpe", has_media=True)
        assert [item["msg_id"] for item in with_media] == [2]

        newest = index.search("sharpe", sort="date")
        assert [item["date"] for item in newest] == sorted(
            [item["date"] for item in newest], reverse=True
        )
    finally:
        index.close()


def test_any_mode_matches_partial_queries(tmp_path):
    index = MessageIndex(tmp_path / "i.sqlite3")
    try:
        index.upsert_messages([row(msg_id=1, text="alpha beta")])
        assert index.search("alpha gamma") == []
        partial = index.search("alpha gamma", mode="any")
        assert [item["msg_id"] for item in partial] == [1]
    finally:
        index.close()


def test_reply_to_is_stored_and_returned(tmp_path):
    index = MessageIndex(tmp_path / "i.sqlite3")
    try:
        index.upsert_messages([row(msg_id=7, text="answer", reply_to_msg_id=3)])
        result = index.search("answer")
        assert result[0]["reply_to"] == 3
    finally:
        index.close()


def test_old_index_gets_reply_column_migrated(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE messages (
            rowid INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            msg_id INTEGER NOT NULL,
            date INTEGER NOT NULL,
            sender_id INTEGER,
            sender_name TEXT,
            text TEXT NOT NULL DEFAULT '',
            has_media INTEGER NOT NULL DEFAULT 0,
            topic_id INTEGER,
            UNIQUE(chat_id, msg_id)
        );
        """
    )
    db.commit()
    db.close()

    index = MessageIndex(path)
    try:
        index.upsert_messages([row(msg_id=1, text="legacy", reply_to_msg_id=0)])
        assert index.search("legacy")[0]["msg_id"] == 1
        columns = {
            item[1] for item in index._db.execute("PRAGMA table_info(messages)").fetchall()
        }
        assert "reply_to_msg_id" in columns
    finally:
        index.close()
