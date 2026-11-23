from __future__ import annotations

import re

_WORD_RE = re.compile(r"[\w]+", re.UNICODE)
_TOKEN_RE = re.compile(r'"([^"]+)"|(\S+)')


def build_fts_query(query: str, mode: str = "auto") -> str:
    """Convert a user query into a safe FTS5 MATCH expression.

    ``auto`` treats every word as a prefix match and joins terms with AND,
    which makes Russian/English inflection work without a stemmer.
    ``raw`` passes the query through for power users who know FTS5 syntax.
    """
    query = (query or "").strip()
    if not query:
        raise ValueError("empty search query")
    if mode == "raw":
        return query
    if mode != "auto":
        raise ValueError("mode must be 'auto' or 'raw'")

    parts: list[str] = []
    for match in _TOKEN_RE.finditer(query):
        phrase, word = match.group(1), match.group(2)
        if phrase:
            words = _WORD_RE.findall(phrase)
            if words:
                parts.append('"' + " ".join(words) + '"')
        elif word:
            for token in _WORD_RE.findall(word):
                parts.append(f'"{token}"*')
    if not parts:
        raise ValueError("query has no searchable words")
    return " AND ".join(parts)
