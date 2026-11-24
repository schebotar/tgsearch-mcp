# История изменений

Формат — по мотивам [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/).
Версия пакета берётся из `src/tgsearch_mcp/__init__.py`; тег `vX.Y.Z`
ставится на коммит в `main`.

## 0.2.0 — 2026-11-24

### Добавлено

- `tgsearch-mcp setup` — локальная страница онбординга: получение ключей
  приложения по чеклисту, QR-код картинкой с автообновлением, ввод пароля 2FA
  и готовые конфиги для OpenCode, Codex, Claude Code, Claude Desktop и Cursor.
  Интерфейс на русском и английском (`RU`/`EN` в углу), по умолчанию язык
  берётся из настроек браузера. Слушает только `127.0.0.1`, требует
  одноразовый ключ из терминала, session string в ответы не попадает.
- `errors.py` распознаёт `API_ID_INVALID` и `API_ID_PUBLISHED_FLOOD` и отдаёт
  их как `api_id_invalid` / `api_id_published` с подсказкой.

### Изменено

- README: конфиг OpenCode V2 (`mcp.servers`) вместо старой схемы; добавлены
  готовые конфиги для Codex.
- Явные зависимости `starlette` и `uvicorn` (раньше приходили транзитивно
  с `mcp`, теперь используются напрямую).

## 0.1.0 — 2026-11-23

Первый публичный выпуск.

### Добавлено

- Read-only MCP-сервер для Telegram на своём user-аккаунте (Telethon/MTProto):
  `tg_status`, `tg_find_chats`, `tg_get_chat`, `tg_list_topics`,
  `tg_search_messages`, `tg_search_global`, `tg_get_messages`,
  `tg_index_chat`, `tg_search_index` — всего 9 инструментов.
- Серверный поиск (`messages.search`, `messages.searchGlobal`), поиск по
  форумным топикам, фильтры по типу сообщения, датам и отправителю.
- Необязательный локальный индекс SQLite FTS5: инкрементальная индексация
  чата, офлайн-поиск, обработка `FloodWait` с `retry_after`.
- `tgsearch-mcp login` (QR или телефон + код/2FA), `logout`, `status`,
  `serve`; session string хранится в `config.json` с правами `0600`.
- XDG-пути (`$XDG_CONFIG_HOME`, `$XDG_DATA_HOME`), `TGSEARCH_HOME` и
  `TGSEARCH_INDEX` для изоляции; относительные XDG-значения игнорируются
  по спецификации.
- `scripts/check_api.py` — проверка `api_id`/`api_hash` до логина без отправки
  кода (pre-login QR-токен), с `--save` в `config.json`.
- `scripts/smoke.py` — офлайн-проба сервера по stdio без кредов;
  `scripts/verify.py` — живой find → search → context после логина.

### Инфраструктура

- Упаковка для PyPI (`hatchling`, `tgsearch-mcp` console script,
  `pipx install tgsearch-mcp`), версия из одного места.
- CI: `actionlint`, `gitleaks` по всей истории, pytest на Python 3.10/3.14,
  сборка и `twine check`, установка колеса в чистое окружение,
  `scripts/smoke.py`.
- Релиз по тегу `vX.Y.Z`: проверка «тег на main» и «тег = `__version__`»,
  повторные проверки, публикация на PyPI по OIDC (Trusted Publishing),
  пропуск уже опубликованной версии, GitHub Release.
- `AGENTS.md` — единый источник правил для ИИ-агентов (OpenCode и VS Code
  Copilot читают его нативно); `tests/test_docs.py` сверяет состав инструментов
  с README, `AGENTS.md` и `scripts/verify.py`.
- `SECURITY.md` — как сообщать об уязвимостях и где хранятся секреты.
