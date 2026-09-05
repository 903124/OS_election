# Wiki Elections Pipeline

Parse **U.S. Senate**, **U.S. House**, **state legislature** (state senates +
state houses/assemblies), **statewide executive** (governor, attorney
general, secretary of state, state treasurer) and **presidential**
(county-level results for all 50 states + D.C.) election data — polling
tables, results, incumbents, winners — for an **inclusive start-year →
end-year range**, from Wikipedia wikitext fetched through the official
**MediaWiki Action API** with strict rate-limit compliance. A GitHub Actions
workflow runs the pipeline on a schedule and commits the refreshed CSVs back
to this repository.

Senate races are **discovered automatically**: for each even year in the range
the pipeline reads the `"{year} United States Senate elections"` overview
article and follows its per-state `{{main|...}}` links — so regular *and*
special elections are picked up without any hardcoded state list. The polling
parser handles both 2018-era tables (`align=center|` cells) and the 2020+
table generation (attribute headers, `sortable` tables with colspan group
headers, `{{efn}}` notes). House results come from each `"{year} United
States House of Representatives elections"` overview article.

> **Note:** this project previously mined a local 22 GB enwiki dump
> (`multistream.xml.bz2` + a SQLite offset index). It now uses the Wikipedia
> API instead, which removes the dump/index entirely — the whole 2018 Senate
> cycle is one batched API request, and the pipeline runs on any machine
> (including free GitHub-hosted runners).

## Project layout

```
├── .github/workflows/update-data.yml   # CI: run pipeline, commit data/
├── wiki_utils.py        # Rate-limited API client + wikitext cleaning helpers
├── senate_elections.py  # Senate race discovery: polling (long) + election boxes
├── house_elections.py   # House results, 1920–2024 article layouts
├── state_legislatures.py # State senate/house results (chamber articles)
├── statewide_elections.py # Gov / AG / SoS / treasurer overview summaries
├── presidential_elections.py # County-level presidential results per state
├── district_lean.py     # Predicted partisan lean per congressional district
├── district_counties.py # Geometric district→county crosswalk builder (regenerates the mapping)
├── cli.py               # argparse entry (senate/house/state-leg/statewide/presidential/lean/crosswalk/all/fetch)
├── requirements.txt     # pandas, requests (+ shapely, pyproj for `crosswalk`)
├── resources/
│   └── district_counties.json  # district→county mapping (geometric crosswalk)
├── .gitignore
└── data/                # Committed pipeline outputs (CSVs + metadata JSON)
    ├── senate/          #   federal senate_*.csv + metadata
    ├── house/           #   house_results_*.csv
    ├── state_senate/    #   state_senate_results_*.csv + metadata
    ├── state_house/     #   state_house_results_*.csv
    ├── statewide/       #   statewide_results_*.csv + metadata
    ├── presidential/    #   presidential_results_*.csv + metadata
    └── district_lean/   #   district_lean_*.csv + district_pvi_summary.csv + metadata
```

## Rate-limit compliance

| Measure | Implementation |
|---|---|
| Batched queries | Up to **50 titles per request** (API max for non-bot clients) — a 35-race Senate cycle = 1 request |
| Serial requests | `RateLimiter` enforces a configurable minimum interval between requests (default **1 s**) |
| `maxlag=5` | Standard Wikimedia politeness parameter; backs off when the cluster reports lag |
| 429 / `Retry-After` | Honoured verbatim before retrying |
| Transient failures | Exponential backoff with jitter on 5xx / network errors / `maxlag` / `ratelimited` |
| User-Agent policy | Descriptive, contact-bearing UA (Wikimedia requirement) |
| Redirects & normalisation | Requested titles transparently resolved to canonical pages |

## Setup

```bash
pip install -r requirements.txt   # Python 3.10+
```

Configuration is read from environment variables (all optional):

| Variable | Default | Purpose |
|---|---|---|
| `WIKI_API_URL` | `https://en.wikipedia.org/w/api.php` | API endpoint (point at another wiki if needed) |
| `WIKI_USER_AGENT` | pipeline default | **Set this** — e.g. `my-pipeline/1.0 (https://github.com/me/repo; me@example.com)` |
| `WIKI_REQUEST_DELAY` | `1.0` | Seconds between requests |
| `WIKI_BATCH_SIZE` | `50` | Titles per request (≤ 50) |
| `WIKI_MAX_RETRIES` | `5` | Retry attempts for transient errors |
| `WIKI_TIMEOUT` | `60` | Per-request timeout (s) |

## Usage

```bash
python cli.py all                                        # everything, 2018–2024
python cli.py senate --start-year 2018 --end-year 2024   # federal Senate cycles
python cli.py house --start-year 2012 --end-year 2024    # federal House results
python cli.py state-leg --start-year 2018 --end-year 2024 # state senates + houses
python cli.py statewide --start-year 2018 --end-year 2024 # gov/AG/SoS/treasurer
python cli.py presidential --start-year 2020 --end-year 2024 # county-level presidential
python cli.py lean                     # district partisan lean, full 2004–2024
python cli.py crosswalk                # rebuild the district->county mapping from geometry
python cli.py all --start-year 2020 --end-year 2023      # odd bounds clamped -> 2020, 2022
python cli.py fetch "2018 United States Senate election in Arizona"   # debug: dump raw wikitext
```

Year ranges are **inclusive** and process **even (federal election) years
only** — odd bounds are clamped inward (`2019–2023` → `2020, 2022`). The
`presidential` pipeline further restricts the range to **presidential years
(divisible by 4)**, so `--start-year 2018 --end-year 2024` processes 2020 and
2024.

Or run modules directly: `python senate_elections.py`, `python house_elections.py`
(both default to 2018–2024).

### Outputs (`data/senate/` + `data/house/`)

Files are grouped by chamber: Senate outputs go to `data/senate/`, House
outputs to `data/house/` (create the folders automatically if needed; pass
`--output <dir>` to use a different base directory). All Senate files carry a
`Year` column; each is written per year plus a combined `_all` file across the
requested range.

| File (relative to `data/`) | Contents |
|---|---|
| `senate/senate_primary_polling_{year}.csv` / `senate/senate_primary_polling_all.csv` | Long format: one row per poll × candidate (Year, State, Poll_Source, Date, Sample, MoE, Candidate, Party, Pct, Incumbent) |
| `senate/senate_general_polling_{year}.csv` / `..._all.csv` | Same schema, general election polls |
| `senate/senate_primary_results_{year}.csv` / `..._all.csv` | Election-box results (Winning / Candidate / Write-in / Total rows) |
| `senate/senate_general_results_{year}.csv` / `..._all.csv` | Same schema, general election |
| `senate/senate_metadata_<ts>.json` | Run metadata + discovered races per year + per-race counts/errors |
| `house/house_results_<year>.csv` | House results: year, state, state_code, district, candidate, party, percentage, winner, incumbent, open_seat |
| `house/house_results_all.csv` | All requested years combined |
| `state_senate/state_senate_results_<year>.csv` / `..._all.csv` | State senate results: year, state, state_code, chamber, district, race (general/primary/special), candidate, party, votes, percentage, winner, incumbent |
| `state_house/state_house_results_<year>.csv` / `..._all.csv` | State house/assembly results, same schema |
| `state_senate/state_leg_metadata_<ts>.json` | Per-chamber coverage report (rows, districts, gaps) |
| `statewide/statewide_results_<year>.csv` / `..._all.csv` | Statewide executives: year, state, state_code, office, candidate, party, percentage, winner, incumbent |
| `statewide/statewide_metadata_<ts>.json` | Per-office/year coverage + missing overview articles |
| `presidential/presidential_results_<year>.csv` / `..._all.csv` | County-level presidential results, one row per county × candidate: year, state, state_code, county, subdivision_type, candidate, party, votes, percentage, total_votes, winning_party |
| `presidential/presidential_metadata_<ts>.json` | Per-state coverage + per-candidate Totals-row cross-check (county sums vs state totals) |
| `district_lean/district_lean_<year>.csv` / `..._all.csv` | One row per district in force that year: year, map_vintage, state, state_code, district, district_note, at_large, counties_whole/_partial (+ name lists), matched_counties, d/r/other/total votes, two-party shares, national share, lean_pct, lean_label, votes_from_partial_pct |
| `district_lean/district_pvi_summary.csv` | Per district × map vintage: mean two-party margin vs mean national margin over the elections held under that vintage (2000s: 2004+2008+2012 · 2010s: 2016+2020 · 2020s: 2024), PVI-style label, `is_current_map` |
| `district_lean/district_lean_metadata_<ts>.json` | Method notes, per-year national baseline, allocation coverage per state, unmatched/unclaimed counties |

**State pipeline coverage notes.** State chamber articles are discovered from
the `{year} United States state legislative elections` overview via its
per-state `{{main|...}}` links. Roughly 80–86% of chambers per cycle yield
district-level results (election-box tables dominate; a table fallback covers
Minnesota-style `District / Candidates / Votes / %` layouts and Pennsylvania
sortable tables). Chambers whose articles carry no parseable results (e.g.
Alaska, several polling-only stubs) are recorded in the metadata JSON rather
than guessed. Statewide overviews for Secretary of State / State Treasurer
do not exist on Wikipedia for 2018 — those cycles are skipped and logged.
Percentages given as `{{percentage|a|{{sum|...}}}}` are computed from raw
votes; cross-party endorsement rows (New York) are merged into one row per
candidate.

### Historical House years (validated 1910–2024)

The House parser handles every article generation between **1910 and 2024**:

| Era | Layout |
|---|---|
| 1910–1916 | `{{USCongressElectionTableHead}}` tables with `! {{ushr\|..\|..\|X}}` header-cell districts, `{{Party stripe}}` candidate bullets |
| ~1912–1956 | `{{ushr|Massachusetts\|1\|X}}` full state names (or capitalised `{{Ushr}}`), lowercase `{{aye}}` winners, `{{Main article\|...}}` / commented-out links, `{{Plainlist\|* ...}}`, "Uncontested" markers |
| 1946–1992 | main-less state sections (`{{See also}}` only), wikilinked `== [[List of ... \|Alabama]] ==` headers, `{{nowrap\|{{ushr\|...\|\|X}}}}` wrappers, bold-only winner markers (1992 MA-9), wikilinked district cells (2004 MA) |
| 1970s | 1978-style `|`-cell district rows, `{{party shortname\|...}}` party labels, whole-span bold winners `'''[[Name]] ([[Party\|DFL]]) NN%'''` |
| 2018–2020 | `{{ushr\|XX\|N\|X}}` codes + `{{Plainlist\|* ...}}` + `{{Sortname}}` |
| 2022–2024 | `{{plainlist}}...{{endplainlist}}`, bundled "Non-voting delegates" tables |

It also copes with article quirks: malformed `{{Plainlist|}}` + bare bullets,
`{{Main<year>...}}` typos, quoted `rowspan="2"` prefixes, multi-seat at-large
slates (`(N seats)` general tickets — Illinois/New York/Pennsylvania 1920s,
Minnesota/Missouri/Virginia 1932), Maine's ranked-choice rounds, and
uncontested seats listed without percentages (recorded as 100% winners).

Validation (offline parse against the live articles, 2026-08): **53 of the 58
even years 1910–2024 reproduce the official seat and party compositions
exactly** (e.g. 1920: R 301 / D 132 / Socialist 1 / Ind. Republican 1; 1940:
D 267 / R 162; 1966: D 248 / R 187; 1986: D 258 / R 177; 2018: D 235 / R 199
with NC-9 left undecided; 2022: R 222 / D 213). The five flagged years
(1910–1916, 1922) are coverage gaps in the *overview articles themselves*
(several states' sections are empty or party-aggregated there), not parser
errors — 1910 yields 264 of 386 seats, 1914 393/435, 1916 373/435.

### Wide-range validation (1910–2024)

All four pipelines were exercised over their full available year ranges with
every output compared against official records:

| Pipeline | Range tested | Result |
|---|---|---|
| house | 1910–2024 | 53/58 years match official compositions; remaining 5 = overview-article data gaps |
| senate | 1914–2024 | 1,933 race articles discovered (≈34.5/cycle), 0 parse failures; races whose titles redirect to the cycle overview are skipped instead of mis-attributed; landmark winners verified across decades (Wagner 1932, LBJ 1948, Humphrey 1948, JFK 1952, RFK 1964, Ted Kennedy 1962, Grassley & Quayle 1980, Feinstein 1992, Clinton 2000, Obama 2004, Rubio & Paul 2010, McCain 2016) |
| statewide | 1980–2024 | governor overviews exist from 1980, AG from 2016, SoS/treasurer from 2020; "Race summary" tables appear from 2000 (earlier cycles skip gracefully); 7/7 landmark winners verified |
| state-leg | 2000–2024 | 13 cycles, ≈1,000 chamber articles, 71K rows; district coverage ≥99.8%; per-candidate coverage grows from 5 states (2000) to 40 (2020) as Wikipedia article coverage expands |

Known era caveats (data, not parser bugs): 1954-era winners' shares written
without a `%` sign are recovered; election-box rows embedding
`change={{decrease}}` templates (1980s–2000s) are handled; races that redirect
to the cycle overview (pre-article era, ≈25% of pre-1930 races) are skipped
and logged in `senate_metadata_*.json`; 2000s state-leg articles that report
only party-aggregated totals (e.g. Texas 2004) yield no candidate rows.

### Table-cell robustness regression pass

A focused regression pass hardened the state-leg summary-table parser against
real-world MediaWiki cell markup that was silently mis-parsed before:

- **Unquoted HTML attributes** before the separator pipe
  (`|id=7A rowspan="2" data-sort-value="13" |7A`) — the 2018 Minnesota House
  table dropped every district from 7A on (all rows mis-attributed to 6B with
  junk candidates). `_cell_value` now strips quoted, single-quoted and
  unquoted attributes in any order; MN 2018 parses to exactly **134
  districts / 134 winners**.
- **CSS-bold winners** (`style="font-weight:bold"`) — the winner cell is now
  located even when it is not `'''wiki-bold'''` and differs from the
  rowspan'd incumbent cell (MN 6B 2018: winner Lislegard, not retiring Metsa);
  votes/pct are found by scanning past `<ref>` cells, party labels and
  rowspan'd year links wherever they sit in the row.
- **Two-row headers** — `!Name !Party !Votes !%` second header rows (and
  Location/Member/Status variants) no longer leak in as candidate rows.
- **`{{Plainlist|* Name - 53%}}` candidate cells** (Oklahoma-style summary
  tables) — items are unpacked into per-candidate rows with party from
  `{{Party stripe}}` and share from the trailing percentage; primary-eliminated
  items are skipped. OK Senate/House 2024 went from junk rows to real results
  (e.g. Bergstrom 53%, Mann 60%).
- District labels normalized (`DISTRICT 1` → `1`), so chamber district counts
  now match seat counts exactly for CT (36/151), KY (100), GA (180), FL (120),
  WI (99), HI (51), MN (67/134), AZ (30) in the affected cycles.
- Fixed shapes are locked in by smoke-test fixtures
  (`stateleg-mn2018-attr-cells-fixture`, `stateleg-plainlist-candidates-fixture`).

### County-level presidential results (all 50 states + D.C.)

The `presidential` pipeline fetches one article per jurisdiction per
presidential year:

    {year} United States presidential election in {State}

(50 states via `{year} United States presidential election in Alabama` …
`... in Wyoming`, plus `... in Washington (state)` and
`... in Washington, D.C.`; a 51-title cycle is ~2 batched API requests.)
Each article's subdivision-level results table is parsed:

| Jurisdiction(s) | Heading | subdivision_type |
|---|---|---|
| 47 states | `By county` | County |
| Louisiana | `By parish` | Parish |
| Virginia | `By county and independent city` (older: `By city and county`) | Locality |
| Alaska | `By borough and census area (estimates)` | Borough/Census area |
| Washington, D.C. | `Results by ward` / `By Ward` | Ward |

All tables share the same two-row header (rowspan/colspan candidate groups,
`#` + `%` sub-columns, `Margin`, `Total`), which the parser mats through an
HTML-style rowspan/colspan grid — tolerating linked header labels, `<ref>`s,
attribute order variants, negative margins, stray empty row separators,
`{{Update}}` banners and header cells where the party sits on a second line
(no `<br />`). Heading sections are tried in document order and the first
table that looks like a *general*-election table wins, so primary-election
tables sharing the same heading text (West Virginia 2012, D.C. 2012) are
skipped. The trailing `!Totals!!...` row is captured and cross-checked
against the sum of county rows per candidate (diff % recorded in the
metadata JSON). Note the cross-check quantifies **article-level** data
inconsistencies, not parser errors — row-level candidate sums match each
county's own Total cell in 326 of 349 parsed state-years; where the totals
deviate (e.g. Illinois 2016, whose county Total cells sum to 5,595,279 while
the article's own Totals row reports the official 5,536,424) the CSV stays
faithful to the per-county table. Each output row also carries the county's
`winning_party` (party of the top vote-getter; `{{party shading}}` fallback
when votes are absent).

### Predicted partisan lean per congressional district (`district_lean/`)

The `lean` pipeline joins the district→county mapping
(`resources/district_counties.json`: all 435 voting districts + DC under the
2000s / 2010s / 2020s map vintages, with geometric Full/Partial county lists
and each partial claimant's area share) with the county-level
presidential CSVs produced by the `presidential` pipeline, and computes a
Cook-PVI-style lean for every congressional district:

    lean = (district Democratic two-party share − national Democratic
            two-party share) × 100        →  "D+x.x" / "R+x.x" / "EVEN"

County votes are allocated to districts as follows: whole counties count at
full weight; partial counties are allocated **in proportion to each claimant
district's geometric share of the county** (weights normalised across
claimants); at-large districts (AK, DE, ND, SD, VT, WY, DC) aggregate their
whole jurisdiction exactly. Allocation is conservative — each county's votes
are distributed across its claimant districts without loss, and the metadata
records per-state allocation coverage (100.0% everywhere in the validation
runs) plus any unmatched county names. The national baseline is the sum of
the same county rows; it reproduces the official national two-party shares
to within 0.03pp in every computed year (e.g. 2016: 51.13% vs 51.11%
official, 2020: 52.29% vs 52.27%).

#### Where the mapping comes from (`district_counties.py` / `cli.py crosswalk`)

The mapping is generated **directly from public boundary geometry — no Excel
workbook involved**:

- district polygons: UCLA cdmaps (`JeffreyBLewis/congressional-district-boundaries`)
  era GeoJSON files, one snapshot congress per cycle (2000s → 112th,
  2010s → 113th, 2020s → 119th, incl. the 2024 AL/GA/LA/NY/NC court redraws);
- county polygons: 2010 Census cartographic county boundaries (1:5m);
- both layers projected to EPSG:5070, per-pair intersection areas stored, and
  classified Full (≥98% of the county's district-covered area) / Partial
  (≥2% of the county, or ≥4% of the district's area — the fragment rule for
  urban districts inside one large county);
- a county listed as Partial carries `county_share_pct` (its geometric share
  inside the claimant district), which the lean uses as the vote weight.

`python cli.py crosswalk` regenerates the mapping (~185 MB of cached,
resumable downloads + per-state checkpoints; needs `shapely` + `pyproj`).
The build self-validates: seat totals = 435 per cycle, contiguous district
numbers, every one of the 3,142 county-equivalents claimed in every cycle,
and known-facts anchors (TX 32/36/38 seats, Bowie County's 2000s→2020s move,
MT at-large → MT-01/MT-02, …). Against 25 actual by-district presidential
results parsed from the state election articles (2012–2024), the
area-weighted lean halves the mean error of the legacy equal-split rule
(3.96 pp vs 8.00 pp).

Each presidential year uses the map vintage actually in force:

| Presidential years | Map vintage |
|---|---|
| 2004, 2008, 2012 | 2000s map (108th–112th Congress; as finally used, incl. TX 2003 / GA 2006 redraws) |
| 2016, 2020 | 2010s map (113th–117th, as first enacted) |
| 2024 | 2020s map (118th–119th, incl. the 2024 AL/GA/LA/NY/NC court redraws) |

Known limitations, all visible in the output columns:

- `votes_from_partial_pct` measures how much of a district's vote came from
  area-weighted partial counties. Districts that contain large shared urban
  counties (IN-07, TX-22, PA-07 …) are dampened toward the state mean —
  treat those rows as rough estimates; at-large and whole-county districts
  are exact.
- Partial allocation weights are geometric area shares, not population
  shares (population-weighted allocation would require block-level data).
- The 2010s snapshot shows the map as first enacted, so the FL 2015 /
  NC 2016 & 2019 / PA 2018 mid-cycle court redraws are not reflected for
  2016/2020.
- Alaska publishes no borough-level table before 2024, so AK-AL is computed
  for 2024 only (empty lean cells elsewhere).

The pipeline reads local CSVs and never fetches on its own: run
`python cli.py presidential` first (or use `lean --fetch-missing`, or the
workflow, which runs both). `district_pvi_summary.csv` additionally averages
each district's lean over the elections held under each vintage — a
Cook-PVI-style summary per map era with an `is_current_map` flag.

## GitHub Actions automation

`.github/workflows/update-data.yml` runs the pipeline and **commits refreshed
CSVs back to `data/senate/`, `data/house/`, `data/state_senate/`,
`data/state_house/`, `data/statewide/`, `data/presidential/` and
`data/district_lean/`** (bot commit
marked `[skip ci]` to avoid loops), plus uploads a convenience artifact.

- **Triggers:** manual `workflow_dispatch` (scope: all / senate / house /
  state-leg / statewide / presidential / lean; start year / end year / delay
  inputs) and a monthly cron (`0 4 1 * *`, 04:00 UTC on the 1st).
- **Runner:** standard `ubuntu-latest` — no self-hosted runner needed since
  there is no dump download.
- **Setup:** push these files to your repo, enable Actions, and (recommended)
  add a `WIKI_USER_AGENT` repository secret with your contact info:

```yaml
WIKI_USER_AGENT: wiki-elections-pipeline/1.0 (https://github.com/<you>/<repo>; <you>@example.com)
```

- **Concurrency:** runs are serialised (`concurrency: update-data`) so two runs
  never race on the same `data/` directory.

To change the schedule, edit the `cron` line in the workflow (UTC timezone).

## Differences from the original notebook

- All duplicated helpers (4 copies of article extraction, 3 of `clean_wikitext`)
  consolidated into `wiki_utils.py`.
- Dump/SQLite index removed — `build_index.py` is no longer needed.
- **Year-range support:** the hardcoded 2018 state list is replaced by race
discovery from each cycle's overview article (`{{main|...}}` links), which
also picks up special elections (e.g. Minnesota/Mississippi 2018, Arizona
2020, Oklahoma 2022) automatically. Every output row is tagged with `Year`.
- Senate batch fetch uses rate-limited batches of ≤ 50 titles instead of
per-state extraction; batch metadata records per-race counts and errors.
- Fixed election-box title extraction (the old regex could only terminate on
  `<ref>`, so titles ending in `\n}}` were labelled "Unknown", breaking
  primary/general classification).
- Paths configurable via env vars / CLI flags instead of hardcoded `E:\wiki\...`.
- `logging` replaces `print`, type hints and docstrings throughout.
