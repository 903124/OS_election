"""
senate_elections.py — Parse U.S. Senate election data (polling + results) for a
range of election cycles, from Wikipedia wikitext fetched via the MediaWiki
Action API.

For every election year in [start_year, end_year] the cycle's overview article
(``{year} United States Senate elections``) is fetched and its ``{{main|...}}``
links are followed to discover every race article — regular *and* special
elections — so no state list is hardcoded.  Even (federal) cycles and odd
(off-year) cycles are both processed by default; odd years are exactly where
the special elections live (NJ/MA 2013, AL 2017, ...).  Odd-year overview
articles exist for every cycle with a special election and link the races
via the same ``{{main|<year> United States Senate special election in
<State>}}`` pattern.  Pass ``include_off_years=False`` (CLI:
``--no-include-off-years``) to process even (federal) years only.
Wikitext is retrieved in rate-limited batches of <= 50 titles
(see wiki_utils).

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

import wiki_utils
from wiki_utils import (
    WikiAPIClient,
    clean_wikitext,
    even_years,
    extract_incumbent_flag,
    fetch_articles_batch,
    normalize_polling_date,
    remove_wikilinks,
    unwrap_format_templates,
)

logger = logging.getLogger(__name__)


def _odd_years(start_year: int, end_year: int) -> List[int]:
    """
    Odd (off-year) years within ``[start_year, end_year]``, inclusive.

    Defined locally — deliberately not imported from ``wiki_utils`` — so this
    module stays compatible with stock wiki_utils versions that only ship
    ``even_years``.  ``_odd_years(2013, 2018)`` → ``[2013, 2015, 2017]``.
    Returns an empty list when the range contains no odd year.
    """
    start = start_year if start_year % 2 == 1 else start_year + 1
    end = end_year if end_year % 2 == 1 else end_year - 1
    return list(range(start, end + 1, 2)) if start <= end else []


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
                 "undecided", "vs.", "moe", "round",
                 "error", "lead", "spread", "pollster")

_DASH_CHARS = ("-", "–", "—")

#: Plausibility bounds for a margin of error in percentage points.  The
#: largest MoE observed in real polling tables is ~11%; anything above 20
#: points cannot come from a real poll and indicates a mis-aligned cell
#: (a sample size like '450 (LV)' or a candidate pct that shifted columns).
_MOE_MAX_PCT = 20.0

_MOE_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
#: '2% – 4' / '2 – 4' — a reported MoE *range* has no single value
_MOE_RANGE_RE = re.compile(r"\d\s*%?\s*[-–—]\s*\.?\d")
#: parenthetical qualifier or range — '(LV)', '(RV)', '(2% – 4%)'
_MOE_PAREN_RE = re.compile(r"\([^)]{1,24}\)")


def _parse_moe_value(raw) -> Optional[float]:
    """
    Parse a raw polling-table MoE cell into a numeric margin of error.

    '±3.7%' / '± 5.0%' / '± 5%' / '+ 3.29%' / '3.42%' → 3.7 / 5.0 / 5.0 / 3.29 / 3.42
    '± -3.1' → 3.1 (sign typo); '± 3.79.%' → 3.79 (source typo)

    Non-numeric or unusable cells → None (empty in the CSV):
      dashes ('–'), '± n/a%', '±n/a', '± nil', 'rowspan=2' markup,
      sample sizes leaked from a mis-aligned cell ('450 (LV)', '500 (RV)'),
      MoE ranges ('± (2% – 4%)') and implausible values (> 20 points).
    """
    if raw is None:
        return None
    try:
        if pd.isna(raw):
            return None
    except (TypeError, ValueError):
        pass
    s = str(raw).strip()
    if not s or s in _DASH_CHARS:
        return None
    low = s.lower()
    if any(t in low for t in ("n/a", "na%", "nil", "rowspan", "?", "unknown")):
        return None
    if _MOE_PAREN_RE.search(s) or _MOE_RANGE_RE.search(s):
        return None
    m = _MOE_NUMBER_RE.search(s)
    if not m:
        return None
    try:
        val = abs(float(m.group(0)))
    except ValueError:
        return None
    if val <= 0 or val > _MOE_MAX_PCT:
        return None
    return round(val, 2)


#: leading cell attributes like style="..." / colspan="2" (possibly several)
_ATTR_RE = re.compile(r"^\s*(?:[a-zA-Z-]+\s*=\s*\"[^\"]*\"\s*)+")


def _strip_cell_markup(cell: str) -> str:
    """Strip leading attributes and inline templates, then any structural pipe."""
    cell = _ATTR_RE.sub("", cell)
    cell = unwrap_format_templates(cell)       # {{Small|x}} → x (keep content!)
    cell = re.sub(r"\{\{[^}]+\}\}", "", cell)  # inline templates, e.g. {{efn|...|name="Key"}}
    # Resolve wikilinks BEFORE dropping the structural pipe: rsplit on '|'
    # first would keep only the text after the last inner pipe of a piped
    # link ([[Scott Brown (politician)|Scott Brown]] → "Scott Brown]]").
    cell = remove_wikilinks(cell)
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
    cell = unwrap_format_templates(cell)       # {{nowrap|date}} → date
    cell = re.sub(r"\{\{[^}]+\}\}", "", cell)
    # malformed source markup: an unclosed whitelisted wrapper — keep the
    # content; any other dangling '{{…' fragment is dropped
    cell = re.sub(
        r"^\s*\{\{\s*(?:small|sm|nowrap|nowrapr|nobr|sort)\s*\|",
        "", cell, flags=re.IGNORECASE,
    )
    cell = re.sub(r"\{\{[^}]*$", " ", cell)
    cell = cell.replace("'''", "")
    # Resolve wikilinks BEFORE dropping the structural pipe: rsplit on '|'
    # first would keep only the text after the last inner pipe of a piped
    # link ([[John James (politician)|John James]] → "John James]]").
    cell = remove_wikilinks(cell)
    if "|" in cell:  # e.g. '{{party shading/D}} |67%'
        cell = cell.rsplit("|", 1)[1]
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
        hl = header.lower().replace(".", "")   # 'M.o.E.' → 'moe'
        if "sample" in hl:
            sample = _clean_cell(cell)
        elif "moe" in hl or "error" in hl:
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



# ──────────────────────────────────────────────
# POLLING SECTION DISCOVERY
# ──────────────────────────────────────────────

#: A polling heading at any level: '== Polling ==', '=== Polls ===',
#: '===Opinion polling===', '==== Public opinion polling ===='.
POLLING_HEADING_RE = re.compile(
    r"^(=+)\s*(?:opinion\s+|public\s+opinion\s+)?poll(?:s|ing)\s*\1\s*$",
    re.IGNORECASE | re.MULTILINE,
)

#: Any wikitext section heading (used to bound a polling section's scope).
_ANY_HEADING_RE = re.compile(r"^(=+)\s*[^=\n].*?\1\s*$", re.MULTILINE)


def _scope_end_from(text: str, from_pos: int, level: int) -> int:
    """
    End offset of a section at heading *level* whose content starts at
    *from_pos*: the next heading of the same or higher level (or end of
    text).  Deeper sub-headings — e.g. ``==== Graphical summary ====""
    under ``=== Polling ===`` — stay inside the scope so tables below them
    are still parsed.
    """
    for m in _ANY_HEADING_RE.finditer(text, from_pos):
        if len(m.group(1)) <= level:
            return m.start()
    return len(text)


def iter_polling_spans(text: str) -> List[Tuple[int, int, int, int]]:
    """
    Locate every polling section in an article.

    Returns a list of ``(heading_start, heading_end, scope_end, level)``
    tuples, in document order.  *scope_end* is where the polling section's
    content stops: the next heading of the same or higher level.  Nested
    polling headings (e.g. ``==== Polling ====`` inside a scope already
    covered) are skipped to keep every table parsed exactly once.
    """
    spans: List[Tuple[int, int, int, int]] = []
    for m in POLLING_HEADING_RE.finditer(text):
        level = len(m.group(1))
        scope_end = _scope_end_from(text, m.end(), level)
        # skip headings nested inside a previous (wider) polling scope
        if spans and m.start() < spans[-1][2]:
            continue
        spans.append((m.start(), m.end(), scope_end, level))
    return spans


def _extract_tables(segment: str) -> List[str]:
    """Return every ``{| ... |}`` wikitable inside *segment*, in order."""
    tables: List[str] = []
    pos = 0
    while True:
        start = segment.find("{|", pos)
        if start == -1:
            break
        end = segment.find("|}", start + 2)
        if end == -1:
            break
        tables.append(segment[start : end + 2])
        pos = end + 2
    return tables


def parse_polling_table_universal(
    section_text: str,
    state_name: str,
    is_primary: bool = False,
    primary_party: Optional[str] = None,
) -> pd.DataFrame:
    """
    Universal polling parser (primary and general election formats).

    Finds the polling heading (``Polling`` / ``Polls`` / ``Opinion polling``,
    any heading level) inside *section_text* and parses **every** wikitable in
    the heading's scope — older articles split polls across several tables,
    and the previous implementation silently kept only the first.

    Supports two table generations:
      * 2018 style — cells carry ``align=center|`` markers; columns are
        aligned against the header row (positional guessing only as a
        fallback, which previously mis-shifted Sample/MoE/candidate values).
      * 2020+ style — plain cells, ``sortable`` tables with colspan/attribute
        headers; columns are aligned against the header row.

    Primary polls have headers like 'Joe<br />Arpaio' (party inferred from the
    section context); general polls carry suffixes like 'Martha McSally (R)'.
    Returns a wide-format DataFrame.
    """
    span = POLLING_HEADING_RE.search(section_text)
    if span is None:
        return pd.DataFrame()
    scope = section_text[span.end() : _scope_end_from(section_text, span.end(), len(span.group(1)))]

    frames: List[pd.DataFrame] = []
    for table_content in _extract_tables(scope):
        df = _parse_one_polling_table(table_content, state_name)
        if not df.empty:
            frames.append(df)

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]


def _first_pipe_cell_text(row: str) -> Optional[str]:
    """Cleaned text of a row's first ``|`` cell (the pollster in multi-line
    tables where the source column carries no wikilink, e.g. 'Quinnipiac')."""
    for line in row.split("\n"):
        s = line.strip()
        if not s.startswith("|") or s.startswith(("|-", "|}", "+")):
            continue
        body = _ATTR_RE.sub("", s[1:]).split("||")[0]
        return _clean_cell(body) or None
    return None


def _plausible_source(text: Optional[str]) -> bool:
    """True when *text* looks like a pollster name rather than a date/value."""
    if not text:
        return False
    if any(m in text for m in _MONTHS):
        return False
    if re.match(r"^[\d.,]+%?$", text.strip()):
        return False
    return True


def _protect_template_pipes(text: str) -> str:
    """
    Replace ``|`` characters inside ``{{…}}`` templates with a placeholder so
    cell-splitting regexes don't truncate cells at template-internal pipes
    (e.g. ``align=center| {{nowrap|May 23, 2015}}`` used to capture only
    ``{{nowrap`` — losing the date and shifting every later column).
    """
    out: List[str] = []
    i, in_tpl = 0, False
    while i < len(text):
        if text.startswith("{{", i):
            in_tpl = True
            out.append("{{")
            i += 2
            continue
        if in_tpl and text.startswith("}}", i):
            in_tpl = False
            out.append("}}")
            i += 2
            continue
        ch = text[i]
        out.append("\x00" if (in_tpl and ch == "|") else ch)
        i += 1
    return "".join(out)


def _parse_one_polling_table(table_content: str, state_name: str) -> pd.DataFrame:
    """Parse a single polling wikitable into a wide-format DataFrame."""
    # ── Header cells (ordered, metadata included) ─────────────────
    headers = _parse_header_cells(table_content)

    def _is_candidate_header(h: str) -> bool:
        # dot-normalised so dotted spellings like 'M.o.E.' match 'moe'
        hl = h.lower().replace(".", "")
        # 'None' alone is a junk placeholder column; 'None of these' (Nevada's
        # ballot option) is kept as a real column.
        return bool(h) and hl != "none" and not any(s in hl for s in _SKIP_HEADERS)

    candidate_headers = [h for h in headers if _is_candidate_header(h)]
    meta_kinds = []
    for h in headers:
        hl = h.lower().replace(".", "")   # 'M.o.E.' → 'moe'
        if "date" in hl:
            kind = "date"
        elif "sample" in hl:
            kind = "sample"
        elif "moe" in hl or "error" in hl:
            kind = "moe"
        elif "poll source" in hl or hl.startswith("source"):
            kind = "source"
        elif hl == "none" or any(s in hl for s in _SKIP_HEADERS):
            kind = "skip"          # lead/margin/other/undecided… — value ignored
        else:
            kind = None            # candidate column
        meta_kinds.append(kind)
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

        row_protected = _protect_template_pipes(row)
        fields = re.findall(r"align=center\|\s*([^\n|]+)", row_protected)
        if not fields:
            fields = re.findall(
                r"(?:\{\{party shading/[^}]+\}\}\s*)?align=center\|\s*([^\n|]+)",
                row_protected,
            )
        if fields:
            fields = [f.replace("\x00", "|") for f in fields]

        if fields:
            # ── 2018-style parsing, header-aligned where possible ──
            clean_fields = [clean_wikitext(f.replace("'''", "")).strip() for f in
                            (unwrap_format_templates(f) for f in fields)]

            # Map fields onto header columns when the counts line up: the
            # poll-source cell often carries no align=center marker, so try
            # both offsets before falling back to the positional heuristic.
            date_idx_hdr = next(
                (i for i, k in enumerate(meta_kinds) if k == "date"), None
            )
            aligned: Optional[Dict] = None
            for offset in (1, 0):
                if date_idx_hdr is None or len(clean_fields) != len(headers) - offset:
                    continue
                vals = ([None] * offset) + clean_fields
                if not any(m in (vals[date_idx_hdr] or "") for m in _MONTHS):
                    continue
                aligned = {"Poll_Source": current_poll_source or ""}
                for i, kind in enumerate(meta_kinds):
                    if kind == "date":
                        aligned["Date"] = vals[i] or ""
                    elif kind == "sample":
                        aligned["Sample"] = vals[i] or ""
                    elif kind == "moe":
                        aligned["MoE"] = vals[i] or None
                cand_vals = [vals[i] for i, k in enumerate(meta_kinds) if k is None]
                cand_vals = [v for v in cand_vals if v is not None]
                for i, header in enumerate(candidate_headers):
                    aligned[header] = cand_vals[i] if i < len(cand_vals) else None
                break

            if aligned is not None:
                if not aligned.get("Poll_Source"):
                    first = _first_pipe_cell_text(row)
                    if _plausible_source(first):
                        current_poll_source = first
                if not aligned.get("Poll_Source"):
                    continue
                rows.append(aligned)
                continue

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
        if parsed is not None and not current_poll_source:
            # plain-text pollster cell (no wikilink) — e.g. 1998-era tables
            first = _first_pipe_cell_text(row)
            if _plausible_source(first):
                current_poll_source = first
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

    ``MoE`` is emitted as a numeric margin of error in percentage points
    (see :func:`_parse_moe_value`): the raw cell text ('± 4.3%') is parsed
    to ``4.3`` and unusable cells (dashes, 'n/a', leaked sample sizes,
    MoE ranges) become empty.
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

    # Placeholder / junk values seen in real polling tables — treat as missing
    # rather than letting '?' or '± ?' leak into the CSV.
    #
    # pandas 3 gotcha: ``astype(str)`` no longer coerces missing values to the
    # literal string 'nan' — NA stays NA (float NaN / pd.NA), so the row-wise
    # regex below would receive a non-string and raise
    # "expected string or bytes-like object, got 'float'".  Guard with an
    # isinstance check so both pandas 2.x and 3.x behave identically.
    _MISSING = ["-", "", "–", "—", "?", "± ?", "n/a", "N/A", "NA", "unknown", "Unknown"]
    _DASH_ONLY_RE = re.compile(r"^[\s\-–—―‒–?±%]*$")   # dashes, '?', '±', '%' only
    for col in ("Pct", "MoE", "Sample"):
        if col in df_long.columns:
            s = df_long[col].astype(str)
            df_long.loc[
                s.apply(lambda v: bool(_DASH_ONLY_RE.match(v)) if isinstance(v, str) else False),
                col,
            ] = pd.NA
    df_long["Pct"] = df_long["Pct"].replace(_MISSING, pd.NA)
    df_long = df_long.dropna(subset=["Pct"])
    if "MoE" in df_long.columns:
        df_long["MoE"] = df_long["MoE"].replace(_MISSING, pd.NA)
        # numeric margin of error in percentage points — unusable cells → NA
        df_long["MoE"] = df_long["MoE"].map(_parse_moe_value)
    if "Sample" in df_long.columns:
        df_long["Sample"] = df_long["Sample"].replace(_MISSING, pd.NA)

    # Multiple tables per polling section can repeat the same poll row; drop
    # exact duplicates so combined CSVs stay tidy.
    df_long = df_long.drop_duplicates(
        subset=[c for c in ("State", "Primary_Type", "Poll_Source", "Date",
                            "Candidate", "Pct") if c in df_long.columns]
    )

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
    # Row templates may embed one level of nested templates inside parameters
    # (e.g. 'change = {{decrease}}4.87' on 1980s-2000s articles), so the
    # parameter body must tolerate balanced '{{...}}' spans.
    _param_body = r"((?:[^{}]|\{\{[^}]*\}\})+?)"
    pattern = (r"\{\{Election box begin(?: no change)?\s*\|?\s*"
               r"((?:[^{}]|\{\{[^}]*\}\})*?)\}\}"
               r"(.*?)\{\{Election box end\}\}")
    all_data: List[Dict] = []
    box_idx = 0

    for header, box_content in re.findall(pattern, text, re.DOTALL):
        box_idx += 1
        # title runs to end-of-line, <ref>, or the closing braces
        title_match = re.search(r"title\s*=\s*([^\n<]+)", header)
        election_title = title_match.group(1).strip() if title_match else "Unknown"
        election_title = clean_wikitext(election_title)

        is_primary = "primary" in election_title.lower()
        election_type = "Primary" if is_primary else "General"

        row_patterns = [
            (r"\{\{Election box winning candidate with party link(?: no change)?[\s\|]" + _param_body + r"\}\}", "Winning"),
            (r"\{\{Election box candidate with party link(?: no change)?[\s\|]" + _param_body + r"\}\}", "Candidate"),
            (r"\{\{Election box write-in with party link(?: no change)?[\s\|]" + _param_body + r"\}\}", "Write-in"),
            (r"\{\{Election box total(?: no change)?[\s\|]" + _param_body + r"\}\}", "Total"),
        ]

        for regex, row_type in row_patterns:
            for params in re.findall(regex, box_content, re.DOTALL):
                row: Dict = {
                    "State": state_name,
                    "Election": election_title,
                    "Election_Type": election_type,
                    "Row_Type": row_type,
                    "Incumbent": False,
                    "_box": box_idx,
                }
                # Value must tolerate '|' inside [[link|display]] and
                # {{template|arg}} spans — a naive [^|\n]+ truncates the value
                # at the first inner pipe (e.g. candidate = [[John Buckley
                # (Virginia politician)|John Buckley]] → "[[John Buckley
                # (Virginia politician)"), corrupting names and losing markup.
                for key, val in re.findall(
                    r"(?:^|\|)\s*(\w+)\s*=\s*"
                    r"((?:\{\{[^{}]*\}\}|\[\[[^\]]*\]\]|[^\|\n{])+)",
                    params,
                ):
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

    # Winner fallback: some articles (1964 NY) list all candidates without a
    # 'winning candidate' template.  Within a box that has no Winning row,
    # the top-voted Candidate row is the winner.
    if "votes" in df.columns:
        votes_num = pd.to_numeric(
            df["votes"].astype(str).str.replace(",", "", regex=False), errors="coerce"
        )
        for _, box_group in df.groupby("_box"):
            has_winning = (box_group["Row_Type"] == "Winning").any()
            if has_winning:
                continue
            cands = box_group[box_group["Row_Type"] == "Candidate"]
            if cands.empty:
                continue
            v = votes_num.loc[cands.index]
            if v.notna().any():
                top = v.idxmax()
                df.loc[top, "Row_Type"] = "Winning"

    df = df.drop(columns=["_box"])
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

#: Heading lookups tolerate spaces and any heading level
#: (``==General election==`` *and* ``== General election ==``).
def _find_heading(text: str, name: str) -> int:
    """Offset of the first level-2 heading matching *name*, or -1."""
    m = re.search(rf"^==\s*{re.escape(name)}\s*==\s*$", text, re.MULTILINE | re.IGNORECASE)
    return m.start() if m else -1


def parse_election_data_universal(text: str, state_name: str, year: Optional[int] = None) -> Dict:
    """
    Parse one state's Senate election article.

    Detects the primary structure (two_party / single_party / jungle /
    no_primary), extracts long-format polling data and election-box results.
    When *year* is given, a ``Year`` column is added to every output frame.

    Polling sections are discovered positionally: every ``Polling``/``Polls``
    heading found in the article is classified as primary or general
    according to where it sits relative to the primary and general-election
    sections.  This also captures articles whose polling section is *not*
    nested under a ``== General election ==`` heading (previously those
    polling tables were skipped entirely).
    """
    results: Dict = {
        "primary_type": "no_primary",
        "incumbent": None,
        "polling_sections_found": 0,
        "primary_polling": pd.DataFrame(),
        "primary_results": pd.DataFrame(),
        "general_polling": pd.DataFrame(),
        "general_results": pd.DataFrame(),
    }

    incumbent_name = extract_infobox_incumbent(text)
    results["incumbent"] = incumbent_name
    logger.info("  Incumbent from infobox: %s", incumbent_name)

    # ── STEP 1: detect primary structure ──────────────────────────
    dem_start = _find_heading(text, "Democratic primary")
    rep_start = _find_heading(text, "Republican primary")
    jungle_start = _find_heading(text, "Primary election")   # jungle format (CA/WA/LA)
    general_start = _find_heading(text, "General election")
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

    # Classify every polling heading as primary or general by position.
    poll_spans = iter_polling_spans(text)
    results["polling_sections_found"] = len(poll_spans)

    dem_end = rep_start if (dem_start != -1 and rep_start > dem_start) else general_start
    rep_end = dem_start if (rep_start != -1 and dem_start > rep_start) else general_start

    def _primary_context(pos: int) -> Optional[Tuple[str, Optional[str]]]:
        """(Primary_Type, party) when *pos* lies inside a primary section."""
        if primary_section_start is None or pos >= general_start:
            return None
        if dem_start != -1 and dem_start <= pos < dem_end:
            return ("Democratic Primary", "D")
        if rep_start != -1 and rep_start <= pos < rep_end:
            return ("Republican Primary", "R")
        return (results["primary_type"], primary_party)

    parsed_any_polling = False
    for head_start, head_end, scope_end, _level in poll_spans:
        ctx = _primary_context(head_start)
        if ctx is not None:
            # a primary-classified polling section must never swallow
            # general-election content (possible when an unusually high
            # heading level widens the scope)
            scope_end = min(scope_end, general_start)
            if head_start >= scope_end:
                continue
        wide = parse_polling_table_universal(
            text[head_start:scope_end], state_name,
            is_primary=ctx is not None,
            primary_party=ctx[1] if ctx else None,
        )
        if wide.empty:
            continue
        parsed_any_polling = True
        if ctx is not None:
            _append_long(wide_to_long_polls(
                wide, state_name, ctx[0],
                is_primary=True, primary_party=ctx[1],
                incumbent_name=incumbent_name,
            ))
        else:
            general_long = wide_to_long_polls(
                wide, state_name, "General",
                is_primary=False, incumbent_name=incumbent_name,
            )
            results["general_polling"] = pd.concat(
                [results["general_polling"], general_long], ignore_index=True
            ) if len(results["general_polling"]) or len(general_long) else general_long

    # Legacy fallback: slices-based polling discovery for articles whose
    # polling tables sit under headings this module's regexes don't match.
    if not parsed_any_polling and not poll_spans and primary_section_start is not None:
        if results["primary_type"] == "two_party":
            if dem_start != -1:
                dem_wide = parse_polling_table_universal(
                    text[dem_start:dem_end], state_name, is_primary=True, primary_party="D"
                )
                if not dem_wide.empty:
                    _append_long(wide_to_long_polls(
                        dem_wide, state_name, "Democratic Primary",
                        is_primary=True, primary_party="D", incumbent_name=incumbent_name,
                    ))
            if rep_start != -1:
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

    if general_start != -1 and general_start < len(text) and not parsed_any_polling:
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
    for key in ("primary_polling", "general_polling"):
        df = results.get(key)
        if df is not None and len(df):
            results[key] = normalize_polling_date_columns(df)
    return results


def normalize_polling_date_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Normalise the free-text ``Date`` column of a polling DataFrame to ISO
    8601 (see :func:`wiki_utils.normalize_polling_date`).

    Adds ``Date_Start`` / ``Date_End`` (pure ISO, machine-sortable) right
    after ``Date``, canonicalises ``Date`` itself (``2002-10-16`` /
    ``2002-10-28 to 2002-10-30`` / ``through 2024-11-04`` / ``2017-09``) and
    preserves the raw source text in ``Date_Original`` (last column).
    """
    if "Date" not in df.columns or "Date_Start" in df.columns:
        return df
    df = df.copy()
    fallback_year = int(df["Year"].iloc[0]) if "Year" in df.columns else None
    originals = df["Date"].astype(str).tolist()
    starts, ends, canonical = [], [], []
    for raw in originals:
        s, e, c = normalize_polling_date(raw, fallback_year=fallback_year)
        starts.append(s)
        ends.append(e)
        canonical.append(c)
    df["Date"] = canonical
    pos = df.columns.get_loc("Date") + 1
    df.insert(pos, "Date_Start", starts)
    df.insert(pos + 1, "Date_End", ends)
    # keep the raw Wikipedia text for provenance / re-processing
    df["Date_Original"] = originals
    return df


# ──────────────────────────────────────────────
# BATCH PROCESSING + OUTPUT
# ──────────────────────────────────────────────

def process_senate_cycles(
    start_year: int,
    end_year: int,
    client: Optional[WikiAPIClient] = None,
    include_off_years: bool = True,
) -> Dict:
    """
    Discover and parse every Senate race for the election years in
    ``[start_year, end_year]``.

    Even (federal) years are always processed; the odd (off-year) cycles in
    the range are included by default — they hold the special elections
    (NJ/MA 2013, AL 2017, ...) — pass ``include_off_years=False`` to
    restrict the run to even years.  Race titles come from each cycle's
    overview article (regular *and* special elections); all race articles
    are then fetched in rate-limited batches and parsed.  Returns per-year
    and combined DataFrames plus metadata.
    """
    years = even_years(start_year, end_year)
    if include_off_years:
        off = _odd_years(start_year, end_year)
        if off:
            logger.info("Including off-year (odd) special cycles: %s", off)
        years = sorted(set(years) | set(off))
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
        logger.warning("No election years in [%d, %d] — nothing to do.", start_year, end_year)
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

        # Some race titles redirect to the cycle's overview article (states
        # whose races have no dedicated article).  Parsing the overview here
        # would attribute every election box in it to this state — skip.
        overview_text = overview_map.get(overview_title(year))
        if overview_text and content == overview_text:
            logger.info("  SKIP — '%s' redirects to the %d overview article", title, year)
            state_details[title] = {
                "year": year, "state": state, "error": "redirects_to_overview",
            }
            meta["failed"] += 1
            continue

        try:
            parsed = parse_election_data_universal(content, state, year=year)
            state_details[title] = {
                "year": year,
                "state": state,
                "primary_type": parsed["primary_type"],
                "content_length": len(content),
                "polling_sections_found": parsed.get("polling_sections_found", 0),
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


def _rebuild_combined_from_disk(senate_dir: str, key: str) -> Optional[pd.DataFrame]:
    """
    Rebuild the ``senate_{key}_all.csv`` combined frame from every
    ``senate_{key}_{year}.csv`` present in *senate_dir*.

    A run that processes only the current cycle (e.g. ``--start-year 2026
    --end-year 2026``) must NOT overwrite the combined CSV with just its own
    rows — that used to silently wipe the 1914–2024 history from the _all
    files.  The per-year files are the source of truth: every year ever
    processed by any run leaves a ``_{year}.csv`` behind, so concatenating
    them (sorted by Year) always yields the full-history combined file.
    """
    import glob
    import os

    parts: List[pd.DataFrame] = []
    for path in sorted(glob.glob(os.path.join(senate_dir, f"senate_{key}_*.csv"))):
        name = os.path.basename(path)
        # match the per-year files only ('senate_{key}_1914.csv', ..., not '_all')
        m = re.fullmatch(rf"senate_{key}_(\d{{4}})\.csv", name)
        if not m:
            continue
        try:
            df_year = pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""])
        except Exception:
            logger.warning("Could not read %s for the combined file — skipping", path)
            continue
        if len(df_year):
            parts.append(df_year)
    if not parts:
        return None
    combined = pd.concat(parts, ignore_index=True)
    if "Year" in combined.columns:
        combined = combined.sort_values(
            ["Year"] + [c for c in ("State", "Date") if c in combined.columns],
            kind="stable",
        ).reset_index(drop=True)
    return combined


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
        # Rebuild the combined file from ALL per-year files on disk (not just
        # the years of this run) so single-cycle runs can never clobber the
        # multi-decade history in senate_{key}_all.csv.
        combined = _rebuild_combined_from_disk(senate_dir, key)
        if combined is not None and len(combined):
            path = os.path.join(senate_dir, f"senate_{key}_all.csv")
            combined.to_csv(path, index=False)
            results[key] = combined   # keep in-memory result consistent
            logger.info("Saved: %s (%d rows, rebuilt from per-year files)", path, len(combined))

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
    run_qc: bool = True,
    include_off_years: bool = True,
) -> Dict:
    """
    CLI entry point: process Senate cycles from *start_year* to *end_year*.

    Even (federal) years are always processed, and the odd (off-year)
    special-election cycles in the range are included by default — pass
    ``include_off_years=False`` to restrict the run to even years.

    When *run_qc* is set (default), a polling quality check — file sizes,
    row coverage, value sanity — runs over the written polling CSVs and a
    ``polling_qc_report.json`` is saved next to them.
    """
    logger.info("U.S. Senate cycles %d–%d", start_year, end_year)
    results = process_senate_cycles(
        start_year, end_year, client=client, include_off_years=include_off_years
    )
    save_results(results, output_dir)
    meta = results["metadata"]
    logger.info(
        "Senate done: %d races processed, %d ok, %d failed -> %s/senate",
        meta["total_processed"], meta["successful"], meta["failed"], output_dir,
    )
    if run_qc:
        try:
            import polling_qc
            polling_qc.check_pipeline(output_dir, pipeline="senate")
        except Exception:  # QC must never break the data run
            logger.exception("Polling QC failed (non-fatal)")
    return results


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    run()
