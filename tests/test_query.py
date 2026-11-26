import pytest

from tgsearch_mcp.query import build_fts_query


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("sharpe", '"sharpe"*'),
        ("sharpe ratio", '"sharpe"* AND "ratio"*'),
        ("шарп коэффициент", '"шарп"* AND "коэффициент"*'),
        ('"risk free"', '"risk free"'),
        ("BTC-USD (2024)", '"BTC"* AND "USD"* AND "2024"*'),
    ],
)
def test_auto_mode_uses_prefix_and_and(query, expected):
    assert build_fts_query(query) == expected


def test_raw_mode_passes_query_through():
    assert build_fts_query("sharpe AND NOT ratio", mode="raw") == "sharpe AND NOT ratio"


def test_any_mode_joins_terms_with_or():
    assert build_fts_query("шарп коэффициент", mode="any") == '"шарп"* OR "коэффициент"*'


def test_empty_query_rejected():
    with pytest.raises(ValueError):
        build_fts_query("   ")


def test_query_without_words_rejected():
    with pytest.raises(ValueError):
        build_fts_query("--- ***")


def test_unknown_mode_rejected():
    with pytest.raises(ValueError):
        build_fts_query("x", mode="fuzzy")
