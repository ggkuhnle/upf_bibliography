# Bibliometrics Pipeline — Full Reference

This document describes every script, configuration option, output file, and
operational detail. For a quick start see [README.md](README.md).

---

## Table of contents

1. [Architecture overview](#1-architecture-overview)
2. [Directory structure](#2-directory-structure)
3. [Configuration](#3-configuration)
4. [Entry-point scripts](#4-entry-point-scripts)
   - 4.1 [make_dashboard.py](#41-make_dashboardpy)
   - 4.2 [make_influence_report.py](#42-make_influence_reportpy)
     - [Paper mode](#paper-mode)
     - [Author mode](#author-mode)
     - [Funder mode](#funder-mode)
     - [Project mode](#project-mode-nct--grant)
     - [Shared options](#shared-options)
   - 4.4 [bibliometrics.py](#44-bibliometricspy)
   - 4.5 [build_all.sh](#45-build_allsh)
   - 4.6 [deploy.sh](#46-deploysh)
5. [Sub-scripts (scripts/)](#5-sub-scripts-scripts)
   - 5.1 [make_citation_network.py](#51-make_citation_networkpy)
   - 5.2 [make_interactive_network.py](#52-make_interactive_networkpy)
   - 5.3 [make_world_map.py](#53-make_world_mappy)
   - 5.4 [make_study_type_dashboard.py](#54-make_study_type_dashboardpy)
   - 5.5 [make_study_type_network.py](#55-make_study_type_networkpy)
   - 5.6 [make_journal_dashboard.py](#56-make_journal_dashboardpy)
   - 5.7 [make_paper_dashboard.py](#57-make_paper_dashboardpy)
   - 5.8 [make_author_report.py](#58-make_author_reportpy)
   - 5.9 [make_network_pdf.py](#59-make_network_pdfpy)
6. [Outputs](#6-outputs)
7. [Data flow](#7-data-flow)
8. [Author deduplication (aliases.json)](#8-author-deduplication-aliasesjson)
9. [Study-type classification](#9-study-type-classification)
10. [Deployment](#10-deployment)
11. [Caches](#11-caches)
12. [Syncing between machines](#12-syncing-between-machines)
13. [Data source and caveats](#13-data-source-and-caveats)
14. [Requirements](#14-requirements)

---

## 1. Architecture overview

```
build_all.sh  ──loops over config/config.json.*──▶  make_dashboard.py
                                                          │
                        ┌─────────────────────────────────┤ --fetch
                        │                                 │
                  bibliometrics.py               sub-scripts (scripts/)
                  (OpenAlex fetch)                    │
                        │                        make_citation_network.py
                        ▼                        make_interactive_network.py
                   data/{prefix}/                make_world_map.py
                   (CSV files)                   make_study_type_*.py
                        │                        make_journal_dashboard.py
                        └─────────────────────▶  make_paper_dashboard.py
                                                          │
                                                          ▼
                                                   output/{prefix}/
                                                   (HTML + PNG)
                                                          │
                                                    deploy.sh
                                                    (rsync to server)

Standalone tools (not called by make_dashboard):
  make_influence_report.py — paper / author / funder / project: corpus + global influence map
  scripts/make_author_report.py  — per-author profile (corpus only)
  scripts/make_network_pdf.py    — print-ready PDF of citation network
```

`make_dashboard.py` is the sole orchestrator for field-level analysis. It calls
each sub-script via `subprocess.run`, so every sub-script can also be invoked
independently to regenerate a single output.

---

## 2. Directory structure

```
upf_bibliography/
│
├── config.json                 # active topic config — written by build_all.sh
│
├── make_dashboard.py           # main entry point
├── make_influence_report.py    # paper or author: corpus network + global influence (standalone)
├── bibliometrics.py            # OpenAlex data retrieval
├── build_all.sh                # multi-topic build + deploy loop
├── deploy.sh                   # rsync one topic to the web server
├── requirements.txt
├── README.md
├── DOCS.md                     # this file
│
├── deploy/                     # ── Deployment assets ───────────────────────
│   └── server_index.html       #   landing page listing all deployed topics
│
├── config/                     # ── Configuration ──────────────────────────
│   ├── config.json.upf         #   ultra-processed food topic
│   ├── config.json.flav        #   flavanol topic
│   ├── config.json.flavonoid   #   flavonoid topic
│   ├── config.json.cocoa       #   cocoa/chocolate topic
│   └── aliases.json            #   author deduplication overrides
│
├── cache/                      # ── Caches (not git-tracked) ───────────────
│   └── influence_cache.sqlite  #   SQLite cache for make_influence_report.py
│
├── scripts/                    # ── Sub-scripts ─────────────────────────────
│   ├── make_citation_network.py
│   ├── make_interactive_network.py
│   ├── make_world_map.py
│   ├── make_study_type_dashboard.py
│   ├── make_study_type_network.py
│   ├── make_journal_dashboard.py
│   ├── make_paper_dashboard.py
│   ├── make_author_report.py
│   ├── make_network_pdf.py
│   └── fix_upf_hardcoded.sh
│
├── data/                       # ── Data (not git-tracked) ──────────────────
│   ├── upf/
│   ├── flavanol/
│   ├── flavonoid/
│   └── cocoa/
│
├── output/                     # ── Outputs (not git-tracked) ───────────────
│   ├── upf/
│   ├── flavanol/
│   ├── flavonoid/
│   └── cocoa/
│
├── reports/                    # Influence reports — selectively committed to git
│   ├── authors/{slug}/         #   author_corpus.html, author_influence.html
│   ├── funders/{slug}/         #   funder_influence.html, funder_corpus.html
│   └── projects/{slug}/        #   project_influence.html, project_corpus.html
│
└── notebooks/                  # Jupyter notebooks for exploratory work
```

---

## 3. Configuration

### Active config: `config.json`

The file read at runtime by every script. `build_all.sh` copies a saved topic
config here before each build and restores the original on exit.

```json
{
  "title":    "Ultra-Processed Food Research",
  "prefix":   "upf",
  "keywords": [
    "ultra-processed food",
    "ultra-processed foods",
    "ultraprocessed food",
    "NOVA food classification"
  ],
  "focal_doi": "10.1038/s41591-019-0675-0"
}
```

| Field | Required | Purpose |
|-------|----------|---------|
| `title` | yes | Human-readable heading used in dashboards and the index page |
| `prefix` | yes | Short slug — controls subfolder names (`data/upf/`, `output/upf/`) and output file prefixes |
| `keywords` | yes | OR-combined search terms sent to OpenAlex. Multi-word phrases match exactly; single words are stemmed. |
| `focal_doi` | no | DOI of a focal paper — used only by `make_influence_report.py`; ignored by `make_dashboard.py` |
| `subclasses` | no | Dict of sub-topic keyword lists for finer study-type grouping |

### Saved topic configs: `config/config.json.*`

One file per topic. The file extension (e.g. `.upf`, `.flav`) has no semantic
meaning — it is just a naming convention.

To switch topics manually:

```bash
cp config/config.json.flav config.json
python make_dashboard.py --fetch
```

To add a new topic, create a new file and run `build_all.sh`:

```bash
cp config.json config/config.json.newtopic   # edit as needed
./build_all.sh
```

### Author aliases: `config/aliases.json`

Maps duplicate OpenAlex author IDs to a canonical identity. See
[section 8](#8-author-deduplication-aliasesjson) for format details.

---

## 4. Entry-point scripts

### 4.1 `make_dashboard.py`

**Purpose:** Orchestrates the full field-level build. Reads `config.json`,
optionally fetches data, runs all sub-scripts, then builds `dashboard.html`
and `index.html`.

```bash
python make_dashboard.py                    # rebuild from existing CSVs
python make_dashboard.py --fetch            # fetch from OpenAlex, then rebuild
python make_dashboard.py --output-dir path  # write HTML to a custom directory
python make_dashboard.py --data-dir path    # read CSVs from a custom directory
```

| Flag | Default | Description |
|------|---------|-------------|
| `--fetch` | off | Run `bibliometrics.py` first to download/update data |
| `--output-dir` | `output/{prefix}` | Directory for generated HTML and PNG |
| `--data-dir` | `data/{prefix}` | Directory containing CSV data files |

**What it builds:**

1. Runs `bibliometrics.py` (if `--fetch`)
2. Runs each sub-script in `scripts/` sequentially via subprocess
3. Builds `dashboard.html` — main analysis page with charts and author table
4. Builds `index.html` — topic landing page with links to all dashboards

**Key constants** (edit in source to tune):

| Constant | Default | Description |
|----------|---------|-------------|
| `MIN_PAPERS` | 3 | Minimum papers for an author to appear in author table |
| `MIN_EDGE_WEIGHT` | 2 | Minimum shared papers to show a co-authorship edge |
| `TOP_N_RANKING` | 25 | Rows shown in country/institution rankings |
| `TOP_N_LABELS` | 30 | Labelled nodes in static network chart |

---

### 4.2 `make_influence_report.py`

**Purpose:** Influence analysis for a **paper**, **author**, **funder**, or **project**
(clinical trial / research grant), combining two complementary views:

- **Corpus report** — shows the focal subject inside the downloaded field corpus (reads local
  CSV data from `bibliometrics.py`). Fast; no network needed after the initial fetch.
- **Influence map** — crawls OpenAlex live to map global citation influence beyond the local
  corpus. Results are cached in `cache/influence_cache.sqlite`.

Both sections run by default. Use `--no-corpus` or `--no-influence` to skip one.

**Interactive graph features (all modes):**

- Year-range slider to filter nodes by publication year
- Layer toggles to show/hide citing papers (n=1), cited papers, and second-hop papers (n=2)
- Hover to reveal node labels
- Single click on a node highlights its direct neighbourhood; background click resets
- Detail panel shows title, year, journal, and citation count for the selected node

#### Paper mode

```bash
# Both reports (corpus + influence)
python make_influence_report.py --doi "10.1093/ajcn/nqac055" --data-dir data/flavanol

# Title search in corpus, then influence map using the found DOI
python make_influence_report.py --paper "Zutphen" --data-dir data/flavonoids

# Influence map only (no local corpus needed)
python make_influence_report.py --doi "10.1093/ajcn/nqac055" --no-corpus

# Corpus report only (no network crawl)
python make_influence_report.py --paper "..." --data-dir data/upf --no-influence
```

**Paper identification** (mutually exclusive):

| Flag | Description |
|------|-------------|
| `--doi` | DOI — used for both corpus lookup and OpenAlex crawl |
| `--paper` | Partial title substring — corpus lookup only; DOI is then read from the corpus row |
| `--paper-id` | Full OpenAlex work URL |

**Paper mode outputs** (written to `reports/{prefix}/papers/{slug}/`):

| File | Description |
|------|-------------|
| `report.html` | Compact corpus graph — top 300 nodes, ego-adjacent. Fast to load. |
| `report_full.html` | Full corpus graph — up to `--corpus-size` nodes. |
| `influence.html` | Global paper influence map. Role-coloured nodes, year slider. |

**Paper influence node roles:**

| Role | Colour | Meaning |
|------|--------|---------|
| `focal` | red `#EF553B` | The focal paper |
| `cites_focal` | purple `#AB63FA` | Papers that cite the focal work |
| `cited_by_focal` | teal `#00CC96` | References of the focal work |
| `d2` | grey `#b0bec5` | Second-hop nodes |

#### Author mode

```bash
# Both author corpus + global influence
python make_influence_report.py --author "Monteiro" --data-dir data/upf

# Using an OpenAlex author ID
python make_influence_report.py --author-id "https://openalex.org/A2149006804" --data-dir data/upf

# Global influence only (no local corpus needed)
python make_influence_report.py --author "Hollman" --no-corpus
```

**Author identification** (mutually exclusive):

| Flag | Description |
|------|-------------|
| `--author` | Author name to search in OpenAlex (also used for corpus lookup) |
| `--author-id` | Full OpenAlex author URL — bypasses name search |

**Author mode outputs** (written to `reports/{prefix}/authors/{slug}/`):

| File | Description |
|------|-------------|
| `author_corpus.html` | Author's papers inside the field corpus, with star focal nodes. |
| `author_influence.html` | Global author influence map — all author papers + their citation neighbourhood. |

**Author influence node roles:**

| Role | Colour | Shape | Meaning |
|------|--------|-------|---------|
| `author_paper` | red `#EF553B` | star | One of the author's own papers |
| `cites_author` | purple `#AB63FA` | circle | Papers that cite any author paper |
| `cited_by_author` | teal `#00CC96` | circle | Papers cited by any author paper |
| `d2` | grey `#b0bec5` | circle | Second-hop citers (layer 2 forward) |

#### Funder mode

```bash
# Funder influence map by name (searches OpenAlex funders)
python make_influence_report.py --funder "Mars" --prefix flavanol

# Using a full OpenAlex funder URL
python make_influence_report.py --funder-id "https://openalex.org/F4320321001" --prefix flavanol

# With an optional corpus to embed the funder papers in
python make_influence_report.py --funder "Mars" --prefix flavanol --data-dir data/flavanol
```

**Funder identification** (mutually exclusive):

| Flag | Description |
|------|-------------|
| `--funder` | Funder name to search in OpenAlex |
| `--funder-id` | Full OpenAlex funder URL — bypasses name search |

**Funder mode outputs** (written to `reports/{prefix}/funders/{slug}/`):

| File | Description |
|------|-------------|
| `funder_corpus.html` | Funder papers inside the field corpus, with star focal nodes. |
| `funder_influence.html` | Global funder influence map — all funder papers + citation neighbourhood. |

**Funder influence node roles:**

| Role | Colour | Shape | Meaning |
|------|--------|-------|---------|
| `author_paper` | red `#EF553B` | star | Paper funded by the focal funder |
| `cites_author` | purple `#AB63FA` | circle | Papers that cite any funder paper |
| `cited_by_author` | teal `#00CC96` | circle | Papers cited by any funder paper |
| `d2` | grey `#b0bec5` | circle | Second-hop citers |

#### Project mode (NCT / grant)

Project mode maps citation influence around a clinical trial or research grant. It combines
two data sources:

1. **ClinicalTrials.gov** — fetches the official linked publications (PMIDs) from the NCT
   record, then resolves them to OpenAlex works.
2. **OpenAlex full-text search** — searches OpenAlex for papers that mention the NCT ID
   in their title or abstract, catching papers not listed in ClinicalTrials.gov.

For grant-funded projects, OpenAlex is queried using `awards.funder_award_id`.

```bash
# Clinical trial
python make_influence_report.py --nct NCT02422745

# Research grant
python make_influence_report.py --award-id 312090 --prefix flavanol

# Combine NCT and award into one project
python make_influence_report.py --nct NCT01799005 --also-award-id 312090 --prefix flavanol

# Combine award with a second NCT
python make_influence_report.py --award-id 312090 --also-nct NCT02422745 --prefix flavanol

# Embed project papers in an existing corpus
python make_influence_report.py --nct NCT02422745 --prefix flavanol --data-dir data/flavanol
```

**Project identification flags:**

| Flag | Description |
|------|-------------|
| `--nct` | ClinicalTrials.gov NCT ID (e.g. `NCT02422745`) — primary source |
| `--award-id` | OpenAlex funder award ID — primary source |
| `--also-nct` | Additional NCT ID to merge with the primary source |
| `--also-award-id` | Additional award ID to merge with the primary source |

**Project mode outputs** (written to `reports/projects/{slug}/`):

| File | Description |
|------|-------------|
| `project_corpus.html` | Project papers inside the field corpus (requires `--data-dir`). |
| `project_influence.html` | Global project influence map — project papers + citation neighbourhood. |

**Project influence node roles:**

| Role | Colour | Shape | Meaning |
|------|--------|-------|---------|
| `project_paper` | red `#EF553B` | star | Paper belonging to the project |
| `cited_by_project` | teal `#00CC96` | circle | Papers cited by project papers (references) |
| `cites_project` | purple `#AB63FA` | circle | Papers that cite project papers (n=1) |
| `d2` | grey `#b0bec5` | circle | Second-hop citers (n=2) |

The project influence map uses a **light design** (white/grey background) to visually
distinguish it from the dark-design author and funder maps.

Layer order in the legend: cited (references) → citing n=1 → citing n=2.

#### Shared options

**Corpus options:**

| Flag | Default | Description |
|------|---------|-------------|
| `--data-dir` | `data/{prefix}` | Directory containing CSV data (`bibliometrics.py` output) |
| `--prefix` | from config | Topic prefix |
| `--corpus-size` | 2000 | Max corpus nodes in the full graph |
| `--max-ego` | 80 | Max direct neighbours shown |
| `--no-llm` | off | Skip Ollama LLM commentary (paper mode only) |
| `--llm-model` | `llama3.1` | Ollama model name |
| `--no-corpus` | off | Skip corpus report entirely |

**Influence / crawl options:**

| Flag | Default | Description |
|------|---------|-------------|
| `--depth` | 2 | Crawl hops in each direction |
| `--max-nodes` | 300 | Maximum nodes in trimmed output graph (paper mode only) |
| `--max-cites` | 100 | Maximum citers fetched per focal node |
| `--max-refs` | 50 | Maximum references fetched per focal node |
| `--max-works` | 200 | Maximum author/funder works fetched from OpenAlex |
| `--d2-seeds` | 15 | Number of top layer-1 citers used as seeds for the layer-2 forward pass |
| `--cache` | `cache/influence_cache.sqlite` | SQLite cache file |
| `--no-influence` | off | Skip influence map entirely |
| `--output-dir` | auto | Override output directory |

> **Note on graph size:** In author, funder, and project modes the graph is not post-hoc
> trimmed — `--max-cites` and `--max-refs` control volume directly at crawl time.
> Reduce these flags (or `--d2-seeds`) if the output HTML is too large to load comfortably.
> Clearing `cache/influence_cache.sqlite` is only needed when you want to re-fetch data
> that has been updated upstream in OpenAlex, not when changing these flags.

---

### 4.4 `bibliometrics.py`

**Purpose:** Queries OpenAlex and writes all CSV data files used by the
dashboard scripts. Called automatically by `make_dashboard.py --fetch`, but
can also be run standalone.

```bash
python bibliometrics.py                    # fetch into data/{prefix}/
python bibliometrics.py --dry-run          # first 2 pages only (for testing)
python bibliometrics.py --output-dir path  # write CSVs to custom directory
```

| Flag | Default | Description |
|------|---------|-------------|
| `--output-dir` | `data/{prefix}` | Where to write CSV outputs |
| `--dry-run` | off | Fetch only the first 2 pages (quick smoke-test) |

**Outputs** (all written to `--output-dir`):

| File | Description |
|------|-------------|
| `papers_detail.csv` | One row per paper: title, DOI, journal, year, citations, study type, open-access flag, authors, institutions, countries, funders |
| `papers_by_author.csv` | Per-author paper count and total citations |
| `papers_by_institution.csv` | Per-institution paper count |
| `papers_by_country.csv` | Per-country paper count |
| `papers_by_year.csv` | Paper count by year |
| `citation_edges.csv` | Paper-level citation edges (source DOI → target DOI) |
| `citation_edges_author.csv` | Author-level citation edges with weight |
| `coauthor_edges.csv` | Co-authorship edges with weight |
| `author_centrality.csv` | PageRank, betweenness, in-degree, out-degree for every author (written by `make_citation_network.py`, not by bibliometrics) |
| `institutions_by_country.csv` | Institution + country lookup |
| `funder_by_author.csv` | Funder strings per author |
| `funding_by_country.csv` | Funder strings per country |
| `country_year.csv` | Country × year paper counts |
| `study_type_by_year.csv` | Study-type × year paper counts |
| `study_type_by_author.csv` | Study-type distribution per author |

**Study-type classification** uses three sources in priority order:
1. PubMed MeSH publication-type tags (~65–70 % coverage)
2. OpenAlex `type` field
3. Title-keyword heuristics

Categories: `RCT`, `Clinical Trial`, `Observational`, `Systematic Review / Meta-analysis`, `Review`, `Other`.

**Author deduplication:** `config/aliases.json` is loaded at startup; duplicate
author IDs are merged before any aggregation.

---

### 4.5 `build_all.sh`

**Purpose:** Loops over every saved topic config in `config/`, swaps it into
`config.json`, runs `make_dashboard.py`, then calls `deploy.sh`. Flavonoid is
always built last (it is the largest dataset).

```bash
./build_all.sh           # build and deploy all topics
./build_all.sh --fetch   # refetch from OpenAlex before building each topic
```

| Flag | Description |
|------|-------------|
| `--fetch` | Passed through to `make_dashboard.py` — triggers a data fetch for each topic |

The original `config.json` is restored on exit (even if the script fails), via
a `trap restore_config EXIT` handler.

---

### 4.6 `deploy.sh`

**Purpose:** Rsyncs a single topic's output directory to the web server and
updates the shared landing page.

```bash
./deploy.sh
```

Reads `config.json` to determine the prefix. Copies:
- `output/{prefix}/` → `gunter@kuhnle.co.uk:~/misc/{prefix}/`
- `server_index.html` → `gunter@kuhnle.co.uk:~/misc/index.html`

---

## 5. Sub-scripts (`scripts/`)

All sub-scripts accept `--output-dir` and `--data-dir` and can be run
standalone. When called by `make_dashboard.py` they inherit the same
output/data directories.

---

### 5.1 `make_citation_network.py`

Builds the Cytoscape.js citation network explorer.

**Inclusion threshold:** authors with ≥ 3 papers **or** ≥ 10 citations.

**Outputs:**

| File | Description |
|------|-------------|
| `citation_network_cytoscape.html` | Top-200 nodes — fast load, default view |
| `citation_network_cytoscape_full.html` | All nodes — slider adjustable, with caveat banner |
| `author_centrality.csv` | PageRank, betweenness, in-degree, out-degree for every author |

**Browser controls:**

| Feature | Description |
|---------|-------------|
| Node slider | Show top N nodes by PageRank (default 200, up to full set) |
| Influence explorer | BFS from an author/funder — `seed→field` (downstream) or `field→seed` (upstream), depth 0–3 |
| Funder highlight | Colour all authors funded by a given organisation |
| Shortest path | Find shortest path between any two authors |
| Autocomplete | On all three search inputs |
| CSV export | Download the currently visible network |
| Community colours | Louvain community detection; toggle on/off |

Node size is proportional to PageRank. Edge thickness is proportional to
shared-paper count.

---

### 5.2 `make_interactive_network.py`

Builds an interactive co-authorship network explorer.

**Output:** `network_interactive.html`

Features: year slider to filter papers, author search, community colour coding.

---

### 5.3 `make_world_map.py`

Builds an institution-level bubble map.

**Outputs:** `world_map.html`, `world_map.png`

Bubble size = number of papers from that institution.

---

### 5.4 `make_study_type_dashboard.py`

**Output:** `study_type.html`

Charts: donut chart (overall study-type breakdown) and stacked bar chart
(study-type mix by year).

---

### 5.5 `make_study_type_network.py`

**Output:** `network_study_type.html`

Co-authorship network where node colour reflects the author's dominant study
type across all their papers.

---

### 5.6 `make_journal_dashboard.py`

**Output:** `journal_dashboard.html`

Charts: top journals by paper count, citation impact vs volume scatter,
study-type mix per journal, publication trends for top journals.

---

### 5.7 `make_paper_dashboard.py`

**Output:** `paper_dashboard.html`

A searchable and sortable table of all papers (title, journal, year, citations,
study type, open access, DOI), plus citation distribution histogram, papers by
year (OA vs non-OA), and study-type / OA breakdowns.

Requires `papers_detail.csv` from `bibliometrics.py --fetch`.

---

### 5.8 `make_author_report.py`

**Purpose:** Generates a bespoke HTML profile for a single author, drawing
on all available CSVs. No re-fetching needed. Optionally generates a short
narrative summary via a local Ollama LLM (gracefully skipped if Ollama is
not running).

```bash
python scripts/make_author_report.py --author "Hollman"
python scripts/make_author_report.py --author-id "https://openalex.org/A5021449136"
python scripts/make_author_report.py --author "Crozier" --prefix flavonoid --top-coauthors 20
python scripts/make_author_report.py --author "Schroeter" --no-llm
```

| Flag | Default | Description |
|------|---------|-------------|
| `--author` | — | Partial name match |
| `--author-id` | — | Full OpenAlex author URL |
| `--prefix` | from config | Topic prefix to locate data and output directories |
| `--top-coauthors` | 15 | Number of top co-authors to feature |
| `--no-llm` | off | Skip Ollama narrative generation |
| `--output-dir` | `reports/{prefix}/{author}/` | Where to write the HTML report |

**Output:** `report.html` — a self-contained author profile with publication
list, citation trends, co-authorship network, study-type breakdown, and
(optionally) an AI-generated narrative.

---

### 5.9 `make_network_pdf.py`

**Purpose:** Generates a static, print-ready PDF of the citation network.
Not called by `make_dashboard.py` — run manually when a printable version
is needed.

```bash
python scripts/make_network_pdf.py
python scripts/make_network_pdf.py --prefix flavonoid --top-nodes 400 --top-labels 40
python scripts/make_network_pdf.py --data-dir data/upf --prefix upf --output upf_net.pdf
```

| Flag | Default | Description |
|------|---------|-------------|
| `--prefix` | from config | Topic prefix |
| `--data-dir` | `data/{prefix}` | Source CSV directory |
| `--top-nodes` | 300 | Maximum nodes to render |
| `--top-labels` | 30 | Number of labelled nodes |
| `--output` | `{prefix}_citation_network.pdf` | Output file path |

**Output:** A PDF using matplotlib/networkx with a spring-force layout,
nodes sized by PageRank, coloured by Louvain community.

---

## 6. Outputs

All HTML outputs are fully self-contained (CSS, JavaScript, and data embedded
inline) — no server required; open directly in any browser.

**Per-topic output directory** (`output/{prefix}/`):

| File | Produced by | Description |
|------|-------------|-------------|
| `index.html` | `make_dashboard.py` | Topic landing page with live stats and links |
| `dashboard.html` | `make_dashboard.py` | Main analysis: trends, rankings, author table |
| `paper_dashboard.html` | `make_paper_dashboard.py` | Searchable paper index |
| `citation_network_cytoscape.html` | `make_citation_network.py` | Citation network (top 200) |
| `citation_network_cytoscape_full.html` | `make_citation_network.py` | Citation network (full) |
| `network_interactive.html` | `make_interactive_network.py` | Co-authorship network |
| `world_map.html` | `make_world_map.py` | Institution bubble map |
| `world_map.png` | `make_world_map.py` | Static PNG of world map |
| `study_type.html` | `make_study_type_dashboard.py` | Study-type breakdown |
| `network_study_type.html` | `make_study_type_network.py` | Study-type co-authorship network |
| `journal_dashboard.html` | `make_journal_dashboard.py` | Journal analysis |
| `author_centrality.csv` | `make_citation_network.py` | Author PageRank/betweenness table |

**Standalone tools** — output directories and files:

| Output directory | File | Mode | Description |
|-----------------|------|------|-------------|
| `reports/{prefix}/papers/{slug}/` | `report.html` | paper | Compact corpus graph (top 300 nodes, ego-adjacent) |
| `reports/{prefix}/papers/{slug}/` | `report_full.html` | paper | Full corpus graph (up to `--corpus-size` nodes) |
| `reports/{prefix}/papers/{slug}/` | `influence.html` | paper | Global paper influence map |
| `reports/{prefix}/authors/{slug}/` | `author_corpus.html` | author | Author's papers inside the field corpus |
| `reports/{prefix}/authors/{slug}/` | `author_influence.html` | author | Global author influence map |
| `reports/{prefix}/funders/{slug}/` | `funder_corpus.html` | funder | Funder papers inside the field corpus |
| `reports/{prefix}/funders/{slug}/` | `funder_influence.html` | funder | Global funder influence map |
| `reports/projects/{slug}/` | `project_corpus.html` | project | Project papers inside the field corpus |
| `reports/projects/{slug}/` | `project_influence.html` | project | Global project influence map |
| `reports/{prefix}/{author}/` | `report.html` | — | Author profile from `make_author_report.py` (corpus only) |

The `{slug}` is derived from the subject name or ID (lowercased, spaces replaced with
underscores). Project slugs use the NCT ID or award ID directly (e.g. `nct02422745`,
`312090`).

---

## 7. Data flow

```
OpenAlex API
     │
     ▼
bibliometrics.py
     │
     ├──▶ data/{prefix}/papers_detail.csv
     ├──▶ data/{prefix}/citation_edges.csv
     ├──▶ data/{prefix}/citation_edges_author.csv
     ├──▶ data/{prefix}/coauthor_edges.csv
     ├──▶ data/{prefix}/papers_by_{author,institution,country,year}.csv
     ├──▶ data/{prefix}/country_year.csv
     ├──▶ data/{prefix}/study_type_by_{year,author}.csv
     ├──▶ data/{prefix}/funder_by_author.csv
     └──▶ data/{prefix}/funding_by_country.csv

data/{prefix}/*.csv
     │
     ├──▶ make_citation_network.py  ──▶ output/{prefix}/citation_network_cytoscape*.html
     │                                   output/{prefix}/author_centrality.csv
     ├──▶ make_interactive_network.py ──▶ output/{prefix}/network_interactive.html
     ├──▶ make_world_map.py           ──▶ output/{prefix}/world_map.{html,png}
     ├──▶ make_study_type_*.py        ──▶ output/{prefix}/study_type*.html
     ├──▶ make_journal_dashboard.py   ──▶ output/{prefix}/journal_dashboard.html
     ├──▶ make_paper_dashboard.py     ──▶ output/{prefix}/paper_dashboard.html
     └──▶ make_dashboard.py           ──▶ output/{prefix}/{index,dashboard}.html
```

---

## 8. Author deduplication (`aliases.json`)

OpenAlex occasionally creates multiple profile IDs for the same person (e.g.
due to name spelling variants). `config/aliases.json` lets you merge them
before any analysis:

```json
{
  "aliases": [
    {
      "canonical_id":   "https://openalex.org/A5021449136",
      "canonical_name": "P.C.H. Hollman",
      "duplicates": [
        "https://openalex.org/A5000123456",
        "https://openalex.org/A5009876543"
      ]
    }
  ]
}
```

`bibliometrics.py` loads this file at startup and replaces every duplicate ID
with the canonical ID before writing any CSV. All downstream scripts therefore
see only the canonical identity.

To add a new alias: find both OpenAlex author URLs, decide which is canonical,
and add an entry. The file is loaded fresh on each run — no rebuild of data is
needed beyond re-running `bibliometrics.py`.

---

## 9. Study-type classification

Each paper is classified into one of six categories:

| Category | Description |
|----------|-------------|
| `RCT` | Randomised controlled trial |
| `Clinical Trial` | Non-randomised interventional study |
| `Observational` | Cohort, case-control, cross-sectional, or other observational design |
| `Systematic Review / Meta-analysis` | Systematic review or meta-analysis |
| `Review` | Narrative review or commentary |
| `Other` | Editorials, letters, protocols, unclassifiable |

Classification uses three sources in priority order:

1. **PubMed MeSH publication-type tags** — fetched from the E-utilities API
   using the paper's PMID. Covers ~65–70 % of the corpus. Highest precision.
2. **OpenAlex `type` field** — covers most papers; lower granularity.
3. **Title-keyword heuristics** — fallback regex matching on title words such
   as "randomized", "cohort", "meta-analysis".

The final label is stored in `papers_detail.csv` and used by all dashboards.
Edge cases (e.g. "randomized crossover pilot study") may be mis-classified;
the automated labels should be treated as indicative.

---

## 10. Deployment

### Single topic

```bash
./deploy.sh
```

Reads `prefix` from `config.json`. Uses `rsync` over SSH:

```
output/{prefix}/          →  gunter@kuhnle.co.uk:~/misc/{prefix}/
deploy/server_index.html  →  gunter@kuhnle.co.uk:~/misc/index.html
```

### All topics

```bash
./build_all.sh           # build then deploy each topic
./build_all.sh --fetch   # fetch, build, then deploy each topic
```

Each topic is built and deployed before moving to the next. Build order:
all topics alphabetically, with `flavonoid` last.

### Server landing page

`deploy/server_index.html` is a static HTML page listing all deployed topics
with links. It is overwritten each time any topic is deployed, so it should
list all topics — not just the one currently being built.

---

## 11. Caches

### `cache/influence_cache.sqlite`

Used by `make_influence_report.py`. Stores raw OpenAlex API responses
as JSON blobs, keyed by work ID. This avoids re-fetching large citation
neighbourhoods on every run. Author works are cached alongside paper works.

The cache grows over time (roughly ~10 MB per few hundred papers crawled). To clear it:

```bash
rm cache/influence_cache.sqlite
```

The file will be recreated on the next run of `make_influence_report.py`.

---

## 12. Syncing between machines

`data/` and `output/` are excluded from git. Use rsync to transfer them:

```bash
# Push to another machine
rsync -avz --exclude='.git/' --exclude='venv/' \
  ~/Documents/upf_bibliography/ \
  WorkLinux:~/Projects/upf_bibliography/

# Pull from another machine
rsync -avz WorkLinux:~/Projects/upf_bibliography/data/ \
  ~/Documents/upf_bibliography/data/
```

To recreate the Python environment on a new machine:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## 13. Data source and caveats

- **Source:** [OpenAlex](https://openalex.org) — open, free, no API key
  required. A `mailto` parameter is sent with every request as a courtesy to
  the OpenAlex team.
- **Coverage:** papers indexed by OpenAlex through the retrieval date. Earlier
  years (pre-2000) may be under-represented.
- **Author disambiguation:** performed by OpenAlex; occasional mis-attribution
  occurs for common names or authors who changed institution. Use
  `config/aliases.json` to correct known cases.
- **Funding data:** incomplete — many papers carry no recorded funder. The
  funder highlight feature will show "unknown" for such authors.
- **Citation counts:** as reported by OpenAlex at retrieval time; will differ
  from Web of Science or Scopus counts.
- **PageRank:** computed on the author-level citation network within the
  retrieved corpus. Values sum to 1.0 — individual values are small; relative
  ranking is what matters.
- **Study-type classification:** automated; edge cases will be mis-classified.
  Do not use category counts as exact figures.
- **Interpretation:** dashboards present data as retrieved. No editorial
  interpretation is implied.

---

## 14. Requirements

```
requests
pandas
numpy
networkx
plotly
kaleido
python-louvain
```

Python 3.9 or later. No external API key is required.

Optional: [Ollama](https://ollama.ai) running locally with a model such as
`llama3.1`, used only by `make_author_report.py` for narrative generation.
The script runs normally without it.

Install:

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

---

*Analysis by G. Kuhnle · Data: [OpenAlex](https://openalex.org)*
