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
    year, level, state, state_code, district, candidate, party,
    percentage, winner, incumbent, open_seat

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
)
from statewide_elections import parse_race_article

logger = logging.getLogger(__name__)

LEVEL = "house"


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
    an even-year file.  (Per-race vote counts are not emitted for House;
    the statewide families keep their own vote-count files.)
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

        # standard house schema: final-round candidate rows only (no totals)
        for r in gen_df.to_dict("records"):
            if r.get("row_type") == "Total":
                continue
            house_rows.append({
                "year": year, "level": LEVEL, "state": state,
                "state_code": state_code, "district": district,
                "candidate": r.get("candidate"), "party": r.get("party"),
                "percentage": r.get("percentage"), "winner": r.get("winner"),
                "incumbent": r.get("incumbent"), "open_seat": True,
            })

    df = pd.DataFrame(
        house_rows,
        columns=["year", "level", "state", "state_code", "district",
                 "candidate", "party", "percentage", "winner", "incumbent",
                 "open_seat"],
    )
    if len(df):
        df = df.drop_duplicates(
            subset=["year", "state", "district", "candidate"], keep="first",
        ).reset_index(drop=True)
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
        for e in re.split(r"\n\s*\*\s*|\|\s*\*\s*", new_pl.group(1)):
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

    # {{collapsible list|title=...|entry1|entry2}} minor candidates
    for cl_m in re.finditer(
        r"\{\{collapsible list\|[^}]*\|([^}]+)\}\}", dsec, re.DOTALL | re.IGNORECASE
    ):
        for e in cl_m.group(1).split("|"):
            e = e.strip()
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
                        "candidate": candidate,
                        "party": party,
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
# BATCH RUNNER
# ──────────────────────────────────────────────

def run(
    start_year: int = 2018,
    end_year: int = 2024,
    out_dir: str = "data",
    out_prefix: str = "house",
    client: Optional[WikiAPIClient] = None,
    include_off_years: bool = True,
) -> pd.DataFrame:
    """
    Fetch & parse House results for the election years in
    ``[start_year, end_year]`` (inclusive).

    Even (federal) years are always processed via the state-section parser;
    the odd (off-year) cycles in the range are included by default — they
    hold the special elections, whose race articles are discovered from the
    odd-year overview and parsed for the final-round (general) result —
    pass ``include_off_years=False`` to restrict the run to even years.

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
            if not df.empty:
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
        combined = pd.concat(all_frames, ignore_index=True)
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
