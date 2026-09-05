# wiki-elections-pipeline

Automated extraction of U.S. election results from Wikipedia into tidy,
analysis-ready CSV datasets. Six pipelines cover federal, state, and
presidential races; a companion pipeline derives a Cook-PVI-style partisan
lean for every congressional district. All data is fetched through the
official **MediaWiki Action API** in compliance with Wikimedia's rate-limit
and etiquette policy.

## Pipelines

| Command | Scope | Output directory | Validated coverage |
|---|---|---|---|
| `senate` | U.S. Senate primaries and general elections — polling, results, incumbents; regular and special races discovered automatically | `data/senate/` | 1914–2024 |
| `house` | U.S. House general-election results by district | `data/house/` | 1910–2024 |
| `state-leg` | State senates and state houses/assemblies | `data/state_senate/`, `data/state_house/` | 2000–2024 |
| `statewide` | Governor, Attorney General, Secretary of State, State Treasurer | `data/statewide/` | 1980–2024 |
| `presidential` | Presidential results by county, parish, or ward for all 50 states + D.C. | `data/presidential/` | presidential years (2004–2024 verified) |
| `lean` | Predicted partisan lean per congressional district | `data/district_lean/` | 2004–2024 |
| `crosswalk` | Regenerate the district→county mapping from public boundary geometry | `resources/district_counties.json` | — |

## How it works

### Data sources and discovery

Each pipeline reads live Wikipedia articles and discovers its targets
dynamically — no hardcoded state or race lists:

- **Senate** — race titles are read from the
  `{year} United States Senate elections` overview article via its per-state
  `{{main|...}}` links, so regular and special elections (e.g. Minnesota and
  Mississippi 2018, Arizona 2020, Oklahoma 2022) are captured automatically.
  The polling parser supports both the 2018-era table generation
  (`align=center|` cells) and the 2020+ generation (attribute headers,
  `sortable` tables with colspan group headers, `{{efn}}` notes).
- **House** — results are read from each
  `{year} United States House of Representatives elections` overview article.
  The parser handles every article generation from 1910 to 2024; see
  [Data quality and validation](#data-quality-and-validation).
- **State legislatures** — chamber articles are discovered from the
  `{year} United States state legislative elections` overview and its
  per-state links. Election-box templates are the primary source; a
  secondary parser covers summary-table layouts (Minnesota-style
  `District / Candidates / Votes / %` tables, Pennsylvania sortable tables,
  Oklahoma-style `{{Plainlist}}` candidate cells).
- **Statewide executive** — summary tables from the governor, Attorney
  General, Secretary of State, and State Treasurer overview articles for
  each cycle.
- **Presidential** — one article per jurisdiction per presidential year:
  `{year} United States presidential election in {State}`, covering all 50
  states, D.C., and every jurisdiction's subdivision table variant (county /
  parish / independent city / borough and census area / ward). A full
  51-jurisdiction cycle costs roughly two batched API requests.
- **Partisan lean** — computed offline at runtime by joining the bundled
  district→county mapping with the presidential CSVs produced by the
  `presidential` pipeline; see
  [Partisan lean methodology](#partisan-lean-methodology).


## Requirements and installation

Python 3.10+ with:

```bash
pip install -r requirements.txt
```

`shapely` and `pyproj` are required only for `crosswalk` (the mapping JSON
shipped in `resources/` is sufficient for all other pipelines, including
`lean`).

## Configuration

All configuration is optional and read from environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `WIKI_API_URL` | `https://en.wikipedia.org/w/api.php` | API endpoint (point at another wiki if needed) |
| `WIKI_USER_AGENT` | pipeline default | Descriptive, contact-bearing User-Agent — e.g. `my-pipeline/1.0 (https://github.com/me/repo; me@example.com)` |
| `WIKI_REQUEST_DELAY` | `1.0` | Minimum seconds between requests |
| `WIKI_BATCH_SIZE` | `50` | Titles per request (≤ 50) |
| `WIKI_MAX_RETRIES` | `5` | Retry attempts for transient errors |
| `WIKI_TIMEOUT` | `60` | Per-request timeout (s) |

## Usage

```bash
python src/cli.py all                                            # every pipeline, 2018–2024
python src/cli.py senate --start-year 2018 --end-year 2024       # federal Senate cycles
python src/cli.py house --start-year 2012 --end-year 2024        # federal House results
python src/cli.py state-leg --start-year 2018 --end-year 2024    # state senates + houses
python src/cli.py statewide --start-year 2018 --end-year 2024    # gov/AG/SoS/treasurer
python src/cli.py presidential --start-year 2020 --end-year 2024 # county-level presidential
python src/cli.py lean                                           # district lean, 2004–2024
python src/cli.py lean --fetch-missing                           # fetch missing presidential years first
python src/cli.py crosswalk                                      # rebuild the district→county mapping
python src/cli.py all --start-year 2020 --end-year 2023          # odd bounds clamp inward → 2020, 2022
python src/cli.py fetch "2018 United States Senate election in Arizona"  # debug: dump raw wikitext
```

Year ranges are **inclusive** and process **even (federal election) years
only**; odd bounds are clamped inward (`2019–2023` processes `2020` and
`2022`). The `presidential` pipeline further restricts the range to
**presidential years (divisible by 4)**, so `--start-year 2018 --end-year
2024` processes 2020 and 2024. The `lean` pipeline always computes the full
2004–2024 range because its three map vintages span that period. Every
subcommand accepts `--output` for a custom base directory and the rate-limit
flags `--delay`, `--batch-size`, `--max-retries`, and `--timeout`.

Each module also runs standalone (e.g. `python src/senate_elections.py`),
which is convenient for development.

## Output data dictionary

Each pipeline writes per-year CSVs plus a combined `_all` file across the
requested range, together with a timestamped metadata JSON recording
coverage and any gaps. All rows carry a `Year` column.

## Data quality and validation

All pipelines were exercised over their full available year ranges with
outputs compared against official records:

| Pipeline | Range tested | Result |
|---|---|---|
| house | 1910–2024 | 53 of 58 even years reproduce the official seat and party compositions exactly; the remaining 5 years reflect coverage gaps in the overview articles themselves, not parser errors |
| senate | 1914–2024 | 1,933 race articles discovered (~34.5 per cycle) with zero parse failures; landmark winners verified across decades (Wagner 1932, LBJ 1948, JFK 1952, RFK 1964, Feinstein 1992, Obama 2004, McCain 2016) |
| statewide | 1980–2024 | Governor overviews exist from 1980, AG from 2016, SoS/treasurer from 2020; "Race summary" tables appear from 2000; earlier cycles are skipped and logged |
| state-leg | 2000–2024 | 13 cycles, ~1,000 chamber articles, 71K rows; district coverage ≥ 99.8%; per-candidate coverage grows from 5 states (2000) to 40 (2020) as Wikipedia coverage expands |
| presidential | 2004–2024 | Per-candidate county sums match each state's own Totals row in 326 of 349 parsed state-years; deviations are article-level inconsistencies, and the CSV stays faithful to the per-county table |
| lean | 2004–2024 | National two-party baseline reproduces official shares to within 0.03 pp in every computed year (2016: 51.13% vs 51.11% official; 2020: 52.29% vs 52.27%) |

### House article generations

The House parser handles every layout generation between 1910 and 2024:

| Era | Layout |
|---|---|
| 1910–1916 | `{{USCongressElectionTableHead}}` tables with header-cell districts and `{{Party stripe}}` candidate bullets |
| ~1912–1956 | `{{ushr|State|N|X}}` district codes, `{{Plainlist}}` candidate blocks, "Uncontested" markers |
| 1946–1992 | state sections without main links, `{{nowrap}}` wrappers, bold-only winner markers |
| 1970s | pipe-cell district rows, `{{party shortname}}` labels, whole-span bold winners |
| 2018–2020 | `{{ushr|XX|N|X}}` codes + `{{Plainlist}}` + `{{Sortname}}` |
| 2022–2024 | `{{plainlist}}…{{endplainlist}}`, bundled non-voting delegate tables |

Additional quirks handled: multi-seat at-large general tickets (Illinois, New
York, Pennsylvania in the 1920s), Maine's ranked-choice rounds, uncontested
seats without percentages (recorded as 100% winners), pre-1954 winner shares
written without a `%` sign, and election-box rows embedding
`{{decrease}}`-style change templates. Races whose titles redirect to the
cycle overview (~25% of pre-1930 races) are skipped rather than
mis-attributed, and each skip is recorded in the metadata JSON.

### Parser robustness

The table parsers tolerate the full range of MediaWiki markup found in real
articles: cell attributes in any order and quoting style, rowspan
inheritance across continuation rows, winner markers via bold /
`{{party shading}}` / CSS-bold, `<ref>` and party-label cells between the
winner and the vote counts, and two-row headers that would otherwise leak in
as candidate rows. Presidential tables are matched through an HTML-style
rowspan/colspan grid that tolerates linked headers, negative margins,
`{{Update}}` banners, and primary tables sharing a heading with the
general-election table. Fixed parser shapes are locked in by smoke-test
fixtures.

## Partisan lean methodology

For every congressional district and presidential year, the `lean` pipeline
computes a Cook-PVI-style measure:

```
lean = (district Democratic two-party share − national Democratic
        two-party share) × 100          →  "D+x.x" / "R+x.x" / "EVEN"
```

County votes are allocated to districts using the bundled mapping:

- **Whole counties** count at full weight.
- **Partial counties** are allocated in proportion to each claimant
  district's geometric share of the county (`county_share_pct`, normalised
  across claimants). Allocation is conservative — each county's votes are
  distributed across its claimant districts without loss, and the metadata
  records per-state allocation coverage plus any unmatched county names.
- **At-large districts** (AK, DE, ND, SD, VT, WY, D.C.) aggregate their whole
  jurisdiction exactly.
- Third-party votes are summed as "other" and excluded from the two-party
  baseline.

`district_pvi_summary.csv` additionally averages each district's margin over
the elections held under each map vintage (2000s map: 2004 + 2008 + 2012 ·
2010s map: 2016 + 2020 · 2020s map: 2024), producing a Cook-PVI-style
summary per map era with an `is_current_map` flag.

### Where the mapping comes from

`resources/district_counties.json` is generated by `district_counties.py`
(`python src/cli.py crosswalk`) directly from public boundary geometry:

- **District polygons** — UCLA cdmaps
  ([JeffreyBLewis/congressional-district-boundaries](https://github.com/JeffreyBLewis/congressional-district-boundaries))
  era GeoJSON files, one snapshot congress per cycle (2000s → 112th,
  2010s → 113th, 2020s → 119th, including the 2024 AL/GA/LA/NY/NC court
  redraws);
- **County polygons** — 2010 U.S. Census cartographic county boundaries
  (1:5m);
- Both layers are projected to EPSG:5070, per-pair intersection areas are
  computed, and each county is classified **Full** (≥ 98% of the county's
  district-covered area) or **Partial** (≥ 2% of the county, or ≥ 4% of the
  district's area — the fragment rule for urban districts inside one large
  county). Partial counties carry `county_share_pct`, which the lean uses as
  the vote weight.

The build self-validates: 435 seats per cycle, contiguous district numbers,
every one of the 3,142 county-equivalents claimed in every cycle, and
known-facts anchors (TX seat counts, Montana at-large → MT-01/MT-02, …).
Against 25 actual by-district presidential results parsed from state election
articles (2012–2024), area-weighted allocation halves the mean error of a
naive equal-split rule (3.96 pp vs 8.00 pp).

Each presidential year uses the map vintage actually in force:

| Presidential years | Map vintage |
|---|---|
| 2004, 2008, 2012 | 2000s map (108th–112th Congress, as finally used, incl. TX 2003 / GA 2006 redraws) |
| 2016, 2020 | 2010s map (113th–117th Congress, as first enacted) |
| 2024 | 2020s map (118th–119th Congress, incl. the 2024 AL/GA/LA/NY/NC court redraws) |

### Known limitations

Partial-county votes are allocated by geometric area share rather than
population, so lean values for districts containing large shared urban
counties (IN-07, TX-22, PA-07, …) are dampened toward the state mean —
at-large and whole-county districts are exact. The 2010s vintage shows the
map as first enacted (FL 2015 / NC 2016 & 2019 / PA 2018 redraws are not
reflected for 2016/2020), and AK-AL is computed for 2024 only because
Alaska publishes no borough-level table before then.

The lean pipeline reads local CSVs and never fetches on its own: run
`python src/cli.py presidential` first or pass `--fetch-missing`.

## Sources and attribution

- Election articles: Wikipedia (English), content available under
  [CC BY-SA](https://creativecommons.org/licenses/by-sa/4.0/).
- District boundary geometry:
  [UCLA cdmaps](https://github.com/JeffreyBLewis/congressional-district-boundaries)
  (Lewis, DeVine, Pitcher, and Martino — UCLA Department of Political Science).
- County boundaries: U.S. Census Bureau 2010 cartographic boundary files.

