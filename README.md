# Wiki Elections Pipeline

Parse **U.S. Senate** and **U.S. House** election data — polling tables,
results, incumbents, open seats — for an **inclusive start-year → end-year
range**, from Wikipedia wikitext fetched through the official **MediaWiki
Action API** with strict rate-limit compliance. A GitHub Actions workflow runs
the pipeline on a schedule and commits the refreshed CSVs back to this
repository.

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
├── cli.py               # argparse entry point (senate / house / all / fetch)
├── requirements.txt     # pandas, requests
├── .gitignore
└── data/                # Committed pipeline outputs (CSVs + metadata JSON)
    ├── senate/          #   senate_*.csv + senate_metadata_<ts>.json
    └── house/           #   house_results_*.csv
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
python cli.py all                                        # Senate + House, 2018–2024
python cli.py senate --start-year 2018 --end-year 2024   # Senate cycles
python cli.py house --start-year 2012 --end-year 2024    # House results
python cli.py all --start-year 2020 --end-year 2023      # odd bounds clamped -> 2020, 2022
python cli.py fetch "2018 United States Senate election in Arizona"   # debug: dump raw wikitext
```

Year ranges are **inclusive** and process **even (federal election) years
only** — odd bounds are clamped inward (`2019–2023` → `2020, 2022`).

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

### Historical House years (validated 1920–2024)

The House parser handles every article generation between **1920 and 2024**:

| Era | Layout |
|---|---|
| ~1912–1956 | `{{ushr|Massachusetts\|1\|X}}` full state names (or capitalised `{{Ushr}}`), lowercase `{{aye}}` winners, `{{Main article\|...}}` / commented-out links, `{{Plainlist\|* ...}}`, "Uncontested" markers |
| 2018–2020 | `{{ushr\|XX\|N\|X}}` codes + `{{Plainlist\|* ...}}` + `{{Sortname}}` |
| 2022–2024 | `{{plainlist}}...{{endplainlist}}`, bundled "Non-voting delegates" tables |

It also copes with article quirks: malformed `{{Plainlist|}}` + bare bullets,
`{{Main<year>...}}` typos, quoted `rowspan="2"` prefixes, multi-seat at-large
slates (`(N seats)` general tickets — Illinois/New York/Pennsylvania 1920s,
Minnesota/Missouri/Virginia 1932), Maine's ranked-choice rounds, and
uncontested seats listed without percentages (recorded as 100% winners).

Validation (offline parse against the live articles, 2026-08): every even year
1920–1940 yields **exactly 435 House winners** (plus territorial delegates),
and 2018–2024 reproduce the official party compositions (e.g. 1920: R 301 / D
132 / Socialist 1 / Ind. Republican 1; 1940: D 267 / R 162; 2018: D 235 / R
199 with NC-9 left undecided; 2022: R 222 / D 213).

## GitHub Actions automation

`.github/workflows/update-data.yml` runs the pipeline and **commits refreshed
CSVs back to `data/senate/` and `data/house/`** (bot commit marked `[skip ci]`
to avoid loops), plus uploads a convenience artifact.

- **Triggers:** manual `workflow_dispatch` (scope / start year / end year /
delay inputs) and a monthly cron (`0 4 1 * *`, 04:00 UTC on the 1st).
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
