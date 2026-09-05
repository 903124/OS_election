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

Each overview's ``== Race summary ==`` section carries one sortable table per
scope (``===States===`` and, for governor, ``=== Territories and federal
district ===``) with rows keyed by ``! [[#State|State]]`` and a Candidates
cell holding a ``{{Plainlist|* ...}}`` bullet per candidate:

    * {{Party stripe|Republican Party (US)}}{{aye}} '''[[Kay Ivey]]''' (Republican) 59.5%

Output columns (written under *output_dir*, default ``data/statewide/``):
    year, state, state_code, office, candidate, party, percentage,
    winner, incumbent

    statewide_results_{year}.csv   per year
    statewide_results_all.csv      combined across the requested range

Usage:
    python cli.py statewide --start-year 2018 --end-year 2024
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
    get_default_client,
    set_default_client,
)

logger = logging.getLogger("statewide_elections")

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
    "district of columbia": "DC", "guam": "GU",
    "northern mariana islands": "MP", "american samoa": "AS",
    "u.s. virgin islands": "VI", "united states virgin islands": "VI",
    "puerto rico": "PR",
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
    "no party preference (united states)": "No party preference",
    "no party preference": "No party preference",
    "nonpartisan": "Nonpartisan",
    "write-in": "Write-in",
}

_COLUMNS = [
    "year", "state", "state_code", "office", "candidate", "party",
    "percentage", "winner", "incumbent",
]


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
    """Header cell labels: the first row-chunk that carries '!' cells."""
    chunks = re.split(r"(?m)^\|-.*$", table)
    for chunk in chunks:
        if re.search(r"(?m)^!", chunk):
            return [_clean_cell(c) for c in re.findall(r"(?m)^!(?:[^!\n]*)", chunk)]
    return []


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
# BATCH DRIVER
# ────────────────────────────────────────────────────────────────────────────

def run(
    start_year: int = 2018,
    end_year: int = 2024,
    output_dir: str = "data",
    client: Optional[WikiAPIClient] = None,
) -> pd.DataFrame:
    """CLI entry point: process statewide executive cycles from *start_year*
    to *end_year*, writing CSVs under ``<output_dir>/statewide/``."""
    years = even_years(start_year, end_year)
    if not years:
        logger.warning("No even years in [%d, %d] — nothing to do.", start_year, end_year)
        return pd.DataFrame()
    if (start_year, end_year) != (years[0], years[-1]):
        logger.info("Odd bounds clamped to even years: %d–%d", years[0], years[-1])
    logger.info("Statewide cycles to process: %s", years)

    statewide_dir = os.path.join(output_dir, "statewide")
    os.makedirs(statewide_dir, exist_ok=True)

    client = client or get_default_client()

    all_frames: List[pd.DataFrame] = []
    meta = {"years": {}, "missing_articles": []}

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

        if frames:
            df_year = pd.concat(frames, ignore_index=True)
            path = os.path.join(statewide_dir, f"statewide_results_{year}.csv")
            df_year.to_csv(path, index=False)
            logger.info("  saved -> %s", path)
            all_frames.append(df_year)
        meta["years"][year] = year_meta

    if all_frames:
        combined = pd.concat(all_frames, ignore_index=True)
        path = os.path.join(statewide_dir, "statewide_results_all.csv")
        combined.to_csv(path, index=False)
        logger.info("Combined -> %s (%s rows)", path, f"{len(combined):,}")

        ts = time.strftime("%Y%m%d_%H%M%S")
        with open(os.path.join(statewide_dir, f"statewide_metadata_{ts}.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2, default=str)

        return combined

    return pd.DataFrame()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    run()
