"""
house_elections.py — Parse U.S. House election RESULTS from Wikipedia
wikitext fetched via the MediaWiki Action API.

Handles both historical and current article layouts:
    HISTORICAL (approx. 1912–1956): district rows like
        ! {{ushr|Massachusetts|1|X}} — full state name, {{Ushr}} capitalised,
        winner marked {{aye}}, no Sortname; {{main}} links often wrapped in
        <!--HTML comments--> (still detected).
    OLD (2018/2020): ==State== {{Main|YEAR ... in State}} + {{Plainlist|* ...}}
    NEW (2022/2024): ==State==\\n{{main|...}} + {{plainlist}}...{{endplainlist}}

State sections are header-bounded and must contain a {{main|YEAR ...}} link,
so tables in the "Special elections" section (which carries no {{main}} of
its own) can never leak into a state's rows.

Odd (off-year) years — special elections:
Odd-year overviews ("{year} United States House of Representatives
elections") have no state sections; they carry one section per special
race ("==Illinois's 2nd congressional district==") linking the race
article via "{{main|<year> <State>'s <N>th congressional district special
election}}".  Those race articles are fetched and parsed with the
statewide pipeline's parse_race_article (same {{Election box}} grammar)
for the final-round (general) result, written in the SAME standard schema
and files as election years:
    house_results_{year}.csv            per year
    house_results_all.csv               combined
No separate off-year file format is emitted.  Off-years are included by
default; pass include_off_years=False (CLI: --no-include-off-years) to
process even years only.

Output columns (standardised):
    year, level, state, state_code, district, total_votes, candidate,
    party, votes, percentage, winner, incumbent, open_seat

``total_votes`` is the race-level total vote cast (repeated on every
candidate row of the race) and ``votes`` the candidate's raw vote count.
Both are populated from the per-state race articles
(``{year} United States House of Representatives elections in {State}``)
— their general-election ``{{Election box}}`` templates carry vote counts
the overview articles never show.  The overview parse (percentages,
winners, incumbents) remains the source of truth; vote counts are merged
onto it by (state, district, candidate name).  Rows whose race article is
missing, has no general box, or names no matching candidate keep empty
vote fields — e.g. most pre-2000s cycles, whose race articles do not
exist.  Pass ``include_votes=False`` (CLI: ``--no-include-votes``) to skip
the extra per-state article fetches; the columns are still emitted (empty)
so the schema stays stable across runs.

Outputs (written under *out_dir*, default ``data/house/``):
    house_results_{year}.csv   per year
    house_results_all.csv      combined

Usage:
    python cli.py house --start-year 2018 --end-year 2024
    python house_elections.py
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Tuple

import pandas as pd

from wiki_utils import (
    WikiAPIClient,
    even_years,
    fetch_articles_batch,
    clean_wikitext,
)
from statewide_elections import (
    parse_race_article,
    _parse_election_box_rows,
    _clean_candidate_param,
    _param_number,
)

logger = logging.getLogger(__name__)

LEVEL = "house"

#: Standardised result schema.  ``total_votes`` (race-level, repeated on
#: every candidate row) and ``votes`` (per candidate) stay in the schema even
#: when unpopulated — vote counts exist only where the per-state race article
#: publishes a general-election {{Election box}}.
HOUSE_RESULT_COLUMNS = [
    "year", "level", "state", "state_code", "district", "total_votes",
    "candidate", "party", "votes", "percentage", "winner", "incumbent",
    "open_seat",
]


def _votes_int(raw) -> Optional[int]:
    """Election-box votes value → int (floats from the parser, '1,676' strings, None)."""
    if raw is None:
        return None
    try:
        if pd.isna(raw):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return int(float(str(raw).replace(",", "")))
    except (TypeError, ValueError):
        return None


def _cast_vote_columns(df: pd.DataFrame) -> pd.DataFrame:
    """votes / total_votes → nullable Int64 so CSVs show clean ints or ''."""
    def _is_na(v) -> bool:
        try:
            return bool(pd.isna(v))
        except (TypeError, ValueError):
            return False
    for col in ("votes", "total_votes"):
        if col in df.columns:
            df[col] = pd.array(
                [pd.NA if _is_na(v) else int(v) for v in df[col]],
                dtype="Int64",
            )
    return df


# ──────────────────────────────────────────────
# YEAR HELPERS
# ──────────────────────────────────────────────

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


# ──────────────────────────────────────────────
# ARTICLE TITLE BUILDER
# ──────────────────────────────────────────────

def article_title(year: int) -> str:
    return f"{year} United States House of Representatives elections"


# ──────────────────────────────────────────────
# OFF-YEAR (ODD) SPECIAL-ELECTION SUPPORT
# ──────────────────────────────────────────────

#: Race-title grammar for House special elections, e.g.
#:   '2013 Illinois's 2nd congressional district special election'  (year first)
#:   'Massachusetts's 5th congressional district special election, 2013'
#:     (year last — legacy alias titles)
#: 'at-large' seats map to the district code 'AL'.
_SPECIAL_TITLE_RE = re.compile(
    r"^(?:(?P<y1>\d{4})\s+)?"
    r"(?P<state>[A-Z][A-Za-z .]*?)'s\s+"
    r"(?P<dist>at-large|\d+(?:st|nd|rd|th))\s+"
    r"congressional district\s+special elections?"
    r"(?:\s*,\s*(?P<y2>\d{4}))?$",
    re.IGNORECASE,
)


def _district_code(raw: str) -> str:
    """'2nd' → '2', 'at-large' → 'AL' (matches {{ushr}} district codes)."""
    raw = raw.strip().lower()
    if raw == "at-large":
        return "AL"
    return re.sub(r"(st|nd|rd|th)$", "", raw)


def state_district_from_special_title(
    title: str, year: Optional[int] = None,
) -> Optional[Tuple[str, str]]:
    """
    Derive (state, district) from a House special race article title.

    '2013 Illinois's 2nd congressional district special election'
        → ('Illinois', '2')
    '2017 Montana's at-large congressional district special election'
        → ('Montana', 'AL')
    Returns None when the title does not match the grammar or — when
    *year* is given — carries a different year.
    """
    m = _SPECIAL_TITLE_RE.match(title.strip())
    if not m:
        return None
    if year is not None:
        years_in_title = {int(g) for g in (m.group("y1"), m.group("y2")) if g}
        if years_in_title and year not in years_in_title:
            return None
    return m.group("state").strip(), _district_code(m.group("dist"))


def discover_special_race_titles(text: str, year: int) -> List[str]:
    """
    Extract House special race article titles from an odd-year overview.

    Race sections link to their dedicated article via
    ``{{main|<year> <State>'s <N>th congressional district special
    election}}``; summary tables may also carry plain ``[[...]]`` links to
    the same articles.  Only year-matching targets are kept, so
    cross-references to other cycles (e.g. ``[[1996 ...#Special
    elections|1996]]``) and Senate specials (no 'congressional district')
    are excluded automatically.
    """
    targets: List[str] = []
    targets += re.findall(r"\{\{\s*[Mm]ain(?:\s+article)?\s*\|\s*([^|}#]+)", text)
    targets += re.findall(r"\[\[([^|\]#]+)\]\]", text)
    titles = []
    for t in targets:
        t = t.strip()
        if str(year) not in t:
            continue
        if "congressional district special election" not in t.lower():
            continue
        if state_district_from_special_title(t, year) is None:
            continue
        titles.append(t)
    return list(dict.fromkeys(titles))


def parse_off_year_specials(
    year: int, race_texts: Dict[str, Optional[str]],
) -> pd.DataFrame:
    """
    Parse fetched House special race articles for one odd year.

    Each race article is parsed with the statewide pipeline's
    ``parse_race_article`` ({{Election box}} grammar) and the final-round
    (general) candidate rows are returned in the SAME standard house schema
    used for election years — identical columns and file format, so
    ``house_results_{year}.csv`` for an odd year is formatted exactly like
    an even-year file.  Per-candidate ``votes`` and the race-level
    ``total_votes`` come straight from the election boxes.
    """
    house_rows: List[Dict] = []

    for title in sorted(race_texts):
        text = race_texts.get(title)
        if not text:
            logger.warning("  %s — race article not found, skipped", title)
            continue
        sd = state_district_from_special_title(title, year)
        if not sd:
            logger.warning("  %s — could not derive state/district, skipped", title)
            continue
        state, district = sd
        state_code = _state_code(state)
        logger.info("  %s — %s district %s (%s chars)", year, state, district,
                    f"{len(text):,}")

        try:
            _, gen_df = parse_race_article(
                text, year, state, state_code, office="House (special)"
            )
        except Exception:  # keep the year alive on per-race errors
            logger.exception("  FAIL %s (%d)", title, year)
            continue

        # race-level total vote: the box's Total row, else the candidate sum
        totals: Dict[str, Optional[int]] = {}
        if len(gen_df):
            for election, grp in gen_df.groupby("election"):
                tot = grp.loc[grp["row_type"] == "Total", "votes"]
                total = _votes_int(tot.iloc[0]) if len(tot) else None
                if total is None:
                    cand = grp.loc[grp["row_type"] != "Total", "votes"]
                    cand = [_votes_int(v) for v in cand]
                    cand = [v for v in cand if v is not None]
                    total = sum(cand) if cand else None
                totals[election] = total

        # standard house schema: final-round candidate rows only (no totals)
        for r in gen_df.to_dict("records"):
            if r.get("row_type") == "Total":
                continue
            house_rows.append({
                "year": year, "level": LEVEL, "state": state,
                "state_code": state_code, "district": district,
                "total_votes": totals.get(r.get("election")),
                "candidate": r.get("candidate"), "party": r.get("party"),
                "votes": _votes_int(r.get("votes")),
                "percentage": r.get("percentage"), "winner": r.get("winner"),
                "incumbent": r.get("incumbent"), "open_seat": True,
            })

    df = pd.DataFrame(
        house_rows,
        columns=HOUSE_RESULT_COLUMNS,
    )
    if len(df):
        df = df.drop_duplicates(
            subset=["year", "state", "district", "candidate"], keep="first",
        ).reset_index(drop=True)
        df = _cast_vote_columns(df)
    return df


# ──────────────────────────────────────────────
# STATE NAME → USPS CODE (pre-1932 articles use full names in {{ushr}})
# ──────────────────────────────────────────────

STATE_NAME_TO_CODE: Dict[str, str] = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT",
    "Delaware": "DE", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI",
    "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA",
    "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME",
    "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI",
    "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO",
    "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM",
    "New York": "NY", "North Carolina": "NC", "North Dakota": "ND",
    "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR",
    "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
    "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
    "District of Columbia": "DC",
}


def _state_code(raw: str) -> str:
    """{{ushr}} first param ('MA', 'Massachusetts', 'Alaska Territory') → USPS code."""
    raw = raw.strip()
    if re.fullmatch(r"[A-Z]{2}", raw):
        return raw
    name = re.sub(r"\s+Territory$", "", raw)
    return STATE_NAME_TO_CODE.get(name, raw.upper()[:2])


# Header names that may legitimately open a state-results section (fallback
# qualification for main-less sections).  State names + home-rule territories.
_STATE_HEADER_NAMES = set(STATE_NAME_TO_CODE) | {
    "American Samoa", "Guam", "Northern Mariana Islands", "Puerto Rico",
    "United States Virgin Islands", "Philippines",
}


# Territory codes used in bundled 'Non-voting delegates' tables (2022+).
TERRITORY_CODE_TO_NAME: Dict[str, str] = {
    "AS": "American Samoa",
    "DC": "District of Columbia",
    "GU": "Guam",
    "MP": "Northern Mariana Islands",
    "PI": "Philippines",
    "PR": "Puerto Rico",
    "VI": "United States Virgin Islands",
}


# ──────────────────────────────────────────────
# PARSER HELPERS
# ──────────────────────────────────────────────

def _split_collapsible_lists(segment: str) -> Tuple[List[str], str]:
    """
    Pull ``{{collapsible list|title=...|item|item}}`` blocks out of *segment*.

    Returns ``(items, remainder)`` — every positional item carrying a vote
    percentage (the "Others" minor candidates) and *segment* with the whole
    blocks removed.  Brace-counted, so nested templates inside the block
    (``title={{nobold|Others}}``, ``{{Party stripe|...}}`` items) survive
    intact; the naive ``[^}]*`` scan previously could not see past them,
    which glued the block's rows onto the preceding candidate bullet
    (2024 NV-1: 'Mark Robertson (Republican) 44.5%' + collapsible list →
    one bullet whose name was taken from a minor candidate's wikilink).
    """
    items: List[str] = []
    out: List[str] = []
    marker = "{{collapsible list"
    low = segment.lower()
    i = 0
    while True:
        j = low.find(marker, i)
        if j == -1:
            out.append(segment[i:])
            break
        out.append(segment[i:j])
        # brace-count the whole block (nested templates allowed)
        depth, k, end = 0, j, -1
        while k < len(segment) - 1:
            if segment.startswith("{{", k):
                depth += 1
                k += 2
            elif segment.startswith("}}", k):
                depth -= 1
                k += 2
                if depth == 0:
                    end = k
                    break
            else:
                k += 1
        if end == -1:                    # unbalanced — keep the source text
            out.append(segment[j:])
            break
        inner = segment[j + len(marker) : end - 2]
        # top-level pipe split: pipes inside nested {{...}} spans AND inside
        # [[link|display]] wikilinks stay within their item
        part, parts = "", []
        d = link = k = 0
        while k < len(inner):
            if inner.startswith("{{", k):
                d += 1
                part += "{{"
                k += 2
                continue
            if inner.startswith("}}", k):
                d = max(0, d - 1)
                part += "}}"
                k += 2
                continue
            if inner.startswith("[[", k):
                link += 1
                part += "[["
                k += 2
                continue
            if inner.startswith("]]", k):
                link = max(0, link - 1)
                part += "]]"
                k += 2
                continue
            if inner[k] == "|" and d == 0 and link == 0:
                parts.append(part)
                part = ""
            else:
                part += inner[k]
            k += 1
        parts.append(part)
        for p in parts:
            p = p.strip()
            if not p or re.match(r"^\s*\w+\s*=", p):
                continue               # named params: title=, liststyle=...
            if "%" in p:
                items.append(p)
        i = end
    return items, "".join(out)


def _extract_candidate_entries(dsec: str) -> List[str]:
    """
    Extract candidate bullet entries from a district section.

    Handles:
      OLD (2018/2020): {{Plainlist|* entry1\\n* entry2\\n}}
      NEW (2022/2024): {{plainlist}}\\n* entry\\n...\\n{{endplainlist}}
      Inline single-candidate rows and {{collapsible list}} minor candidates.
    """
    entries: List[str] = []

    # NEW format: {{plainlist}} ... {{endplainlist}}
    # (also tolerates the malformed hybrid ' {{Plainlist|}}' + bare bullets
    #  + {{endplainlist}} seen in the 2022/2024 articles)
    new_pl = re.search(
        r"\{\{[Pp]lainlist\s*\|?\s*\}\}(.*?)\{\{endplainlist\}\}", dsec, re.DOTALL | re.IGNORECASE
    )
    if new_pl:
        # collapsible 'Others' blocks inside the list must come out BEFORE the
        # bullet split — their inner '| item' lines otherwise glue onto the
        # preceding bullet and corrupt its candidate name (2024 NV-1)
        _cl_items, pl_body = _split_collapsible_lists(new_pl.group(1))
        for e in re.split(r"\n\s*\*\s*|\|\s*\*\s*", pl_body):
            e = e.strip()
            if e:
                entries.append(e)

    # OLD format: {{Plainlist|* entry\\n* entry\\n}}  (brace-counted for nesting)
    if not entries:
        pl_start = dsec.lower().find("{{plainlist")
        if pl_start != -1:
            depth, pl_end = 0, -1
            for k in range(pl_start, len(dsec)):
                if dsec[k : k + 2] == "{{":
                    depth += 1
                elif dsec[k : k + 2] == "}}":
                    depth -= 1
                    if depth == 0:
                        pl_end = k + 2
                        break
            if pl_end != -1:
                pl = dsec[pl_start:pl_end]
                pipe = pl.find("|")
                if pipe != -1:
                    inner = pl[pipe + 1 : -2].strip()
                    _cl_items, inner = _split_collapsible_lists(inner)
                    for e in re.split(r"\n?\s*\*\s*", inner):
                        e = e.strip()
                        if e:
                            entries.append(e)

    # Inline single candidate (no list wrapper)
    if not entries:
        m = re.search(
            r"\|\s*(?:nowrap\s*\|)?\s*(\{\{Party\s+stripe\|[^}]+\}\}.*?%)", dsec, re.DOTALL
        )
        if m:
            entries.append(m.group(1).strip())
        else:
            # Uncontested inline candidates carry no percentage at all.
            m = re.search(
                r"\|\s*(?:nowrap\s*\|)?\s*(\{\{Party\s+stripe\|[^}]+\}\}[^\n]*)", dsec
            )
            if m:
                entries.append(m.group(1).strip())
            else:
                # Inline winner-only cell without a list wrapper or party
                # stripe (2000 TX-2): '| {{Aye}} \'\'\' [[Name]]\'\'\' (Party) 92%'
                m = re.search(r"\|\s*(\{\{[Aa]ye\}\}\s*[^\n]+)", dsec)
                if m:
                    entries.append(m.group(1).strip())

    # {{collapsible list|title=...|entry1|entry2}} minor candidates —
    # brace-aware extraction (nested templates tolerated); runs on the whole
    # section so blocks outside a list wrapper are captured too.  Items were
    # already stripped from the plainlist bodies above, so each block yields
    # its items exactly once.
    cl_items, _rest = _split_collapsible_lists(dsec)
    for e in cl_items:
        if e and "%" in e:
            entries.append(e)

    return entries


def _normalize_party(raw: str) -> str:
    p = raw.replace("Party (US)", "").replace("Party (United States)", "")
    p = p.replace("(US)", "").replace("(United States)", "").strip()
    # En-dash variants (1960s Minnesota tables use 'Democratic–Farmer–Labor')
    p = p.replace("\u2013", "-")
    p = p.replace("Minnesota Democratic-Farmer-Labor", "DFL")
    p = p.replace("North Dakota Democratic-NPL", "Democratic-NPL")
    if p.endswith(" Party"):
        p = p[:-6]
    return p.strip()


def _party_from_paren(entry: str) -> Optional[str]:
    """
    Extract a party label from a parenthesised group of a candidate bullet.

    Handles plain labels '(Republican)', wikilinked labels
    '([[Minnesota Democratic–Farmer–Labor Party|DFL]])' and non-standard
    parties '(Socialist Workers)'.  Non-party parentheticals — years,
    '(incumbent)', '(special)', '(2 seats)' — are rejected.
    """
    party_keywords = re.compile(
        r"democratic|republican|independent|libertarian|green|conservative|"
        r"liberal|progressive|prohibition|socialist|farmer|labor|union|"
        r"constitution|reform|freedom|populist|whig|unionist|silver|"
        r"readjuster|know.?nothing|dfl|npl|patriot|taxpayer|working families",
        re.IGNORECASE,
    )
    blacklist = re.compile(
        r"incumbent|redistricted|retired|re-.?elected|unopposed|uncontested|"
        r"special|deceased|withdrawn|runoff|lost|gain|hold|seats?|new|"
        r"appointed|defeated|election|primary|general",
        re.IGNORECASE,
    )
    for pm in re.finditer(r"\(([^()]*)\)", entry):
        label = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", r"\1", pm.group(1))
        # Unwrap templates inside the label: '{{party shortname|DFL Party}}' -> 'DFL Party'
        label = re.sub(r"\{\{[^|}]+\|([^}]*)\}\}", r"\1", label)
        label = re.sub(r"\{\{([^}|]+)\}\}", r"\1", label)
        label = label.strip()
        if not label or blacklist.search(label):
            continue
        norm = _normalize_party(label)
        if party_keywords.search(norm):
            return norm
    return None


def _parse_candidate(entry: str) -> Tuple[Optional[str], str, bool, Optional[float]]:
    """Parse one candidate bullet → (name, party, winner, percentage)."""
    # {{Aye}} marks the winner; pre-1932 articles use lowercase {{aye}}.
    winner = re.search(r"\{\{aye\}\}", entry, re.IGNORECASE) is not None

    # Party
    party_m = re.search(r"\{\{Party\s+stripe\|([^}]+)\}\}", entry)
    if party_m:
        party = _normalize_party(party_m.group(1))
    else:
        party = _party_from_paren(entry) or "Unknown"

    # Candidate name — priority: Sortname > wikilink > plain text
    candidate: Optional[str] = None
    for pat in (
        r"'''\{\{[Ss]ortname\|([^}]+)\}\}'''",
        r"\{\{[Ss]ortname\|([^}]+)\}\}",
    ):
        m = re.search(pat, entry)
        if m:
            parts = m.group(1).split("|")
            if len(parts) >= 2:
                candidate = f"{parts[0]} {parts[1]}"
                break

    if not candidate:
        # Bold-span extraction: 1970s bullets bold the whole
        # "'''[[Name]] ([[Party|DFL]]) NN%'''" span — take the first link inside.
        bold_m = re.search(r"'''(.+?)'''", entry, re.DOTALL)
        if bold_m:
            bold_span = bold_m.group(1)
            link_m = re.search(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", bold_span)
            if link_m:
                candidate = link_m.group(1)
            else:
                # Bold plain text, e.g. '''George P. Miller''' (Republican) 61.2%
                txt = re.sub(r"\{\{[^}]+\}\}", "", bold_span)
                txt = re.sub(r"\([^)]*\)", "", txt)
                txt = txt.replace("[[", "").replace("]]", "").strip()
                if txt:
                    candidate = txt.strip()

    if not candidate:
        # Prefer the first wikilink NOT inside an open parenthetical — the
        # party label '([[Some Party|DFL]])' would otherwise be mistaken for
        # the candidate name (1976 Minnesota bullets bold the whole
        # '[[Name]] ([[Party|DFL]]) NN%' span).
        for m in re.finditer(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", entry):
            before = entry[: m.start()]
            if before.count("(") > before.count(")"):
                continue
            candidate = m.group(1)
            break

    if not candidate:
        # Plain-text name before a parenthesised party label:
        # 'Cliff Thomallo ([[American Independent Party|American Independent]]) 1.1%'
        # or '* Morris Herring (Republican) 29.9%'.  Leading templates are
        # stripped first ('{{Party stripe|X}}John Smith (Republican) ...').
        entry2 = re.sub(r"^(?:\{\{[^}]+\}\}\s*)+", "", entry)
        m = re.match(r"([^\(\[\{\*\|]+?)\s*\(", entry2)
        if m:
            name = m.group(1).strip()
            if name and not re.search(
                r"\b(uncontested|unopposed|round|runoff|withdrew)\b", name, re.IGNORECASE
            ):
                candidate = name

    if not candidate:
        m = re.search(
            r"\}\}\s*([^\(\[\{\*\|]+?)\s*"
            r"\((?:Democratic|Republican|Independent|Libertarian|Green|DFL)\)",
            entry,
        )
        if m:
            candidate = m.group(1).strip()

    if candidate:
        candidate = re.sub(r"'''|''", "", candidate)
        candidate = re.sub(r"\{\{[^}]+\}\}", "", candidate).strip()
        candidate = re.sub(r"\s*\([^)]*\)$", "", candidate).strip()
        if candidate in ("", "Unknown"):
            candidate = None

    pct_m = re.search(r"(\d+\.?\d*)%", entry)
    if not pct_m:
        # 1954-era typo: winner share written without the % sign
        # ('(Democratic) 52.7' at end of the bullet).
        pct_m = re.search(r"\([^)]*\)\s+(\d+\.?\d*)\s*$", entry)
    percentage = float(pct_m.group(1)) if pct_m else None

    return candidate, party, winner, percentage


def _incumbent_from_section(dsec: str) -> Optional[str]:
    """
    Extract incumbent name from a district section.

    OLD: {{Sortname|First|Last}} | {{Party shading/Text/...}}
    NEW: | [[Name|Display]] followed by | {{Party shading/.../Text}}
    """
    m = re.search(
        r"\{\{Sortname\|([^}]+)\}\}\s*\|\s*\{\{Party shading/Text/[^}]+\}\}", dsec
    )
    if m:
        parts = m.group(1).split("|")
        if len(parts) >= 2:
            return f"{parts[0]} {parts[1]}"

    m = re.search(
        r"\|\s*\[\[(?:[^\]|]+\|)?([^\]]+)\]\]\s*\n"   # | [[...|Display]]
        r"(?:\|[^\n]*\n)*?"                            # optional intervening cells
        r"\|\s*\{\{[Pp]arty shading/[^}]+\}\}",        # | {{Party shading/...}} (any era)
        dsec,
    )
    if m:
        name = m.group(1).strip()
        return re.sub(r"\s*\([^)]+\)$", "", name)
    return None


def _parse_year(text: str, year: int) -> pd.DataFrame:
    """Parse all House election results from one year's article."""
    rows: List[Dict] = []
    y = str(year)

    header_pat = r"(?m)^==\s*([^=\n]+?)\s*==$"
    main_pat = (
        r"\{\{[Mm]ain(?:\s+article)?\s*\|?\s*" + y +
        r" United States House of Representatives elections?"
        r"(?:\s+in\s+[^}]+|[^}]*)?\}\}"
    )

    header_matches = list(re.finditer(header_pat, text))
    if not header_matches:
        logger.warning("No state sections matched for %d", year)
        return pd.DataFrame()

    def header_state(raw: str) -> str:
        """Resolve '== State ==' / '== [[List of ...|State]] ==' to the state name."""
        raw = raw.strip()
        m = re.search(r"\|([^]|]+)\]\]\s*$", raw)      # [[X|State]]
        if m:
            raw = m.group(1).strip()
        else:
            m = re.match(r"\[\[([^]|]+)\]\]$", raw)      # [[State]]
            if m:
                raw = m.group(1).strip()
        return re.sub(r"\s*\([^)]*\)$", "", raw).strip()

    # State sections are header-bounded (header → next level-2 header) and
    # qualify only when their span contains a {{main|YEAR ... elections ...}}
    # link (often wrapped in <!--comments--> on pre-1932 articles).  The
    # "Special elections" section carries its own tables but no {{main}}, so
    # its rows are excluded instead of being mis-attributed to a neighbour.
    unique_sections: List[Tuple[str, int, int]] = []
    for i, hm in enumerate(header_matches):
        state_name = header_state(hm.group(1))
        if not state_name or state_name.startswith("="):
            continue
        sec_end = header_matches[i + 1].start() if i + 1 < len(header_matches) else len(text)
        if not re.search(main_pat, text[hm.end():sec_end]):
            # FALLBACK (1946-1992 era): many state sections carry no {{main}}
            # template at all - just '{{See also|List of United States
            # representatives from <State>}}'.  Qualify a main-less section
            # only when the header is literally a state/territory name AND
            # the section actually contains {{ushr}} district rows.  Non-state
            # headers ('Special elections', 'Overall results', ...) stay
            # excluded so special-election rows can never leak between states.
            name_key = state_name
            if name_key not in _STATE_HEADER_NAMES:
                continue
            if not re.search(r"\{\{[Uu]shr\|", text[hm.end():sec_end]):
                continue
        unique_sections.append((state_name, hm.start(), sec_end))

    # District rows: {{ushr|MA|2|X}} (2018-era) or {{ushr|Massachusetts|1|X}} /
    # {{Ushr|Delaware|AL|X}} (pre-1932: full state name, case, |T variant);
    # '! rowspan=2 nowrap |{{ushr|...}}' prefix allowed (2022 redistricting).
    # The cell marker may be '!' (header cell) or '|' (1978-era tables put
    # {{ushr}} in ordinary data cells); {{nowrap|{{ushr|...}}}} wrappers occur
    # (1992 MI).  Some 2004+ sections link the district instead:
    # '[[United States House of Representatives, Massachusetts District 1|...]]'.
    dist_pat = (
        r"[!|]\s*(?:rowspan\s*=\s*\"?\d+\"?\s*)?(?:nowrap\s*)?\|?\s*"
        r"(?:\{\{nowrap\|\s*)?"
        r"\{\{[Uu]shr\|([^|}]+)\|([0-9]+|AL)(?:\|[^|}]*)?\}\}"
    )
    dist_pat_link = (
        r"!\s*(?:rowspan\s*=\s*\"?\d+\"?\s*)?\|?\s*"
        r"\[\[United States House of Representatives,\s*([^|\]]+?)\s+District\s+(\d+)"
    )

    # number of {{ushr}} rows declaring each race (multi-seat detector)
    seat_counts: Dict[Tuple[str, str], int] = {}

    for state_name, sec_start, sec_end in unique_sections:
        state_text = text[sec_start:sec_end]

        dist_matches = [
            (m.start(), m.group(1).strip(), m.group(2))
            for m in re.finditer(dist_pat, state_text)
        ]
        dist_matches += [
            (m.start(), m.group(1).strip(), m.group(2))
            for m in re.finditer(dist_pat_link, state_text)
        ]
        dist_matches.sort(key=lambda t: t[0])

        for jdx, (ds, raw_state, district) in enumerate(dist_matches):
            state_code = _state_code(raw_state)
            # Pre-1932 rows carry the full state name (e.g. 'Alaska
            # Territory' for delegates); 2018+ rows carry 2-letter codes.
            if re.fullmatch(r"[A-Z]{2}", raw_state):
                if state_name in STATE_NAME_TO_CODE:
                    row_state = state_name
                else:
                    # Bundled delegates table: resolve code to territory name.
                    row_state = TERRITORY_CODE_TO_NAME.get(raw_state, state_name)
            else:
                row_state = re.sub(r"\s+Territory$", "", raw_state) or state_name
            race_key = (row_state, district)
            de = dist_matches[jdx + 1][0] if jdx < len(dist_matches) - 1 else len(state_text)
            dsec = state_text[ds:de]
            # Track how many seats this race declares: ushr rows plus any
            # '(N seats)' annotation on a shared general-ticket row
            # (e.g. '! rowspan=2 | {{ushr|Illinois|AL|X}}<br/>(2 seats)').
            seats_hint = seat_counts.get(race_key, 0) + 1
            ann = re.search(r"\(\s*(\d+)\s+seats?\s*\)", dsec[:400])
            if ann:
                seats_hint = max(seats_hint, int(ann.group(1)))
            seat_counts[race_key] = seats_hint

            incumbent_name = _incumbent_from_section(dsec)
            open_seat = bool(
                re.search(
                    r"\b(retired|vacant|resigned|died|new district|new seat)\b",
                    dsec, re.IGNORECASE,
                )
            )
            # Uncontested races list the winner with no vote percentage
            # (a bare '* Uncontested' bullet beside the candidate).
            uncontested = bool(
                re.search(r"\buncontested\b|\bunopposed\b", dsec, re.IGNORECASE)
            )

            # Parse every candidate bullet first: an uncontested winner is
            # identified by having no vote share anywhere in the race.
            entries = _extract_candidate_entries(dsec)
            parsed: List[List] = []
            for entry in entries:
                if not entry:
                    continue
                # A bare '* Uncontested' bullet is not a candidate (1940-era
                # style).  The marker can also trail a real candidate bullet
                # ('(Democratic) Uncontested', 1934-era) — keep those.
                bare = re.sub(r"\{\{[^}]+\}\}", "", entry).strip()
                if re.fullmatch(r"(?:\[\[[^\]]*\]\]\s*)?uncontested[.!]?", bare, re.IGNORECASE):
                    continue
                if entry.startswith("'''") and re.search(
                    r"\b(round|runoff|first|instant)\b", entry, re.IGNORECASE
                ):
                    continue

                candidate, party, winner, percentage = _parse_candidate(entry)
                if not candidate:
                    continue
                parsed.append([candidate, party, winner, percentage,
                               bool(re.search(r"'''", entry))])

            # Bold-only winner fallback: some sections (1992 MA-9) mark the
            # winner by bold type alone, without {{aye}}.  When a race parsed
            # rows but no winner and exactly one bolded candidate bullet
            # exists, that bullet is the winner.
            if parsed and not any(p[2] for p in parsed):
                bold_rows = [p for p in parsed if p[4]]
                if len(bold_rows) == 1:
                    bold_rows[0][2] = True

            any_pct = any(pct is not None for _, _, _, pct, _ in parsed)
            for candidate, party, winner, percentage, _bold in parsed:
                if percentage is None:
                    # Keep an uncontested winner at 100%: either the table says
                    # so, or the winning candidate is the only one listed.
                    if not (winner and (uncontested or not any_pct)):
                        continue
                    percentage = 100.0

                rows.append(
                    {
                        "year": year,
                        "level": LEVEL,
                        "state": row_state,
                        "state_code": state_code,
                        "district": district,
                        "total_votes": None,   # filled by _attach_vote_counts
                        "candidate": candidate,
                        "party": party,
                        "votes": None,         # filled by _attach_vote_counts
                        "percentage": percentage,
                        "winner": winner,
                        "incumbent": bool(
                            incumbent_name
                            and candidate.lower() == incumbent_name.lower()
                        ),
                        "open_seat": open_seat,
                    }
                )

    # Multi-seat at-large completion: some tables elect N seats from one
    # shared candidate list but mark fewer than N winners with {{aye}}
    # (New York 1940 marks only the first of its two at-large seats).
    # Where a race declares N rows and N > marked winners >= 1, promote the
    # top-voted runners-up.  Races with no marked winner (e.g. NC-9 2018,
    # voided) are left untouched.
    by_race: Dict[Tuple[str, str], List[Dict]] = {}
    for r in rows:
        by_race.setdefault((r["state"], r["district"]), []).append(r)
    for key, rs in by_race.items():
        seats = seat_counts.get(key, 1)
        winners = [r for r in rs if r["winner"]]
        if 1 < seats and 1 <= len(winners) < seats:
            need = seats - len(winners)
            runners = sorted(
                (r for r in rs if not r["winner"]),
                key=lambda r: r["percentage"] if r["percentage"] is not None else -1.0,
                reverse=True,
            )
            for r in runners[:need]:
                r["winner"] = True

    return pd.DataFrame(rows)


def parse_year(text: str, year: int) -> pd.DataFrame:
    """Public wrapper around :func:`_parse_year` (handy for ad-hoc debugging)."""
    return _parse_year(text, year)


# ──────────────────────────────────────────────
# VOTE-COUNT ENRICHMENT (per-state race articles)
# ──────────────────────────────────────────────

#: '{{main|2024 United States House of Representatives elections in Alabama}}'
#: — singular 'election' on single-district states ('election in Alaska').
#: Also tolerates {{Main article|...}}, {{further|...}}, {{see also|...}}.
_STATE_RACE_TITLE_RE = re.compile(
    r"\{\{\s*(?:[Mm]ain(?:\s+article)?|[Ff]urther|[Ss]ee also)\s*\|\s*("
    r"\d{4}\s+United States House of Representatives elections?\s+in\s+"
    r"[^}|#<>]+)",
)

#: District of a general-election box, from its title:
#:   "2024 Alabama's 1st congressional district election"   → '1'
#:   "2018 Alaska's at-large congressional district"        → 'AL'
_BOX_TITLE_DISTRICT_RE = re.compile(
    r"'s\s+(at-large|\d{1,2}(?:st|nd|rd|th))\s+congressional district",
    re.IGNORECASE,
)

#: '== District 12 ==' / '== 12th district ==' level-2 race headings
_DISTRICT_HEADING_RE = re.compile(
    r"^(?:district\s+(\d{1,2})|(\d{1,2})(?:st|nd|rd|th)\s+district)$",
    re.IGNORECASE,
)

#: any heading/box-title marking a primary (or nomination) contest
_PRIMARY_MARK_RE = re.compile(
    r"primary|caucus|convention|nomination", re.IGNORECASE
)

_ANY_HEADING_FOR_VOTES_RE = re.compile(
    r"(?m)^(={2,4})\s*([^=\n].*?)\s*\1\s*$"
)


def _discover_state_race_titles(text: str, year: int) -> List[str]:
    """
    Per-state race-article titles linked from an even-year overview
    ('{{main|{year} United States House of Representatives elections in
    <State>}}'), in document order, deduplicated.  Fragments and refs are
    trimmed; non-state targets ('... election ratings') never match.
    """
    titles: List[str] = []
    for m in _STATE_RACE_TITLE_RE.finditer(text):
        t = m.group(1).strip().rstrip(",")
        if str(year) in t and " in " in t:
            titles.append(t)
    return list(dict.fromkeys(titles))


def _state_code_from_race_title(title: str) -> Optional[str]:
    """
    '2024 United States House of Representatives elections in Alabama'
    → 'AL'.  Tolerates possessive targets ("... in Alaska's at-large
    congressional district") by dropping the possessive tail.  Territory
    race articles (American Samoa, DC, Guam, … — non-voting delegates)
    resolve through the same name map so their delegate vote counts, when
    published, attach to the overview's territory rows.
    """
    m = re.search(r"\s+in\s+(.+)$", title)
    if not m:
        return None
    state_part = re.sub(r"'s\s+.*$", "", m.group(1).strip())
    state_part = re.sub(r"^the\s+", "", state_part, flags=re.IGNORECASE)
    name_to_code = dict(STATE_NAME_TO_CODE)
    name_to_code.update({v: k for k, v in TERRITORY_CODE_TO_NAME.items()})
    if state_part in name_to_code:
        return name_to_code[state_part]
    code = _state_code(state_part)          # 2-letter / 'X Territory' forms
    return code if code in name_to_code else None


def _norm_candidate_key(name: Optional[str]) -> str:
    """Case/punctuation-insensitive candidate key for name matching."""
    if not name:
        return ""
    s = re.sub(r"[^a-z\s]", " ", str(name).lower())
    return re.sub(r"\s+", " ", s).strip()


def _heading_chain(text: str, pos: int) -> List[str]:
    """
    Headings enclosing the offset *pos*, from the nearest level-2 heading
    (inclusive) down to the innermost heading before *pos* — e.g.
    ['District 2', 'Republican primary', 'Results'].
    """
    chain: List[Tuple[int, int, str]] = []   # (level, start, title)
    for m in _ANY_HEADING_FOR_VOTES_RE.finditer(text, 0, pos):
        chain.append((len(m.group(1)), m.start(), m.group(2).strip()))
    # keep the innermost chain: walk backwards, keeping strictly shallower levels
    selected: List[str] = []
    need = 4
    for level, _start, title in reversed(chain):
        if level <= need:
            selected.append(title)
            need = level - 1
            if need < 2:
                break
    selected.reverse()
    return selected


def _box_district(
    title: str, chain: List[str],
) -> Optional[str]:
    """
    District code ('1'…, 'AL') of a general-election box: the nearest
    'District N' / 'Nth district' level-2 heading, else the box title's
    "<State>'s Nth congressional district" fragment, else the chain's
    possessive heading ("Alaska's at-large congressional district").
    """
    for heading in chain:
        m = _DISTRICT_HEADING_RE.match(heading)
        if m:
            return m.group(1) or m.group(2)
    m = _BOX_TITLE_DISTRICT_RE.search(title)
    if m:
        return _district_code(m.group(1))
    for heading in chain:
        m = _BOX_TITLE_DISTRICT_RE.search(heading)
        if m:
            return _district_code(m.group(1))
    return None


def _parse_state_article_votes(
    text: str, state_name: str, state_code: str,
    default_district: Optional[str] = None,
) -> Tuple[Dict[Tuple[str, str], Optional[int]], Dict[Tuple[str, str, str], int]]:
    """
    Parse one per-state race article's general-election boxes.

    Returns ``(totals, votes)`` keyed by ``(state_code, district)`` and
    ``(state_code, district, normalised candidate)``.  For districts with
    several general boxes (concurrent special + regular, general + runoff)
    the LAST non-special box wins — the final November result the overview
    rows describe; special-only districts keep the last special box.

    Boxes whose district neither headings nor titles reveal fall back to
    *default_district* — populated only for single-district (at-large)
    states, where the article's one general race can only be that seat.

    Jungle-primary states (California, Washington, Alaska 2022+) combine
    the top-two primary and the general election in ONE
    ``{{Election box open primary begin}}`` block; the general half after
    the ``{{Election box open primary general election}}`` divider is kept.
    """
    boxes: List[Tuple[int, bool, str, List[str], str]] = []  # (order, is_special, title, chain, content)
    for order, m in enumerate(re.finditer(
        r"\{\{Election box (?:open primary )?begin(?: no change)?\s*\|?\s*"
        r"((?:[^{}]|\{\{[^}]*\}\})*?)\}\}"
        r"(.*?)\{\{Election box end\}\}",
        text, re.DOTALL,
    )):
        title_m = re.search(r"title\s*=\s*([^\n<|]+)", m.group(1))
        title = clean_wikitext(title_m.group(1).strip()) if title_m else ""
        chain = _heading_chain(text, m.start())
        is_special = "special" in title.lower() or any(
            "special" in h.lower() for h in chain
        )
        content = m.group(2)
        # Jungle-primary combined blocks (California 2024-era): the primary
        # and general halves share one box, split by the
        # 'open primary general election' divider — everything after the
        # divider is the general election, whatever the box title says
        # (CA-16's recount box is titled '... primary' yet carries the
        # general result).  A divider-less 'open primary' box holds only
        # the jungle primary — never a general result.
        divider = re.search(
            r"\{\{Election box open primary general election(?: no change)?\}\}",
            content,
        )
        if divider:
            content = content[divider.end():]
        elif m.group(0).startswith("{{Election box open primary begin"):
            continue
        elif _PRIMARY_MARK_RE.search(title) or any(
            _PRIMARY_MARK_RE.search(h) for h in chain
        ):
            continue
        boxes.append((order, is_special, title, chain, content))

    if not boxes:
        logger.info("    %s — no general-election boxes", state_name)
        return {}, {}

    # best general box per district: last non-special, else last special
    best: Dict[str, Tuple[int, bool, str, List[str], str]] = {}
    for box in boxes:
        district = _box_district(box[2], box[3]) or default_district
        if district is None:
            logger.info(
                "    %s — box %r: district unresolved, skipped",
                state_name, box[2][:60],
            )
            continue
        key = (not box[1], box[0])          # non-special beats special, then latest
        if district not in best or key > (not best[district][1], best[district][0]):
            best[district] = box

    totals: Dict[Tuple[str, str], Optional[int]] = {}
    votes: Dict[Tuple[str, str, str], int] = {}
    for district, (_order, _special, title, _chain, content) in best.items():
        box_total: Optional[int] = None
        for row in _parse_election_box_rows(content):
            row_type = row.get("row_type")
            if row_type == "Total":
                box_total = _votes_int(_param_number(row.get("votes")))
                continue
            if row_type not in ("Winning", "Candidate", "Write-in"):
                continue
            # fusion ballots (CT/NY/SC…): a cumulative cross-party line is
            # carried as a Winning row with party=Total — skip it, the same
            # candidate's party lines below add up to that figure anyway
            if (row.get("party") or "").strip().lower() == "total":
                continue
            name, _is_inc = _clean_candidate_param(row.get("candidate", ""))
            count = _votes_int(_param_number(row.get("votes")))
            if not name or count is None:
                continue    # nameless write-in/scattering rows: totals only
            key = (state_code, district, _norm_candidate_key(name))
            # multi-party fusion lines of one candidate stack up to the
            # total the overview reports (171,337 + 8,931 = 180,268 for
            # CT-5 2024)
            votes[key] = votes.get(key, 0) + count
        if box_total is None:
            counts = [v for (sc, d, _n), v in votes.items() if sc == state_code and d == district]
            box_total = sum(counts) if counts else None
        totals[(state_code, district)] = box_total
        logger.info(
            "    %s — district %s: %s candidate votes, total %s",
            state_name, district,
            sum(1 for (sc, d, _n) in votes if sc == state_code and d == district),
            f"{box_total:,}" if box_total else "—",
        )
    return totals, votes


def _attach_vote_counts(
    df: pd.DataFrame,
    overview_text: str,
    year: int,
    client: Optional["WikiAPIClient"] = None,
) -> pd.DataFrame:
    """
    Merge per-candidate ``votes`` and race-level ``total_votes`` onto the
    overview-derived results frame, keyed by (state_code, district, candidate
    name).  Race articles that are missing or carry no matching general box
    leave the affected rows' vote fields empty — enrichment is best-effort
    by design ("if possible").
    """
    if df.empty:
        return df
    titles = _discover_state_race_titles(overview_text, year)
    if not titles:
        logger.info("     no per-state race articles linked — votes stay empty")
        return df
    logger.info("     fetching %d per-state race articles for vote counts ...",
                len(titles))
    content_map = (
        client.fetch_wikitext(titles)
        if client is not None
        else fetch_articles_batch(titles)
    )

    totals: Dict[Tuple[str, str], Optional[int]] = {}
    votes: Dict[Tuple[str, str, str], int] = {}
    # single-district (at-large) states: any district-less general box can
    # only be that seat — the overview's district set provides the fallback
    state_districts = df.groupby("state_code")["district"].agg(
        lambda s: sorted({str(d) for d in s})
    ).to_dict()
    for title in titles:
        content = content_map.get(title)
        if not content:
            logger.info("     %s — article not found, skipped", title)
            continue
        state_code = _state_code_from_race_title(title)
        if state_code is None:
            logger.warning("     %s — state unresolved, skipped", title)
            continue
        state_name = re.sub(r"\s+", " ", re.search(
            r"\s+in\s+(.+)$", title).group(1).replace("'s ", " "))
        districts = state_districts.get(state_code, [])
        default_district = districts[0] if len(districts) == 1 else None
        t, v = _parse_state_article_votes(
            content, state_name, state_code, default_district=default_district,
        )
        totals.update(t)
        votes.update(v)

    if not votes and not totals:
        logger.info("     no vote counts parsed — votes stay empty")
        return df

    df = df.copy()
    if votes:
        df["votes"] = [
            votes.get((sc, str(d), _norm_candidate_key(c)))
            for sc, d, c in zip(df["state_code"], df["district"], df["candidate"])
        ]
    if totals:
        df["total_votes"] = [
            totals.get((sc, str(d)))
            for sc, d in zip(df["state_code"], df["district"])
        ]
    matched = df["votes"].notna().sum() if "votes" in df.columns else 0
    raced = df["total_votes"].notna().sum() if "total_votes" in df.columns else 0
    logger.info(
        "     votes attached: %s/%s candidate rows, %s/%s races with totals",
        f"{matched:,}", f"{len(df):,}", f"{raced:,}",
        f"{df.groupby(['state_code', 'district']).ngroups:,}",
    )
    return df


# ──────────────────────────────────────────────
# BATCH RUNNER
# ──────────────────────────────────────────────

def run(
    start_year: int = 2018,
    end_year: int = 2024,
    out_dir: str = "data",
    out_prefix: str = "house",
    client: Optional[WikiAPIClient] = None,
    include_off_years: bool = True,
    include_votes: bool = True,
) -> pd.DataFrame:
    """
    Fetch & parse House results for the election years in
    ``[start_year, end_year]`` (inclusive).

    Even (federal) years are always processed via the state-section parser;
    the odd (off-year) cycles in the range are included by default — they
    hold the special elections, whose race articles are discovered from the
    odd-year overview and parsed for the final-round (general) result —
    pass ``include_off_years=False`` to restrict the run to even years.

    When *include_votes* is on (default), each even year additionally
    fetches the per-state race articles linked from the overview and merges
    their general-election election-box vote counts onto the results
    (``votes`` per candidate, ``total_votes`` per race — empty where no
    article/box exists, e.g. most pre-2000s cycles); off-year special rows
    carry their box vote counts directly.  Pass ``include_votes=False``
    (CLI: ``--no-include-votes``) to skip the extra fetches — the vote
    columns are still emitted, empty, so the schema stays stable.

    Saves ``{out_prefix}_results_{year}.csv`` per year plus a combined
    ``{out_prefix}_results_all.csv`` into *out_dir*; off-year rows use the
    same standard schema and the same files as election years.  Returns the
    combined results frame.
    """
    import os

    even_list = even_years(start_year, end_year)
    off_years = _odd_years(start_year, end_year) if include_off_years else []
    if off_years:
        logger.info("Including off-year (odd) special cycles: %s", off_years)
    years = sorted(set(even_list) | set(off_years))
    if not years:
        logger.warning("No election years in [%d, %d] — nothing to do.",
                       start_year, end_year)
        return pd.DataFrame()
    if not include_off_years and even_list and \
            (start_year, end_year) != (even_list[0], even_list[-1]):
        logger.info("Odd bounds clamped to even years: %d–%d",
                    even_list[0], even_list[-1])
    logger.info("House cycles to process: %s", years)

    house_dir = os.path.join(out_dir, "house")
    os.makedirs(house_dir, exist_ok=True)

    titles = {y: article_title(y) for y in years}
    logger.info("Fetching %d House articles via the MediaWiki API ...", len(titles))
    content_map = (
        client.fetch_wikitext(list(titles.values()))
        if client is not None
        else fetch_articles_batch(list(titles.values()))
    )

    all_frames: List[pd.DataFrame] = []

    def _save(df: pd.DataFrame, name: str) -> None:
        if df.empty:
            return
        fname = os.path.join(house_dir, name)
        df.to_csv(fname, index=False)
        logger.info("     saved -> %s", fname)

    for year, title in titles.items():
        text = content_map.get(title)
        if not text:
            logger.warning("FAIL %d — article not found (%s)", year, title)
            continue

        logger.info("OK %d — %s chars", year, f"{len(text):,}")

        if year % 2 == 0:
            # ── even cycle: regular general elections via state sections ──
            df = _parse_year(text, year)
            logger.info("     %s candidate rows", f"{len(df):,}")
            if include_votes and not df.empty:
                df = _attach_vote_counts(df, text, year, client=client)
            if not df.empty:
                df = _cast_vote_columns(df)
                _save(df, f"{out_prefix}_results_{year}.csv")
                all_frames.append(df)
            continue

        # ── odd (off-year) cycle: special elections via race articles ──
        race_titles = discover_special_race_titles(text, year)
        logger.info("     %d special races discovered", len(race_titles))
        if not race_titles:
            continue
        race_texts = (
            client.fetch_wikitext(race_titles)
            if client is not None
            else fetch_articles_batch(race_titles)
        )
        results_df = parse_off_year_specials(year, race_texts)
        logger.info("     %s result rows", f"{len(results_df):,}")
        _save(results_df, f"{out_prefix}_results_{year}.csv")
        if not results_df.empty:
            all_frames.append(results_df)

    if all_frames:
        combined = _cast_vote_columns(pd.concat(all_frames, ignore_index=True))
        combined_path = os.path.join(house_dir, f"{out_prefix}_results_all.csv")
        combined.to_csv(combined_path, index=False)
        logger.info("Combined -> %s (%s rows)", combined_path, f"{len(combined):,}")

    if all_frames:
        return pd.concat(all_frames, ignore_index=True)
    return pd.DataFrame()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    run()
