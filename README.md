# Keyword Bibliometrics

A configurable, open bibliometric analysis pipeline built on [OpenAlex](https://openalex.org).
Point it at any set of search terms and it produces a full suite of interactive dashboards:
publication trends, country and institution rankings, author networks, study-type breakdown,
journal analysis, a world map, a citation network explorer, and a searchable paper index —
all as self-contained HTML files.

---

## Quick start

```bash
git clone https://github.com/ggkuhnle/upf_bibliography
cd upf_bibliography
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

Edit `config.json` to set your topic, then fetch data and build all dashboards:

```bash
python make_dashboard.py --fetch
```

Output lands in `output/{prefix}/`. Open `output/{prefix}/index.html` in any browser.

---

## Building all topics at once

```bash
./build_all.sh           # build and deploy all topics
./build_all.sh --fetch   # refetch from OpenAlex, then build and deploy all topics
```

Topics are built in the order defined by the `config.json.*` files present, with
`flavonoid` always last (largest dataset). The active `config.json` is restored on exit.

---

## Configuration

All topic-specific settings live in `config.json`:

```json
{
  "title":   "Ultra-Processed Food Research",
  "prefix":  "upf",
  "keywords": [
    "ultra-processed food",
    "ultra-processed foods",
    "ultraprocessed food",
    "NOVA food classification"
  ]
}
```

| Field | Purpose |
|-------|---------|
| `title` | Human-readable label used in dashboard headings |
| `prefix` | Short identifier used as the subfolder name (`output/upf/`, `data/upf/`) |
| `keywords` | Search terms sent to OpenAlex. Multi-word phrases are matched exactly; single words are stemmed. Terms are OR-combined. |

Save per-topic configs and swap as needed:

```bash
cp config.json config.json.upf         # save current topic
cp config.json.flav config.json        # switch to flavanols
python make_dashboard.py --fetch
```

Each topic gets its own isolated subdirectories — `data/{prefix}/` for raw data,
`output/{prefix}/` for generated HTML — so multiple topics coexist without collisions.

---

## Directory layout

```
upf_bibliography/
├── config.json                       # active topic config
├── config.json.upf                   # saved configs for each topic
├── config.json.flav
├── config.json.flavonoid
├── config.json.cocoa
│
├── bibliometrics.py                  # data retrieval from OpenAlex
├── make_dashboard.py                 # main entry point — builds all dashboards
├── make_citation_network.py          # citation network + Cytoscape explorer
├── make_interactive_network.py       # co-authorship network explorer
├── make_world_map.py                 # world map (HTML + PNG)
├── make_study_type_dashboard.py      # study-type charts
├── make_study_type_network.py        # study-type co-authorship network
├── make_journal_dashboard.py         # journal analysis
├── make_paper_dashboard.py           # per-paper analysis and searchable index
│
├── aliases.json                      # author deduplication overrides for OpenAlex
├── build_all.sh                      # build and deploy all topics in one command
├── deploy.sh                         # rsync a single topic's output to the web server
├── server_index.html                 # landing page listing all deployed topics
├── requirements.txt
│
├── data/                             # downloaded data — not git-tracked
│   ├── upf/
│   ├── flavanol/
│   ├── flavonoid/
│   └── cf/
│
└── output/                           # generated HTML and PNG — not git-tracked
    ├── upf/
    ├── flavanol/
    ├── flavonoid/
    └── cf/
```

---

## Dashboards produced

All files are written to `output/{prefix}/`:

| File | Description |
|------|-------------|
| `index.html` | Landing page with live stats and links to all dashboards |
| `dashboard.html` | Main overview: country/institution/author rankings, temporal trends, network metrics, PageRank, author position analysis, study-type breakdown. Includes a searchable/filterable table of all authors with PageRank, degree, betweenness and community. |
| `paper_dashboard.html` | Per-paper analysis: searchable and sortable table of all papers (title, journal, year, citations, study type, open access, DOI), citation distribution, papers by year (OA vs non-OA), study-type and open-access breakdowns. Requires `--fetch` to generate. |
| `citation_network_cytoscape.html` | Citation network explorer: adjustable node count slider (default 500), BFS influence tracing (seed→field / field→seed, depth 0–3), funder highlighting, autocomplete search, shortest-path finder, CSV export. Also exports `author_centrality.csv` with PageRank and betweenness for every author. |
| `network_interactive.html` | Co-authorship network explorer (year slider, author search, community colours) |
| `world_map.html` | Institution-level bubble map; PNG version also written |
| `study_type.html` | Study-type breakdown: donut chart and stacked bar |
| `network_study_type.html` | Co-authorship network with nodes coloured by dominant study type |
| `journal_dashboard.html` | Journal analysis: top journals, impact vs volume, study-type mix, trends |

---

## Scripts

### `make_dashboard.py` — main entry point

Builds the main dashboard and `index.html`. With `--fetch` it first calls `bibliometrics.py`
to download data, then runs all other `make_*.py` scripts automatically.

```bash
python make_dashboard.py              # rebuild HTML from existing data
python make_dashboard.py --fetch      # fetch from OpenAlex then build everything
```

Individual scripts can also be run standalone to regenerate a single dashboard.

---

### `bibliometrics.py` — data retrieval

Queries OpenAlex using the keywords in `config.json`. Handles cursor pagination,
rate limiting, and resumable checkpoints. Writes all CSV tables to `data/{prefix}/`.

```bash
python bibliometrics.py               # fetch into data/{prefix}/
python bibliometrics.py --dry-run     # first 2 pages only (testing)
```

**Outputs include** `papers_detail.csv` (one row per paper, required by the paper dashboard),
all author/institution/country aggregations, citation and co-authorship edge lists, and
funder data.

**Study-type classification** uses three sources in priority order:
1. PubMed MeSH publication-type tags (~65–70 % coverage)
2. OpenAlex work type
3. Title-keyword heuristics

Categories: `RCT`, `Clinical Trial`, `Observational`, `Systematic Review / Meta-analysis`, `Review`, `Other`.

**Author deduplication:** `aliases.json` maps duplicate OpenAlex author IDs to a single
canonical identity, correcting split profiles before analysis.

---

### `make_citation_network.py` — citation network explorer

Builds a Cytoscape.js citation network. Authors are included if they have ≥ 3 papers
**or** ≥ 10 citations (so high-impact single-paper authors are not excluded). A slider
defaults to showing the top 500 nodes but can be increased to the full set.

Features:
- Node sizing by PageRank or in-degree; Louvain community detection
- **Influence explorer**: BFS from an author or funder — `seed → field` (who the seed
  influenced) or `field → seed` (what influenced the seed), depth 0–3, colour-coded
- **Funder highlight**: show all authors funded by a given organisation
- **Shortest path** between any two authors
- Autocomplete on all search inputs (authors with institution, funders with badge)
- CSV export of the visible network
- Exports `author_centrality.csv` with citation-network PageRank, betweenness, in-degree
  and out-degree for every author — used to add a PageRank column to the author search table

---

## Deployment

`deploy.sh` rsyncs the current topic's output to a web server:

```bash
./deploy.sh
```

The prefix is read from `config.json` at deploy time. It copies `output/{prefix}/` to
`~/misc/{prefix}/` on the server and deploys `server_index.html` to `~/misc/index.html`.

To build and deploy all topics in one go:

```bash
./build_all.sh           # build only
./build_all.sh --fetch   # refetch then build and deploy
```

---

## Syncing between machines

`data/` and `output/` are not in git. Use rsync to transfer them between machines:

```bash
rsync -avz --exclude='.git/' --exclude='venv/' \
  ~/Documents/upf_bibliography/ \
  WorkLinux:~/Projects/upf_bibliography/
```

Recreate the venv on a new machine if needed:

```bash
python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt
```

---

## Data source and caveats

- **Source:** [OpenAlex](https://openalex.org) — open, free, no API key required.
- **Coverage:** papers indexed by OpenAlex through the retrieval date; earlier years may be under-represented.
- **Author disambiguation:** as provided by OpenAlex; occasional mis-attribution may occur for common names. Use `aliases.json` to correct known cases.
- **Funding data:** incomplete — many papers carry no recorded funder.
- **PageRank:** computed on the citation network; values sum to 1.0 across all authors so individual values are small — relative ranking is what matters.
- **Study-type classification:** automated; edge cases will be mis-classified.
- **Interpretation:** dashboards present data as retrieved. No editorial interpretation is implied.

---

## Requirements

```
requests
pandas
numpy
networkx
plotly
kaleido
python-louvain
```

Python 3.9 or later. No API key needed for OpenAlex.

---

## Licence

GPL-3.0 — see [LICENSE](LICENSE).

*Analysis by G. Kuhnle · Data: [OpenAlex](https://openalex.org)*
