"""
district_lean.py — Predicted partisan lean for every U.S. congressional
district, computed by joining two data sources:

  (1) the district -> county crosswalk mapping
      ``resources/district_counties.json``, generated directly from public
      boundary geometry by ``district_counties.py`` (UCLA cdmaps district
      polygons x 2010 Census county polygons, Full/Partial classification;
      NO Excel workbook involved), and

  (2) the county-level presidential results produced by
      ``presidential_elections.py`` (``data/presidential/presidential_results_{year}.csv``).

Method
------
For each presidential election year Y in [start_year, end_year] the pipeline
uses the map vintage that was actually in force:

    2004, 2008, 2012  ->  2000s map (108th-112th Congress, 2003-2013)
    2016, 2020        ->  2010s map (113th-117th Congress, 2013-2023)
    2024              ->  2020s map (118th-119th Congress, 2023-present)

(Nov 2012 elected the 113th Congress but voted under the 2000s map; the
2000s snapshot shows the lines as finally used, including the Texas 2003 /
Georgia 2006-07 mid-decade redraws.)

Every listed county's two-party vote (Democratic vs Republican; all other
parties are summed as "other" and excluded from the lean) is allocated to
the districts that contain it:

  * whole counties   -> 100% of the county's vote to the district;
  * partial counties -> allocated in proportion to each claimant district's
    geometric share of the county (``county_share_pct`` in the mapping;
    whole claimants carry weight 1.0, weights normalised across claimants).
    The legacy equal-split rule amplified boundary slivers — a 2% geometric
    sliver would have carried 50% of a two-claimant county's vote;
  * at-large districts (AK, DE, ND, SD, VT, WY, DC) -> all counties of the
    jurisdiction, i.e. exact statewide aggregation.

Allocation is conservative: each county's votes are distributed across its
claimant districts without loss, so state totals are preserved.

lean_pct = (district Democratic two-party share - national Democratic
two-party share) * 100, reported as ``D+x.x`` / ``R+x.x`` / ``EVEN`` —
the same construction as the Cook Partisan Voting Index, except that the
district vote is estimated from counties rather than counted inside the
district lines. Because split counties are allocated by geometric area
shares (not population), leans for districts containing large shared
urban counties are dampened toward the state mean;
``votes_from_partial_pct`` measures how much of each district's vote came
from that approximation.

The PVI-style summary (``district_pvi_summary.csv``) averages the lean over
the presidential elections held under each vintage
(2000s: 2004+2008+2012 · 2010s: 2016+2020 · 2020s: 2024).

Inputs are read-only: this module does NOT fetch anything unless
``fetch_missing=True``, in which case missing
``presidential_results_{year}.csv`` files are produced on the fly by
calling ``presidential_elections.run`` (rate-limited MediaWiki API).

Output columns (written under *output_dir*, default ``data/district_lean/``):
    year, map_vintage, state, state_code, district, district_note, at_large,
    counties_whole, counties_partial, counties_whole_list,
    counties_partial_list, matched_counties, d_votes, r_votes, other_votes,
    total_votes, d_share_two_party_pct, r_share_two_party_pct,
    national_d_share_two_party_pct, lean_pct, lean_label,
    votes_from_partial_pct

    district_lean_{year}.csv        one row per district in force that year
    district_lean_all.csv           combined across the requested range
    district_pvi_summary.csv        per-district lean averaged per vintage
    district_lean_metadata_{ts}.json  method notes + coverage + unmatched counties

Usage:
    python cli.py lean                                  # 2004-2024 from local CSVs
    python cli.py lean --start-year 2016 --end-year 2024
    python cli.py lean --fetch-missing                  # fetch missing presidential CSVs first
    python cli.py lean --mapping resources/district_counties.json --presidential-dir data/presidential
    python cli.py crosswalk                             # regenerate the mapping JSON from geometry
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import unicodedata
from typing import Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger("district_lean")

MAPPING_NAME = "district_counties.json"

#: map vintage in force at each presidential election (see module docstring)
VINTAGE_FOR_YEAR: Dict[int, str] = {
    2004: "2000s", 2008: "2000s", 2012: "2000s",
    2016: "2010s", 2020: "2010s",
    2024: "2020s",
}
LEAN_YEARS: Tuple[int, ...] = tuple(sorted(VINTAGE_FOR_YEAR))
MAP_VINTAGES: Tuple[str, ...] = ("2000s", "2010s", "2020s")

#: two-party classification (presidential_elections.py already normalised
#: DFL / Democratic-NPL into these labels; be tolerant of dash variants)
DEM_PARTIES = {"democratic", "democratic-farmer-labor", "democratic-npl"}
REP_PARTIES = {"republican"}

# ────────────────────────────────────────────────────────────────────────────
# COUNTY NAME NORMALISATION
# ────────────────────────────────────────────────────────────────────────────

_STRIP_SUFFIXES = ("county", "parish", "borough", "census area", "municipality")


def normalize_county(name: str) -> str:
    """Reduce a county / parish / borough name from either source to a match key.

    * case-folded; punctuation (St., DeKalb, De Witt ...) removed;
    * ``City of X`` (xlsx style) -> ``X City`` — the trailing "city" is KEPT
      so Virginia/Maryland/Missouri independent cities never collide with
      their same-named counties (Fairfax County vs Fairfax City, St. Louis
      County vs St. Louis City, Baltimore County vs Baltimore City);
    * every other county-type suffix ("County", "Parish", "Borough",
      "Census Area", "Municipality") is stripped, because the presidential
      CSVs use bare names for most subdivisions but keep "Borough"/"Census
      Area" (Alaska) and the disambiguating "County"/"City" (Virginia).

    >>> normalize_county("St. Joseph County") == normalize_county("St. Joseph")
    True
    >>> normalize_county("City of Fairfax") == normalize_county("Fairfax City")
    True
    """
    t = (name or "").strip().lower()
    t = unicodedata.normalize("NFKD", t)
    t = "".join(ch for ch in t if not unicodedata.combining(ch))  # ñ -> n
    if t.startswith("city of "):
        t = t[len("city of "):].strip() + " city"
    # strip the trailing county-type suffix (only one ever trails a name),
    # but never the "city" marker
    for suffix in sorted(_STRIP_SUFFIXES, key=len, reverse=True):
        pat = r"[\s\-]+" + re.escape(suffix) + r"$"
        stripped = re.sub(pat, "", t)
        if stripped != t:
            t = stripped
            break
    return re.sub(r"[^a-z0-9]", "", t)


def _key_variants(key: str) -> List[str]:
    """Ordered lookup variants for a county match key: the key itself, then
    with a trailing ``city`` / ``county`` marker stripped (one at a time).
    Used ONLY through the uniqueness-guarded *alias* index built by
    :func:`load_county_counts` — a raw variant hit could silently bind a
    city to its same-named county."""
    out = [key]
    for token in ("city", "county"):
        if key.endswith(token) and len(key) > len(token):
            stripped = key[:-len(token)]
            if stripped not in out:
                out.append(stripped)
    return out


def _party_class(party: str) -> str:
    p = re.sub(r"[–—−]", "-", str(party or "").strip().lower())
    if p in DEM_PARTIES:
        return "D"
    if p in REP_PARTIES:
        return "R"
    return "O"


def lean_label(lean_pct: Optional[float]) -> str:
    """Format a lean in percentage points as ``D+x.x`` / ``R+x.x`` / ``EVEN``."""
    if lean_pct is None or pd.isna(lean_pct):
        return ""
    if abs(lean_pct) < 0.05:
        return "EVEN"
    side = "D" if lean_pct > 0 else "R"
    return f"{side}+{abs(lean_pct):.1f}"


# ────────────────────────────────────────────────────────────────────────────
# DISTRICT -> COUNTY MAPPING (geometric crosswalk JSON)
# ────────────────────────────────────────────────────────────────────────────

#: safety net for mapping files that carry no jurisdiction names
ABBR_FALLBACK_NAMES: Dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana",
    "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
}

def load_district_map(mapping_path: str) -> Tuple[List[dict], dict]:
    """Load ``resources/district_counties.json`` (from district_counties.py)
    into per-district records.

    Returns (districts, meta). Each district record::

        {"state": "Indiana", "state_code": "IN", "district": "IN-02",
         "district_num": "02", "district_note": "",
         "vintages": {"2000s": {"exists": True, "at_large": False,
                                "counties": [("Fulton County", "fulton", False), ...]},
                      "2010s": {...}, "2020s": {...}}}

    ``counties`` entries are (raw_display_name, match_key, is_partial);
    jurisdictions with a single seat in a vintage are flagged ``at_large``
    and carry an empty county list (the lean consumes the statewide sum).
    """
    with open(mapping_path, encoding="utf-8") as fh:
        raw = json.load(fh)
    vintages_raw: Dict[str, Dict[str, dict]] = raw["vintages"]
    src_meta: dict = raw.get("meta", {}) or {}

    meta: dict = {
        "path": str(mapping_path),
        "generated": src_meta.get("generated", ""),
        "source": src_meta.get("source", {}),
        "snapshot_congress": src_meta.get("snapshot_congress", {}),
        "snapshot_note": src_meta.get("snapshot_note", ""),
        "classification_note": src_meta.get("classification_note", ""),
        "thresholds": src_meta.get("thresholds", {}),
        "jurisdictions": src_meta.get("jurisdictions", {}),
        "seats": src_meta.get("seats", {}),
        "event_notes": src_meta.get("event_notes", {}),
        "validation": src_meta.get("validation", {}),
        "states_total": len(vintages_raw.get(MAP_VINTAGES[0], {})),
        "duplicate_labels": [],
        "unrecognised_labels": [],
    }

    districts_by_label: Dict[Tuple[str, str], dict] = {}
    states = sorted({abbr for cyc in vintages_raw.values() for abbr in cyc})
    for abbr in states:
        state_name = (meta["jurisdictions"].get(abbr, {})
                      .get("name") or ABBR_FALLBACK_NAMES.get(abbr, abbr))
        for vintage in MAP_VINTAGES:
            state_map = vintages_raw.get(vintage, {}).get(abbr, {})
            # a jurisdiction with a single seat in a vintage is at-large in
            # that vintage (labelled "XX-AL", consuming the statewide sum);
            # MT is the real case: at-large 2000s/2010s, MT-01/MT-02 in 2020s
            solo = len(state_map) == 1
            for dnum in state_map:
                label = f"{abbr}-AL" if solo else f"{abbr}-{int(dnum):02d}"
                rec = districts_by_label.setdefault(
                    (abbr, label),
                    {"state": state_name, "state_code": abbr,
                     "district": label, "label_full": label,
                     "district_num": "00" if solo else f"{int(dnum):02d}",
                     "district_note": "", "vintages": {}})
                entry = state_map[dnum]
                counties: List[Tuple[str, str, bool, float]] = []
                if not solo:
                    for name in entry.get("full", []):
                        key = normalize_county(name)
                        if key:
                            counties.append((name, key, False, 1.0))
                    for part in entry.get("partial", []):
                        key = normalize_county(part["county"])
                        if key:
                            # geometric share of the county's area inside this
                            # district; used to weight vote allocation
                            counties.append(
                                (part["county"], key, True,
                                 float(part.get("county_share_pct", 100.0)) / 100.0))
                rec["vintages"][vintage] = {
                    "exists": True,
                    "at_large": solo,
                    "counties": counties,
                }

    # vintages where a district did not exist
    for rec in districts_by_label.values():
        for vintage in MAP_VINTAGES:
            rec["vintages"].setdefault(
                vintage, {"exists": False, "at_large": False, "counties": []})

    districts = sorted(districts_by_label.values(),
                       key=lambda d: (d["state_code"], int(d["district_num"])))
    meta["districts_total"] = len(districts)
    if not districts:
        raise ValueError(f"No district rows found in {mapping_path!r}")
    return districts, meta


# ────────────────────────────────────────────────────────────────────────────
# PRESIDENTIAL COUNTY VOTES
# ────────────────────────────────────────────────────────────────────────────

def load_county_counts(pres_dir: str, year: int):
    """Aggregate one year's county-level presidential CSV into two-party totals.

    Returns (counts, national, n_rows, alias) where

    * ``counts`` maps ``(state_code, county_key)`` ->
      ``{"D": v, "R": v, "O": v}`` (votes; all third parties in "O"),
    * ``national`` = the same aggregation over the whole file,
    * ``alias`` maps ``(state_code, variant_key)`` -> ``county_key`` for
      UNAMBIGUOUS shortened forms. The article series writes independent
      cities bare in some eras ("Portsmouth") while the crosswalk always
      says "Portsmouth City"; the alias lets ``Portsmouth City`` fall back
      to ``Portsmouth`` — but only when exactly one CSV subdivision shares
      that shortened key, so "Fairfax City" can never be bound to Fairfax
      County (or vice versa) in years where the suffixes are missing.
    """
    path = os.path.join(pres_dir, f"presidential_results_{year}.csv")
    if not os.path.exists(path):
        return None, None, 0, {}
    df = pd.read_csv(path, dtype={"state_code": str, "county": str, "party": str})
    df["votes"] = pd.to_numeric(df["votes"], errors="coerce").fillna(0)
    df["pc"] = df["party"].map(_party_class)
    df["ckey"] = df["county"].map(normalize_county)

    def _agg(group_frame: pd.DataFrame) -> Dict:
        out: Dict = {}
        for (sc, ckey, pc), v in group_frame.groupby(
                ["state_code", "ckey", "pc"], observed=True)["votes"].sum().items():
            out.setdefault((sc, ckey), {"D": 0.0, "R": 0.0, "O": 0.0})[pc] = float(v)
        return out

    counts = _agg(df)
    national = {"D": 0.0, "R": 0.0, "O": 0.0}
    for pc, v in df.groupby("pc")["votes"].sum().items():
        national[pc] = float(v)

    # uniqueness-guarded bare-form alias (see docstring)
    variants_of: Dict[Tuple[str, str], set] = {}
    for (sc, ckey) in counts:
        for v_key in _key_variants(ckey):
            variants_of.setdefault((sc, v_key), set()).add(ckey)
    alias: Dict[Tuple[str, str], str] = {
        k: next(iter(v)) for k, v in variants_of.items() if len(v) == 1
    }
    return counts, national, int(len(df)), alias


# ────────────────────────────────────────────────────────────────────────────
# LEAN COMPUTATION
# ────────────────────────────────────────────────────────────────────────────

_ROW_FIELDS = [
    "year", "map_vintage", "state", "state_code", "district", "district_note",
    "at_large", "counties_whole", "counties_partial", "counties_whole_list",
    "counties_partial_list", "matched_counties", "d_votes", "r_votes",
    "other_votes", "total_votes", "d_share_two_party_pct",
    "r_share_two_party_pct", "national_d_share_two_party_pct", "lean_pct",
    "lean_label", "votes_from_partial_pct",
]


def compute_year_lean(
    districts: List[dict],
    counts: Dict[Tuple[str, str], Dict[str, float]],
    national: Dict[str, float],
    year: int,
    alias: Optional[Dict[Tuple[str, str], str]] = None,
) -> Tuple[pd.DataFrame, dict]:
    """Allocate county votes to districts for one presidential year.

    Returns (rows_frame, info) — info carries unmatched county names and
    per-state allocation coverage for the metadata JSON.
    """
    alias = alias or {}

    def _resolve(state_code: str, key: str):
        """Exact match first, then uniqueness-guarded bare-form fallback.
        Returns the CSV-side county key, or None when unmatched."""
        if (state_code, key) in counts:
            return key
        for v_key in _key_variants(key):
            mapped = alias.get((state_code, v_key))
            if mapped is not None and (state_code, mapped) in counts:
                return mapped
        return None

    vintage = VINTAGE_FOR_YEAR[year]
    nat_d = national.get("D", 0.0)
    nat_r = national.get("R", 0.0)
    nat_tp = nat_d + nat_r
    nat_d_share = (nat_d / nat_tp * 100.0) if nat_tp else float("nan")

    by_state: Dict[str, List[dict]] = {}
    for d in districts:
        if d["vintages"][vintage]["exists"]:
            by_state.setdefault(d["state_code"], []).append(d)

    rows: List[dict] = []
    info: dict = {"year": year, "vintage": vintage, "unmatched": {},
                  "unclaimed": {}, "multi_whole": [], "states": {}}

    for state_code, dists in sorted(by_state.items()):
        # ── resolve every mapping county entry to a CSV-side key ─────────
        # (the same real-world county can be written "Bedford City" in one
        # row and "Bedford County" in another; both must collapse onto the
        # single CSV subdivision so votes are neither split nor doubled)
        resolved: Dict[str, Optional[str]] = {}
        for d in dists:
            for _raw, ckey, _partial, _w in d["vintages"][vintage]["counties"]:
                if ckey not in resolved:
                    resolved[ckey] = _resolve(state_code, ckey)

        # ── county -> claimant index for this state/vintage (CSV-key space) ──
        # whole claimants carry weight 1.0 (the full county), partial
        # claimants the geometric county-area share recorded in the mapping
        claim: Dict[str, Dict[str, list]] = {}
        for d in dists:
            vinfo = d["vintages"][vintage]
            if vinfo["at_large"]:
                continue
            for _raw, ckey, partial, area_w in vinfo["counties"]:
                rkey = resolved[ckey]
                if rkey is None:
                    continue
                slot = claim.setdefault(rkey, {"whole": [], "partial": []})
                side = "partial" if partial else "whole"
                if all(x[0]["district"] != d["district"] for x in slot[side]):
                    slot[side].append((d, area_w))

        # counties present in the vote data but claimed by nobody (at-large
        # states consume every county via the statewide sum, so they can
        # never have leftovers)
        state_keys = {ck for (sc, ck) in counts if sc == state_code}
        if any(d["vintages"][vintage]["at_large"] for d in dists):
            unclaimed: List[str] = []
        else:
            unclaimed = sorted(state_keys - set(claim))
        if unclaimed:
            info["unclaimed"][state_code] = unclaimed

        # ── per-district allocation ───────────────────────────────────────
        state_available = sum(
            sum(v.values()) for (sc, _ck), v in counts.items() if sc == state_code)
        state_allocated = 0.0
        unmatched_state: List[str] = []

        def _county_weight(rkey: str, district_label: str) -> float:
            """This district's share of the county's votes: its geometric
            area weight normalised across all claimants (whole = 1.0).
            Falls back to an equal split if weights are unavailable."""
            slot = claim[rkey]
            entries = slot["whole"] + slot["partial"]
            if not entries:
                return 0.0
            sum_w = sum(w for _d, w in entries)
            if sum_w <= 0:
                return 1.0 / len(entries)
            for d_rec, w in entries:
                if d_rec["district"] == district_label:
                    return w / sum_w
            return 0.0

        for d in dists:
            vinfo = d["vintages"][vintage]
            counties = vinfo["counties"]
            whole_names, partial_names = [], []
            d_votes = r_votes = o_votes = partial_votes = 0.0
            matched = 0

            if vinfo["at_large"]:
                for (sc, ckey), v in counts.items():
                    if sc != state_code:
                        continue
                    d_votes += v["D"]; r_votes += v["R"]; o_votes += v["O"]
                    matched += 1
                counties_whole_n = counties_partial_n = 0
                whole_list = partial_list = ""
            else:
                used: set = set()          # CSV keys already consumed by THIS district
                for raw, ckey, is_partial, area_w in counties:
                    rkey = resolved[ckey]
                    if rkey is None:
                        unmatched_state.append(raw)
                        continue
                    if rkey in used:       # city+county spellings of one unit
                        continue
                    used.add(rkey)
                    v = counts[(state_code, rkey)]
                    tv = v["D"] + v["R"] + v["O"]
                    slot = claim[rkey]
                    weight = _county_weight(rkey, d["district"])
                    if is_partial:
                        partial_names.append(raw)
                        partial_votes += weight * tv
                    else:
                        if len(slot["whole"]) > 1:
                            info["multi_whole"].append(
                                {"year": year, "state": state_code, "county": raw,
                                 "districts": [x[0]["district"] for x in slot["whole"]]})
                        whole_names.append(raw)
                    d_votes += weight * v["D"]
                    r_votes += weight * v["R"]
                    o_votes += weight * v["O"]
                    matched += 1
                counties_whole_n = len(whole_names)
                counties_partial_n = len(partial_names)
                whole_list = "; ".join(whole_names)
                partial_list = "; ".join(partial_names)

            total = d_votes + r_votes + o_votes
            state_allocated += total
            tp = d_votes + r_votes
            d_share = (d_votes / tp * 100.0) if tp else float("nan")
            r_share = (r_votes / tp * 100.0) if tp else float("nan")
            lean = d_share - nat_d_share if tp else float("nan")
            rows.append({
                "year": year,
                "map_vintage": vintage,
                "state": d["state"],
                "state_code": state_code,
                "district": d["district"],
                "district_note": d["district_note"],
                "at_large": vinfo["at_large"],
                "counties_whole": counties_whole_n,
                "counties_partial": counties_partial_n,
                "counties_whole_list": whole_list,
                "counties_partial_list": partial_list,
                "matched_counties": matched,
                "d_votes": round(d_votes),
                "r_votes": round(r_votes),
                "other_votes": round(o_votes),
                "total_votes": round(total),
                "d_share_two_party_pct": round(d_share, 2) if tp else None,
                "r_share_two_party_pct": round(r_share, 2) if tp else None,
                "national_d_share_two_party_pct": round(nat_d_share, 2),
                "lean_pct": round(lean, 2) if tp else None,
                "lean_label": lean_label(lean) if tp else "",
                "votes_from_partial_pct": round(partial_votes / total * 100.0, 1) if total else None,
            })

        if unmatched_state:
            info["unmatched"][state_code] = sorted(set(unmatched_state))
        if state_available:
            info["states"][state_code] = {
                "allocated_vs_available_pct": round(
                    state_allocated / state_available * 100.0, 2),
                "districts": len(dists),
            }

    frame = pd.DataFrame(rows, columns=_ROW_FIELDS)
    info["rows"] = int(len(frame))
    info["national"] = {
        "d_votes": round(nat_d), "r_votes": round(nat_r),
        "d_share_two_party_pct": round(nat_d_share, 2),
    }
    return frame, info


def summarize_pvi(all_frame: pd.DataFrame) -> pd.DataFrame:
    """Average the per-year lean over the elections held under each map
    vintage (PVI-style: mean district two-party margin minus mean national
    margin — identical to averaging the yearly leans)."""
    usable = all_frame.dropna(subset=["lean_pct"])
    if not len(usable):
        return pd.DataFrame()
    grp = usable.groupby(
        ["state", "state_code", "district", "district_note", "at_large",
         "map_vintage"], as_index=False).agg(
        years_used=("year", lambda s: "+".join(str(y) for y in sorted(s))),
        n_years=("year", "nunique"),
        lean_pct_mean=("lean_pct", "mean"),
        d_share_mean=("d_share_two_party_pct", "mean"),
        national_d_share_mean=("national_d_share_two_party_pct", "mean"),
        votes_from_partial_pct_max=("votes_from_partial_pct", "max"),
        counties_whole=("counties_whole", "first"),
        counties_partial=("counties_partial", "first"),
        total_votes_mean=("total_votes", "mean"),
    )
    grp["lean_pct_mean"] = grp["lean_pct_mean"].round(2)
    grp["d_share_mean"] = grp["d_share_mean"].round(2)
    grp["national_d_share_mean"] = grp["national_d_share_mean"].round(2)
    grp["total_votes_mean"] = grp["total_votes_mean"].round(0).astype("Int64")
    grp["pvi_label"] = grp["lean_pct_mean"].map(lean_label)
    grp["is_current_map"] = grp["map_vintage"] == "2020s"
    grp = grp.rename(columns={"lean_pct_mean": "pvi_pct"})
    grp = grp.sort_values(["state_code", "district", "map_vintage"])
    cols = ["state", "state_code", "district", "district_note", "at_large",
            "map_vintage", "is_current_map", "years_used", "n_years",
            "d_share_mean", "national_d_share_mean", "pvi_pct", "pvi_label",
            "total_votes_mean", "counties_whole", "counties_partial",
            "votes_from_partial_pct_max"]
    return grp[cols].reset_index(drop=True)


# ────────────────────────────────────────────────────────────────────────────
# DRIVER
# ────────────────────────────────────────────────────────────────────────────

def _resolve_mapping_path(mapping_path: Optional[str]) -> str:
    if mapping_path:
        if not os.path.exists(mapping_path):
            raise FileNotFoundError(f"district->county mapping not found: {mapping_path}")
        return mapping_path
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join("resources", MAPPING_NAME),
                 os.path.join(here, "resources", MAPPING_NAME),
                 os.path.join(here, MAPPING_NAME)):
        if os.path.exists(cand):
            return cand
    raise FileNotFoundError(
        f"district->county mapping not found (looked for resources/{MAPPING_NAME!r} "
        f"relative to CWD and to the module) — pass --mapping explicitly or run "
        f"`cli.py crosswalk` to build it from boundary geometry")


def run(
    start_year: int = 2004,
    end_year: int = 2024,
    output_dir: str = "data",
    mapping_path: Optional[str] = None,
    presidential_dir: Optional[str] = None,
    client=None,
    fetch_missing: bool = False,
) -> pd.DataFrame:
    """Compute predicted district partisan leans for the presidential years
    in [start_year, end_year], writing CSVs under
    ``<output_dir>/district_lean/``.

    Years without a local ``presidential_results_{year}.csv`` are skipped
    (with a warning) unless *fetch_missing* is True, in which case
    ``presidential_elections.run`` produces them first via the API.
    """
    years = [y for y in LEAN_YEARS if start_year <= y <= end_year]
    if not years:
        logger.warning(
            "No presidential year with a known map vintage in [%d, %d] — "
            "supported years: %s", start_year, end_year, list(LEAN_YEARS))
        return pd.DataFrame()

    mapping_file = _resolve_mapping_path(mapping_path)
    pres_dir = presidential_dir or os.path.join(output_dir, "presidential")
    lean_dir = os.path.join(output_dir, "district_lean")
    os.makedirs(lean_dir, exist_ok=True)

    logger.info("District->county mapping: %s", mapping_file)
    districts, map_meta = load_district_map(mapping_file)
    n_at_large = sum(1 for d in districts
                     if all(v["at_large"] or not v["exists"]
                            for v in d["vintages"].values()))
    logger.info(
        "Loaded %d districts (%d states incl. DC; %d at-large) across vintages %s",
        len(districts), map_meta["states_total"], n_at_large, ", ".join(MAP_VINTAGES))

    all_frames: List[pd.DataFrame] = []
    meta: dict = {
        "method": {
            "lean": "district Democratic two-party share minus national "
                    "Democratic two-party share (Cook-PVI construction)",
            "allocation": "whole counties: full weight; partial counties: "
                          "allocated by geometric county-area shares "
                          "(county_share_pct) normalised across claimant "
                          "districts; at-large: entire jurisdiction",
            "map_vintage_for_year": {str(k): v for k, v in VINTAGE_FOR_YEAR.items()},
            "note_2012": "Nov 2012 voted under the 2000s map (108th-112th "
                         "Congress lines, as finally used incl. TX 2003 / GA 2006)",
            "caveat": "partial counties are area-weighted, not population-"
                      "weighted; leans for districts containing large shared "
                      "counties are dampened toward the state mean — see "
                      "votes_from_partial_pct; the 2010s crosswalk snapshot "
                      "shows the map as first enacted (FL 2015 / NC 2016 & "
                      "2019 / PA 2018 court redraws are not reflected); "
                      "Alaska has no borough-level table before 2024, so "
                      "AK-AL is computed for 2024 only",
        },
        "mapping": {k: v for k, v in map_meta.items() if k != "path"},
        "years": {},
    }

    for year in years:
        counts, national, n_rows, alias = load_county_counts(pres_dir, year)
        if counts is None:
            if fetch_missing and client is not None:
                import presidential_elections
                logger.info("%d — presidential CSV missing, fetching…", year)
                presidential_elections.run(start_year=year, end_year=year,
                                           output_dir=output_dir, client=client)
                counts, national, n_rows, alias = load_county_counts(pres_dir, year)
            else:
                logger.warning(
                    "%d — %s not found; skipping (run `cli.py presidential` "
                    "first or pass --fetch-missing)", year, os.path.join(
                        pres_dir, f"presidential_results_{year}.csv"))
                meta["years"][year] = {"skipped": "presidential CSV missing"}
                continue
        if not counts:
            logger.warning("%d — presidential CSV present but empty; skipped", year)
            meta["years"][year] = {"skipped": "presidential CSV empty"}
            continue

        frame, info = compute_year_lean(districts, counts, national, year, alias)
        path = os.path.join(lean_dir, f"district_lean_{year}.csv")
        frame.to_csv(path, index=False)
        logger.info(
            "  %d [%s map] %d districts, national D two-party %.2f%% -> %s",
            year, VINTAGE_FOR_YEAR[year], len(frame),
            info["national"]["d_share_two_party_pct"], path)
        meta["years"][year] = {
            "vintage": VINTAGE_FOR_YEAR[year],
            "presidential_rows": n_rows,
            "states_present": len({sc for (sc, _ck) in counts}),
            "rows": info["rows"],
            "national": info["national"],
            "allocation_coverage": info["states"],
            "unmatched_counties": info["unmatched"] or None,
            "unclaimed_counties": info["unclaimed"] or None,
            "multi_whole_counties": info["multi_whole"] or None,
        }
        all_frames.append(frame)

    if not all_frames:
        logger.warning("No lean rows computed — nothing written.")
        return pd.DataFrame()

    combined = pd.concat(all_frames, ignore_index=True)
    path = os.path.join(lean_dir, "district_lean_all.csv")
    combined.to_csv(path, index=False)
    logger.info("Combined -> %s (%s rows)", path, f"{len(combined):,}")

    pvi = summarize_pvi(combined)
    if len(pvi):
        path = os.path.join(lean_dir, "district_pvi_summary.csv")
        pvi.to_csv(path, index=False)
        logger.info("PVI summary -> %s (%s rows)", path, f"{len(pvi):,}")

    ts = time.strftime("%Y%m%d_%H%M%S")
    with open(os.path.join(lean_dir, f"district_lean_metadata_{ts}.json"), "w",
              encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, default=str)
    return combined


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    run()
