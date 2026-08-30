"""
senate_elections.py — Parse U.S. Senate election data (polling + results) for a
range of election cycles, from Wikipedia wikitext fetched via the MediaWiki
Action API.

For every even year in [start_year, end_year] the cycle's overview article
(``{year} United States Senate elections``) is fetched and its ``{{main|...}}``
links are followed to discover every race article — regular *and* special
elections — so no state list is hardcoded. Wikitext is then retrieved in
rate-limited batches of <= 50 titles (see wiki_utils).

Outputs (written under *output_dir*, default ``data/senate/``; all carry a
Year column):
    senate_primary_polling_{year}.csv   long format, one row per poll x candidate
    senate_general_polling_{year}.csv
    senate_primary_results_{year}.csv
    senate_general_results_{year}.csv
    senate_{key}_all.csv                combined across the requested range
    senate_metadata_<ts>.json           run metadata + per-race details

Usage:
    python cli.py senate --start-year 2018 --end-year 2024
    python senate_elections.py           # direct run, defaults (2018-2024)
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
from typing import Dict, List, Optional, Tuple

import pandas as pd

from wiki_utils import (
    WikiAPIClient,
    clean_wikitext,
    even_years,
    extract_incumbent_flag,
    fetch_articles_batch,
    remove_wikilinks,
)

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# RACE DISCOVERY (from cycle overview articles)
# ──────────────────────────────────────────────

RESULT_KEYS = ("primary_polling", "primary_results", "general_polling", "general_results")


def overview_title(year: int) -> str:
    """Title of a cycle's overview article, e.g. '2018 United States Senate elections'."""
    return f"{year} United States Senate elections"


def discover_race_titles(text: str, year: int) -> List[str]:
    """
    Extract per-race article titles from a cycle's overview article.

    Race sections link to their dedicated article via
    ``{{main|<year> United States Senate [special] election in <State>}}``;
    following these picks up special elections automatically and ignores
    non-race sections (Results summary, Predictions, ...).
    """
    pattern = (
        r"\{\{\s*[Mm]ain\s*\|\s*("
        + str(year)
        + r" United States Senate (?:special )?election in [^}|]+?)\s*(?:\|[^}]*)?\}\}"
    )
    return list(dict.fromkeys(re.findall(pattern, text)))


def state_from_title(title: str) -> Optional[str]:
    """
    Derive the state label from a race article title.

    '2018 United States Senate election in Arizona'             → 'Arizona'
    '2018 United States Senate special election in Mississippi' → 'Mississippi (special)'
    """
    m = re.search(r"United States Senate (special )?election in (.+?)\s*$", title)
    if not m:
        return None
    state = m.group(2).strip()
    return f"{state} (special)" if m.group(1) else state


# ──────────────────────────────────────────────
# INCUMBENT / NAME HELPERS
# ──────────────────────────────────────────────

def extract_infobox_incumbent(text: str) -> Optional[str]:
    """
    Extract the incumbent senator from the article infobox.

    Looks for ``| before_election = [[Name]]``; returns the display name or None.
    """
    if not text:
        return None
    match = re.search(r"\|\s*before_election\s*=\s*\[\[([^\]]+)\]\]", text)
    if match:
        incumbent_raw = match.group(1)
        if "|" in incumbent_raw:  # [[Name|Display]] → Display
            return incumbent_raw.split("|")[-1].strip()
        return incumbent_raw.strip()
    return None


def normalize_candidate_name(name: str) -> str:
    """Normalize a candidate name for comparison/merging (markup + incumbent)."""
    if not name:
        return name
    name = clean_wikitext(name)
    name, _ = extract_incumbent_flag(name)
    return re.sub(r"\s+", " ", name).strip()


def merge_duplicate_candidates(
    df: pd.DataFrame,
    name_col: str = "Candidate",
    group_cols: Optional[List[str]] = None,
) -> pd.DataFrame:
    """Normalize candidate names in *df* so identical names can be grouped/dropped."""
    if df.empty or name_col not in df.columns:
        return df
    df = df.copy()
    df[name_col] = df[name_col].apply(normalize_candidate_name)
    return df


def parse_candidate_header(header: str, is_primary: bool = False) -> Tuple[str, str]:
    """
    Parse candidate name and party from a polling-table column header.

    'Martha McSally (R)' → ('Martha McSally', 'R')
    'Joe Arpaio'         → ('Joe Arpaio', 'Unknown')   (primary polls)
    """
    header = clean_wikitext(header)
    party_match = re.search(r"\(([DRILG])\)$", header)
    if party_match:
        party = party_match.group(1)
        candidate = re.sub(r"\s*\([DRILG]\)$", "", header).strip()
    else:
        party = "Unknown"
        candidate = header.strip()
    return candidate, party


# ──────────────────────────────────────────────
# POLLING TABLE PARSER
# ──────────────────────────────────────────────

_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

#: Substrings that identify a polling-table header as metadata (not a candidate).
_SKIP_HEADERS = ("poll source", "source", "date", "sample", "margin", "other",
                 "undecided", "vs.", "moe", "round")

_DASH_CHARS = ("-", "–", "—")

#: leading cell attributes like style="..." / colspan="2" (possibly several)
_ATTR_RE = re.compile(r"^\s*(?:[a-zA-Z-]+\s*=\s*\"[^\"]*\"\s*)+")


def _strip_cell_markup(cell: str) -> str:
    """Strip leading attributes and inline templates, then any structural pipe."""
    cell = _ATTR_RE.sub("", cell)
    cell = re.sub(r"\{\{[^}]+\}\}", "", cell)  # inline templates, e.g. {{efn|...|name="Key"}}
    if "|" in cell:
        cell = cell.rsplit("|", 1)[1]
    return cell


def _parse_header_cells(table_content: str) -> List[str]:
    """
    Collect ordered header cells ('!' lines) from a wikitable, in all layout
    generations (2018 'style="..."| Name', 2020 'style="..." | Name',
    sortable 'colspan="7"|X' and pipe-less '![[Name]]').

    Spanning headers (colspan >= 2, e.g. 'Sara Gideon vs. Susan Collins')
    are skipped: they group columns but have no data column of their own.
    """
    headers: List[str] = []
    for line in table_content.split("\n"):
        line = line.strip()
        if not line.startswith("!"):
            continue
        span = re.search(r'colspan\s*=\s*"?(\d+)', line)
        if span and int(span.group(1)) > 1:
            continue
        headers.append(clean_wikitext(_strip_cell_markup(line[1:])))
    return headers


def _split_row_cells(segment: str) -> List[str]:
    """
    Split a modern-style table row into raw cell strings.

    Cells start with '|' (attributes and inline '||' handled); continuation
    lines are appended to the previous cell (multi-line refs/templates).
    """
    cells: List[str] = []
    for line in segment.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith(("!", "|-", "|}", "|+")):
            continue
        if stripped.startswith("|"):
            body = _ATTR_RE.sub("", stripped[1:])
            for part in body.split("||"):
                cells.append(part.strip())
        elif cells:
            cells[-1] += " " + stripped
    return cells


def _clean_cell(cell: str) -> str:
    """Plain text out of a table cell (templates, refs, party shading, bold)."""
    cell = html.unescape(cell)
    cell = re.sub(r"<ref[^>]*>.*?</ref>", "", cell, flags=re.DOTALL)
    cell = re.sub(r"\{\{[^}]+\}\}", "", cell)
    cell = cell.replace("'''", "")
    if "|" in cell:  # e.g. '{{party shading/D}} |67%'
        cell = cell.rsplit("|", 1)[1]
    cell = remove_wikilinks(cell)
    cell = re.sub(r"<[^>]+>", " ", cell)
    return re.sub(r"\s+", " ", cell).strip()


def _parse_modern_row(
    row: str, headers: List[str]
) -> Tuple[Optional[str], Optional[Dict]]:
    """
    Parse a 2020+ polling-table row (no 'align=center|' markers) by aligning
    row cells to the header columns.

    When the cell count matches the header count, the date column is taken
    directly from the header layout (robust against month names inside
    broken <ref> markup).  Otherwise detection falls back to the first cell
    containing a month name.

    Returns (poll_source, {Date, Sample, MoE, candidates}) — either may be
    None when the row is a continuation row or has no recognisable date.
    """
    cells = _split_row_cells(row)
    if not cells:
        return None, None

    date_col = next((i for i, h in enumerate(headers) if "date" in h.lower()), None)
    if date_col is not None and len(cells) == len(headers):
        date_idx = date_col
    else:
        date_idx = next(
            (i for i, c in enumerate(cells) if any(m in c for m in _MONTHS)), None
        )
    if date_idx is None or date_idx >= len(cells):
        return None, None

    date_value = _clean_cell(cells[date_idx])
    if not any(m in date_value for m in _MONTHS):
        return None, None  # misaligned junk — skip the row

    source = None
    for cell in cells[:date_idx]:
        m = re.search(r"\[\[([^\]]+)\]\]", cell)
        if m:
            source = m.group(1).split("|")[-1].strip()
            break

    sample = moe = ""
    candidate_values: List[str] = []
    for header, cell in zip(headers[date_idx + 1:], cells[date_idx + 1:]):
        hl = header.lower()
        if "sample" in hl:
            sample = _clean_cell(cell)
        elif "margin" in hl or "moe" in hl:
            moe = _clean_cell(cell)
        elif hl == "none" or any(s in hl for s in _SKIP_HEADERS):
            continue
        else:
            candidate_values.append(_clean_cell(cell))

    if sample and sample[0] in _DASH_CHARS:
        sample = ""  # e.g. '– (LV)' — sample size not reported
    if moe and moe[0] in _DASH_CHARS:
        moe = ""    # e.g. '–' — margin of error not reported
    parsed = {
        "Date": _clean_cell(cells[date_idx]),
        "Sample": sample,
        "MoE": moe,
        "candidates": candidate_values,
    }
    return source, parsed


def parse_polling_table_universal(
    section_text: str,
    state_name: str,
    is_primary: bool = False,
    primary_party: Optional[str] = None,
) -> pd.DataFrame:
    """
    Universal polling table parser (primary and general election formats).

    Supports two table generations:
      * 2018 style — cells carry ``align=center|`` markers; columns are
        inferred positionally (Sample, MoE, candidates).
      * 2020+ style — plain cells, ``sortable`` tables with colspan/attribute
        headers; columns are aligned against the header row instead.

    Primary polls have headers like 'Joe<br />Arpaio' (party inferred from the
    section context); general polls carry suffixes like 'Martha McSally (R)'.
    Returns a wide-format DataFrame.
    """
    polling_match = re.search(r"===+\s*Polling\s*===+", section_text)
    if polling_match is None:
        return pd.DataFrame()
    polling_section = section_text[polling_match.start():]

    table_start = polling_section.find("{|")
    table_end = polling_section.find("|}", table_start)
    if table_start == -1 or table_end == -1:
        return pd.DataFrame()
    table_content = polling_section[table_start : table_end + 2]

    # ── Header cells (ordered, metadata included) ─────────────────
    headers = _parse_header_cells(table_content)

    def _is_candidate_header(h: str) -> bool:
        hl = h.lower()
        # 'None' alone is a junk placeholder column; 'None of these' (Nevada's
        # ballot option) is kept as a real column.
        return bool(h) and hl != "none" and not any(s in hl for s in _SKIP_HEADERS)

    candidate_headers = [h for h in headers if _is_candidate_header(h)]
    if not candidate_headers:
        logger.warning("No candidate headers found in %s polling table", state_name)
        return pd.DataFrame()

    # ── Data rows ─────────────────────────────────────────────────
    rows: List[Dict] = []
    raw_rows = re.split(r"\|-\s*", table_content)
    current_poll_source: Optional[str] = None

    for row in raw_rows:
        if "! Poll" in row or not row.strip():
            continue

        source_match = re.search(r"\|\s*(?:rowspan\s*=\s*\d+\s*)?\[\[([^\]]+)\]\]", row)
        if source_match:
            source_raw = source_match.group(1)
            current_poll_source = (
                source_raw.split("|")[-1].strip() if "|" in source_raw else source_raw.strip()
            )

        fields = re.findall(r"align=center\|\s*([^\n|]+)", row)
        if not fields:
            fields = re.findall(
                r"(?:\{\{party shading/[^}]+\}\}\s*)?align=center\|\s*([^\n|]+)", row
            )

        if fields:
            # ── 2018-style positional parsing ──────────────────────
            clean_fields = [clean_wikitext(f.replace("'''", "")).strip() for f in
                            (re.sub(r"\{\{[^}]+\}\}\s*", "", f) for f in fields)]

            first_field = clean_fields[0]
            if not any(month in first_field for month in _MONTHS):
                continue  # continuation (rowspan) row
            if not current_poll_source:
                continue

            row_data: Dict = {"Poll_Source": current_poll_source, "Date": first_field}
            remaining = clean_fields[1:]
            num_candidates = len(candidate_headers)

            if len(remaining) >= num_candidates + 2:          # Sample + MoE + candidates
                row_data["Sample"] = remaining[0]
                row_data["MoE"] = None if remaining[1] in _DASH_CHARS else remaining[1]
                start_idx = 2
            elif len(remaining) >= num_candidates + 1:        # Sample + (MoE?)+ candidates
                row_data["Sample"] = remaining[0]
                if remaining[1] in _DASH_CHARS:
                    row_data["MoE"] = None
                    start_idx = 2
                elif re.match(r"^[\d.]+%?$", remaining[1]):   # no MoE column
                    row_data["MoE"] = None
                    start_idx = 1
                else:
                    row_data["MoE"] = remaining[1]
                    start_idx = 2
            else:                                             # best effort
                row_data["Sample"] = remaining[0] if remaining else ""
                row_data["MoE"] = None
                start_idx = 1

            candidate_values = remaining[start_idx:]
            for i, header in enumerate(candidate_headers):
                row_data[header] = candidate_values[i] if i < len(candidate_values) else None

            rows.append(row_data)
            continue

        # ── 2020+ style: align cells to header columns ────────────
        source, parsed = _parse_modern_row(row, headers)
        if source:
            current_poll_source = source
        if parsed is None or not current_poll_source:
            continue

        row_data = {
            "Poll_Source": current_poll_source,
            "Date": parsed["Date"],
            "Sample": parsed["Sample"],
            "MoE": parsed["MoE"] or None,
        }
        for header, value in zip(candidate_headers, parsed["candidates"]):
            row_data[header] = value or None
        rows.append(row_data)

    return pd.DataFrame(rows)


def wide_to_long_polls(
    df_wide: pd.DataFrame,
    state_name: str,
    primary_type: str = "Unknown",
    is_primary: bool = False,
    primary_party: Optional[str] = None,
    incumbent_name: Optional[str] = None,
) -> pd.DataFrame:
    """
    Melt a wide polling DataFrame into long format with metadata columns.

    Long format columns:
        State, Primary_Type, Poll_Source, Date, Sample, MoE,
        Candidate, Party, Pct, Incumbent
    """
    if df_wide.empty:
        return pd.DataFrame()

    meta_cols = [c for c in ("Poll_Source", "Date", "Sample", "MoE") if c in df_wide.columns]
    non_candidate = ("other", "undecided")
    candidate_cols = [
        c for c in df_wide.columns
        if c not in meta_cols and not any(nc in c.lower() for nc in non_candidate)
    ]
    if not candidate_cols:
        logger.warning("No candidate columns found in %s", state_name)
        return pd.DataFrame()

    df_long = df_wide.melt(
        id_vars=meta_cols, value_vars=candidate_cols,
        var_name="Candidate_Header", value_name="Pct",
    )

    parsed = df_long["Candidate_Header"].apply(lambda h: parse_candidate_header(h, is_primary))
    df_long["Candidate"] = [p[0] for p in parsed]
    df_long["Party"] = [p[1] for p in parsed]

    incumbent_extracted = df_long["Candidate"].apply(extract_incumbent_flag)
    df_long["Candidate"] = [p[0] for p in incumbent_extracted]
    df_long["Incumbent"] = [p[1] for p in incumbent_extracted]

    if is_primary and primary_party:
        df_long.loc[df_long["Party"] == "Unknown", "Party"] = primary_party

    if incumbent_name:
        incumbent_lower = incumbent_name.lower().strip()
        df_long.loc[
            df_long["Candidate"].str.lower().str.strip() == incumbent_lower, "Incumbent"
        ] = True

    df_long["State"] = state_name
    df_long["Primary_Type"] = primary_type

    df_long["Pct"] = df_long["Pct"].replace(["-", "", "–", "—"], pd.NA)
    df_long = df_long.dropna(subset=["Pct"])
    if "MoE" in df_long.columns:
        df_long["MoE"] = df_long["MoE"].replace(["-", "–", "—", "", "N/A", "n/a"], pd.NA)

    col_order = ["State", "Primary_Type", "Poll_Source", "Date", "Sample",
                 "MoE", "Candidate", "Party", "Pct", "Incumbent"]
    return df_long[[c for c in col_order if c in df_long.columns]]


# ──────────────────────────────────────────────
# ELECTION BOX PARSER
# ──────────────────────────────────────────────

def parse_election_boxes(
    text: str, state_name: str, incumbent_name: Optional[str] = None
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Parse ``{{Election box ...}}`` templates into (primary, general) DataFrames.

    Handles candidate-name bracket removal, incumbent detection (embedded
    suffix and infobox match) and drops 'change' columns.
    """
    pattern = r"\{\{Election box begin(?: no change)?\s*\|?\s*([^}]*?)\}\}(.*?)\{\{Election box end\}\}"
    all_data: List[Dict] = []

    for header, box_content in re.findall(pattern, text, re.DOTALL):
        # title runs to end-of-line, <ref>, or the closing braces
        title_match = re.search(r"title\s*=\s*([^\n<]+)", header)
        election_title = title_match.group(1).strip() if title_match else "Unknown"
        election_title = clean_wikitext(election_title)

        is_primary = "primary" in election_title.lower()
        election_type = "Primary" if is_primary else "General"

        row_patterns = [
            (r"\{\{Election box winning candidate with party link(?: no change)?[\s\|]([^{}]+)\}\}", "Winning"),
            (r"\{\{Election box candidate with party link(?: no change)?[\s\|]([^{}]+)\}\}", "Candidate"),
            (r"\{\{Election box write-in with party link(?: no change)?[\s\|]([^{}]+)\}\}", "Write-in"),
            (r"\{\{Election box total(?: no change)?[\s\|]([^{}]+)\}\}", "Total"),
        ]

        for regex, row_type in row_patterns:
            for params in re.findall(regex, box_content, re.DOTALL):
                row: Dict = {
                    "State": state_name,
                    "Election": election_title,
                    "Election_Type": election_type,
                    "Row_Type": row_type,
                    "Incumbent": False,
                }
                for key, val in re.findall(r"(?:^|\|)\s*(\w+)\s*=\s*([^|\n]+)", params):
                    val = clean_wikitext(val.strip())
                    if key.lower() == "candidate":
                        val = remove_wikilinks(val)
                        val, is_inc = extract_incumbent_flag(val)
                        if is_inc:
                            row["Incumbent"] = True
                        row[key] = val
                    elif key.lower() == "change" and is_primary:
                        continue
                    elif key.lower() == "incumbent":
                        continue
                    else:
                        row[key] = val
                all_data.append(row)

    if not all_data:
        return pd.DataFrame(), pd.DataFrame()

    df = pd.DataFrame(all_data)
    cols_to_remove = [c for c in df.columns if c.lower() == "change"]
    if cols_to_remove:
        df = df.drop(columns=cols_to_remove)

    if incumbent_name and "candidate" in df.columns:
        incumbent_lower = incumbent_name.lower().strip()
        df.loc[df["candidate"].str.lower().str.strip() == incumbent_lower, "Incumbent"] = True

    primary_df = df[df["Election_Type"] == "Primary"].copy()
    general_df = df[df["Election_Type"] == "General"].copy()
    return primary_df, general_df


# ──────────────────────────────────────────────
# UNIVERSAL PARSER (per state)
# ──────────────────────────────────────────────

def parse_election_data_universal(text: str, state_name: str, year: Optional[int] = None) -> Dict:
    """
    Parse one state's Senate election article.

    Detects the primary structure (two_party / single_party / jungle /
    no_primary), extracts long-format polling data and election-box results.
    When *year* is given, a ``Year`` column is added to every output frame.
    """
    results: Dict = {
        "primary_type": "no_primary",
        "incumbent": None,
        "primary_polling": pd.DataFrame(),
        "primary_results": pd.DataFrame(),
        "general_polling": pd.DataFrame(),
        "general_results": pd.DataFrame(),
    }

    incumbent_name = extract_infobox_incumbent(text)
    results["incumbent"] = incumbent_name
    logger.info("  Incumbent from infobox: %s", incumbent_name)

    # ── STEP 1: detect primary structure ──────────────────────────
    dem_start = text.find("==Democratic primary==")
    rep_start = text.find("==Republican primary==")
    jungle_start = text.find("==Primary election==")   # jungle-primary format (CA/WA/LA)
    general_start = text.find("==General election==")
    if general_start == -1:
        general_start = len(text)

    has_dem = dem_start != -1 and dem_start < general_start
    has_rep = rep_start != -1 and rep_start < general_start
    has_jungle = jungle_start != -1 and jungle_start < general_start

    primary_party: Optional[str] = None
    if has_dem and has_rep:
        results["primary_type"] = "two_party"
        primary_section_start = min(dem_start, rep_start)
    elif has_dem:
        primary_section_start, results["primary_type"] = dem_start, "single_party"
        primary_party = "D"
    elif has_rep:
        primary_section_start, results["primary_type"] = rep_start, "single_party"
        primary_party = "R"
    elif has_jungle:
        primary_section_start, results["primary_type"] = jungle_start, "jungle"
    else:
        results["primary_type"] = "no_primary"
        primary_section_start = None

    # ── STEP 2: parse polling tables ──────────────────────────────
    def _append_long(df_long: pd.DataFrame) -> None:
        if len(df_long):
            results["primary_polling"] = pd.concat(
                [results["primary_polling"], df_long], ignore_index=True
            )

    if primary_section_start is not None:
        if results["primary_type"] == "two_party":
            if dem_start != -1:
                dem_end = rep_start if rep_start > dem_start else general_start
                dem_wide = parse_polling_table_universal(
                    text[dem_start:dem_end], state_name, is_primary=True, primary_party="D"
                )
                if not dem_wide.empty:
                    _append_long(wide_to_long_polls(
                        dem_wide, state_name, "Democratic Primary",
                        is_primary=True, primary_party="D", incumbent_name=incumbent_name,
                    ))
            if rep_start != -1:
                rep_end = dem_start if dem_start > rep_start else general_start
                rep_wide = parse_polling_table_universal(
                    text[rep_start:rep_end], state_name, is_primary=True, primary_party="R"
                )
                if not rep_wide.empty:
                    _append_long(wide_to_long_polls(
                        rep_wide, state_name, "Republican Primary",
                        is_primary=True, primary_party="R", incumbent_name=incumbent_name,
                    ))
        else:
            primary_wide = parse_polling_table_universal(
                text[primary_section_start:general_start], state_name,
                is_primary=True, primary_party=primary_party,
            )
            if not primary_wide.empty:
                results["primary_polling"] = wide_to_long_polls(
                    primary_wide, state_name, results["primary_type"],
                    is_primary=True, primary_party=primary_party,
                    incumbent_name=incumbent_name,
                )

    if general_start != -1 and general_start < len(text):
        general_wide = parse_polling_table_universal(
            text[general_start:], state_name, is_primary=False
        )
        if not general_wide.empty:
            results["general_polling"] = wide_to_long_polls(
                general_wide, state_name, "General",
                is_primary=False, incumbent_name=incumbent_name,
            )

    # ── STEP 3: parse election boxes ────────────────────────────────────
    results["primary_results"], results["general_results"] = parse_election_boxes(
        text, state_name, incumbent_name
    )

    if year is not None:
        for key in RESULT_KEYS:
            df = results[key]
            if len(df):
                df = df.copy()
                df.insert(0, "Year", year)
                results[key] = df
    return results


# ──────────────────────────────────────────────
# BATCH PROCESSING + OUTPUT
# ──────────────────────────────────────────────

def process_senate_cycles(
    start_year: int,
    end_year: int,
    client: Optional[WikiAPIClient] = None,
) -> Dict:
    """
    Discover and parse every Senate race for the even years in
    ``[start_year, end_year]``.

    Race titles come from each cycle's overview article (regular *and*
    special elections); all race articles are then fetched in rate-limited
    batches and parsed.  Returns per-year and combined DataFrames plus
    metadata.
    """
    years = even_years(start_year, end_year)
    meta: Dict = {
        "start_year": start_year,
        "end_year": end_year,
        "years_processed": years,
        "total_processed": 0,
        "successful": 0,
        "failed": 0,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    state_details: Dict[str, Dict] = {}
    races_discovered: Dict[int, List[str]] = {}
    frames: Dict[str, Dict[int, List[pd.DataFrame]]] = {key: {} for key in RESULT_KEYS}

    def _assemble() -> Dict:
        by_year: Dict[str, Dict[int, pd.DataFrame]] = {}
        combined: Dict[str, pd.DataFrame] = {}
        for key in RESULT_KEYS:
            by_year[key] = {}
            parts: List[pd.DataFrame] = []
            for year in sorted(frames[key]):
                df_year = pd.concat(frames[key][year], ignore_index=True)
                by_year[key][year] = df_year
                parts.append(df_year)
            combined[key] = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
            logger.info(
                "%s: %s rows across %d year(s)", key, len(combined[key]), len(by_year[key])
            )
        return {
            "metadata": meta,
            "state_details": state_details,
            "races_discovered": races_discovered,
            "by_year": by_year,
            **combined,
        }

    if not years:
        logger.warning("No even years in [%d, %d] — nothing to do.", start_year, end_year)
        return _assemble()

    # ── STEP 1: discover race titles from overview articles ────────
    logger.info(
        "Discovering Senate races %d–%d via overview articles ...", years[0], years[-1]
    )
    overview_titles = [overview_title(y) for y in years]
    overview_map = (
        client.fetch_wikitext(overview_titles)
        if client is not None
        else fetch_articles_batch(overview_titles)
    )
    for year in years:
        text = overview_map.get(overview_title(year))
        if not text:
            logger.warning("Overview article not found for %d — skipping", year)
            races_discovered[year] = []
            continue
        titles = discover_race_titles(text, year)
        races_discovered[year] = titles
        logger.info("  %d: %d races found", year, len(titles))

    race_map: Dict[str, Tuple[int, str]] = {}
    for year, titles in races_discovered.items():
        for title in titles:
            state = state_from_title(title)
            if state:
                race_map[title] = (year, state)
            else:
                logger.warning("Could not derive state from title: %s", title)

    if not race_map:
        logger.warning("No Senate races discovered for the requested range.")
        return _assemble()

    # ── STEP 2: fetch all race articles in rate-limited batches ────
    logger.info("Fetching %d Senate race articles via the MediaWiki API ...", len(race_map))
    content_map = (
        client.fetch_wikitext(list(race_map))
        if client is not None
        else fetch_articles_batch(list(race_map))
    )

    # ── STEP 3: parse each race ────────────────────────────────────
    for title, (year, state) in race_map.items():
        meta["total_processed"] += 1
        logger.info("=" * 72)
        logger.info("Processing: %s (%d)", state, year)

        content = content_map.get(title)
        if not content:
            logger.warning("  FAIL — article not found (%s)", title)
            state_details[title] = {"year": year, "state": state, "error": "Article not found"}
            meta["failed"] += 1
            continue

        try:
            parsed = parse_election_data_universal(content, state, year=year)
            state_details[title] = {
                "year": year,
                "state": state,
                "primary_type": parsed["primary_type"],
                "content_length": len(content),
                "primary_polling_count": len(parsed["primary_polling"]),
                "primary_results_count": len(parsed["primary_results"]),
                "general_polling_count": len(parsed["general_polling"]),
                "general_results_count": len(parsed["general_results"]),
            }
            meta["successful"] += 1
            for key in RESULT_KEYS:
                if len(parsed[key]):
                    frames[key].setdefault(year, []).append(parsed[key])

            logger.info(
                "  OK — %s | primary polls: %d | primary results: %d | "
                "general polls: %d | general results: %d",
                parsed["primary_type"],
                len(parsed["primary_polling"]), len(parsed["primary_results"]),
                len(parsed["general_polling"]), len(parsed["general_results"]),
            )
        except Exception as exc:  # keep the batch alive on per-race errors
            logger.exception("  FAIL %s (%d): %s", state, year, exc)
            state_details[title] = {"year": year, "state": state, "error": str(exc)}
            meta["failed"] += 1

    logger.info("=" * 72)
    return _assemble()


def save_results(results: Dict, output_dir: str = "data") -> str:
    """Write per-year and combined CSVs + metadata JSON under *output_dir*/senate."""
    import os

    senate_dir = os.path.join(output_dir, "senate")
    os.makedirs(senate_dir, exist_ok=True)
    by_year: Dict[str, Dict[int, pd.DataFrame]] = results.get("by_year", {})

    for key in RESULT_KEYS:
        for year, df_year in sorted(by_year.get(key, {}).items()):
            if len(df_year):
                path = os.path.join(senate_dir, f"senate_{key}_{year}.csv")
                df_year.to_csv(path, index=False)
                logger.info("Saved: %s", path)
        combined = results.get(key)
        if combined is not None and len(combined):
            path = os.path.join(senate_dir, f"senate_{key}_all.csv")
            combined.to_csv(path, index=False)
            logger.info("Saved: %s", path)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    metadata_path = os.path.join(senate_dir, f"senate_metadata_{timestamp}.json")
    with open(metadata_path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "metadata": results["metadata"],
                "state_details": results["state_details"],
                "races_discovered": results.get("races_discovered", {}),
            },
            fh, indent=2, default=str,
        )
    logger.info("Saved: %s", metadata_path)
    return output_dir


def run(
    start_year: int = 2018,
    end_year: int = 2024,
    output_dir: str = "data",
    client: Optional[WikiAPIClient] = None,
) -> Dict:
    """CLI entry point: process Senate cycles from *start_year* to *end_year*."""
    logger.info("U.S. Senate cycles %d–%d", start_year, end_year)
    results = process_senate_cycles(start_year, end_year, client=client)
    save_results(results, output_dir)
    meta = results["metadata"]
    logger.info(
        "Senate done: %d races processed, %d ok, %d failed -> %s/senate",
        meta["total_processed"], meta["successful"], meta["failed"], output_dir,
    )
    return results


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    run()
