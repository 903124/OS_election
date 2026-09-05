"""
presidential_elections.py — Parse **county-level** U.S. presidential election
results from the Wikipedia article series

    "{year} United States presidential election in {State}"

fetched via the MediaWiki Action API (batched, rate-limited: a full
51-jurisdiction cycle is only ~2 HTTP requests thanks to 50-title batches).

For each presidential year (divisible by 4) in [start_year, end_year] the
pipeline fetches one article per state plus Washington, D.C., and parses the
subdivision-level results table. All 51 jurisdictions share the same
two-row-header table layout, only the heading and first column differ:

    Alabama..Wyoming   "By county"                -> County
    Louisiana          "By parish"                -> Parish
    Virginia           "By county and independent
                        city"                     -> County/Independent city
    Alaska             "By borough and census
                        area (estimates)"         -> Borough/Census area
    Washington, D.C.   "Results by ward"          -> Ward

Table shape (Indiana 2024 shown; cells may carry {{party shading/...}},
style attributes in any order, <ref>s, links, negative margins):

    {|width="60%" class="wikitable sortable"
    ! rowspan="2" |[[List of counties in Indiana|County]]
    ! colspan="2" |Donald Trump<br />Republican
    ! colspan="2" |Kamala Harris<br />Democratic
    ! colspan="2" |Various candidates<br />Other parties
    ! colspan="2" |Margin
    ! rowspan="2" |Total
    |-
    ! data-sort-type="number" |#
    ! data-sort-type="number" |%  ... (one pair per candidate group)
    |-
    | {{party shading/Republican}} |[[Adams County, Indiana|Adams]]
    | {{party shading/Republican}} |10,528
    | {{party shading/Republican}} |75.28%
    ...
    |-
    !Totals!!1,720,347!!58.43%!!1,163,603!!39.52%!!60,386!!2.05%!!556,744!!18.91%!!2,944,336
    |}

The parser mats every table through an HTML-style rowspan/colspan grid, so
header variants (linked labels, <ref>s, attribute order, stray empty row
separators, {{Update}} banners before the table) are tolerated, and the
trailing "Totals" row is captured for a per-candidate cross-check against
the sum of county rows (recorded in the metadata JSON).

Output columns (written under *output_dir*, default ``data/presidential/``):
    year, state, state_code, county, subdivision_type, candidate, party,
    votes, percentage, total_votes, winning_party

    presidential_results_{year}.csv   per year (all 50 states + DC)
    presidential_results_all.csv      combined across the requested range
    presidential_metadata_{ts}.json   per-state coverage + totals cross-check

Usage:
    python cli.py presidential --start-year 2018 --end-year 2024
    python presidential_elections.py
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import time
from typing import Dict, List, Optional, Tuple

import pandas as pd

from wiki_utils import (
    WikiAPIClient,
    even_years,
    get_default_client,
)

logger = logging.getLogger("presidential_elections")

# ────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ────────────────────────────────────────────────────────────────────────────

#: (title suffix, display name, state code) for all 50 states + D.C.
#: Washington needs the "(state)" disambiguator, D.C. the comma form.
PLACES: List[Tuple[str, str, str]] = [
    ("Alabama", "Alabama", "AL"),
    ("Alaska", "Alaska", "AK"),
    ("Arizona", "Arizona", "AZ"),
    ("Arkansas", "Arkansas", "AR"),
    ("California", "California", "CA"),
    ("Colorado", "Colorado", "CO"),
    ("Connecticut", "Connecticut", "CT"),
    ("Delaware", "Delaware", "DE"),
    ("Florida", "Florida", "FL"),
    ("Georgia", "Georgia", "GA"),
    ("Hawaii", "Hawaii", "HI"),
    ("Idaho", "Idaho", "ID"),
    ("Illinois", "Illinois", "IL"),
    ("Indiana", "Indiana", "IN"),
    ("Iowa", "Iowa", "IA"),
    ("Kansas", "Kansas", "KS"),
    ("Kentucky", "Kentucky", "KY"),
    ("Louisiana", "Louisiana", "LA"),
    ("Maine", "Maine", "ME"),
    ("Maryland", "Maryland", "MD"),
    ("Massachusetts", "Massachusetts", "MA"),
    ("Michigan", "Michigan", "MI"),
    ("Minnesota", "Minnesota", "MN"),
    ("Mississippi", "Mississippi", "MS"),
    ("Missouri", "Missouri", "MO"),
    ("Montana", "Montana", "MT"),
    ("Nebraska", "Nebraska", "NE"),
    ("Nevada", "Nevada", "NV"),
    ("New Hampshire", "New Hampshire", "NH"),
    ("New Jersey", "New Jersey", "NJ"),
    ("New Mexico", "New Mexico", "NM"),
    ("New York", "New York", "NY"),
    ("North Carolina", "North Carolina", "NC"),
    ("North Dakota", "North Dakota", "ND"),
    ("Ohio", "Ohio", "OH"),
    ("Oklahoma", "Oklahoma", "OK"),
    ("Oregon", "Oregon", "OR"),
    ("Pennsylvania", "Pennsylvania", "PA"),
    ("Rhode Island", "Rhode Island", "RI"),
    ("South Carolina", "South Carolina", "SC"),
    ("South Dakota", "South Dakota", "SD"),
    ("Tennessee", "Tennessee", "TN"),
    ("Texas", "Texas", "TX"),
    ("Utah", "Utah", "UT"),
    ("Vermont", "Vermont", "VT"),
    ("Virginia", "Virginia", "VA"),
    ("Washington (state)", "Washington", "WA"),
    ("West Virginia", "West Virginia", "WV"),
    ("Wisconsin", "Wisconsin", "WI"),
    ("Wyoming", "Wyoming", "WY"),
    ("Washington, D.C.", "District of Columbia", "DC"),
]

#: Subdivision-level results headings across the article series (level 2-4).
#: Note: primary-election tables often share these headings (e.g. West
#: Virginia 2012 "Results by county" under the Republican primary) —
#: parse_state_page therefore tries EVERY matching heading and prefers the
#: first table that looks like a general-election results table.
_COUNTY_HEADING_RE = re.compile(
    r"(?im)^={2,4}\s*("
    r"by county(?: and independent city)?"
    r"|by parish"
    r"|by borough[^=\n]*"
    r"|results by ward"
    r"|by ward"
    r"|results by county[^=\n]*"
    r"|county results"
    r"|by city and county"
    r")\s*={2,4}\s*$"
)

_ANY_HEADING_RE = re.compile(r"(?m)^={2,6}[^=].*?={2,6}\s*$")

_COLUMNS = [
    "year", "state", "state_code", "county", "subdivision_type",
    "candidate", "party", "votes", "percentage", "total_votes", "winning_party",
]

_PARTY_ALIASES: Dict[str, str] = {
    "republican": "Republican",
    "republican party": "Republican",
    "democratic": "Democratic",
    "democrat": "Democratic",
    "democratic party": "Democratic",
    "democratic-farmer-labor": "Democratic-Farmer-Labor",
    "dfl": "Democratic-Farmer-Labor",
    "democratic-npl": "Democratic-NPL",
    "democratic-nonpartisan league": "Democratic-NPL",
    "libertarian": "Libertarian",
    "libertarian party": "Libertarian",
    "green": "Green",
    "green party": "Green",
    "independent": "Independent",
    "independent politician": "Independent",
    "constitution": "Constitution",
    "constitution party": "Constitution",
    "reform": "Reform",
    "reform party": "Reform",
    "write-in": "Write-in",
    "other parties": "Other",
    "all others": "Other",
    "others": "Other",
    "various candidates": "Other",
    "no party preference": "No party preference",
}

_SHADING_RE = re.compile(r"\{\{\s*party shading/([^|}]+)", re.I)
_PCT_VALUE_RE = re.compile(r"-?\d+(?:\.\d+)?")

#: first-column labels that mark the subdivision column
_SUBDIV_RE = re.compile(
    r"county|parish|borough|census area|ward|independent city|municipal|locality",
    re.I,
)


def presidential_years(start_year: int, end_year: int) -> List[int]:
    """Presidential cycles (divisible by 4) within the inclusive even-year
    range: ``presidential_years(2018, 2024)`` -> ``[2020, 2024]``."""
    return [y for y in even_years(start_year, end_year) if y % 4 == 0]


def page_title(year: int, suffix: str) -> str:
    return f"{year} United States presidential election in {suffix}"


# ────────────────────────────────────────────────────────────────────────────
# LOW-LEVEL WIKITEXT HELPERS
# ────────────────────────────────────────────────────────────────────────────

def _strip_refs(text: str) -> str:
    text = re.sub(r"<ref[^>]*/>", "", text)
    text = re.sub(r"<ref[^>]*>.*?</ref>", "", text, flags=re.S | re.I)
    return text


def _clean_text(text: str) -> str:
    """Normalise a table cell / header label to plain readable text."""
    t = text or ""
    t = re.sub(r"<!--.*?-->", "", t, flags=re.S)
    t = _strip_refs(t)
    t = re.sub(r"\{\{(?:Party|party) shading/[^}|]*\|?", "", t)
    t = re.sub(r"\{\{(?:nowrap|nobr)\|([^}]*)\}\}", r"\1", t, flags=re.I)
    t = re.sub(r"\{\{[^{}]*\}\}", "", t)          # leftover simple templates
    t = re.sub(r"\{\{[^{}]*\}\}", "", t)          # second pass for nesting
    t = re.sub(r"<[^>]+>", " ", t)                # tags (br, sup, span...)
    t = t.replace("'''", "").replace("''", "")
    t = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", r"\1", t)
    t = html.unescape(t)
    t = re.sub(r"\s+", " ", t)
    return t.strip(" |\t\n")


def _shading_party(raw_cell: str) -> str:
    """Winning party hinted by {{party shading/X}} in a cell ('Others'→Other)."""
    m = _SHADING_RE.search(raw_cell or "")
    if not m:
        return ""
    return _normalize_party(m.group(1))


def _normalize_party(raw: str) -> str:
    p = _clean_text(raw or "").strip().lower().rstrip(".")
    p = re.sub(r"[\u2010\u2013\u2014]", "-", p)  # hyphen/en-dash/em-dash -> '-'
    if not p:
        return ""
    if p in _PARTY_ALIASES:
        return _PARTY_ALIASES[p]
    p2 = re.sub(r"\s+party(?: of the)?(?: \(united states\))?$", "", p).strip()
    return _PARTY_ALIASES.get(p2, p.title())


def _parse_number(text: str, kind: str):
    """Parse a votes (#) or percentage (%) cell to int / float, or None."""
    t = _clean_text(text)
    t = t.replace(",", "").replace("−", "-")
    m = _PCT_VALUE_RE.search(t)
    if not m:
        return None
    try:
        val = float(m.group(0))
    except ValueError:
        return None
    return int(round(val)) if kind == "votes" else val


def _eval_percentage_template(raw: str) -> Optional[float]:
    """'{{percentage|123|1000}}' -> 12.3 (rare in these tables, safety net)."""
    m = re.search(r"\{\{\s*percentage\s*\|\s*(-?[\d.,]+)\s*\|\s*(-?[\d.,]+)", raw, re.I)
    if not m:
        return None
    try:
        num = float(m.group(1).replace(",", ""))
        den = float(m.group(2).replace(",", ""))
        return round(num / den * 100, 2) if den else None
    except ValueError:
        return None


# ────────────────────────────────────────────────────────────────────────────
# TABLE GRID (rowspan / colspan aware, HTML-table algorithm)
# ────────────────────────────────────────────────────────────────────────────

def _depth0_split(text: str, separator: str) -> List[str]:
    """Split *text* at *separator* ('||' or '!!') occurrences that sit outside
    [[..]] links and {{..}} templates."""
    parts, buf = [], []
    link_depth = tpl_depth = 0
    i, n = 0, len(text)
    while i < n:
        two = text[i:i + 2]
        if two == "[[":
            link_depth += 1
            buf.append(two)
            i += 2
            continue
        if two == "]]":
            link_depth = max(0, link_depth - 1)
            buf.append(two)
            i += 2
            continue
        if two == "{{":
            tpl_depth += 1
            buf.append(two)
            i += 2
            continue
        if two == "}}":
            tpl_depth = max(0, tpl_depth - 1)
            buf.append(two)
            i += 2
            continue
        if (link_depth == 0 and tpl_depth == 0
                and text.startswith(separator, i)):
            parts.append("".join(buf))
            buf = []
            i += 2
            continue
        buf.append(text[i])
        i += 1
    parts.append("".join(buf))
    return parts


def _split_attrs_content(raw: str) -> Tuple[str, str]:
    """Split a raw cell body into (attribute string, content).

    Wikitext cells look like ``| attrs | content`` — the content follows the
    LAST top-level pipe that is not inside [[..]] / {{..}} (this also handles
    the unquoted-attribute form ``id=7A rowspan="2" data-sort-value="13" |7A``
    known from the 2018 Minnesota House tables).
    """
    link_depth = tpl_depth = 0
    last = -1
    i, n = 0, len(raw)
    while i < n:
        two = raw[i:i + 2]
        if two == "[[":
            link_depth += 1
            i += 2
            continue
        if two == "]]":
            link_depth = max(0, link_depth - 1)
            i += 2
            continue
        if two == "{{":
            tpl_depth += 1
            i += 2
            continue
        if two == "}}":
            tpl_depth = max(0, tpl_depth - 1)
            i += 2
            continue
        if raw[i] == "|" and link_depth == 0 and tpl_depth == 0:
            last = i
        i += 1
    if last < 0:
        return "", raw
    return raw[:last], raw[last + 1:]


def _attrs_dict(attrs: str) -> Dict[str, int]:
    """Extract rowspan / colspan (default 1) from a cell attribute string."""
    out = {"rowspan": 1, "colspan": 1}
    for m in re.finditer(
        r'([A-Za-z][\w:-]*)\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s|]+))', attrs
    ):
        key = m.group(1).lower()
        if key in out:
            try:
                out[key] = max(1, int(m.group(3) or m.group(4) or m.group(5)))
            except (TypeError, ValueError):
                pass
    return out


def _row_cells(chunk: str) -> List[Tuple[bool, str]]:
    """Tokenize one row chunk (text between ``|-`` lines) into cells.

    Returns a list of ``(is_header_cell, raw_body)`` tuples; ``raw_body``
    excludes the leading ``|``/``!`` marker. Chained ``||`` / ``!!`` cells on
    one line are split; caption (``|+``) lines and nested ``|-`` are skipped;
    continuation lines are appended to the current cell.
    """
    cells: List[Tuple[bool, str]] = []
    cur: Optional[str] = None
    cur_header = False
    for line in chunk.split("\n"):
        s = line.strip()
        if not s:
            if cur is not None:
                cur += "\n"
            continue
        if s.startswith("|}"):
            break
        if s.startswith("|+") or s.startswith("|-"):
            continue
        if s[0] in "!|":
            header = s[0] == "!"
            parts = _depth0_split(s[1:], "!!" if header else "||")
            for part in parts:
                if cur is not None:
                    cells.append((cur_header, cur))
                cur = part
                cur_header = header
        elif cur is not None:
            cur += "\n" + s
    if cur is not None:
        cells.append((cur_header, cur))
    return cells


def _grid(table: str) -> List[Dict]:
    """Mat an entire wikitable into a list of row dicts ``{col: raw_content}``
    with rowspan/colspan expansion, like an HTML table renderer."""
    rows: List[Dict] = []
    pending: Dict[int, Tuple[str, int]] = {}
    for chunk in re.split(r"(?m)^\|-.*$", table):
        cells = _row_cells(chunk)
        if not cells:
            continue
        occupied = {c: v for c, (v, rem) in pending.items() if rem > 0}
        cur: Dict[int, str] = dict(occupied)
        col = 0
        for is_header, raw in cells:
            attrs, content = _split_attrs_content(raw)
            ad = _attrs_dict(attrs)
            rs, cs = ad["rowspan"], ad["colspan"]
            while col in cur:
                col += 1
            for k in range(cs):
                cur[col + k] = content
                if rs > 1:
                    # occupies *rs* rows counting forward (decremented below)
                    pending[col + k] = (content, rs)
            col += cs
        pending = {c: (v, r - 1) for c, (v, r) in pending.items() if r - 1 > 0}
        rows.append({"cells": cur, "header": all(h for h, _ in cells)})
    return rows


# ────────────────────────────────────────────────────────────────────────────
# COUNTY TABLE PARSER
# ────────────────────────────────────────────────────────────────────────────

def _county_section(text: str) -> str:
    """Wikitext chunk after the subdivision-results heading (any level 2-4)."""
    m = _COUNTY_HEADING_RE.search(text)
    if not m:
        return ""
    rest = text[m.end():]
    nxt = _ANY_HEADING_RE.search(rest)
    return rest[: nxt.start()] if nxt else rest


def _wikitables(section_text: str) -> List[str]:
    """Outermost ``{| ... |}`` tables inside *section_text*."""
    tables, depth, start, i = [], 0, None, 0
    while i < len(section_text):
        if section_text.startswith("{|", i):
            if depth == 0:
                start = i
            depth += 1
            i += 2
        elif section_text.startswith("|}", i):
            depth -= 1
            if depth == 0 and start is not None:
                tables.append(section_text[start:i + 2])
                start = None
            i += 2
        else:
            i += 1
    return tables


def _subdivision_type(label: str) -> str:
    low = (label or "").lower()
    if "borough" in low or "census area" in low:
        return "Borough/Census area"
    if "parish" in low:
        return "Parish"
    if "ward" in low:
        return "Ward"
    if "independent city" in low:
        return "County/Independent city"
    if "county" in low:
        return "County"
    return label.strip().title() if label.strip() else "County"


def _plan_columns(header_rows: List[Dict]) -> Optional[Dict]:
    """Classify columns from the (1- or 2-row) header grid.

    Returns ``{"county_col", "total_col", "subdivision", "candidates": [...]}``
    where each candidate is ``{"name", "party", "votes_col", "pct_col"}``
    (votes_col / pct_col may be None when only one metric exists).
    """
    r0 = header_rows[0]["cells"]
    r1 = header_rows[1]["cells"] if len(header_rows) > 1 else {}
    if not r0:
        return None
    ncol = max(r0) + 1
    county_col = total_col = None
    subdivision_label = ""
    groups: List[Dict] = []
    skip_cols = set()
    c = 0
    while c < ncol:
        raw_cell = r0.get(c, "")          # keep raw: <br /> splits name/party
        raw0 = _clean_text(raw_cell)
        low0 = raw0.lower()
        raw1 = _clean_text(r1.get(c, "")) if c in r1 else ""
        # sub-column of a colspan pair: distinct label on the second row
        if low0 and raw1 and raw1.lower() != low0:
            gcols = [c]
            while (c + 1 < ncol
                   and _clean_text(r0.get(c + 1, "")).lower() == low0
                   and (c + 1) not in skip_cols):
                c += 1
                gcols.append(c)
            if "margin" in low0 or low0.startswith("total"):
                skip_cols.update(gcols)          # derived column, not a candidate
            else:
                groups.append({"raw_label": raw_cell, "cols": gcols})
        else:
            # singleton (rowspan=2 through both header rows)
            if _SUBDIV_RE.search(low0) and county_col is None:
                county_col = c
                subdivision_label = raw0
            elif low0.startswith("total") and total_col is None:
                total_col = c
            # 'margin' / other singletons are ignored
        c += 1

    if county_col is None or not groups:
        return None

    body_rows = []  # filled by caller for calibration; use header first
    candidates = []
    for g in groups:
        name, party = _candidate_from_label(g["raw_label"])
        vc = pc = None
        if len(g["cols"]) >= 2:
            for col in g["cols"]:
                sub = _clean_text(r1.get(col, "")).lower().strip(".")
                if sub in ("#", "votes", "vote", "votes cast", "count", "number", "n"):
                    vc = col
                elif sub in ("%", "percent", "pct", "pct."):
                    pc = col
        candidates.append({
            "name": name, "party": party,
            "cols": g["cols"], "votes_col": vc, "pct_col": pc,
        })
    return {
        "county_col": county_col,
        "total_col": total_col,
        "subdivision": _subdivision_type(subdivision_label),
        "candidates": candidates,
        "n_header_rows": len(header_rows),
    }


def _candidate_from_label(label: str) -> Tuple[str, str]:
    """'Donald Trump<br />Republican' -> ('Donald Trump', 'Republican').

    Splits on <br /> or a raw NEWLINE before cleaning — several article
    generations put the party on a second line inside the header cell, and
    cleaning would fuse name and party into one token."""
    parts = re.split(r"<br\s*/?>|\n", label or "", maxsplit=1)
    name = _clean_text(parts[0])
    party = _normalize_party(parts[1]) if len(parts) > 1 else ""
    if name.lower() in ("various candidates", "all others", "others",
                        "other candidates", "other"):
        name = "Other"
    return name, party


def _calibrate_vote_pct_columns(
    plan: Dict, rows: List[Dict], first_body: int
) -> None:
    """Fill missing votes_col / pct_col assignments from the body data:
    the column whose values mostly carry '%' is the percentage column."""
    for cand in plan["candidates"]:
        if cand["votes_col"] is not None and cand["pct_col"] is not None:
            continue
        stats = {col: [0, 0] for col in cand["cols"]}  # [pct_hits, numeric]
        for row in rows[first_body:first_body + 400]:
            if row["header"]:
                continue
            for col in cand["cols"]:
                raw = row["cells"].get(col, "")
                txt = _clean_text(raw)
                if not txt:
                    continue
                if "%" in txt:
                    stats[col][0] += 1
                elif _PCT_VALUE_RE.search(txt):
                    stats[col][1] += 1
        if cand["votes_col"] is None and cand["pct_col"] is None:
            if len(cand["cols"]) == 2:
                a, b = cand["cols"]
                if stats[a][0] >= stats[b][0]:
                    cand["pct_col"], cand["votes_col"] = a, b
                else:
                    cand["pct_col"], cand["votes_col"] = b, a
            elif len(cand["cols"]) == 1:
                col = cand["cols"][0]
                if stats[col][0] > stats[col][1]:
                    cand["pct_col"] = col
                else:
                    cand["votes_col"] = col
        elif cand["votes_col"] is None:
            others = [c for c in cand["cols"] if c != cand["pct_col"]]
            cand["votes_col"] = others[0] if others else None
        elif cand["pct_col"] is None:
            others = [c for c in cand["cols"] if c != cand["votes_col"]]
            cand["pct_col"] = others[0] if others else None


_TOTALS_LABEL_RE = re.compile(
    r"^(?:state\s+)?(?:totals?|total votes?|statewide)$", re.I
)


def parse_county_table(
    table: str, year: int, state: str, state_code: str
) -> Tuple[pd.DataFrame, Dict]:
    """Parse one subdivision-results wikitable.

    Returns ``(records_df, info)`` where *info* carries ``totals_row``
    (per-candidate votes from the trailing Totals row, when present),
    ``county_sums`` and ``counties``.
    """
    rows = _grid(table)

    # leading header rows (stop at the first body row)
    header_rows: List[Dict] = []
    first_body = 0
    for idx, row in enumerate(rows):
        if not row["cells"]:
            continue
        if row["header"] and not any(
            r["header"] is False for r in rows[:idx]
        ):
            header_rows.append(row)
            first_body = idx + 1
        else:
            break

    plan = _plan_columns(header_rows)
    if plan is None:
        return pd.DataFrame(columns=_COLUMNS), {}

    _calibrate_vote_pct_columns(plan, rows, first_body)
    county_col, total_col = plan["county_col"], plan["total_col"]
    subdivision = plan["subdivision"]

    records: List[Dict] = []
    totals_row: Dict[str, int] = {}
    counties_seen = set()

    for row in rows[first_body:]:
        cells = row["cells"]
        first_label = _clean_text(cells.get(county_col, ""))
        if row["header"]:
            if _TOTALS_LABEL_RE.match(first_label):
                for cand in plan["candidates"]:
                    if cand["votes_col"] is not None:
                        v = _parse_number(cells.get(cand["votes_col"], ""), "votes")
                        if v is not None:
                            totals_row[cand["name"]] = v
            continue
        county = _clean_text(cells.get(county_col, ""))
        if not county:
            continue

        row_records: List[Dict] = []
        group_votes: List[Tuple[Dict, Optional[int]]] = []
        for cand in plan["candidates"]:
            votes = pct = None
            if cand["votes_col"] is not None:
                votes = _parse_number(cells.get(cand["votes_col"], ""), "votes")
            if cand["pct_col"] is not None:
                raw_pct = cells.get(cand["pct_col"], "")
                pct = _parse_number(raw_pct, "pct")
                if pct is None:
                    pct = _eval_percentage_template(raw_pct)
            group_votes.append((cand, votes))
            if votes is None and pct is None:
                continue
            total_votes = (
                _parse_number(cells.get(total_col, ""), "votes")
                if total_col is not None else None
            )
            rec = {
                "year": year,
                "state": state,
                "state_code": state_code,
                "county": county,
                "subdivision_type": subdivision,
                "candidate": cand["name"],
                "party": cand["party"],
                "votes": votes,
                "percentage": pct,
                "total_votes": total_votes,
                "winning_party": "",  # filled below
            }
            records.append(rec)
            row_records.append(rec)
        counties_seen.add(county)

        # winning party: top votes among the named candidates in this row
        scored = [(v, cand) for cand, v in group_votes if v is not None]
        if scored:
            best = max(scored, key=lambda t: (t[0],))[1]
            win = best["party"] or ("Other" if best["name"] == "Other" else best["name"])
        else:
            win = _shading_party(cells.get(county_col, ""))
        for rec in row_records:
            rec["winning_party"] = win

    df = pd.DataFrame(records, columns=_COLUMNS)
    if len(df):
        df = df.drop_duplicates(
            subset=["county", "candidate"], keep="first"
        ).reset_index(drop=True)
        df["votes"] = df["votes"].astype("Int64")
        df["total_votes"] = df["total_votes"].astype("Int64")

    county_sums: Dict[str, int] = {}
    if len(df) and totals_row:
        for cand_name, tot in totals_row.items():
            s = df.loc[df["candidate"] == cand_name, "votes"].dropna()
            if len(s):
                county_sums[cand_name] = int(s.sum())

    info = {
        "counties": len(counties_seen),
        "rows": int(len(df)),
        "totals_row": totals_row,
        "county_sums": county_sums,
        "has_total": total_col is not None,
        "complete_groups": bool(plan["candidates"]) and all(
            c["votes_col"] is not None and c["pct_col"] is not None
            for c in plan["candidates"]
        ),
    }
    return df, info


# ────────────────────────────────────────────────────────────────────────────
# STATE PAGE PARSER
# ────────────────────────────────────────────────────────────────────────────

def parse_state_page(
    text: str, year: int, state: str, state_code: str
) -> Tuple[pd.DataFrame, Dict]:
    """Parse one "{year} United States presidential election in {State}"
    article into county-level records.

    Heading sections are tried in document order and the first table that
    yields rows AND looks like a general-election table (Total column or
    complete #/% candidate pairs) wins — this skips primary-election tables
    that share the same heading text (WV 2012, DC 2012). When no heading
    matches at all, the whole page is scanned as a fallback (DC 2004).
    """
    candidate_tables: List[str] = []
    for m in _COUNTY_HEADING_RE.finditer(text):
        rest = text[m.end():]
        nxt = _ANY_HEADING_RE.search(rest)
        section = rest[: nxt.start()] if nxt else rest
        candidate_tables.extend(_wikitables(section))
    if not candidate_tables:
        candidate_tables = _wikitables(text)

    best_df, best_info = pd.DataFrame(columns=_COLUMNS), {}
    for table in candidate_tables:
        df, info = parse_county_table(table, year, state, state_code)
        if not len(df):
            continue
        if info.get("has_total") or info.get("complete_groups"):
            return df, info
        if not len(best_df):       # last resort: incomplete table
            best_df, best_info = df, info
    return best_df, best_info


# ────────────────────────────────────────────────────────────────────────────
# BATCH DRIVER
# ────────────────────────────────────────────────────────────────────────────

def run(
    start_year: int = 2018,
    end_year: int = 2024,
    output_dir: str = "data",
    client: Optional[WikiAPIClient] = None,
) -> pd.DataFrame:
    """CLI entry point: process presidential cycles from *start_year* to
    *end_year*, writing CSVs under ``<output_dir>/presidential/``."""
    years = presidential_years(start_year, end_year)
    if not years:
        logger.warning(
            "No presidential election year (divisible by 4) in [%d, %d] — "
            "nothing to do.", start_year, end_year,
        )
        return pd.DataFrame()
    logger.info("Presidential cycles to process: %s", years)

    pres_dir = os.path.join(output_dir, "presidential")
    os.makedirs(pres_dir, exist_ok=True)

    client = client or get_default_client()

    all_frames: List[pd.DataFrame] = []
    meta: Dict = {"years": {}, "missing_articles": []}

    for year in years:
        titles = [page_title(year, suffix) for suffix, _, _ in PLACES]
        content = client.fetch_wikitext(titles)

        frames: List[pd.DataFrame] = []
        year_meta: Dict = {}
        for suffix, state, code in PLACES:
            title = page_title(year, suffix)
            text = content.get(title)
            if not text:
                logger.warning("  %s %-22s — article missing, skipped", year, state)
                meta["missing_articles"].append(title)
                year_meta[code] = None
                continue
            df, info = parse_state_page(text, year, state, code)
            if not len(df):
                logger.warning(
                    "  %s %-22s — no county rows parsed (article lacks the "
                    "standard results table)", year, state,
                )
                year_meta[code] = {"rows": 0, "counties": 0}
                continue

            # totals cross-check (trailing 'Totals' row vs county sums)
            diffs = {}
            if info.get("totals_row") and info.get("county_sums"):
                for cand, tot in info["totals_row"].items():
                    s = info["county_sums"].get(cand)
                    if s:
                        diffs[cand] = round((s - tot) / tot * 100, 3) if tot else None

            tv = df["total_votes"].dropna()
            year_meta[code] = {
                "state": state,
                "rows": info.get("rows", int(len(df))),
                "counties": info.get("counties", 0),
                "candidates": sorted(df["candidate"].unique().tolist()),
                "total_votes": int(tv.max()) if len(tv) else None,
                "totals_row": info.get("totals_row") or None,
                "county_sums_vs_totals_pct": diffs or None,
            }
            logger.info(
                "  %s %-22s %4d rows / %3d %s", year, state, len(df),
                info.get("counties", 0),
                df["subdivision_type"].iloc[0].lower(),
            )
            frames.append(df)

        if frames:
            df_year = pd.concat(frames, ignore_index=True)
            path = os.path.join(pres_dir, f"presidential_results_{year}.csv")
            df_year.to_csv(path, index=False)
            logger.info("  saved -> %s", path)
            all_frames.append(df_year)
        meta["years"][year] = year_meta

    if all_frames:
        combined = pd.concat(all_frames, ignore_index=True)
        path = os.path.join(pres_dir, "presidential_results_all.csv")
        combined.to_csv(path, index=False)
        logger.info("Combined -> %s (%s rows)", path, f"{len(combined):,}")

        ts = time.strftime("%Y%m%d_%H%M%S")
        with open(os.path.join(pres_dir, f"presidential_metadata_{ts}.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2, default=str)
        return combined

    return pd.DataFrame()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    run()
