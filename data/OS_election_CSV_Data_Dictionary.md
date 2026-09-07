# wiki-elections-pipeline — CSV Data Dictionary

| | |
|---|---|
| **Repository** | <https://github.com/903124/OS_election> |
| **Applies to** | All `.csv` files under `data/` |
| **Version** | 1.0 |
| **Date** | 2026-09-05 |
| **Data source** | English Wikipedia election articles, retrieved through the MediaWiki Action API by the modules in `src/` |

---

## 1. Purpose and scope

This document specifies the structure and semantics of every CSV dataset published in the repository. For each file family it defines the record grain, enumerates every column with its type, population rule, and a representative value, and records the limitations that are relevant to downstream analysis. All descriptions were verified against the shipped data and against the parser implementations that produce it.

Metadata JSON sidecar files (`*_metadata_*.json`) and the district-to-county crosswalk (`resources/district_counties.json`) are not CSV files and are therefore outside the scope of this document; they are referenced only where a column's semantics depend on them.

## 2. File organization and naming

The repository ships 247 CSV files organised into 10 file families across 7 directories. Two naming patterns apply:

| Pattern | Meaning |
|---|---|
| `<name>_<year>.csv` | Data for a single election cycle. Year files cover even federal election years only. |
| `<name>_all.csv` | Concatenation of all per-year files of the family. The combined file carries the year column present in the per-year files and its schema is the union of the per-year schemas. |

The single member without a per-year split is `district_pvi_summary.csv`, which is itself a summary over multiple election years.

Because each per-year file is a subset of the corresponding `_all` file, this document specifies **one schema per family**; the schema applies to every member of that family unless a deviation is explicitly noted.

### 2.1 Storage format

- Encoding: UTF-8; plain comma-separated values with a single header row.
- Fields containing commas (thousands-separated counts, date ranges) are enclosed in double quotes per the usual CSV convention.
- Per-year files within the senate results families vary in **column order** and occasionally contain additional artefact columns; consumers concatenating year files manually should align on column names rather than position. The `_all` files are already aligned.

## 3. Conventions and notation

| Notation | Meaning |
|---|---|
| `int`, `decimal` | Numeric values stored as plain digits or decimals. Where a field retains source formatting (separators, `%`), the type is given as `string` and the required cleansing is stated in the description. |
| `string` | Free-form or enumerated text. Enumerated values are listed explicitly where they exist. |
| `boolean` | The literal strings `True` / `False` (Python representation), not `0/1` or `Y/N`. |
| *empty* | An empty cell indicates that the value was absent from the source article, is not applicable to the record's row type, or could not be resolved by the parser. An empty cell never denotes zero. |

Two casing conventions coexist: the six `senate_*` families use capitalised headers (`Year`, `State`, …); all other families use lowercase headers (`year`, `state`, …). Column names must be normalised when joining across families.

## 4. File inventory

| # | Directory / pattern | Files | Year coverage | Records | Content |
|---|---|---:|---|---:|---|
| 1 | `data/senate/senate_general_results_*.csv` | 54 | 1920–2024 | 16,340 | U.S. Senate general-election results, one record per candidate and per turnout summary, per race |
| 2 | `data/senate/senate_primary_results_*.csv` | 54 | 1920–2024 | 13,547 | U.S. Senate primary-election results (same structure as #1) |
| 3 | `data/senate/senate_general_polling_*.csv` | 20 | 1970–2024 | 5,454 | Opinion polls preceding Senate general elections |
| 4 | `data/senate/senate_primary_polling_*.csv` | 15 | 1938–2024 | 2,807 | Opinion polls preceding Senate primaries |
| 5 | `data/house/house_results_*.csv` | 53 | 1920–2024 (1978 absent) | 57,469 | U.S. House general-election results by district |
| 6 | `data/state_senate/state_senate_results_*.csv` | 12 | 2004–2024 | 25,084 | State senate election results by district |
| 7 | `data/state_house/state_house_results_*.csv` | 12 | 2004–2024 | 65,376 | State house / assembly election results by district |
| 8 | `data/statewide/statewide_results_*.csv` | 12 | 2004–2024 | 2,490 | Governor, attorney general, secretary of state, and state treasurer results |
| 9 | `data/presidential/presidential_results_*.csv` | 7 | 2004–2024 | 119,172 | Presidential results by county, parish, or equivalent subdivision |
| 10 | `data/district_lean/district_lean_*.csv` + `district_pvi_summary.csv` | 8 | 2004–2024 | 5,232 + 1,306 | Cook-PVI-style partisan lean per congressional district, and a per-map-vintage summary |

Record counts aggregate all files of a family, including `_all`. Per-year file counts: families 1–2, 53 files each (1920–2024); family 3, 19 files (1970–2024, irregular); family 4, 14 files (1938–2024, irregular); family 5, 52 files (1920–2024, 1978 not present in the shipped dataset); families 6–8, 11 files each (2004–2024); family 9, 6 files (2004–2024); family 10, 6 lean year files plus `_all` plus the summary file.

---

## 5. `data/senate/` — U.S. Senate results

Four result and polling families are produced by `src/senate_elections.py`. Race articles are discovered dynamically from each cycle's overview article; regular and special elections are both captured, and a special election surfaces as an additional `Election` title within the same `Year` and `State`.

### 5.1 `senate_general_results_{year}.csv`, `senate_general_results_all.csv`

**Grain.** One record per candidate per election box, plus one turnout-summary record per box. An article may contain several boxes when a special election is held alongside the regular election.

**Column specification.**

| Column | Type | Description | Example |
|---|---|---|---|
| `Year` | int | Even federal election year of the race. | `2004` |
| `State` | string | Full name of the state or district holding the election. | `Alabama` |
| `Election` | string | Title of the source results table or article section, verbatim. Together with `Year` and `State` this is the discriminator between regular and special elections. Values follow article naming and are not normalised. | `2004 United States Senate election in Alabama` |
| `Election_Type` | string | Constant `General` within this family; the column exists so that general and primary files can be concatenated. | `General` |
| `Row_Type` | string | Record kind; enumerated values in Table 5.1 a. | `Winning` |
| `Incumbent` | boolean | `True` when the candidate was the incumbent at election time. | `True` |
| `party` | string | Party name exactly as given in the article's election-box template. | `Republican Party (United States)` |
| `candidate` | string | Candidate name with wikilink markup removed; empty on turnout-summary records. | `Richard Shelby` |
| `votes` | string | Vote count as rendered in the article, normally with thousands separators. Separators must be stripped before numeric conversion. Empty for a small number of scattered historical records. | `"1,242,200"` |
| `percentage` | string | Vote share with trailing `%`, one or two decimals. | `67.55%` |
| `name` *(variant)* | string | Parser artefact: a non-standard election-box template parameter captured from a small number of 2020s articles. Carries no analytical value; populated only in the year files where the parameter occurred, and present in `_all` as an effectively empty column. | *(usually empty)* |

**Table 5.1 a — `Row_Type` enumeration.**

| Value | Meaning |
|---|---|
| `Winning` | Election winner, taken from the article's winning-candidate box, or the top-voted candidate where the article declares no explicit winner. |
| `Candidate` | Losing candidate. |
| `Write-in` | Declared write-in candidacy. |
| `Total` | Turnout summary for the box: `candidate` and `party` empty, `votes` equal to total votes cast, `percentage` equal to `100.0%`. Candidate-level analysis must exclude these records. |

**Structural notes.** The ten core columns are stable across the family. Column order varies between year files — older files sometimes place `party` before `candidate` — and the artefact column `name` appears in a few 2020s files. Alignment on column names is required when concatenating year files; `senate_general_results_all.csv` is already aligned.

### 5.2 `senate_primary_results_{year}.csv`, `senate_primary_results_all.csv`

Structure is identical to §5.1; the family covers partisan primaries. Deviations:

| Column | Deviation from §5.1 |
|---|---|
| `Election_Type` | Constant `Primary`. |
| `Election` | Primary-table titles, e.g. `Republican primary results`, `Democratic primary results`, `2004 California Republican primary`. The party holding the primary is identifiable only through this title. |
| `Row_Type` | Same enumeration; `Total` records summarise primary turnout. |
| `name`, `pad` *(variants)* | The artefact column `pad` occurs in a small number of primary files (notably 2020) alongside `name`; both are residual template parameters and are effectively empty. |

States whose seats are decided without a primary (convention systems, and all states in years without a contested primary) produce no records in this family; primary coverage is consequently sparser than general coverage.
## 6. `data/senate/` — U.S. Senate polling

Both polling families share one 11-column schema and are stored in long format: one record per poll per candidate, so a poll covering *n* candidates contributes *n* records sharing `Poll_Source` and `Date`.

### 6.1 `senate_general_polling_{year}.csv`, `senate_general_polling_all.csv`

| Column | Type | Description | Example |
|---|---|---|---|
| `Year` | int | Election year the poll refers to. | `2004` |
| `State` | string | State whose Senate race was polled. | `Arkansas` |
| `Primary_Type` | string | Constant `General` within this family. The column name is shared with the primary-polling family. | `General` |
| `Poll_Source` | string | Pollster or sponsoring organisation as named in the article. | `SurveyUSA` |
| `Date` | string | Field date(s) of the poll, free text exactly as printed; ranges retain the en dash. No normalised date column exists. | `"October 31 – November 1, 2004"` |
| `Sample` | string | Sample size text. Population qualifiers: `LV` = likely voters, `RV` = registered voters. Empty or `?` where unreported. | `549 (LV)` |
| `MoE` | string | Margin of error with `±` prefix; empty where the poll reported none. | `± 4.3%` |
| `Candidate` | string | Candidate as listed in the poll row. | `Blanche Lincoln` |
| `Party` | string | One-letter party code as used by polling tables: `D`, `R`, `I` (independent), `L` (Libertarian), `G` (Green), `Unknown`. | `D` |
| `Pct` | string | Poll percentage with `%` suffix, whole numbers. | `53%` |
| `Incumbent` | boolean | `True` when the polled candidate is the incumbent. | `True` |

### 6.2 `senate_primary_polling_{year}.csv`, `senate_primary_polling_all.csv`

The schema is identical to §6.1. Only `Primary_Type` carries different content:

| Value | Meaning |
|---|---|
| `Democratic Primary` / `Republican Primary` | Ordinary contested partisan primary. |
| `single_party` | Only one party's primary effectively decides the seat. |
| `jungle` | All-candidate primary in which the top finishers advance (California, Louisiana, and Washington-style systems). |

Year files exist only where primary polling tables exist in the source articles, which accounts for the irregular year coverage (1938 through 2024).

## 7. `data/house/` — `house_results_{year}.csv`, `house_results_all.csv`

U.S. House of Representatives general-election results, produced by `src/house_elections.py` from each cycle's House overview article and the per-state sections within it. 52 year files (even years 1920–2024 except 1978) plus `_all`; 57,469 records.

**Grain.** One record per candidate per district race.

| Column | Type | Description | Example |
|---|---|---|---|
| `year` | int | Even federal election year. | `2004` |
| `level` | string | Constant `house`; supports vertical concatenation with other office families. | `house` |
| `state` | string | Full state name. | `Alabama` |
| `state_code` | string | Two-letter USPS state code. | `AL` |
| `district` | string | District number without zero padding (`1`–`52`), or `AL` for at-large seats. | `1` |
| `candidate` | string | Candidate name, cleaned of markup. | `Jo Bonner` |
| `party` | string | Normalised short party name — without the `(United States)` suffix used in the senate families. Frequent values: `Democratic`, `Republican`, `Libertarian`, `Independent`, `Green`, `Constitution`, `DFL`, `Socialist`, `Reform`. The value `Unknown` is used where the source article does not identify a party. | `Republican` |
| `percentage` | decimal | Candidate's reported vote share in the district, one decimal. Uncontested winners are recorded at `100.0`. Shares of all candidates in a race sum to approximately 100. | `63.2` |
| `winner` | boolean | `True` for the candidate(s) elected. Multi-seat at-large races (states electing several at-large members before 1967) legitimately contain several `True` records per district. | `True` |
| `incumbent` | boolean | `True` when the candidate is the sitting representative standing for re-election. | `True` |
| `open_seat` | boolean | Race-level flag repeated on every candidate record of the race. `True` when the source section signals an open seat — the incumbent retired, the seat was vacant, a resignation or death occurred, or the district or seat is new — that is, no incumbent sought re-election. | `False` |

**Coverage notes.** Candidate-level completeness before the 1940s reflects the completeness of the source overview articles rather than parser behaviour; the same upstream gaps explain the absent 1978 file. Voided races (e.g. North Carolina's 9th district, 2018) may appear without any `winner = True` record, and a small number of races list the winner only.

## 8. `data/state_senate/` and `data/state_house/` — state legislative results

Both directories are produced by `src/state_legislatures.py` from per-chamber Wikipedia articles and share one 12-column schema:

- `state_senate_results_{year}.csv` — 11 year files (2004–2024) plus `_all`; 25,084 records; `chamber` is constantly `State Senate`.
- `state_house_results_{year}.csv` — 11 year files (2004–2024) plus `_all`; 65,376 records; `chamber` is constantly `State House` and covers the houses/assemblies of all fifty states.

**Grain.** One record per candidate per district race, including special elections held within the same cycle.

| Column | Type | Description | Example |
|---|---|---|---|
| `year` | int | Even election year of the chamber cycle. | `2004` |
| `state` | string | Full state name. | `Arizona` |
| `state_code` | string | Two-letter USPS code. | `AZ` |
| `chamber` | string | `State Senate` or `State House`; constant per directory, retained to support concatenation of both families. | `State Senate` |
| `district` | string | Legislative district identifier as used by the state. Predominantly numeric; several states use named or lettered districts (e.g. Vermont's `Addison` district). | `1` |
| `race` | string | `general` for the regular November election; `special general` for a special election held in the same cycle and article. | `general` |
| `candidate` | string | Candidate name, cleaned. | `Ken Bennett` |
| `party` | string | Normalised party name with state-level variants preserved: `Democratic`, `Republican`, `Democratic-Farmer-Labor`, `Democratic-NPL`, `Libertarian`, `Independent`, `Green`, `Write-in`, among others. Empty for a small number of older records where the source article omitted the party. | `Republican` |
| `votes` | string | Raw vote count, frequently in decimal notation (e.g. `50727.0`) because the parser reads source numbers as floats. Conversion must go through a float type. | `50727.0` |
| `percentage` | decimal | Vote share within the district race; where the source article provided percentages computed from counts, the parser recomputes them from `votes`. In multi-member districts the share is split across several winners. | `59.64` |
| `winner` | boolean | `True` for elected candidate(s). In multi-member districts (e.g. the two- to three-member state house districts of Arizona, Idaho, and New Jersey) several records per district and race are `True`. | `True` |
| `incumbent` | boolean | `True` when the candidate was the district's sitting member. | `True` |

**Coverage notes.** Candidate-level detail expands substantially over the covered period (approximately 5 states published candidate-level boxes in 2000 versus approximately 40 by 2020), so earlier years skew toward winner-only records. District coverage itself is at or above 99.8% in every shipped cycle.

## 9. `data/statewide/` — `statewide_results_{year}.csv`, `statewide_results_all.csv`

Statewide executive-office results — governor, attorney general, secretary of state, and state treasurer — produced by `src/statewide_elections.py` from the "Race summary" tables of each cycle's per-office overview articles. 11 year files (2004–2024) plus `_all`; 2,490 records.

**Grain.** One record per candidate per office per jurisdiction.

| Column | Type | Description | Example |
|---|---|---|---|
| `year` | int | Even election year. | `2004` |
| `state` | string | Full name of the state or territory. The governor tables include territories and the District of Columbia, so values such as `Puerto Rico` and `Guam` occur. | `Delaware` |
| `state_code` | string | Two-letter USPS code, including `PR`, `GU`, and `DC` for non-states. | `DE` |
| `office` | string | One of `governor`, `attorney general`, `secretary of state`, `state treasurer`. | `governor` |
| `candidate` | string | Candidate name, cleaned. | `Ruth Ann Minner` |
| `party` | string | Normalised party name. Beyond the two major parties the column contains `Libertarian`, `Independent`, `Green`, `Write-in`, territorial parties (e.g. `Popular Democratic Party (Puerto Rico)`), and historical fusion labels. | `Democratic` |
| `percentage` | decimal | Reported share of the vote, one decimal. | `50.9` |
| `winner` | boolean | `True` for the winner of the office. | `True` |
| `incumbent` | boolean | `True` when the candidate held the office entering the election. | `True` |

**Coverage notes.** The office families begin at different years upstream: gubernatorial summary tables exist from 1980 (the shipped run starts at 2004), attorney-general overviews exist from 2016, and secretary-of-state and treasurer overviews from 2020; consequently those three offices appear in fewer files. The 2018 cycle lacks secretary-of-state and treasurer overview articles on Wikipedia entirely, so `statewide_results_2018.csv` contains no records for those offices.
## 10. `data/presidential/` — `presidential_results_{year}.csv`, `presidential_results_all.csv`

County-level presidential election results for all fifty states and the District of Columbia, produced by `src/presidential_elections.py`. 6 year files (2004–2024) plus `_all`; 119,172 records.

**Grain.** One record per subdivision per candidate group. Third-party and other candidates are aggregated into a single group.

| Column | Type | Description | Example |
|---|---|---|---|
| `year` | int | Presidential election year (divisible by 4). | `2004` |
| `state` | string | Full jurisdiction name; the fifty states plus `Washington, D.C.`. | `Alabama` |
| `state_code` | string | Two-letter USPS code, `DC` included. | `AL` |
| `county` | string | Subdivision name without the type suffix (e.g. `Autauga`, not `Autauga County`). The nature of the subdivision is given by `subdivision_type`. | `Autauga` |
| `subdivision_type` | string | Kind of first-level subdivision used by the jurisdiction's article: `County` (most states), `Parish` (Louisiana), `Locality` (Virginia's independent cities), `Ward` (District of Columbia), `Borough/Census area` (Alaska). | `County` |
| `candidate` | string | Major-party candidates by name. All remaining candidates are aggregated into one record with the value `Other`; in some recent cycles the source labels the group `Various candidates`, producing the party value `Various Parties`. | `George W. Bush` |
| `party` | string | Party of the candidate record: `Republican`, `Democratic`, `Other`; explicit third-party records in later cycles carry `Libertarian`, `Green`, `Constitution`, `Independent`, state variants (`Democratic-Farmer-Labor`, `Democratic-NPL`), `Various Parties`, or `Nonparty`. | `Republican` |
| `votes` | int | Votes for the candidate (or for the `Other` aggregate) in the subdivision. Plain digits, no separators. | `15196` |
| `percentage` | decimal | Candidate's share of the subdivision's total vote, two decimals. | `75.67` |
| `total_votes` | int | Total votes cast in the subdivision; repeated on every record of that subdivision. | `20081` |
| `winning_party` | string | Party that carried the subdivision; one winner label per subdivision. | `Republican` |

**Processing note.** Candidate records reproduce the article's per-subdivision table. The article's `Totals` row is used only for an internal cross-check recorded in the metadata sidecar and is not written to the CSV. Because third parties are aggregated into `Other`, the two-party share of a subdivision is computed by excluding the `Other` record; the lean pipeline (§11) performs exactly this computation.

## 11. `data/district_lean/` — congressional district partisan lean

Two related families are computed offline by `src/district_lean.py`, which does not fetch from Wikipedia itself. The pipeline joins the bundled district-to-county crosswalk with the presidential datasets of §10 and computes a Cook-PVI-style lean for every congressional district and presidential year, applying the district map vintage in force at each election (2000s map for 2004/2008/2012, 2010s map for 2016/2020, 2020s map for 2024).

### 11.1 `district_lean_{year}.csv`, `district_lean_all.csv`

6 year files (2004, 2008, 2012, 2016, 2020, 2024) plus `_all`; 5,232 records.

**Grain.** One record per congressional district per presidential year.

| Column | Type | Description | Example |
|---|---|---|---|
| `year` | int | Presidential election year measured. | `2004` |
| `map_vintage` | string | District map generation in force: `2000s` (2004/2008/2012), `2010s` (2016/2020), `2020s` (2024). | `2000s` |
| `state` | string | Full jurisdiction name, states plus the District of Columbia. | `Alaska` |
| `state_code` | string | Two-letter code. | `AK` |
| `district` | string | Congressional district in `{state}-{number}` form; at-large seats take the form `{state}-AL` (e.g. `AK-AL`). | `AK-AL` |
| `district_note` | string | Reserved note field originating in the crosswalk; constantly empty in the shipped data. | *(empty)* |
| `at_large` | boolean | `True` for the seven single-seat jurisdictions (AK, DE, ND, SD, VT, WY, DC), whose lean is an exact statewide aggregation. | `True` |
| `counties_whole` | int | Number of counties allocated in full to the district. | `5` |
| `counties_partial` | int | Number of counties shared with other districts, allocated by geometric area share. | `1` |
| `counties_whole_list` | string | County names corresponding to `counties_whole`, separated by semicolon and space. | `Baldwin County; Escambia County; …` |
| `counties_partial_list` | string | County names corresponding to `counties_partial`, same separator. | `Clarke County` |
| `matched_counties` | int | Total counties claimed by the district (whole plus partial). | `6` |
| `d_votes` | int | Democratic votes allocated to the district, from the §10 data. | `91594` |
| `r_votes` | int | Republican votes allocated. | `166771` |
| `other_votes` | int | Votes for all other parties, allocated; excluded from the two-party computation. | `1935` |
| `total_votes` | int | Sum of `d_votes`, `r_votes`, and `other_votes`. | `260300` |
| `d_share_two_party_pct` | decimal | Democratic share of the two-party vote in the district, two decimals. Empty where the district has no allocatable votes (see limitations, §13.6). | `35.45` |
| `r_share_two_party_pct` | decimal | Republican two-party share; the two shares sum to 100. | `64.55` |
| `national_d_share_two_party_pct` | decimal | National Democratic two-party share for the year — the baseline against which every district is measured. | `48.79` |
| `lean_pct` | decimal | `(d_share − national_d_share) × 100`. Positive values denote a more Democratic electorate than the nation, negative values a more Republican one. Empty where the shares are absent. | `-13.33` |
| `lean_label` | string | Human-readable PVI label to one decimal: `D+x.x`, `R+x.x`, or `EVEN`. | `R+13.3` |
| `votes_from_partial_pct` | decimal | Share of the district's vote allocated through partial counties. Serves as the quality indicator for the area-share approximation described in §13.6: `0.0` identifies whole-county districts whose lean is exact, and higher values (above approximately 25) indicate stronger dampening toward the state mean. | `1.5` |

### 11.2 `district_pvi_summary.csv`

The PVI-style roll-up: one record per district per map vintage (1,306 records). Each record averages the district's margin over the presidential elections held under that vintage — 2000s map: 2004, 2008, 2012; 2010s map: 2016, 2020; 2020s map: 2024.

| Column | Type | Description | Example |
|---|---|---|---|
| `state`, `state_code`, `district`, `district_note`, `at_large` | as §11.1 | District identity columns, with the semantics of §11.1, repeated on every vintage record. | `Alaska` / `AK` / `AK-AL` / *(empty)* / `True` |
| `map_vintage` | string | Map era averaged over by this record. | `2020s` |
| `is_current_map` | boolean | `True` for the newest vintage of the district. Filtering on this column yields one current-PVI record per district. | `True` |
| `years_used` | string | Presidential years averaged, joined by `+`. | `2024` |
| `n_years` | int | Number of elections in the average; 1, 2, or 3. | `1` |
| `d_share_mean` | decimal | Mean Democratic two-party share across `years_used`. | `43.11` |
| `national_d_share_mean` | decimal | Mean national Democratic two-party share over the same years. | `49.25` |
| `pvi_pct` | decimal | `d_share_mean − national_d_share_mean`. Sign convention: negative values denote a Republican-leaning district. | `-6.14` |
| `pvi_label` | string | Readable label derived from `pvi_pct`, with the sign resolved into the `D`/`R` prefix. | `R+6.1` |
| `total_votes_mean` | int | Mean total votes (two-party plus other) across the averaged years. | `337457` |
| `counties_whole`, `counties_partial` | int | Whole and partial county counts of the district under that vintage, as in §11.1. | `0` / `0` |
| `votes_from_partial_pct_max` | decimal | Maximum `votes_from_partial_pct` among the averaged years; an upper bound on the reliance of the average on area-share allocation. | `0.0` |

## 12. Cross-file relationships

The families are designed to combine on a small number of natural keys.

| Key | Links | Remarks |
|---|---|---|
| `year` + `state_code` + `district` | `house_results_*` ↔ `district_lean_*`, `district_pvi_summary.csv` | The house family stores the district as `"1"` … `"52"` or `"AL"`; the lean families store `{state}-{number}` (e.g. `AK-AL`). Normalisation consists of prefixing the state code. |
| `year` + `state_code` + `county` | `presidential_results_*` ↔ districts | Allocation requires the crosswalk resource `resources/district_counties.json` (outside the CSV scope); this is the join the lean pipeline performs internally. |
| `Year` + `State` (+ `Election`) | within each senate family | Groups records by race; `Election` separates regular from special elections. |
| `chamber` / `level` / `office` | family stacking | `chamber` merges the two state-legislature families; `level` and `office` serve the same purpose for the house and statewide families. |

Formatting differences that must be resolved before joins: senate `votes`/`percentage` retain separators and `%` suffixes; state-legislature `votes` is in decimal string form; the remaining families store numerics in directly parseable form.

## 13. Known limitations

1. **Senate results (§5).** `Total` records are turnout summaries and must be excluded from candidate-level analysis. `votes` and `percentage` retain source formatting. The artefact columns `name` and `pad` occur in a few 2020s files. Column order varies between year files. The `Election` column is the only reliable discriminator between regular and special elections.
2. **House (§7).** The 1978 file is absent from the shipped dataset. Early-decade candidate lists are as complete as the source articles. Multi-seat at-large years contain multiple winners per district.
3. **State legislatures (§8).** `votes` frequently appears in decimal string form; `party` is occasionally empty; multi-member districts elect several winners per district and race.
4. **Statewide (§9).** Territorial records occur for governor; attorney-general, secretary-of-state, and treasurer coverage begins later than gubernatorial coverage.
5. **Presidential (§10).** Third-party candidates are aggregated into a single `Other` record. `percentage` is measured against the subdivision's own total.
6. **Lean and PVI (§11).** Partial-county allocation uses geometric area shares rather than population, so leans of districts containing large shared urban counties are dampened toward the state mean; `votes_from_partial_pct` quantifies this exposure per district. Jurisdictions without subdivision-level source tables — Alaska before 2024 — legitimately contain zero votes and empty share and lean fields. The sign conventions of `lean_pct` (positive = Democratic) and `pvi_pct` (negative = Republican) differ; both columns are accompanied by their respective labels, which resolve any ambiguity.
