from pathlib import Path

import pytest

import ingest.parse as parse
from ingest.parse import html_to_text, parse_10k, split_items

FIXTURE = Path(__file__).parent / "fixtures" / "sample_10k.htm"


@pytest.fixture
def text() -> str:
    return html_to_text(FIXTURE.read_bytes())


@pytest.fixture(autouse=True)
def small_thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    # The fixture is tiny; scale the size-based heuristics down to match.
    monkeypatch.setattr(parse, "MIN_ITEMS_FOR_SECTIONING", 4)
    monkeypatch.setattr(parse, "_APPENDED_MIN_CHARS", 100)


def test_hidden_xbrl_style_and_page_numbers_removed(text: str) -> None:
    assert "HIDDEN_XBRL_FACT" not in text
    assert "color: red" not in text
    assert "\n3\n" not in f"\n{text}\n"


def test_tables_flattened_with_currency_and_signs_merged(text: str) -> None:
    assert "Net sales | $1,234 | $1,100" in text
    assert "Operating margin | 12% | (3)" in text


def test_longest_run_wins_over_table_of_contents(text: str) -> None:
    by_item = {s.item: s.text for s in split_items(text)}
    assert "BUSINESS_MARKER" in by_item["1"]
    assert "RISK_MARKER" in by_item["1A"]


def test_running_headers_do_not_split_a_section(text: str) -> None:
    by_item = {s.item: s.text for s in split_items(text)}
    assert "RUNNING_HEADER_CONTINUATION" in by_item["1"]


def test_appended_financial_section_is_relabelled(text: str) -> None:
    sections = split_items(text)
    labelled = {
        (s.item, marker)
        for s in sections
        for marker in ("APPENDED_MDA_MARKER", "$1,234")
        if marker in s.text
    }
    assert ("7", "APPENDED_MDA_MARKER") in labelled
    assert ("8", "$1,234") in labelled
    exhibits = [s.text for s in sections if s.item == "15"]
    assert any("EXHIBIT_LIST" in t for t in exhibits)


def test_no_text_lost_or_duplicated(text: str) -> None:
    joined = "\n".join(s.text for s in split_items(text))
    assert sorted(joined.split("\n")) == sorted(text.split("\n"))


def test_front_matter_kept(text: str) -> None:
    first = split_items(text)[0]
    assert first.item is None and "FORM 10-K" in first.text


def test_falls_back_to_single_section_when_too_few_items(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(parse, "MIN_ITEMS_FOR_SECTIONING", 50)
    sections = parse_10k(FIXTURE.read_bytes())
    assert len(sections) == 1 and sections[0].item is None
