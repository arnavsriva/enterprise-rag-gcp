"""10-K HTML (inline XBRL) -> clean text -> sections by Item.

Text extraction keeps paragraph structure (one paragraph per line) and flattens tables into
`cell | cell | cell` rows so financial figures stay next to their labels.

Item splitting. 10-Ks mention each "Item N" heading many times: table of contents, per-page
running headers, cross-references, and the real heading. So we:
  1. cut the text at every heading-like line and label each segment with its Item;
  2. merge adjacent segments with the same label (per-page running headers);
  3. for each Item, the *longest* run starts a new section; shorter runs (e.g. the table of
     contents) stay attached to the section they sit in, so no text is ever dropped.
Several issuers (JPM, XOM, CVX, DIS, NVDA) put a stub under Items 7/8 ("see Financial
Section") and append the MD&A and financial statements after Part III/IV. Inside such an
oversized late section we repeat the procedure with MD&A / financial-statement sub-headings
so that material is labelled Item 7 / Item 8. If too few Items are found we fall back to one
unsectioned document rather than guessing.
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass
from itertools import pairwise

from bs4 import BeautifulSoup, Tag, XMLParsedAsHTMLWarning
from bs4.element import NavigableString

ITEM_TITLES: dict[str, str] = {
    "1": "Business",
    "1A": "Risk Factors",
    "1B": "Unresolved Staff Comments",
    "1C": "Cybersecurity",
    "2": "Properties",
    "3": "Legal Proceedings",
    "4": "Mine Safety Disclosures",
    "5": "Market for Registrant's Common Equity, Related Stockholder Matters and Issuer Purchases of Equity Securities",
    "6": "[Reserved]",
    "7": "Management's Discussion and Analysis of Financial Condition and Results of Operations",
    "7A": "Quantitative and Qualitative Disclosures About Market Risk",
    "8": "Financial Statements and Supplementary Data",
    "9": "Changes in and Disagreements with Accountants on Accounting and Financial Disclosure",
    "9A": "Controls and Procedures",
    "9B": "Other Information",
    "9C": "Disclosure Regarding Foreign Jurisdictions that Prevent Inspections",
    "10": "Directors, Executive Officers and Corporate Governance",
    "11": "Executive Compensation",
    "12": "Security Ownership of Certain Beneficial Owners and Management and Related Stockholder Matters",
    "13": "Certain Relationships and Related Transactions, and Director Independence",
    "14": "Principal Accountant Fees and Services",
    "15": "Exhibits and Financial Statement Schedules",
    "16": "Form 10-K Summary",
}

# Minimum distinct Items for the sectioning to be trusted.
MIN_ITEMS_FOR_SECTIONING = 6

_BLOCK_TAGS = [
    "p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "header", "footer", "blockquote", "pre", "hr", "center",
]  # fmt: skip

_HEADING_RE = re.compile(
    "^(?:part\\s+[iv]+\\s*[,.\\-\u2013\u2014:]?\\s*)?item\\s*(\\d{1,2}\\s*[a-c]?)\\b\\s*[.:\\-\u2013\u2014]?\\s*(.*)$",
    re.IGNORECASE,
)
_PAGE_NOISE_RE = re.compile(r"^(?:\d{1,3}|[ivxlc]{1,6}|table of contents|page \d+)$", re.IGNORECASE)
_HIDDEN_STYLE_RE = re.compile(r"display\s*:\s*none", re.IGNORECASE)


@dataclass(frozen=True)
class Section:
    item: str | None  # e.g. "1A"; None = front matter or unsectioned document
    title: str
    text: str


# ---------------------------------------------------------------- HTML -> text


def _cell_text(cell: Tag) -> str:
    return " ".join(cell.get_text(" ", strip=True).split())


def _flatten_table(table: Tag) -> str:
    """One line per row. SEC tables split '$' and ')' / '%' into their own cells; re-join them."""
    rows: list[str] = []
    for tr in table.find_all("tr"):
        cells: list[str] = []
        for raw in (_cell_text(td) for td in tr.find_all(["td", "th"])):
            if not raw:
                continue
            closes_previous = raw in {")", "%", ")%", "%)"}
            opened_by_previous = bool(cells) and cells[-1] in {"$", "(", "$(", "($"}
            if cells and (closes_previous or opened_by_previous):
                cells[-1] += raw
            else:
                cells.append(raw)
        cells = [c for c in cells if c not in {"$", "("}]
        if cells:
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def html_to_text(html: bytes | str) -> str:
    # Filings are XHTML (inline XBRL); the lenient HTML parser handles their quirks best.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(html, "lxml")

    for tag in soup(["script", "style", "head", "title", "noscript"]):
        tag.decompose()
    # Inline XBRL header (hidden machine-readable facts) and any other hidden blocks.
    for tag in soup.find_all(lambda t: t.name in {"ix:header", "header"} and t.find("ix:hidden")):
        tag.decompose()
    for tag in soup.find_all(style=_HIDDEN_STYLE_RE):
        tag.decompose()

    for table in soup.find_all("table"):
        if table.find_parent("table") is not None:
            continue  # nested tables are flattened with their outer table
        flat = _flatten_table(table)
        table.replace_with(NavigableString(f"\n{flat}\n" if flat else "\n"))

    for tag in soup.find_all(_BLOCK_TAGS):
        if tag.name in {"br", "hr"}:
            tag.replace_with(NavigableString("\n"))
        else:
            tag.insert_before(NavigableString("\n"))
            tag.insert_after(NavigableString("\n"))

    text = soup.get_text()
    lines = []
    for line in text.replace("\xa0", " ").replace("​", "").splitlines():
        line = " ".join(line.split())
        if line and not _PAGE_NOISE_RE.match(line):
            lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------- text -> sections

_LATE_ITEMS = {"10", "11", "12", "13", "14", "15", "16"}
_APPENDED_MIN_CHARS = 50_000  # a late Item this large contains appended MD&A / financials
_MDA_RE = re.compile("^management[\u2019']s discussion and analysis", re.IGNORECASE)
_FS_RE = re.compile(
    r"^(?:report of independent registered public accounting firm"
    r"|consolidated (?:statements?|balance sheets?) of"
    r"|notes to (?:the )?consolidated financial statements)",
    re.IGNORECASE,
)


def _normalise_item(raw: str) -> str:
    return re.sub(r"\s+", "", raw).upper()


def _item_headings(lines: list[str]) -> list[tuple[int, str]]:
    found = []
    for i, line in enumerate(lines):
        if len(line) > 200:
            continue
        m = _HEADING_RE.match(line)
        if m and _normalise_item(m.group(1)) in ITEM_TITLES:
            found.append((i, _normalise_item(m.group(1))))
    return found


def _appended_headings(lines: list[str]) -> list[tuple[int, str]]:
    found = []
    for i, line in enumerate(lines):
        if len(line) <= 150:
            if _MDA_RE.match(line):
                found.append((i, "7"))
            elif _FS_RE.match(line):
                found.append((i, "8"))
    return found


@dataclass
class _Run:
    label: str
    start: int
    end: int


def _assign(
    lines: list[str], heads: list[tuple[int, str]], lead_label: str | None
) -> list[tuple[str | None, list[str]]]:
    """Split `lines` into labelled sections using the longest-run rule (see module docstring).

    Text before the first chosen run gets `lead_label`. Returns (label, lines) in order.
    """
    if not heads:
        return [(lead_label, lines)]
    runs: list[_Run] = []
    for (start, label), (end, _) in pairwise([*heads, (len(lines), "")]):
        if runs and runs[-1].label == label:
            runs[-1].end = end
        else:
            runs.append(_Run(label, start, end))

    def size(run: _Run) -> int:
        return sum(len(x) for x in lines[run.start : run.end])

    longest: dict[str, _Run] = {}
    for run in runs:
        if run.label not in longest or size(run) > size(longest[run.label]):
            longest[run.label] = run
    chosen = {id(r) for r in longest.values()}

    sections: list[tuple[str | None, list[str]]] = [(lead_label, lines[: heads[0][0]])]
    for run in runs:
        body = lines[run.start : run.end]
        if id(run) in chosen:
            sections.append((run.label, list(body)))
        else:
            sections[-1][1].extend(body)
    return [(label, body) for label, body in sections if body]


def split_items(text: str) -> list[Section]:
    lines = text.split("\n")
    heads = _item_headings(lines)
    if len({label for _, label in heads}) < MIN_ITEMS_FOR_SECTIONING:
        return [Section(item=None, title="Full document", text=text)]

    sections: list[Section] = []
    for label, body in _assign(lines, heads, lead_label=None):
        is_appended = label in _LATE_ITEMS and sum(len(x) for x in body) > _APPENDED_MIN_CHARS
        parts = _assign(body, _appended_headings(body), label) if is_appended else [(label, body)]
        for item, part in parts:
            title = ITEM_TITLES[item] if item else "Front matter"
            sections.append(Section(item, title, "\n".join(part)))
    return sections


def parse_10k(html: bytes | str) -> list[Section]:
    return [s for s in split_items(html_to_text(html)) if s.text.strip()]
