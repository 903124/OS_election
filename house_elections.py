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

Output columns (standardised):
    year, level, state, state_code, district, candidate, party,
    percentage, winner, incumbent, open_seat

Outputs (written to *out_dir*, default ``data/``):
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

logger = logging.getLogger(__name__)

LEVEL = "house"


# ──────────────────────────────────────────────
# ARTICLE TITLE BUILDER
# ──────────────────────────────────────────────

def article_title(year: int) -> str:
    return f"{year} United States House of Representatives elections"


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
    p = p.replace("Minnesota Democratic-Farmer-Labor", "DFL")
    p = p.replace("North Dakota Democratic-NPL", "Democratic-NPL")
    if p.endswith(" Party"):
        p = p[:-6]
    return p.strip()


def _parse_candidate(entry: str) -> Tuple[Optional[str], str, bool, Optional[float]]:
    """Parse one candidate bullet → (name, party, winner, percentage)."""
    # {{Aye}} marks the winner; pre-1932 articles use lowercase {{aye}}.
    winner = re.search(r"\{\{aye\}\}", entry, re.IGNORECASE) is not None

    # Party
    party_m = re.search(r"\{\{Party\s+stripe\|([^}]+)\}\}", entry)
    if party_m:
        party = _normalize_party(party_m.group(1))
    else:
        pt_m = re.search(
            r"\((Democratic|Republican|Independent|Libertarian|Green|"
            r"DFL|Constitution|Working Families|Progressive|Reform)\)",
            entry,
        )
        party = pt_m.group(1) if pt_m else "Unknown"
        party = party.replace(" (US)", "").strip()

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
        for pat in (
            r"'''\[\[([^\]|]+)\|([^\]]+)\]\]'''",
            r"'''\[\[([^\]]+)\]\]'''",
            r"\[\[([^\]|]+)\|([^\]]+)\]\]",
            r"\[\[([^\]]+)\]\]",
        ):
            m = re.search(pat, entry)
            if m:
                candidate = m.groups()[-1]
                candidate = re.sub(r"\s*\([^)]+\)$", "", candidate)
                break

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
        if candidate in ("", "Unknown"):
            candidate = None

    pct_m = re.search(r"(\d+\.?\d*)%", entry)
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

    # State sections are header-bounded (header → next level-2 header) and
    # qualify only when their span contains a {{main|YEAR ... elections ...}}
    # link (often wrapped in <!--comments--> on pre-1932 articles).  The
    # "Special elections" section carries its own tables but no {{main}}, so
    # its rows are excluded instead of being mis-attributed to a neighbour.
    unique_sections: List[Tuple[str, int, int]] = []
    for i, hm in enumerate(header_matches):
        state_name = hm.group(1).strip()
        if state_name.startswith("="):
            continue
        sec_end = header_matches[i + 1].start() if i + 1 < len(header_matches) else len(text)
        if not re.search(main_pat, text[hm.end():sec_end]):
            continue
        unique_sections.append((state_name, hm.start(), sec_end))

    # District rows: {{ushr|MA|2|X}} (2018-era) or {{ushr|Massachusetts|1|X}} /
    # {{Ushr|Delaware|AL|X}} (pre-1932: full state name, case, |T variant);
    # '! rowspan=2 nowrap |{{ushr|...}}' prefix allowed (2022 redistricting).
    dist_pat = (
        r"!\s*(?:rowspan\s*=\s*\"?\d+\"?\s*)?(?:nowrap\s*)?\|?\s*"
        r"\{\{[Uu]shr\|([^|}]+)\|([0-9]+|AL)(?:\|[^|}]*)?\}\}"
    )

    # number of {{ushr}} rows declaring each race (multi-seat detector)
    seat_counts: Dict[Tuple[str, str], int] = {}

    for state_name, sec_start, sec_end in unique_sections:
        state_text = text[sec_start:sec_end]

        dist_matches = list(re.finditer(dist_pat, state_text))
        for jdx, dm in enumerate(dist_matches):
            raw_state = dm.group(1).strip()
            district = dm.group(2)
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
            ds = dm.start()
            de = dist_matches[jdx + 1].start() if jdx < len(dist_matches) - 1 else len(state_text)
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
            parsed: List[Tuple[str, str, bool, Optional[float]]] = []
            for entry in _extract_candidate_entries(dsec):
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
                parsed.append((candidate, party, winner, percentage))

            any_pct = any(pct is not None for _, _, _, pct in parsed)
            for candidate, party, winner, percentage in parsed:
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
) -> pd.DataFrame:
    """
    Fetch & parse House results for the even years in
    ``[start_year, end_year]`` (inclusive).

    Saves ``{out_prefix}_results_{year}.csv`` per year plus a combined
    ``{out_prefix}_results_all.csv`` into *out_dir*; returns the combined frame.
    """
    import os

    years = even_years(start_year, end_year)
    if not years:
        logger.warning("No even years in [%d, %d] — nothing to do.", start_year, end_year)
        return pd.DataFrame()
    if (start_year, end_year) != (years[0], years[-1]):
        logger.info("Odd bounds clamped to even years: %d–%d", years[0], years[-1])
    logger.info("House cycles to process: %s", years)

    os.makedirs(out_dir, exist_ok=True)

    titles = {y: article_title(y) for y in years}
    logger.info("Fetching %d House articles via the MediaWiki API ...", len(titles))
    content_map = (
        client.fetch_wikitext(list(titles.values()))
        if client is not None
        else fetch_articles_batch(list(titles.values()))
    )

    all_frames: List[pd.DataFrame] = []

    for year, title in titles.items():
        text = content_map.get(title)
        if not text:
            logger.warning("FAIL %d — article not found (%s)", year, title)
            continue

        logger.info("OK %d — %s chars", year, f"{len(text):,}")
        df = _parse_year(text, year)
        logger.info("     %s candidate rows", f"{len(df):,}")

        if not df.empty:
            fname = os.path.join(out_dir, f"{out_prefix}_results_{year}.csv")
            df.to_csv(fname, index=False)
            logger.info("     saved -> %s", fname)
            all_frames.append(df)

    if all_frames:
        combined = pd.concat(all_frames, ignore_index=True)
        combined_path = os.path.join(out_dir, f"{out_prefix}_results_all.csv")
        combined.to_csv(combined_path, index=False)
        logger.info("Combined -> %s (%s rows)", combined_path, f"{len(combined):,}")
        return combined

    return pd.DataFrame()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    run()
