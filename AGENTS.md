# tgsearch-mcp — AGENTS.md

Read-only Telegram search для MCP-хостов: поиск чата, серверный поиск сообщений
внутри чата, чтение контекста и необязательный локальный FTS5-индекс.

Этот файл — **единственный источник правил для ИИ-агентов** в открытом формате
[AGENTS.md](https://agents.md/). OpenCode и VS Code Copilot читают его нативно,
поэтому файлов-загрузчиков (`CLAUDE.md` и т.п.) нет. Правила для конкретных
вендоров (`GEMINI.md`, `.cursor/rules`, `.github/instructions`) не заводим:
новый загрузчик — только если клиент не умеет читать `AGENTS.md`.

Репозиторий ведёт один человек, поэтому процесс короткий: `main` + CI на push,
релиз — тегом. Но инварианты ниже не зависят от размера команды.

## Инварианты

- **Только чтение.** Сервер не отправляет, не редактирует, не удаляет, не
  вступает и не выходит из чатов. Новый инструмент с таким поведением — не
  «фича», а нарушение контракта. Проверка: `tests/test_server.py`.
- **Секреты не утекают.** Session string — полный доступ к аккаунту Telegram.
  Его нельзя печатать, логировать, класть в ошибки, тесты, примеры и README.
  При отладке проверяй только «задан / не задан».
- **Значения из конфига — не в stdout MCP-сервера.** stdout занят протоколом;
  диагностика идёт в stderr.
- **Тесты не ходят в сеть.** Живые проверки — отдельные скрипты
  (`scripts/verify.py`, `scripts/check_api.py`), их не запускает pytest.
- **Документация описывает текущую сборку.** Состав инструментов, числа и
  команды синхронизируются в том же PR, который их меняет. Ссылок на невлитые
  ветки в файлах нет.

## Стек и команды

Python ≥ 3.10, `mcp` (stdio), `telethon`, `pydantic`, `qrcode`, hatchling.

```sh
.venv/bin/pytest -q                        # офлайн-тесты
.venv/bin/python scripts/smoke.py          # boot по stdio без кредов (для CI)
.venv/bin/python scripts/verify.py         # живой find → search → context (нужен login)
.venv/bin/python scripts/check_api.py      # проверка api_id/api_hash без входа
.venv/bin/tgsearch-mcp status              # JSON: конфиг, логин, индекс
.venv/bin/python -m build                  # sdist + wheel
```

Установка для пользователя: `pipx install tgsearch-mcp`.

## Архитектура

| Файл | Ответственность |
| --- | --- |
| `src/tgsearch_mcp/cli.py` | `login` (QR/телефон), `logout`, `status`, `serve`; argparse-граница |
| `src/tgsearch_mcp/server.py` | `MCPServer`, 9 инструментов, тонкие обёртки над `TelegramService` |
| `src/tgsearch_mcp/telegram.py` | Telethon-клиент, поиск, история, топики, индексация, `status()` |
| `src/tgsearch_mcp/config.py` | `config.json`, env, XDG-пути, права `0600` |
| `src/tgsearch_mcp/index.py` | SQLite FTS5-кэш сообщений |
| `src/tgsearch_mcp/formatting.py` | нормализация entity/message в JSON-словари |
| `src/tgsearch_mcp/query.py` | построение FTS-запроса |
| `src/tgsearch_mcp/errors.py` | `TgSearchError` → структурированный payload |
| `scripts/smoke.py` | CI-проба: каталог + `tg_status` без сети и кредов |
| `scripts/verify.py` | живой smoke после логина |
| `scripts/check_api.py` | валидация пары `api_id`/`api_hash` (pre-login QR-токен) |

Слой инструментов намеренно тонкий: логика — в `TelegramService`, ошибки —
через `_safe()` → `error_payload()`. Инструмент не должен бросать исключение
наружу.

## Каталог инструментов

В сборке ровно **9** инструментов; список сверяется тестом `tests/test_docs.py`
с README и с `EXPECTED_TOOLS` в `scripts/verify.py`.

| Инструмент | Назначение |
| --- | --- |
| `tg_status` | Конфиг, логин, аккаунт, индекс. Никогда не падает. |
| `tg_find_chats` | Диалоги + публичный каталог; возвращает `ref`. |
| `tg_get_chat` | About, участники, закреп, forum-флаг, ссылка. |
| `tg_list_topics` | Топики супергруппы (forum). |
| `tg_search_messages` | Серверный поиск внутри чата (`messages.search`). |
| `tg_search_global` | Поиск по доступным аккаунту чатам (`searchGlobal`). |
| `tg_get_messages` | История по `ids` / `before_id` / `after_id` / `around_id`. |
| `tg_index_chat` | Построить/обновить локальный FTS5-индекс чата. |
| `tg_search_index` | Локальный поиск по индексу, в том числе офлайн. |

Выключенных модулей нет. Если инструмент «есть в коде, но агент его не
вызывает» — сверься с этой таблицей и `tests/test_server.py`.

## Конфигурация

Приоритет для `api_id`/`api_hash`/`session`: **env → `config.json`**;
`TGSEARCH_HOME` подменяет оба XDG-каталога, `TGSEARCH_INDEX` — только файл
индекса. `./.env` читает **только** `scripts/check_api.py`; сам сервер `.env`
не парсит (переменные должны быть экспортированы).

| Путь | Содержимое |
| --- | --- |
| `$XDG_CONFIG_HOME/tgsearch-mcp/config.json` | `api_id`, `api_hash`, session (`0600`) |
| `$XDG_DATA_HOME/tgsearch-mcp/index.sqlite3` | локальный FTS5-индекс (`0600`) |

Оба пути и запрет относительных XDG-значений закреплены в
`tests/test_config.py`; `TGSEARCH_HOME`/`TGSEARCH_INDEX` — основной способ
изолировать тесты.

## Telegram / Telethon

- **FloodWait не глотаем.** `_run` повторяет только короткие задержки;
  длинный `FloodWaitError` уходит наружу как `retry_after`. В `tg_index_chat`
  он возвращается в `stopped.retry_after`, чтобы индексацию можно было
  продолжить.
- **Резолв entity** идёт через кэш и `dialogs()`; для приватных чатов без
  диалога используй `tg_find_chats`, а не догадки.
- **Один клиент на процесс**, `receive_updates=False`, семафор на 3
  параллельных запроса, таймаут на запрос из конфига.
- **`chat`** принимает `@username`, `t.me`-ссылку и числовой id (`-100…`).
  Инвайт-ссылки не поддерживаются осознанно.
- **Локальный индекс — кэш, не зеркало.** Он покрывает только чаты, которые
  явно индексировали, и хранит текст сообщений в открытом виде.

## Как добавить инструмент

1. `@mcp.tool()` в `src/tgsearch_mcp/server.py`; параметры — `Annotated[...,
   Field(description=...)]`, у каждого параметра понятное описание и границы.
2. Логику — в `TelegramService`, ошибки — через `TgSearchError`; обёртка
   возвращает `_safe(...)`.
3. Тест в `tests/test_server.py` (каталог) или `tests/test_service.py`
   (поведение) — без сети.
4. Обнови **три места**: таблицу в `README.md`, таблицу в этом файле и
   `EXPECTED_TOOLS` в `scripts/verify.py`, если меняется число инструментов.
5. Если меняется поведение — строка в `CHANGELOG.md`.

`tests/test_docs.py` упадёт, если README, `AGENTS.md`, `verify.py` и реальный
каталог разойдутся.

## Тесты и проверки

- `tests/` — только офлайн: tmp-каталоги, `TGSEARCH_HOME`, мок-сервисы.
- `scripts/smoke.py` — запускает сервер по stdio из временного каталога без
  кредов: 9 инструментов, `tg_status.ok`, `configured=false`. Это то, что
  гоняет CI.
- `scripts/verify.py` — после `login`: каталог, `tg_status`, find → search →
  context. Exit `2` = ещё не авторизован.
- `scripts/check_api.py` — проверка кредов до логина; ничего не отправляет,
  только запрашивает pre-login QR-токен, который истекает.

## Релиз

- Версия — **единственный источник** `__version__` в
  `src/tgsearch_mcp/__init__.py`; `pyproject.toml` берёт её через
  `[tool.hatch.version]`.
- Релиз: поднять версию + `CHANGELOG.md` → тег `vX.Y.Z` на `main` → workflow.
- `.github/workflows/release.yml` проверяет, что тег на `main` и совпадает с
  `__version__`, гоняет тесты и сборку, пропускает уже опубликованную версию,
  публикует на PyPI по OIDC и создаёт GitHub Release.
- В текстах issue/PR/файлов — только синтетические данные: никаких реальных
  чатов, username, id аккаунтов и session string.

## Чего не делать

- Не добавлять запись/отправку/вступление — даже «на будущее».
- Не коммитить `config.json`, `.env`, `*.session`, `index.sqlite3*`.
- Не ходить в Telegram из pytest и не мокать сеть «наполовину»: тесты работы
  с API — только на уровне сервиса с подставными объектами.
- Не дублировать форматирование — общий код в `formatting.py`.
- Не заводить вендорные файлы правил и вложенные `AGENTS.md`, пока файлов
  мало: один корневой файл + этот документ.
