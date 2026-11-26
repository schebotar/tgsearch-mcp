import asyncio
import re
from pathlib import Path

import pytest

from tgsearch_mcp import server

ROOT = Path(__file__).resolve().parents[1]
TOOL_PATTERN = re.compile(r"^\|\s*`(tg_[a-z_]+)`\s*\|", re.MULTILINE)


def catalog() -> set[str]:
    return {tool.name for tool in asyncio.run(server.mcp.list_tools())}


def section(text: str, heading: str) -> str:
    """Body of a `## heading` section, up to the next `## ` heading."""
    marker = f"## {heading}"
    start = text.index(marker) + len(marker)
    rest = text[start:]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def documented_tools(path: Path, heading: str) -> list[str]:
    return TOOL_PATTERN.findall(section(path.read_text(encoding="utf-8"), heading))


def test_readme_lists_every_tool():
    assert set(documented_tools(ROOT / "README.md", "Tools")) == catalog()


def test_agents_md_lists_every_tool():
    assert set(documented_tools(ROOT / "AGENTS.md", "Каталог инструментов")) == catalog()


def test_docs_have_no_duplicate_or_missing_tools():
    expected = catalog()
    for path, heading in (
        (ROOT / "README.md", "Tools"),
        (ROOT / "AGENTS.md", "Каталог инструментов"),
    ):
        documented = documented_tools(path, heading)
        assert len(documented) == len(set(documented)), f"{path.name}: дубли инструментов"
        assert set(documented) == expected, f"{path.name}: состав не совпадает с сервером"


def test_verify_expected_tools_matches_catalog():
    expected = len(catalog())
    for name in ("verify.py", "smoke.py"):
        source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        match = re.search(r"^EXPECTED_TOOLS\s*=\s*(\d+)\s*$", source, re.MULTILINE)
        assert match, f"EXPECTED_TOOLS не найден в scripts/{name}"
        assert int(match.group(1)) == expected, name


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
