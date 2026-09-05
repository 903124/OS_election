"""
district_counties.py — build the district -> county crosswalk directly from
public boundary geometry (NO Excel workbook involved):

    district polygons : UCLA cdmaps (JeffreyBLewis/congressional-district-boundaries)
                        era GeoJSON files, one snapshot congress per cycle
    county polygons   : 2010 Census cartographic county boundaries (1:5m)

  2000s cycle  = map as of the 112th Congress (2003-2013)
                 (lines as finally used, incl. the TX 2003 mid-decade redraw)
  2010s cycle  = map as of the 113th Congress (2013-2023, first enacted)
  2020s cycle  = map as of the 119th Congress (2023-)
                 (repo carries the final post-2024 court-ordered redraws
                  for AL / GA / LA / NC / NY)

Method (identical ratios to the original cd-county pipeline that produced
the legacy workbook, here writing the mapping JSON directly):

  * both layers projected to EPSG:5070 (Albers Equal Area); because both
    share the projection, local area distortion cancels in the ratios;
  * for every (county, district) intersection piece we keep the piece's
    share of the county's district-covered area (``county_frac``) and of
    the district's own area (``dist_frac``);
  * classification:
      Full    county_frac >= 0.98
      Partial county_frac >= 0.02            ("county-share")
      Partial dist_frac  >= 0.04             ("district-fragment" — catches
              urban districts sitting mostly inside one large county,
              e.g. AZ-09 in Maricopa, CA-34 in Los Angeles)

Output: ``resources/district_counties.json`` — the mapping consumed by
``district_lean.py`` (replaces the legacy
``US_Congressional_Districts_by_County_2000s-2020s.xlsx`` join):

    {"meta": {...provenance, thresholds, event notes, validation...},
     "vintages": {"2000s": {"IN": {"1": {"full": ["Adams County", ...],
                                          "partial": [{"county": "Allen County",
                                                       "share_pct": 4.9,
                                                       "kind": "county-share"}]},
                            ...}, ...},
                  "2010s": {...}, "2020s": {...}}}

Download policy (a local run must never time out): every artifact is
cached on disk, finished files are never re-downloaded, transfers resume
byte-range into a ``.part`` file, retries back off exponentially, and
per-state crosswalks are checkpointed so an interrupted build picks up
where it stopped.  Era files for the three snapshots total ~185 MB.

Usage:
    python cli.py crosswalk                    # full build (resumable)
    python cli.py crosswalk --force            # redo everything
    python district_counties.py --cache-dir .geo_cache
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import time
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("district_counties")

# ----------------------------------------------------------------- sources --
LEWIS_REPO = "JeffreyBLewis/congressional-district-boundaries"
LEWIS_RAW = f"https://raw.githubusercontent.com/{LEWIS_REPO}/master/GeoJson"
GITHUB_API_TREE = (
    f"https://api.github.com/repos/{LEWIS_REPO}/git/trees/master?recursive=1")
COUNTY_URL = ("https://eric.clst.org/assets/wiki/uploads/Stuff/"
              "gz_2010_us_050_00_5m.json")

# ------------------------------------------------------------------ cycles --
#: cycle label -> congress number whose map is the snapshot
CYCLES: Dict[str, int] = {"2000s": 112, "2010s": 113, "2020s": 119}
#: preferred congress per cycle, with defensive fallback chains
CONGRESS_FALLBACKS: Dict[int, List[int]] = {119: [119, 118]}
CYCLE_ORDER: Tuple[str, ...] = ("2000s", "2010s", "2020s")

# ----------------------------------------------------------- classification --
FULL_THRESHOLD = 0.98
PARTIAL_THRESHOLD = 0.02
FRAGMENT_THRESHOLD = 0.04

# ---------------------------------------------------------------- downloads --
DOWNLOAD_WORKERS = 6
HTTP_CONNECT_TIMEOUT = 30
HTTP_MAX_TIME_PER_ATTEMPT = 1800
HTTP_RETRIES = 8

# ------------------------------------------------------------ jurisdictions --
FIPS: Dict[str, Tuple[str, str]] = {
    "01": ("Alabama", "AL"), "02": ("Alaska", "AK"), "04": ("Arizona", "AZ"),
    "05": ("Arkansas", "AR"), "06": ("California", "CA"), "08": ("Colorado", "CO"),
    "09": ("Connecticut", "CT"), "10": ("Delaware", "DE"),
    "11": ("District of Columbia", "DC"), "12": ("Florida", "FL"),
    "13": ("Georgia", "GA"), "15": ("Hawaii", "HI"), "16": ("Idaho", "ID"),
    "17": ("Illinois", "IL"), "18": ("Indiana", "IN"), "19": ("Iowa", "IA"),
    "20": ("Kansas", "KS"), "21": ("Kentucky", "KY"), "22": ("Louisiana", "LA"),
    "23": ("Maine", "ME"), "24": ("Maryland", "MD"), "25": ("Massachusetts", "MA"),
    "26": ("Michigan", "MI"), "27": ("Minnesota", "MN"), "28": ("Mississippi", "MS"),
    "29": ("Missouri", "MO"), "30": ("Montana", "MT"), "31": ("Nebraska", "NE"),
    "32": ("Nevada", "NV"), "33": ("New Hampshire", "NH"), "34": ("New Jersey", "NJ"),
    "35": ("New Mexico", "NM"), "36": ("New York", "NY"), "37": ("North Carolina", "NC"),
    "38": ("North Dakota", "ND"), "39": ("Ohio", "OH"), "40": ("Oklahoma", "OK"),
    "41": ("Oregon", "OR"), "42": ("Pennsylvania", "PA"), "44": ("Rhode Island", "RI"),
    "45": ("South Carolina", "SC"), "46": ("South Dakota", "SD"),
    "47": ("Tennessee", "TN"), "48": ("Texas", "TX"), "49": ("Utah", "UT"),
    "50": ("Vermont", "VT"), "51": ("Virginia", "VA"), "53": ("Washington", "WA"),
    "54": ("West Virginia", "WV"), "55": ("Wisconsin", "WI"), "56": ("Wyoming", "WY"),
}
ABBR_TO_NAME = {v[1]: v[0] for v in FIPS.values()}
NAME_TO_ABBR = {v[0]: v[1] for v in FIPS.values()}
ABBR_TO_FIPS = {v[1]: k for k, v in FIPS.items()}
STATE_ORDER = sorted(FIPS.keys())          # alphabetical by FIPS == by name

#: state names as used in Lewis repo file names
LEWIS_NAME_TO_ABBR = {"District Of Columbia": "DC", **NAME_TO_ABBR}

#: LSAD -> display suffix (Census 2010 cartographic county file)
_LSAD_SUFFIX = {
    "County": "County", "Parish": "Parish", "Borough": "Borough",
    "CA": "Census Area", "city": "City", "Cty&Bor": "City and Borough",
    "Muny": "Municipality", "Muno": "Municipality", "": "",
}
_LSAD_CAPITALISED = {"city": "City"}

# Static notes for events the geometry cannot see (snapshot caveats that
# travel with the mapping so downstream users see them in the metadata).
EVENT_NOTES: Dict[Tuple[str, str], str] = {
    ("TX", "2000s"): "Map reflects the 2003 Tom DeLay mid-decade redraw, in "
                     "force for the 109th-112th Congresses (2006 court-drawn "
                     "interim lines finalised by 2007 are included).",
    ("TX", "2020s"): "2021 legislature-enacted map; reversed the 2000s move "
                     "that had shifted Texarkana out of TX-01.",
    ("AL", "2020s"): "2023 map invalidated by Allen v. Milligan; court-drawn "
                     "additional majority-Black district (AL-02) used from "
                     "the 2024 election.",
    ("GA", "2020s"): "2023 legislature map; court-ordered Black-majority "
                     "districts added from the 2024 election.",
    ("LA", "2020s"): "Callais-era litigation; a second majority-Black "
                     "district (LA-06) drawn for the 2024 election.",
    ("NC", "2020s"): "October 2023 legislature map enacted for the 2024 "
                     "election (NC-01/NC-03 boundaries changed).",
    ("NY", "2020s"): "2023 map enacted by the Independent Redistricting "
                     "Commission path for the 2024 election.",
    ("PA", "2010s"): "Snapshot is the 113th-Congress map; the 2018 Pennsylvania "
                     "Supreme Court redraw used for 115th-117th is NOT shown.",
    ("NC", "2010s"): "Snapshot is the 113th-Congress map; 2016 and 2019 court "
                     "redraws used later in the decade are NOT shown.",
    ("VA", "2010s"): "Snapshot is the 113th-Congress map; the 2020 redistricting "
                     "commission map is NOT shown.",
    ("FL", "2010s"): "Snapshot is the 113th-Congress map; 2015-16 court-ordered "
                     "redraws (Fair Districts) are NOT shown.",
}

_NAME_RE = re.compile(r"^(?P<state>.+)_(?P<a>\d+)_to_(?P<b>\d+)\.geojson$")


# ════════════════════════════════════════════════════════════════════════════
# DOWNLOADS (resumable, cached, parallel)
# ════════════════════════════════════════════════════════════════════════════

def _parses_as_json(path: Path) -> bool:
    raw = path.read_bytes()
    for enc in ("utf-8", "latin-1"):
        try:
            json.loads(raw.decode(enc))
            return True
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    return False


def download_file(url: str, dest: Path, retries: int = HTTP_RETRIES) -> Path:
    """Resumable byte-range download with exponential backoff.

    Atomic ``.part`` rename on success; JSON payloads are validated before
    the cache entry is committed.
    """
    import urllib.request

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(1, retries + 1):
        resume_from = part.stat().st_size if part.exists() else 0
        headers = {"User-Agent": "wiki-elections-pipeline"}
        if resume_from:
            headers["Range"] = f"bytes={resume_from}-"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(
                    req, timeout=HTTP_MAX_TIME_PER_ATTEMPT) as resp:
                mode = "ab" if (resume_from and resp.status == 206) else "wb"
                with open(part, mode) as fh:
                    while True:
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        fh.write(chunk)
            if dest.suffix in (".json", ".geojson") and not _parses_as_json(part):
                logger.warning("  payload unparsable, refetching %s", url)
                part.unlink(missing_ok=True)
                continue
            part.replace(dest)
            return dest
        except Exception as exc:                                # noqa: BLE001
            wait = min(60, 2 ** attempt)
            logger.warning("  %s (attempt %d/%d) for %s — backing off %ds",
                           exc, attempt, retries, url, wait)
            time.sleep(wait)
    raise RuntimeError(f"download failed after {retries} attempts: {url}")


def download_cached(url: str, dest: Path, force: bool = False) -> Path:
    if dest.exists() and not force and dest.stat().st_size > 0:
        if dest.suffix not in (".json", ".geojson") or _parses_as_json(dest):
            logger.info("  cached: %s", dest.name)
            return dest
    logger.info("  GET %s -> %s", url, dest.name)
    return download_file(url, dest)


# ════════════════════════════════════════════════════════════════════════════
# ERA-FILE INVENTORY + SELECTION
# ════════════════════════════════════════════════════════════════════════════

def inventory_via_api() -> List[str]:
    import urllib.request
    req = urllib.request.Request(
        GITHUB_API_TREE, headers={"User-Agent": "wiki-elections-pipeline"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        tree = json.load(resp)
    return [t["path"] for t in tree.get("tree", [])
            if t["path"].startswith("GeoJson/") and t["path"].endswith(".geojson")]


def inventory_via_clone() -> List[str]:
    """Fallback: blob-less, no-checkout clone — lists 900+ files cheaply."""
    logger.info("  GitHub API unavailable — falling back to blob-less clone")
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            ["git", "clone", "--filter=blob:none", "--no-checkout", "--depth",
             "1", f"https://github.com/{LEWIS_REPO}.git", tmp + "/probe"],
            check=True, capture_output=True, timeout=900)
        out = subprocess.run(
            ["git", "-C", tmp + "/probe", "ls-tree", "-r", "--name-only",
             "HEAD"], check=True, capture_output=True, text=True).stdout
    return [l for l in out.splitlines() if l.endswith(".geojson")]


def inventory() -> List[str]:
    try:
        paths = inventory_via_api()
        if paths:
            logger.info("  inventory via GitHub API: %d geojson files", len(paths))
            return paths
    except Exception as exc:                                    # noqa: BLE001
        logger.warning("  GitHub API failed (%s)", exc)
    return inventory_via_clone()


def select_era_files(paths: List[str]) -> Dict[str, Dict[str, dict]]:
    """Manifest: abbr -> cycle -> {file, url, snapshot_congress, ...}.

    For each cycle-snapshot congress, the NARROWEST era file span covering
    it wins (so a file dedicated to that congress beats a mega-era file).
    """
    by_state: Dict[str, List[Tuple[int, int, str]]] = {}
    for p in paths:
        m = _NAME_RE.match(Path(p).name)
        if not m:
            continue
        abbr = LEWIS_NAME_TO_ABBR.get(m.group("state"))
        if abbr is None:
            continue
        by_state.setdefault(abbr, []).append(
            (int(m.group("a")), int(m.group("b")), p))

    manifest: Dict[str, Dict[str, dict]] = {}
    for abbr, spans in sorted(by_state.items()):
        manifest[abbr] = {}
        for cycle, target in CYCLES.items():
            candidates: List[tuple] = []
            for t in CONGRESS_FALLBACKS.get(target, [target]):
                for a, b, p in spans:
                    if a <= t <= b:
                        candidates.append((t, b - a, -a, a, b, p))
                if candidates:
                    break
            if not candidates:
                raise RuntimeError(
                    f"no era file covers congress {target} for {abbr}")
            candidates.sort()
            _t, _span, _na, a, b, p = candidates[0]
            fname = Path(p).name
            manifest[abbr][cycle] = {
                "file": fname,
                "url": f"{LEWIS_RAW}/{fname.replace(' ', '%20')}",
                "startcong": a, "endcong": b, "snapshot_congress": _t,
            }
    return manifest


# ════════════════════════════════════════════════════════════════════════════
# GEOMETRIC CROSSWALK
# ════════════════════════════════════════════════════════════════════════════

def _load_json_loose(path: Path):
    raw = path.read_bytes()
    try:
        return json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return json.loads(raw.decode("latin-1"))


def _save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def _county_display_name(props: dict) -> Optional[str]:
    """Census NAME + LSAD -> the display form used by the mapping.

    * "Fairfax" + "city"   -> "Fairfax City"  (independent cities keep the
      marker so they never collide with their same-named counties)
    * "Fairfax" + "County" -> "Fairfax County"
    * Alaska forms: "Census Area", "City and Borough", "Municipality", "Borough"
    * entities with an empty LSAD (District of Columbia, Carson City) use
      their bare NAME — both are real county equivalents and are kept.
    """
    name = str(props.get("NAME") or "").strip()
    if not name:
        return None
    lsad = str(props.get("LSAD") or "").strip()
    if lsad == "":
        return name
    suffix = _LSAD_SUFFIX.get(lsad, "")
    if not suffix:
        return name
    suffix = _LSAD_CAPITALISED.get(lsad, suffix)
    return f"{name} {suffix}"


def load_counties(county_file: Path):
    """FIPS-filtered county table + projected polygons grouped by state.

    Returns (records, by_state) where records maps geoid -> {name, state,
    census_area} and by_state maps abbr -> [(geoid, projected_polygon)].
    """
    from shapely.geometry import shape
    from shapely.ops import transform as shp_transform
    from pyproj import Transformer

    transformer = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)

    data = _load_json_loose(county_file)
    records: Dict[str, dict] = {}
    by_state: Dict[str, List[tuple]] = defaultdict(list)
    skipped = 0
    for feat in data["features"]:
        props = feat.get("properties", {})
        st = str(props.get("STATE") or "").zfill(2)
        if st not in FIPS:
            continue
        display = _county_display_name(props)
        if display is None:
            skipped += 1
            continue
        geoid = st + str(props.get("COUNTY") or "").zfill(3)
        geom = feat.get("geometry")
        if not geom:
            continue
        poly = shp_transform(transformer.transform, shape(geom))
        records[geoid] = {
            "fips": geoid,
            "name": display,
            "state": FIPS[st][1],
            "census_area": props.get("CENSUSAREA"),
        }
        by_state[FIPS[st][1]].append((geoid, poly))
    logger.info("counties loaded: %d (51 jurisdictions; %d pseudo-counties "
                "skipped)", len(records), skipped)
    return dict(records), dict(by_state)


def load_districts(abbr: str, meta: dict, lewis_dir: Path) -> Dict[int, "object"]:
    """Congress-snapshot districts for one state, merged per district number.

    Features carry their own congress span (startcong/endcong); we keep the
    ones in force at the snapshot congress.  At-large / delegate codes
    (0, >=90) normalise to seat 1.
    """
    from shapely.geometry import shape
    from shapely.ops import transform as shp_transform, unary_union
    from pyproj import Transformer

    transformer = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)
    target = meta["snapshot_congress"]
    data = _load_json_loose(lewis_dir / meta["file"])

    pieces: Dict[int, list] = defaultdict(list)
    for feat in data["features"]:
        props = feat.get("properties", {})
        sc, ec = props.get("startcong"), props.get("endcong")
        if sc is None or ec is None:
            continue
        if not (int(sc) <= target <= int(ec)):
            continue
        geom = feat.get("geometry")
        if not geom:
            continue
        d = int(float(props.get("district") or 0))
        d = 1 if (d == 0 or d >= 90) else d          # 0 = at-large, 98 = DC
        pieces[d].append(shape(geom))

    if not pieces:
        # Some era files label features with a narrower congress span than
        # the file name claims (DC: single delegate feature 115-119 inside
        # "103_to_119").  DC's single seat exists in every congress, so fall
        # back to the file's full feature set rather than dropping the
        # jurisdiction.
        logger.warning("  %s %s: no feature spans congress %d — using file "
                       "features unfiltered", abbr, meta["file"], target)
        for feat in data["features"]:
            geom = feat.get("geometry")
            if not geom:
                continue
            d = int(float(feat["properties"].get("district") or 0))
            d = 1 if (d == 0 or d >= 90) else d
            pieces[d].append(shape(geom))

    return {d: shp_transform(transformer.transform, unary_union(gs))
            for d, gs in sorted(pieces.items())}


def crosswalk_state(abbr: str, manifest: dict, county_list: List[tuple],
                    counties: Dict[str, dict], lewis_dir: Path) -> Dict[str, dict]:
    """{cycle: {geoid: [piece records]}} for one jurisdiction."""
    out: Dict[str, dict] = {}
    for cycle in CYCLE_ORDER:
        districts = load_districts(abbr, manifest[cycle], lewis_dir)
        dnums = list(districts.keys())
        geoms = [districts[d] for d in dnums]
        from shapely.strtree import STRtree
        tree = STRtree(geoms)
        pos_to_d = {i: d for i, d in enumerate(dnums)}
        darea = {d: districts[d].area for d in dnums}

        cycle_out: Dict[str, List[dict]] = {}
        for geoid, cg_proj in county_list:
            cname = counties[geoid]["name"] if geoid in counties else ""
            hits: Dict[int, float] = {}
            for pos in tree.query(cg_proj):
                d = pos_to_d[pos]
                inter = cg_proj.intersection(districts[d])
                if not inter.is_empty and inter.area > 0:
                    hits[d] = inter.area
            if not hits:
                continue
            covered = sum(hits.values())
            recs = []
            for d, a in hits.items():
                recs.append({
                    "district": d,
                    "county": cname,
                    "inter_km2": round(a / 1e6, 4),
                    "county_frac": round(a / covered, 6) if covered else 0.0,
                    "county_frac_raw": round(a / cg_proj.area, 6)
                                       if cg_proj.area else 0.0,
                    "dist_frac": round(a / darea[d], 6) if darea[d] else 0.0,
                })
            recs.sort(key=lambda r: (-r["county_frac"], r["district"]))
            cycle_out[geoid] = recs
        out[cycle] = cycle_out
        logger.info("  %s %s: %d counties x %d districts",
                    abbr, cycle, len(cycle_out), len(dnums))
    return out


# ════════════════════════════════════════════════════════════════════════════
# CLASSIFICATION -> MAPPING JSON
# ════════════════════════════════════════════════════════════════════════════

def classify(cw_by_cycle: Dict[str, dict]) -> Dict[str, Dict[str, dict]]:
    """crosswalk -> {cycle: {district(str): {full: [names], partial: [...]}}}."""
    out: Dict[str, Dict[str, dict]] = {}
    for cycle, counties in cw_by_cycle.items():
        fulls: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
        partials: Dict[str, Dict[str, tuple]] = defaultdict(dict)
        frag: Dict[str, Dict[str, tuple]] = defaultdict(dict)
        for geoid, recs in counties.items():
            for r in recs:
                d = str(r["district"])
                cf, df = r["county_frac"], r["dist_frac"]
                if cf >= FULL_THRESHOLD:
                    fulls[d].append((geoid, r["county"]))
                elif cf >= PARTIAL_THRESHOLD:
                    partials[d][geoid] = (r["county"], cf, df)
                elif df >= FRAGMENT_THRESHOLD:
                    frag[d][geoid] = (r["county"], df, cf)
        listing: Dict[str, dict] = {}
        for d in set(fulls) | set(partials) | set(frag):
            full_names = [n for _g, n in sorted(fulls.get(d, []))]
            parts = []
            for geoid, (n, frac, dfrac) in sorted(partials.get(d, {}).items()):
                parts.append({"county": n,
                              "county_share_pct": round(frac * 100, 1),
                              "district_share_pct": round(dfrac * 100, 1),
                              "kind": "county-share"})
            for geoid, (n, df, cfrac) in sorted(frag.get(d, {}).items()):
                if geoid in partials.get(d, {}):
                    continue
                parts.append({"county": n,
                              "county_share_pct": round(cfrac * 100, 1),
                              "district_share_pct": round(df * 100, 1),
                              "kind": "district-fragment"})
            listing[d] = {"full": full_names, "partial": parts}
        out[cycle] = listing
    return out


def build_mapping(manifest: Dict[str, Dict[str, dict]],
                  per_state: Dict[str, Dict[str, dict]],
                  validation: Optional[dict]) -> dict:
    """Assemble the consolidated mapping JSON (lean-consumable)."""
    vintages: Dict[str, Dict[str, dict]] = {c: {} for c in CYCLE_ORDER}
    seats: Dict[str, Dict[str, int]] = {}
    for abbr, cw in sorted(per_state.items()):
        listing = classify(cw)
        for cycle in CYCLE_ORDER:
            cyc_listing = listing[cycle]
            # at-large jurisdictions: the district IS the whole state, so
            # mirror the legacy workbook's "Entire state (at-large)" — an
            # empty county list — instead of enumerating every county
            if len(cyc_listing) == 1:
                cyc_listing = {next(iter(cyc_listing)): {"full": [], "partial": []}}
            vintages[cycle][abbr] = cyc_listing
        seats[abbr] = {c: len(listing[c]) for c in CYCLE_ORDER}

    meta = {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "jurisdictions": {abbr: {"name": ABBR_TO_NAME.get(abbr, abbr),
                                  "fips": ABBR_TO_FIPS.get(abbr, "")}
                          for abbr in sorted(per_state)},
        "source": {
            "district_boundaries":
                f"UCLA cdmaps — github.com/{LEWIS_REPO} era GeoJSON files",
            "county_boundaries":
                "2010 Census cartographic county boundaries (1:5m), "
                "eric.clst.org mirror of gz_2010_us_050_00_5m.json",
            "projection": "EPSG:5070 (NAD83 / Conus Albers Equal Area); "
                          "both layers treated identically so area ratios "
                          "are projection-consistent",
        },
        "snapshot_congress": {c: CYCLES[c] for c in CYCLE_ORDER},
        "snapshot_note":
            "Cycle snapshots are taken at the END congress of each cycle so "
            "mid-decade court redraws are reflected; the 2020s snapshot "
            "includes the final post-2024 court-ordered redraws for "
            "AL / GA / LA / NC / NY.",
        "thresholds": {
            "full_county_frac": FULL_THRESHOLD,
            "partial_county_frac": PARTIAL_THRESHOLD,
            "partial_district_frac": FRAGMENT_THRESHOLD,
        },
        "classification_note":
            "Full = county_frac >= 98% of the county's district-covered "
            "area; Partial = >= 2% of the county, or >= 4% of the "
            "district's own area (fragment rule for urban districts inside "
            "one large county).",
        "event_notes": {f"{abbr}:{cycle}": note
                        for (abbr, cycle), note in EVENT_NOTES.items()},
        "seats": seats,
        "era_files": {abbr: {c: manifest[abbr][c]["file"] for c in CYCLE_ORDER}
                      for abbr in sorted(manifest)},
        "validation": validation or {},
    }
    return {"meta": meta, "vintages": vintages}


# ════════════════════════════════════════════════════════════════════════════
# VALIDATION
# ════════════════════════════════════════════════════════════════════════════

_KNOWN_FACTS = [
    ("TX seats 32/36/38",
     lambda seats: seats["TX"] == {"2000s": 32, "2010s": 36, "2020s": 38}),
    ("OR seats 5/5/6 (6th seat added after 2020 Census)",
     lambda seats: seats["OR"] == {"2000s": 5, "2010s": 5, "2020s": 6}),
    ("MT at-large until 2023, then MT-01/MT-02",
     lambda seats: seats["MT"] == {"2000s": 1, "2010s": 1, "2020s": 2}),
    ("ID 2 seats in every cycle",
     lambda seats: set(seats["ID"].values()) == {2}),
    ("FL seats 25/27/28",
     lambda seats: seats["FL"] == {"2000s": 25, "2010s": 27, "2020s": 28}),
    ("NY seats 29/27/26",
     lambda seats: seats["NY"] == {"2000s": 29, "2010s": 27, "2020s": 26}),
    ("PA seats 19/18/17",
     lambda seats: seats["PA"] == {"2000s": 19, "2010s": 18, "2020s": 17}),
    ("DC exactly one (non-voting) seat in every cycle",
     lambda seats: all(seats["DC"][c] == 1 for c in CYCLE_ORDER)),
]


def validate_mapping(vintages: Dict[str, Dict[str, dict]],
                     per_state: Optional[Dict[str, dict]] = None,
                     counties: Optional[Dict[str, dict]] = None) -> dict:
    """Seat totals, contiguity, empty lists, coverage, known-facts anchors."""
    results: Dict[str, dict] = {"errors": [], "warnings": [], "checks": {}}

    # A. seat totals == 435 (states only; DC reported separately)
    for cycle in CYCLE_ORDER:
        total = sum(len(vintages[cycle][a]) for a in vintages[cycle] if a != "DC")
        results["checks"][f"seat_total_{cycle}"] = total
        if total != 435:
            results["errors"].append(f"seat total {cycle} = {total} (expected 435)")

    # B. district numbers contiguous 1..N; no empty county lists
    # (single-district jurisdictions are at-large and legitimately empty)
    for cycle in CYCLE_ORDER:
        for abbr, dists in vintages[cycle].items():
            nums = sorted(int(d) for d in dists)
            if nums != list(range(1, len(nums) + 1)):
                results["errors"].append(
                    f"{abbr} {cycle}: district numbers not contiguous 1..N: {nums}")
            if len(nums) > 1:
                for d, entry in dists.items():
                    if not entry["full"] and not entry["partial"]:
                        results["warnings"].append(
                            f"{abbr} {cycle} district {d}: empty county list")

    # C. known-facts anchors
    seats = {a: {c: len(vintages[c][a]) for c in CYCLE_ORDER}
             for a in vintages["2000s"]}
    passed = 0
    for label, check in _KNOWN_FACTS:
        try:
            ok = bool(check(seats))
        except Exception:                                       # noqa: BLE001
            ok = False
        results["checks"][label] = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        else:
            results["errors"].append(f"known fact FAILED: {label}")
    # spot anchors on listing content
    try:
        ok = ("Bowie County" in vintages["2000s"]["TX"]["4"]["full"]
              and "Bowie County" in [p["county"] for p in
                                     vintages["2020s"]["TX"]["1"]["partial"]])
    except Exception:                                           # noqa: BLE001
        ok = False
    results["checks"]["Bowie Co TX: full in TX-04 (2000s), partial in TX-01 (2020s)"] = (
        "PASS" if ok else "FAIL")
    if not ok:
        results["errors"].append("known fact FAILED: Bowie County placement")
    results["checks"]["known_facts_passed"] = f"{passed}/{len(_KNOWN_FACTS)}"

    # D. geometry-level county coverage: every county under >= 1 district
    if per_state is not None and counties is not None:
        for cycle in CYCLE_ORDER:
            claimed: set = set()
            for cw in per_state.values():
                claimed.update(cw.get(cycle, {}).keys())
            missing = sorted(set(counties) - claimed)
            results["checks"][f"county_coverage_{cycle}"] = (
                f"{len(claimed)}/{len(counties)}")
            if missing:
                results["errors"].append(
                    f"{cycle}: {len(missing)} counties intersect no district: "
                    + ", ".join(counties[g]["name"] for g in missing[:12]))
    return results


# ════════════════════════════════════════════════════════════════════════════
# DRIVER
# ════════════════════════════════════════════════════════════════════════════

def _default_cache_dir() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, ".geo_cache")


def _default_output_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "resources", "district_counties.json")


def run(cache_dir: Optional[str] = None, output_path: Optional[str] = None,
        force: bool = False, workers: int = DOWNLOAD_WORKERS) -> dict:
    """Full build: fetch -> crosswalk -> classify -> validate -> mapping JSON.

    Every stage is cached/resumable: era files and the county file are
    cached by URL, per-state crosswalks are checkpointed, and the mapping
    JSON is only rewritten after a successful validation.
    """
    cache = Path(cache_dir or _default_cache_dir())
    lewis_dir = cache / "lewis_files"
    crosswalk_dir = cache / "crosswalk"
    lewis_dir.mkdir(parents=True, exist_ok=True)
    crosswalk_dir.mkdir(parents=True, exist_ok=True)
    out_path = Path(output_path or _default_output_path())
    t0 = time.time()

    # 1. counties ------------------------------------------------------------
    logger.info("[1/4] county boundaries (2010 cartographic 1:5m)")
    county_file = download_cached(COUNTY_URL, cache / "counties_2010_5m.json",
                                  force=force)
    counties, by_state = load_counties(county_file)

    # 2. era-file selection ---------------------------------------------------
    logger.info("[2/4] selecting district era files for snapshot congresses %s",
                {c: CYCLES[c] for c in CYCLE_ORDER})
    manifest_path = cache / "lewis_manifest.json"
    if manifest_path.exists() and not force:
        manifest = _load_json_loose(manifest_path)
        logger.info("  cached manifest: %s", manifest_path.name)
    else:
        manifest = select_era_files(inventory())
        _save_json(manifest_path, manifest)
    uniq: Dict[str, str] = {}
    for abbr, cycles in manifest.items():
        for cycle in CYCLE_ORDER:
            fname = cycles[cycle]["file"]
            uniq.setdefault(fname, cycles[cycle]["url"])

    # 3. downloads -------------------------------------------------------------
    logger.info("[3/4] ensuring %d unique era files (%d workers)",
                len(uniq), workers)
    missing = [(fname, url) for fname, url in sorted(uniq.items())
               if force or not (lewis_dir / fname).exists()
               or (lewis_dir / fname).stat().st_size == 0]
    if missing:
        logger.info("  %d/%d era files need downloading", len(missing), len(uniq))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(download_cached, url, lewis_dir / fname): fname
                    for fname, url in missing}
            for i, fut in enumerate(as_completed(futs), 1):
                fut.result()
                if i % 10 == 0 or i == len(missing):
                    logger.info("  %d/%d era files ready", i, len(missing))
    else:
        logger.info("  all era files cached")

    # 4. per-state crosswalk (checkpointed) ------------------------------------
    logger.info("[4/4] geometric crosswalk, 51 jurisdictions x %d cycles",
                len(CYCLE_ORDER))
    progress_path = crosswalk_dir / "_progress.json"
    progress = _load_json_loose(progress_path) if progress_path.exists() else {}
    per_state: Dict[str, Dict[str, dict]] = {}
    for fips in STATE_ORDER:
        abbr = FIPS[fips][1]
        ckpt = crosswalk_dir / f"{abbr}.json"
        if ckpt.exists() and progress.get(abbr) == "done" and not force:
            per_state[abbr] = _load_json_loose(ckpt)
            continue
        cw = crosswalk_state(abbr, manifest[abbr], by_state[abbr], counties,
                             lewis_dir)
        _save_json(ckpt, cw)
        progress[abbr] = "done"
        _save_json(progress_path, progress)
        per_state[abbr] = cw

    # validate + write ----------------------------------------------------------
    vintages = {c: {} for c in CYCLE_ORDER}
    for abbr, cw in per_state.items():
        listing = classify(cw)
        for cycle in CYCLE_ORDER:
            vintages[cycle][abbr] = listing[cycle]
    validation = validate_mapping(vintages, per_state=per_state,
                                  counties=counties)
    if validation["errors"]:
        for err in validation["errors"]:
            logger.error("VALIDATION: %s", err)
        raise RuntimeError(
            f"crosswalk validation failed with {len(validation['errors'])} "
            "error(s) — mapping JSON not written")

    mapping = build_mapping(manifest, per_state, validation)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _save_json(out_path, mapping)
    logger.info("Mapping JSON -> %s (%.1f KB) in %.1fs", out_path,
                out_path.stat().st_size / 1024, time.time() - t0)
    return mapping


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache-dir", default=None,
                    help="download/checkpoint cache (default: <repo>/.geo_cache)")
    ap.add_argument("--output", default=None,
                    help="mapping JSON path (default: resources/district_counties.json)")
    ap.add_argument("--force", action="store_true",
                    help="ignore caches and redo everything")
    ap.add_argument("--workers", type=int, default=DOWNLOAD_WORKERS)
    args = ap.parse_args()
    run(cache_dir=args.cache_dir, output_path=args.output,
        force=args.force, workers=args.workers)
