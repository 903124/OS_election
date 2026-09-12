"""
statewide_elections.py — Parse U.S. **statewide** executive election results
(Governor, Attorney General, Secretary of State, State Treasurer) from the
Wikipedia overview articles ("Race summary" tables), fetched via the
MediaWiki Action API.

For each even year in [start_year, end_year] up to four overview articles are
fetched in a single batched request:

    {year} United States gubernatorial elections
    {year} United States attorney general elections
    {year} United States secretary of state elections
    {year} United States state treasurer elections

(Secretary of State / State Treasurer overview articles do not exist for
2018 — those cycles are skipped and logged.)

ODD (off-year) cycles in the range are processed by default
(``include_off_years=True``; CLI ``--include-off-years``/``--no-include-off-years``)
— New Jersey and Virginia gubernatorial elections, the
Kentucky/Louisiana/Mississippi statewide slate, and occasional special
elections.  Pass ``include_off_years=False`` (CLI:
``--no-include-off-years``) to process even (federal) years only.  Odd-year
overview articles exist for every cycle since 2001, so the same "Race
summary" parsing applies.

Each overview's ``== Race summary ==`` section carries one sortable table per
scope (``===States===`` and, for governor, ``=== Territories and federal
district ===``) with rows keyed by ``! [[#State|State]]`` and a Candidates
cell holding a ``{{Plainlist|* ...}}`` bullet per candidate:

    * {{Party stripe|Republican Party (US)}}{{aye}} '''[[Kay Ivey]]''' (Republican) 59.5%

Per-race primary/general results (vote counts included) are parsed from the
per-state race articles discovered via ``{{main|...}}`` links on each
overview — the same dynamic-discovery approach as the Senate pipeline.
Race articles carry ``{{Election box ...}}`` result templates under
level-2 headings (``== Democratic primary ==``, ``== Republican primary ==``,
``== Jungle primary ==``, ``== General election ==``, ...); boxes are
classified as primary or general by the heading they sit under, so titles
without the word "primary" (e.g. Mississippi's "2019 Republican") are
handled correctly.  A conservative fallback parses simple
Candidate/Votes/% wikitables in primary sections that have no boxes;
ranked-choice multi-round tables (Virginia 2021 GOP convention) and
by-county/by-district breakdowns are intentionally skipped.

Output columns (written under *output_dir*, default ``data/statewide/``):
    year, state, state_code, office, candidate, party, percentage,
    winner, incumbent

    statewide_results_{year}.csv            per year, overview summaries
                                            (general election, % only)
    statewide_results_all.csv               combined across ALL runs on disk
    statewide_primary_results_{year}.csv    per-race primary results
                                            (votes + %, from race articles)
    statewide_primary_results_all.csv
    statewide_general_results_{year}.csv    per-race general results
                                            (votes + %, from race articles)
    statewide_general_results_all.csv

Usage:
    python cli.py statewide --start-year 2018 --end-year 2024
    python cli.py statewide --start-year 2001 --end-year 2025
    python cli.py statewide --start-year 2018 --end-year 2024 --no-include-off-years
    python statewide_elections.py
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Dict, List, Optional

import pandas as pd

from wiki_utils import (
    WikiAPIClient,
    clean_wikitext,
    even_years,
    extract_incumbent_flag,
    get_default_client,
    remove_wikilinks,
    set_default_client,
    unwrap_format_templates,
)

logger = logging.getLogger("statewide_elections")


def _odd_years(start_year: int, end_year: int) -> List[int]:
    """
    Odd (off-year) years within ``[start_year, end_year]``, inclusive.

    Defined locally — deliberately not imported from ``wiki_utils`` — so this
    module stays compatible with stock wiki_utils versions that only ship
    ``even_years``.  ``_odd_years(2019, 2024)`` → ``[2019, 2021, 2023]``.
    Returns an empty list when the range contains no odd year.
    """
    start = start_year if start_year % 2 == 1 else start_year + 1
    end = end_year if end_year % 2 == 1 else end_year - 1
    return list(range(start, end + 1, 2)) if start <= end else []


# ────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ────────────────────────────────────────────────────────────────────────────

OFFICES: Dict[str, str] = {
    "governor": "{y} United States gubernatorial elections",
    "attorney general": "{y} United States attorney general elections",
    "secretary of state": "{y} United States secretary of state elections",
    "state treasurer": "{y} United States state treasurer elections",
}

# canonical two-letter codes for states + territories that elect executives
STATE_CODES: Dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    # territories / federal district with elected executives
    "district of columbia": "DC", "guam": "GU", "guamanian": "GU",
    "northern mariana islands": "MP", "american samoa": "AS",
    "u.s. virgin islands": "VI", "united states virgin islands": "VI",
    "puerto rico": "PR",
    # longer forms appearing in race-article titles
    "new york state": "NY",
}

PARTY_NORMALIZE: Dict[str, str] = {
    "democratic party (us)": "Democratic",
    "republican party (us)": "Republican",
    "libertarian party (us)": "Libertarian",
    "green party (us)": "Green",
    "constitution party (us)": "Constitution",
    "independent american party": "Independent American",
    "progressive party (us)": "Progressive",
    "independent": "Independent",
    "independent politician": "Independent",
    "independent (politician)": "Independent",
    "no party preference (united states)": "No party preference",
    "no party preference": "No party preference",
    "nonpartisan": "Nonpartisan",
    "write-in": "Write-in",
}

_COLUMNS = [
    "year", "state", "state_code", "office", "candidate", "party",
    "percentage", "winner", "incumbent",
]

# ─── per-race primary/general outputs (from race articles) ────────────────

_RACE_COLUMNS = [
    "year", "state", "state_code", "office", "election", "row_type",
    "candidate", "party", "votes", "percentage", "winner", "incumbent",
]

# Level-2 headings that hold nominating contests.  "convention" covers the
# Virginia GOP canvass-style nominating conventions; "jungle" covers the
# Louisiana nonpartisan blanket primary.  "Lieutenant gubernatorial
# nomination" sections match "nomination" but hold no vote tables, so they
# are filtered out naturally by the box/table parsers.
_PRIMARY_HEADING_RE = re.compile(r"primary|jungle|convention|nomination", re.I)
_GENERAL_HEADING_RE = re.compile(r"general election|runoff|^results$", re.I)

# Guards against overview-level {{main}} links (no state in the title).
_NON_STATE_TITLES_RE = re.compile(
    r"united states (?:gubernatorial|attorney general|secretary of state|"
    r"state treasurer|elections?$)",
    re.I,
)


def _state_code(name: str) -> str:
    return STATE_CODES.get(name.strip().lower(), "")


def normalize_party(raw: str) -> str:
    """'Republican Party (US)' / 'Arizona Democratic Party' -> short label."""
    p = clean_wikitext(raw or "").strip()
    if not p:
        return ""
    key = p.lower().strip()
    if key in PARTY_NORMALIZE:
        return PARTY_NORMALIZE[key]
    # strip trailing "(US)" style qualifiers
    key = re.sub(r"\s*\((?:us|united states)\)\s*$", "", key).strip()
    if key in PARTY_NORMALIZE:
        return PARTY_NORMALIZE[key]
    # 'Conservative Party of New York State' / 'Utah Constitution Party'
    key = re.sub(r"\s+of\s+[a-z ]+$", "", key)
    key = re.sub(r"^(?:[a-z ]+?)\s+(?=[a-z]+ party$)", "", key)  # state prefix
    key = re.sub(r"\s+party$", "", key).strip()
    if not key:
        return ""
    return " ".join(w.capitalize() if w not in ("of", "the") else w for w in key.split())


# ────────────────────────────────────────────────────────────────────────────
# LOW-LEVEL TEXT HELPERS
# ────────────────────────────────────────────────────────────────────────────

def _strip_refs(text: str) -> str:
    text = re.sub(r"<ref[^>]*/>", "", text)
    text = re.sub(r"<ref[^>]*>.*?</ref>", "", text, flags=re.S | re.I)
    return text


def _clean_cell(text: str) -> str:
    """Normalise a table cell to plain text (links, templates, entities)."""
    t = _strip_refs(text)
    t = re.sub(r"<!--.*?-->", "", t, flags=re.S)
    t = re.sub(r"\{\{sort(?:name)?\|([^|}]+)\|([^|}]+)[^}]*\}\}",
               lambda m: f"{m.group(1).strip()} {m.group(2).strip()}", t, flags=re.I)
    t = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", r"\1", t)
    t = re.sub(r"'''?", "", t)
    t = re.sub(r"\{\{(?:Party|party) shading/[^}|]*\|?", "", t)
    t = re.sub(r"\{\{[^{}]*\}\}", "", t)  # any leftover simple template
    t = re.sub(r"<[^>]+>", " ", t)
    t = t.replace("&nbsp;", " ").replace("&amp;", "&")
    return t.strip(" |\n\t")


def _section(text: str, heading: str, level: int = 2) -> str:
    """Return the content after *heading* (== level) up to the next same-level heading."""
    pat = re.compile(r"(?m)^={%d}\s*%s\s*={%d}\s*$" % (level, re.escape(heading), level), re.I)
    m = pat.search(text)
    if not m:
        return ""
    rest = text[m.end():]
    nxt = re.search(r"(?m)^={%d}[^=].*?={%d}\s*$" % (level, level), rest)
    return rest[: nxt.start()] if nxt else rest


# ────────────────────────────────────────────────────────────────────────────
# TABLE EXTRACTION
# ────────────────────────────────────────────────────────────────────────────

def _wikitables(section_text: str) -> List[str]:
    """Split a wikitext chunk into individual {| ... |} tables (outermost)."""
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


def _table_headers(table: str) -> List[str]:
    """
    Header cell labels, in order, across ALL header rows.

    Some overview tables (e.g. 2015 gubernatorial) use a two-row header:
    ``! State | ! Incumbent | ! Results`` followed by ``! State | ! Governor
    | ! Party | ... | ! Candidates``.  Chunks that carry only '!' lines are
    header rows and are concatenated; the first chunk mixing '!' and '|'
    lines ends the header (its '!' cells are still collected).
    """
    chunks = re.split(r"(?m)^\|-.*$", table)
    headers: List[str] = []
    for chunk in chunks:
        if not re.search(r"(?m)^!", chunk):
            continue
        headers.extend(_clean_cell(c) for c in re.findall(r"(?m)^!(?:[^!\n]*)", chunk))
        if re.search(r"(?m)^\|", chunk):
            break  # header row shares its chunk with data rows — headers end
    return headers


def _table_rows(table: str) -> List[str]:
    """Row chunks of a table (split on any '|-' separator line)."""
    parts = re.split(r"(?m)^\|-.*$", table)
    return [p for p in parts[1:] if p.strip()]


# ────────────────────────────────────────────────────────────────────────────
# CANDIDATE BULLET PARSING
# ────────────────────────────────────────────────────────────────────────────

_PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")


def _parse_candidate_bullet(bullet: str) -> Optional[Dict]:
    """Parse one '* {{Party stripe|...}}{{aye}} '''[[Name]]''' (Party) 59.5%' bullet."""
    b = _strip_refs(bullet)
    b = re.sub(r"<!--.*?-->", "", b, flags=re.S)
    if not b.strip():
        return None

    stripe = re.search(r"\{\{\s*[Pp]arty (?:stripe|shade)\s*\|\s*([^|}]+)", b)
    party = normalize_party(stripe.group(1)) if stripe else ""
    b_wo_stripe = re.sub(r"\{\{\s*[Pp]arty (?:stripe|shade)\s*\|[^}]*\}\}", "", b)

    winner = bool(re.search(r"\{\{\s*[Aa]ye\s*\}\}", b_wo_stripe))

    # percentage (before entity/template cleanup so '59.5%' survives)
    pct = None
    pm = _PCT_RE.search(b_wo_stripe)
    if pm:
        pct = float(pm.group(1))
        b_wo_stripe = b_wo_stripe[: pm.start()]

    # candidate name
    name = ""
    m = re.search(r"'''\s*(\[\[[^]]+\]\]|[^']+?)\s*'''", b_wo_stripe)
    if m:
        name = m.group(1)
        winner = winner or True  # bolded candidate == winner marker in these tables
    else:
        # unbolded leading link or plain text
        m = re.match(r"\s*(?:\[\[([^]]+)\]\]|[^*(\n]+)", b_wo_stripe.strip())
        if m:
            name = m.group(1) or m.group(0)

    name = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", r"\1", name or "")
    name = re.sub(r"\{\{sortname\|([^|}]+)\|([^|}]+)[^}]*\}\}",
                  lambda mm: f"{mm.group(1).strip()} {mm.group(2).strip()}", name, flags=re.I)
    name = re.sub(r"\(\s*(?:incumbent|lying in state|deceased|withdrew|resigned)[^)]*\)", "",
                  name, flags=re.I)
    name = re.sub(r"\{\{[^{}]*\}\}", "", name)
    name = re.sub(r"<[^>]+>", "", name)
    # the unbolded-link branch above captures the raw link interior
    # ('Jack Conway (politician)|Jack Conway') — keep the display part.  This
    # must run AFTER template resolution: sortname params contain '|' too.
    if "|" in name:
        name = name.rsplit("|", 1)[1].strip()
    name = name.replace("&nbsp;", " ")
    name = re.sub(r"\s+", " ", name).strip(" ,;")
    # trailing parenthetical party label '(Republican)' — already captured via stripe
    if not stripe:
        pm2 = re.search(r"\(([^)]+)\)\s*$", name)
        if pm2 and pm2.group(1).lower() not in ("incumbent", "write-in",
                                                "write-in candidate", "deceased",
                                                "withdrew", "lying in state"):
            party = party or normalize_party(pm2.group(1))
            name = name[: pm2.start()].strip()
        if not party:
            # 2002-era bullets place the party label between the bold name and
            # the percentage: "'''[[Bob R. Riley]]''' (Republican) 49.2%"
            for pm3 in re.finditer(r"\(([^)]{2,40})\)", b_wo_stripe):
                label = pm3.group(1).strip()
                if label.lower() in ("incumbent", "write-in", "write-in candidate",
                                     "deceased", "withdrew", "lying in state",
                                     "running for other office", "appointed"):
                    continue
                norm = normalize_party(label)
                if norm and not re.match(r"^(election|runoff|special|term)", norm.lower()):
                    party = norm
                    break
    name = name.strip(" ,;")

    if not name:
        return None
    # Two-round territory tables use pseudo-bullets ('First round:', 'Runoff:')
    if re.match(r"^(first|second|third)\s+round\s*:?\s*$|^runoff\s*:?\s*$", name, re.I):
        return None
    if party.lower() == "write-in" or "write-in" in name.lower():
        name = re.sub(r"\(write-in[^)]*\)", "", name, flags=re.I).strip(" ,;")
    return {"candidate": name, "party": party, "percentage": pct, "winner": winner}


def _candidates_from_cell(cell_text: str) -> List[Dict]:
    """Extract candidate bullets from a Candidates table cell.

    Only ``*`` bullet lines are considered — bold text in other cells
    (e.g. 'Result: \'\'\'Republican hold\'\'\''.) must never leak in.
    """
    raw = _strip_refs(cell_text)
    m = re.search(r"\{\{\s*[Pp]lainlist\s*\|(.*?)\}\}\s*$", raw, re.S)
    bullets_text = m.group(1) if m else raw
    lines = re.findall(r"(?m)^\s*\*\s*(.+)$", bullets_text)
    out = []
    for b in lines:
        parsed = _parse_candidate_bullet(b)
        if parsed:
            out.append(parsed)
    return out


# ────────────────────────────────────────────────────────────────────────────
# OVERVIEW ARTICLE PARSER
# ────────────────────────────────────────────────────────────────────────────

def parse_overview(text: str, year: int, office: str) -> pd.DataFrame:
    """Parse the 'Race summary' (or 'Summary') tables of a statewide overview."""
    race = _section(text, "Race summary", level=2)
    if not race:
        race = _section(text, "Summary", level=2)
    if not race:
        logger.warning("  no 'Race summary'/'Summary' section found")
        return pd.DataFrame(columns=_COLUMNS)

    rows: List[Dict] = []
    for table in _wikitables(race):
        headers = [h.lower() for h in _table_headers(table)]
        if not any("state" in h or "territory" in h for h in headers) or not any(
            "candidate" in h for h in headers
        ):
            continue  # rating / prediction / composition tables

        for chunk in _table_rows(table):
            sm = re.search(r"(?m)^!\s*\[\[#[^|\]]*\|([^\]]+)\]\]", chunk)
            if not sm:
                # 2022-style: '! [[2022 Alabama gubernatorial election|Alabama]]'
                sm = re.search(r"(?m)^!\s*\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", chunk)
            if not sm:
                sm = re.search(r"(?m)^!\s*([A-Za-z .]+)", chunk)
            state_raw = (sm.group(1) or "").strip() if sm else ""
            state = re.sub(r"\s*\(.*$", "", state_raw).strip()
            state = re.sub(r"<br\s*/?>", " ", state)   # 'Northern Mariana<br />Islands'
            state = re.sub(r"\s+", " ", state).strip()
            if not state:
                continue
            # header chunks can masquerade as rows ('! State' ...)
            if state.lower().rstrip("s") in {"state", "territory", "district", "candidate"}:
                continue

            lines = [ln for ln in chunk.split("\n") if ln.strip()]
            incumbent = ""
            for ln in lines[1:]:
                s = ln.strip()
                if s.startswith("|") or s.startswith("!"):
                    val = _clean_cell(s.lstrip("|!"))
                    if val and not re.match(r"^(r|d|indep|\+|-)\s*\+?\d+", val.lower()):
                        incumbent = val
                    break

            cands = _candidates_from_cell(chunk)
            if not cands:
                continue

            inc_key = incumbent.lower()
            for c in cands:
                rows.append({
                    "year": year,
                    "state": state,
                    "state_code": _state_code(state),
                    "office": office,
                    "candidate": c["candidate"],
                    "party": c["party"],
                    "percentage": c["percentage"],
                    "winner": bool(c["winner"]),
                    "incumbent": bool(inc_key and inc_key in c["candidate"].lower()),
                })

    df = pd.DataFrame(rows, columns=_COLUMNS)
    if len(df):
        # de-dup identical rows (tables can repeat across subsections)
        df = df.drop_duplicates(
            subset=["year", "state", "office", "candidate"], keep="first"
        ).reset_index(drop=True)
        # single-seat offices: at most one winner per state — keep the top
        # percentage when the wikitext marks several candidates with {{aye}}
        grp = df.groupby(["year", "state", "office"])
        win_count = grp["winner"].transform("sum")
        max_pct = grp["percentage"].transform("max")
        stray = (
            df["winner"]
            & (win_count > 1)
            & df["percentage"].notna()
            & (df["percentage"] != max_pct)
        )
        if stray.any():
            logger.info(
                "  demoting %d stray winner flag(s), e.g. %s",
                int(stray.sum()), df.loc[stray, "state"].unique()[:4],
            )
            df.loc[stray, "winner"] = False
    return df


# ────────────────────────────────────────────────────────────────────────────
# PER-RACE PRIMARY / GENERAL PARSING (race articles)
# ────────────────────────────────────────────────────────────────────────────

def discover_race_titles(text: str, year: int) -> List[str]:
    """
    Extract per-state race article titles from an overview article.

    Race sections link to their dedicated article via ``{{main|...}}``;
    only titles carrying the cycle year and ending in the singular
    "... election" are kept, so overview-level links (e.g. to the next
    cycle's plural overview) are excluded automatically.
    """
    titles: List[str] = []
    for raw in re.findall(r"\{\{\s*[Mm]ain(?:\s+list)?\s*\|([^}]+?)\s*(?:\|[^}]*)?\}\}", text):
        for part in raw.split("|"):
            t = part.strip()
            if str(year) not in t or _NON_STATE_TITLES_RE.search(t):
                continue
            if not t.endswith("election"):
                continue
            titles.append(t)
    return list(dict.fromkeys(titles))


#: Race-title grammar: "{year} {State} [qualifier] {office} election"
_RACE_TITLE_PATTERNS = [
    # governor — "2021 New Jersey gubernatorial election",
    # "2021 California gubernatorial recall election",
    # "2010 New York gubernatorial special election" (hypothetical shapes)
    re.compile(
        r"^(?P<y>\d{4})\s+(?P<state>.+?)\s+gubernatorial\s+"
        r"(?P<qual>special\s+|recall\s+|runoff\s+)?elections?$", re.I),
    # other statewide executives — "2023 Kentucky Attorney General election",
    # "2022 New York State Attorney General election",
    # "2022 Rhode Island General Treasurer election"
    re.compile(
        r"^(?P<y>\d{4})\s+(?P<state>.+?)\s+(?:special\s+|recall\s+|runoff\s+)?"
        r"(?:State Attorney General|Attorney General|Secretary of State|"
        r"General Treasurer|State Treasurer|Treasurer|"
        r"State Auditor|Auditor|Commissioner of Agriculture|"
        r"Agriculture Commissioner|Land Commissioner|Insurance Commissioner|"
        r"Labor Commissioner|Commissioner of Labor|Comptroller|Controller|"
        r"Superintendent of Public Instruction)\s+elections?$", re.I),
]


def state_from_race_title(title: str, year: int) -> Optional[str]:
    """
    Derive the state label from a race article title, or None.

    '2021 New Jersey gubernatorial election'   → 'New Jersey'
    '2021 California gubernatorial recall election' → 'California'
    '2023 Kentucky Attorney General election'  → 'Kentucky'
    """
    t = title.strip()
    for pat in _RACE_TITLE_PATTERNS:
        m = pat.match(t)
        if m and int(m.group("y")) == year:
            return m.group("state").strip()
    return None


def _race_sections(text: str) -> List:
    """Level-2 sections as (name, start, end) triples, in document order."""
    marks = [
        (m.start(), m.end(), m.group(1).strip())
        for m in re.finditer(r"(?m)^==\s*([^=].*?)\s*==\s*$", text)
    ]
    sections = []
    for i, (start, hdr_end, name) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        sections.append((name, start, end))
    return sections


def _race_election_type(section_name: str) -> str:
    """Classify a race-article section as Primary / General / '' (ignored)."""
    if _PRIMARY_HEADING_RE.search(section_name):
        return "Primary"
    if _GENERAL_HEADING_RE.search(section_name):
        return "General"
    return ""


#: Row templates of the {{Election box}} family.  Row templates may embed
#: nested templates inside parameters (e.g. ticket cells like
#'{{ubl|{{nowrap|[[A]] (incumbent)}}|[[B]] (incumbent)}}' on New Mexico 2022),
#: so the parameter body must tolerate two levels of balanced '{{...}}' spans.
_EB_PARAM_BODY = r"((?:[^{}]|\{\{(?:[^{}]|\{\{[^{}]*\}\})*\}\})+?)"
_EB_ROW_PATTERNS = [
    (r"\{\{Election box winning candidate(?: with party link| without party|"
     r" for a political party)?(?: no change)?[\s\|]" + _EB_PARAM_BODY + r"\}\}", "Winning"),
    (r"\{\{Election box candidate(?: with party link| without party|"
     r" for a political party)?(?: no change)?[\s\|]" + _EB_PARAM_BODY + r"\}\}", "Candidate"),
    (r"\{\{Election box write-in(?: with party link)?(?: no change)?[\s\|]"
     + _EB_PARAM_BODY + r"\}\}", "Write-in"),
    (r"\{\{Election box total(?: no change)?[\s\|]" + _EB_PARAM_BODY + r"\}\}", "Total"),
]


def _parse_election_box_rows(box_text: str) -> List[Dict]:
    """Rows of one {{Election box begin ... end}} chunk as raw param dicts."""
    # Value must tolerate '|' inside [[link|display]] and template spans —
    # a naive [^|\n]+ would truncate '[[John Buckley (Virginia politician)|John
    # Buckley]]'.  One level of template nesting is allowed so ticket cells
    # like '{{ubl|{{nowrap|[[A]] (incumbent)}}|[[B]] (incumbent)}}' survive
    # intact (New Mexico 2022 general box).
    _value = r"((?:\{\{(?:[^{}]|\{\{[^{}]*\}\})*\}\}|\[\[[^\]]*\]\]|[^\|\n{])+)"
    rows: List[Dict] = []
    for regex, row_type in _EB_ROW_PATTERNS:
        for params in re.findall(regex, box_text, re.DOTALL):
            row: Dict = {"row_type": row_type}
            for key, val in re.findall(r"(?:^|\|)\s*(\w+)\s*=\s*" + _value, params):
                row[key.lower()] = val.strip()
            rows.append(row)
    return rows


def _fallback_cell(cell: str) -> str:
    """Wikitable cell → plain text (attrs, links, templates stripped)."""
    t = _clean_cell(cell)
    t = re.sub(r'^\s*(?:[a-zA-Z-]+\s*=\s*"[^"]*"\s*)+', "", t)  # cell attributes
    return t.replace("|", " ").strip()


def _parse_simple_votes_table(table: str, section_name: str) -> List[Dict]:
    """
    Conservative fallback for primary sections without election boxes.

    Only simple ``Candidate | Votes | %`` tables are parsed: the header must
    mention Candidate and (Votes or %), must NOT be a ranked-choice
    multi-round table (Round 1..n column groups), and must not be a
    by-county / by-district breakdown.  Returns raw row dicts with
    candidate / votes / percentage keys.
    """
    headers = _table_headers(table)
    joined = " | ".join(headers).lower()
    if "candidate" not in joined and "nominee" not in joined:
        return []
    if not ("vote" in joined or "%" in joined or "percent" in joined):
        return []
    if re.search(r"round\s*\d|county|district|parish|precinct", joined):
        return []

    out: List[Dict] = []
    for chunk in _table_rows(table):
        # One chunk == one data row.  Cells are '|' lines; '||' separates
        # cells packed onto a single line.  Header ('!') lines are skipped.
        cells: List[str] = []
        for ln in chunk.split("\n"):
            s = ln.strip()
            if s.startswith("||"):
                cells.extend(s[2:].split("||"))
            elif s.startswith("|"):
                cells.append(s[1:])
        cleaned = [c for c in (_fallback_cell(c) for c in cells) if c]
        if len(cleaned) < 2:
            continue
        name = cleaned[0]
        if not name or len(name) > 60:
            continue
        if re.match(r"^(total|valid|rejected|turned away|majority|swing|electorate|"
                    r"registered|turnout|blank|spoilt|source)", name, re.I):
            continue
        votes, pct = None, None
        for cell in cleaned[1:]:
            vm = re.search(r"\d{1,3}(?:,\d{3})+|\d{3,}\b", cell)
            pm = _PCT_RE.search(cell)
            if pm and pct is None:
                pct = float(pm.group(1))
            elif vm and votes is None:
                votes = int(vm.group(0).replace(",", ""))
        if votes is None and pct is None:
            continue
        out.append({
            "row_type": "Candidate",
            "candidate": name,
            "votes": votes,
            "percentage": pct,
            "party": "",
            "election": section_name,
        })
    return out


def _param_number(raw) -> Optional[float]:
    """Numeric {{Election box}} param value (strip refs/tags/commas/%/nbsp)."""
    if raw in (None, ""):
        return None
    s = re.sub(r"<[^>]+>", "", str(raw))
    s = s.replace("&nbsp;", " ").replace(",", "").strip()
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None


#: Ticket cells list the governor candidate first, then the running mate:
#'{{ubl|[[Phil Murphy]] (incumbent)|[[Sheila Oliver]] (incumbent)}}'
_TICKET_LIST_RE = re.compile(
    r"\{\{\s*(?:ubl|unbulleted\s*list|plainlist|flatlist|hlist|"
    r"bulleted\s*list|pagelist)\s*\|(.*?)\}\}", re.S | re.I,
)


def _top_of_ticket(val: str) -> str:
    """
    Keep the top-of-ticket candidate from a list-template cell.

    Governor general-election boxes name the full governor/lieutenant-governor
    ticket via ``{{ubl|...}}``; keeping only the first item matches the grain
    of the overview summaries (one row per gubernatorial candidate).
    """
    m = _TICKET_LIST_RE.search(val)
    if not m:
        return val
    for item in m.group(1).split("|"):
        item = item.strip()
        if item and not re.match(r"^\s*\w+\s*=", item):  # skip named params
            return item
    return val


def _clean_candidate_param(raw: str) -> "tuple[str, bool]":
    """{{Election box}} candidate param → (plain top-of-ticket name, incumbent)."""
    val = unwrap_format_templates(raw or "")   # {{nowrap|x}} → x
    val = remove_wikilinks(val)                 # [[A|B]] → B (before pipe splits)
    val = _top_of_ticket(val)                   # {{ubl|A|B}} → A
    val, is_inc = extract_incumbent_flag(clean_wikitext(val))
    return re.sub(r"\s+", " ", val).strip(" ,;"), is_inc


def parse_race_article(
    text: str,
    year: int,
    state: str,
    state_code: str,
    office: str,
) -> "tuple[pd.DataFrame, pd.DataFrame]":
    """
    Parse one per-state race article into (primary, general) DataFrames.

    Election boxes are classified by the level-2 heading they sit under, so
    box titles without the word "primary" (e.g. Mississippi's
    "2019 Republican", New Jersey's long official-certification titles) are
    attributed correctly.  Primary sections without boxes fall back to
    simple Candidate/Votes/% wikitables.
    """
    primary_rows: List[Dict] = []
    general_rows: List[Dict] = []

    for section_name, start, end in _race_sections(text):
        election_type = _race_election_type(section_name)
        if not election_type:
            continue
        section_text = text[start:end]

        # ── election boxes (dominant format, 2001–2026) ──────────────
        box_rows: List[Dict] = []
        for header, box_content in re.findall(
            r"\{\{Election box begin(?: no change)?\s*\|?\s*"
            r"((?:[^{}]|\{\{[^}]*\}\})*?)\}\}"
            r"(.*?)\{\{Election box end\}\}",
            section_text, re.DOTALL,
        ):
            title_match = re.search(r"title\s*=\s*([^\n<]+)", header)
            box_title = clean_wikitext(title_match.group(1)) if title_match else section_name
            box_title = box_title.strip() or section_name

            # jungle-primary / runoff boxes sit under a primary heading but
            # the box title may say "general" — the heading wins.
            for row in _parse_election_box_rows(box_content):
                row["election"] = box_title
                box_rows.append(row)

        # ── simple wikitable fallback for primary sections w/o boxes ──
        if election_type == "Primary" and not box_rows:
            for table in _wikitables(section_text):
                for row in _parse_simple_votes_table(table, section_name):
                    box_rows.append(row)

        for row in box_rows:
            candidate, is_inc = _clean_candidate_param(row.get("candidate", ""))

            # nameless write-in / scattering rows ('|party=Write-ins' with no
            # candidate param): keep the votes under a Write-in row instead of
            # a nameless Candidate row
            if not candidate and row.get("row_type") in ("Winning", "Candidate"):
                party_lbl = (row.get("party") or "").strip()
                if re.search(r"write.?in|scattering|blank", party_lbl, re.I):
                    row["row_type"] = "Write-in"
                    candidate = party_lbl.title().replace("Write-In", "Write-in")

            votes_raw = _param_number(row.get("votes"))
            pct_raw = _param_number(row.get("percentage"))
            votes = int(votes_raw) if votes_raw is not None else None
            pct = round(pct_raw, 2) if pct_raw is not None else None

            # placeholder rows with no name and no numbers (empty box stubs,
            # withdrawn-candidate slots) carry no information — drop them
            if (
                not candidate
                and votes is None
                and pct is None
                and row.get("row_type") in ("Winning", "Candidate", "Write-in")
            ):
                continue

            record = {
                "year": year,
                "state": state,
                "state_code": state_code,
                "office": office,
                "election": row.get("election", section_name),
                "row_type": row.get("row_type", "Candidate"),
                "candidate": candidate,
                "party": normalize_party(row.get("party", "")),
                "votes": votes,
                "percentage": pct,
                "winner": row.get("row_type") == "Winning",
                "incumbent": is_inc,
            }
            if election_type == "Primary":
                primary_rows.append(record)
            else:
                general_rows.append(record)

    def _frame(rows: List[Dict]) -> pd.DataFrame:
        df = pd.DataFrame(rows, columns=_RACE_COLUMNS)
        if len(df):
            df = df.drop_duplicates(
                subset=["year", "state", "office", "election", "row_type", "candidate"],
                keep="first",
            ).reset_index(drop=True)
        return df

    return _frame(primary_rows), _frame(general_rows)


def _rebuild_combined(statewide_dir: str, family: str = "results_") -> Optional[pd.DataFrame]:
    """
    Rebuild ``statewide_{family}all.csv`` from every per-year file on disk.

    A run that processes only one cycle (or only off-years) must NOT
    overwrite the combined CSV with just its own rows — that would silently
    wipe history from the _all files.  Per-year files are the source of
    truth: every year ever processed by any run leaves a file behind, so
    concatenating them (sorted by year) always yields the full-history
    combined file.  *family* is "results_" (overview summaries),
    "primary_results_" or "general_results_".
    """
    import glob

    parts: List[pd.DataFrame] = []
    for path in sorted(glob.glob(os.path.join(statewide_dir, f"statewide_{family}*.csv"))):
        name = os.path.basename(path)
        m = re.fullmatch(rf"statewide_{re.escape(family)}(\d{{4}})\.csv", name)
        if not m:
            continue  # skip the _all file itself
        try:
            df_year = pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""])
        except Exception:
            logger.warning("Could not read %s for the combined file — skipping", path)
            continue
        df_year.insert(0, "_year", int(m.group(1)))
        parts.append(df_year)

    if not parts:
        return None
    combined = pd.concat(parts, ignore_index=True)
    combined = combined.sort_values("_year", kind="stable")
    combined = combined.drop(columns=["_year"])
    return combined


# ────────────────────────────────────────────────────────────────────────────
# BATCH DRIVER
# ────────────────────────────────────────────────────────────────────────────

def run(
    start_year: int = 2018,
    end_year: int = 2024,
    output_dir: str = "data",
    client: Optional[WikiAPIClient] = None,
    include_off_years: bool = True,
    fetch_races: bool = True,
) -> pd.DataFrame:
    """CLI entry point: process statewide executive cycles from *start_year*
    to *end_year*, writing CSVs under ``<output_dir>/statewide/``.

    Even (federal) years are always processed, and the odd (off-year) cycles
    in the range are included by default (NJ/VA gubernatorial, the KY/LA/MS
    statewide slate, occasional specials) — pass ``include_off_years=False``
    to restrict the run to even years.  When *fetch_races* is true the
    per-state race articles are fetched and parsed for primary and general
    results (vote counts), written to
    ``statewide_primary_results_{year}.csv`` /
    ``statewide_general_results_{year}.csv``.
    """
    years = even_years(start_year, end_year)
    if include_off_years:
        off = _odd_years(start_year, end_year)
        if off:
            logger.info("Including off-year (odd) cycles: %s", off)
        years = sorted(set(years) | set(off))
    if not years:
        logger.warning("No election years in [%d, %d] — nothing to do.", start_year, end_year)
        return pd.DataFrame()
    if not include_off_years and (start_year, end_year) != (years[0], years[-1]):
        logger.info("Odd bounds clamped to even years: %d–%d", years[0], years[-1])
    logger.info("Statewide cycles to process: %s", years)

    statewide_dir = os.path.join(output_dir, "statewide")
    os.makedirs(statewide_dir, exist_ok=True)

    client = client or get_default_client()

    meta = {"years": {}, "races": {}, "missing_articles": []}
    # title -> (year, state, office) for every discovered race article
    race_map: Dict[str, tuple] = {}

    # ── STEP 1: overview articles → general summary tables ─────────────
    for year in years:
        titles = {office: pat.format(y=year) for office, pat in OFFICES.items()}
        content = client.fetch_wikitext(list(titles.values())) if client else {}

        frames = []
        year_meta = {}
        for office, title in titles.items():
            text = content.get(title)
            if not text:
                logger.warning("  %s %s — overview article missing, skipped", year, office)
                meta["missing_articles"].append(title)
                year_meta[office] = None
                continue
            df = parse_overview(text, year, office)
            n_states = df["state"].nunique() if len(df) else 0
            logger.info("  %s %-19s %3d candidate rows / %2d states", year, office,
                        len(df), n_states)
            year_meta[office] = {"rows": int(len(df)), "states": int(n_states)}
            if len(df):
                frames.append(df)

            # discover per-state race articles for primary/general parsing
            if fetch_races:
                for race_title in discover_race_titles(text, year):
                    state = state_from_race_title(race_title, year)
                    if not state:
                        logger.debug("  skipping non-race link: %s", race_title)
                        continue
                    race_map.setdefault(race_title, (year, state, office))

        if frames:
            df_year = pd.concat(frames, ignore_index=True)
            path = os.path.join(statewide_dir, f"statewide_results_{year}.csv")
            df_year.to_csv(path, index=False)
            logger.info("  saved -> %s", path)
        meta["years"][year] = year_meta

    # ── STEP 2: race articles → primary / general results ──────────────
    primary_frames: Dict[int, List[pd.DataFrame]] = {}
    general_frames: Dict[int, List[pd.DataFrame]] = {}
    if fetch_races and race_map:
        logger.info("Fetching %d statewide race articles ...", len(race_map))
        content = client.fetch_wikitext(list(race_map))
        for title, (year, state, office) in sorted(race_map.items()):
            text = content.get(title)
            if not text:
                logger.warning("  race article missing: %s", title)
                meta["missing_articles"].append(title)
                continue
            try:
                primary_df, general_df = parse_race_article(
                    text, year, state, _state_code(state), office,
                )
            except Exception:
                logger.exception("  parse failed: %s", title)
                meta["races"][title] = {"year": year, "state": state,
                                        "office": office, "error": True}
                continue
            meta["races"][title] = {
                "year": year, "state": state, "office": office,
                "primary_rows": int(len(primary_df)),
                "general_rows": int(len(general_df)),
            }
            if len(primary_df):
                primary_frames.setdefault(year, []).append(primary_df)
            if len(general_df):
                general_frames.setdefault(year, []).append(general_df)
            logger.info(
                "  %s [%s] %s — primary rows: %d, general rows: %d",
                year, office, title, len(primary_df), len(general_df),
            )

    def _write_family(frames_by_year: Dict[int, List[pd.DataFrame]], family: str) -> None:
        for year in sorted(frames_by_year):
            df_year = pd.concat(frames_by_year[year], ignore_index=True)
            path = os.path.join(statewide_dir, f"statewide_{family}{year}.csv")
            df_year.to_csv(path, index=False)
            logger.info("  saved -> %s", path)

    _write_family(primary_frames, "primary_results_")
    _write_family(general_frames, "general_results_")

    # ── STEP 3: combined _all files rebuilt from ALL per-year files ────
    combined_summary = None
    for family, label in (("results_", "results"),
                          ("primary_results_", "primary results"),
                          ("general_results_", "general results")):
        combined = _rebuild_combined(statewide_dir, family)
        if combined is not None:
            path = os.path.join(statewide_dir, f"statewide_{family}all.csv")
            combined.to_csv(path, index=False)
            logger.info("Combined -> %s (%s rows)", path, f"{len(combined):,}")
            if family == "results_":
                combined_summary = combined
        else:
            logger.info("No per-year %s files on disk yet — combined file skipped", label)

    ts = time.strftime("%Y%m%d_%H%M%S")
    with open(os.path.join(statewide_dir, f"statewide_metadata_{ts}.json"), "w",
              encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, default=str)

    return combined_summary if combined_summary is not None else pd.DataFrame()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    run()
