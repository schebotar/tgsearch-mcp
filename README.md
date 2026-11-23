# tgsearch-mcp

Read-only Telegram search for MCP hosts (Claude, VS Code, Cursor, …), built on
your own user account over MTProto — **find a chat, then find the message, fast**.

Most Telegram MCP servers are download wrappers: to "search" they export history
and filter it locally. This server uses Telegram's **server-side search**
(`messages.search`, `messages.searchGlobal`), so a query over a 100k-message
channel returns in well under a second instead of minutes.

## Why not `tdl`/`tdl-mcp`

| | `tdl-mcp` | `tgsearch-mcp` |
| --- | --- | --- |
| Search messages | export history + local filter | `messages.search` (server-side) |
| Text messages | media-only unless `--all` | everything |
| Public chat directory | no | `contacts.search` |
| Connection | new `tdl` process per call | one persistent MTProto client |
| Local index | no | optional SQLite FTS5 cache |

`tdl` remains a good companion for media downloads; this server is for search.

## Install

```sh
pipx install tgsearch-mcp     # or: uv tool install tgsearch-mcp
```

Requires Python ≥ 3.10 and a Python build with SQLite FTS5 (default on CPython
from python.org, Homebrew and distro packages).

## 1. Create a Telegram app (one time)

Go to <https://my.telegram.org> → *API development tools* → create an app.
Note the `api_id` and `api_hash`.

## 2. Log in (one time)

```sh
tgsearch-mcp login            # QR: Settings → Devices → Link Desktop Device
tgsearch-mcp login --phone +79991234567   # phone + code (+ 2FA password)
```

You can also pass `--api-id`/`--api-hash`, or set `TG_API_ID`/`TG_API_HASH`.
The session string is stored in `~/.tgsearch-mcp/config.json` with mode `0600`.
The account shows up in Telegram → *Settings → Devices*; revoke it there or with
`tgsearch-mcp logout`.

## 3. Register the server

**Claude Code / Claude Desktop / VS Code / any `mcpServers` host:**

```json
{
  "mcpServers": {
    "telegram": {
      "command": "tgsearch-mcp",
      "args": ["serve"]
    }
  }
}
```

`serve` is the default, so `"args": []` works too. Use `python -m tgsearch_mcp`
if you installed the package into a specific interpreter.

## Verify it works

No Telegram account needed:

```sh
pytest -q                     # unit tests: parsing, FTS5 index, tool catalog
tgsearch-mcp status           # JSON: configured / logged_in / index
python scripts/verify.py      # boots the server over stdio like a real host
```

`scripts/verify.py` prints the tool catalog and calls `tg_status`; when the
session is authorized it also runs find → search → context. Exit codes:
`0` ok, `1` protocol failure, `2` not logged in yet.

With a real account:

```sh
tgsearch-mcp login                                          # QR or --phone
python scripts/verify.py --find quant --query "sharpe ratio"
python scripts/verify.py --chat @durov --query test          # any public chat
```

To watch the raw JSON-RPC exchange, run `tgsearch-mcp serve` and pipe
newline-delimited `initialize` / `tools/list` / `tools/call` messages into it —
that is exactly what the script does.

## Tools

| Tool | What it does |
| --- | --- |
| `tg_status` | Config, login, account and index health. Never errors. Start here. |
| `tg_find_chats` | Search your dialogs and the public directory. Returns `ref` for other tools. |
| `tg_get_chat` | About, members, pinned message, forum flag, link, restrictions. |
| `tg_list_topics` | Forum topics of a supergroup (with `topic_id`). |
| `tg_search_messages` | **Server-side search inside one chat**: query, kind, dates, sender, topic. |
| `tg_search_global` | Search across every chat the account can see. |
| `tg_get_messages` | History by `ids` / `before_id` / `after_id` / `around_id`. |
| `tg_index_chat` | Build/refresh the optional local FTS5 index for one chat. |
| `tg_search_index` | Instant local search over indexed chats (works offline). |

`chat` accepts `@username`, `https://t.me/...` links, and numeric ids
(`-100…` works).

Typical agent flow:

```
tg_find_chats("quant trading")        → ref: @quant_channel
tg_search_messages("@quant_channel", "backtest Sharpe")
tg_get_messages("@quant_channel", around_id=1234, limit=20)   → context
```

Everything is read-only: there is no code path that sends, edits, deletes,
joins or leaves a chat.

## Local index (optional)

Server-side search is exact but capped by Telegram's own tokenizer. For large
histories you query repeatedly, or for offline search:

```
tg_index_chat("@big_channel", limit=20000)   # incremental on later runs
tg_search_index("шарп коэффициент")          # prefix match, Russian-friendly
```

The index lives in `~/.tgsearch-mcp/index.sqlite3` and stores message text.
Delete it (or set `TGSEARCH_INDEX`) to keep text out of the default location.
Server-side search remains the primary path — the index is a cache, not a
mirror, and only covers chats you explicitly indexed.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `TG_API_ID` / `TG_API_HASH` | — | Telegram app credentials |
| `TG_SESSION_STRING` | — | Session string (overrides `config.json`) |
| `TGSEARCH_HOME` | `~/.tgsearch-mcp` | Config + index directory |
| `TGSEARCH_INDEX` | `$TGSEARCH_HOME/index.sqlite3` | Local index path |
| `TGSEARCH_TIMEOUT` | `60` | Per-request timeout, seconds |
| `TGSEARCH_DIALOG_LIMIT` | `1000` | Max dialogs fetched per listing |
| `TGSEARCH_INCLUDE_ARCHIVED` | `true` | Include archived dialogs |
| `TGSEARCH_DIALOG_TTL` | `300` | Dialog cache TTL, seconds |
| `TGSEARCH_INDEX_TEXT_LIMIT` | `10000` | Max chars stored per message |

## Limitations

- **Private chats** are searchable only if the account is a member (or has
  access via a folder/link). Private invite links are not auto-joined.
- **Secret chats (E2E)** are not accessible through MTProto sessions.
- `contacts.search` matches chat **titles/usernames**, not message content.
  `searchGlobal` covers chats the account can reach — not arbitrary public
  channels you never opened. Indexing a public channel requires reading it.
- Telegram applies rate limits; the server surfaces `FloodWait` as
  `retry_after` instead of hanging, and caps concurrency.
- Automated crawling of many channels is against the spirit of Telegram's
  terms and can get an account limited. Keep ranges modest and prefer
  server-side search over mass indexing.

## Development

```sh
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
.venv/bin/tgsearch-mcp status
```

Tests cover query building, formatting/parsing, the FTS5 index and the MCP tool
catalog; they never touch the network.

## Security

The session string is a **full credential for your Telegram account** — anyone
who has it can act as you. It is stored with mode `0600` under
`~/.tgsearch-mcp`, never logged, and never sent anywhere except Telegram itself.
Protect it like an SSH key.

## License

MIT.
