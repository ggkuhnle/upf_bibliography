# Keyword Bibliometrics

A configurable, open bibliometric analysis pipeline built on [OpenAlex](https://openalex.org).
Point it at any set of search terms and it produces a full suite of interactive dashboards:
publication trends, country and institution rankings, author networks, study-type breakdown,
journal analysis, a world map, and a citation network explorer — all as self-contained HTML files.

**→ [Live example: Ultra-Processed Food Research](https://ggkuhnle.github.io/upf_bibliography/)**

---

## Quick start

```bash
git clone https://github.com/ggkuhnle/upf_bibliography
cd upf_bibliography
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

Edit `config.json` to set your topic, then run:

```bash
python make_dashboard.py --fetch
```

`--fetch` retrieves data from OpenAlex before building all dashboards.
Omit it on subsequent runs to rebuild the HTML from already-downloaded data.

Output lands in `output/{prefix}/index.html`.

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
| `prefix` | Short identifier used as the output subfolder name (`output/upf/`, `data/upf/`) |
| `keywords` | Search terms sent to OpenAlex `title_and_abstract.search`. Multi-word phrases are matched exactly; single words are stemmed by OpenAlex. Terms are OR-combined. |

### Multi-topic use

Save per-topic configs alongside `config.json` and swap as needed:

```bash
cp config.json config.json.upf         # save current topic
cp config.json.flav config.json        # switch to flavanols
python make_dashboard.py --fetch
```

Each topic gets its own isolated subdirectory — `data/flavanol/` for raw data,
`output/flavanol/` for generated HTML — so multiple topics coexist without collisions.

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
│
├── deploy.sh                         # rsync to web server
├── server_index.html                 # landing page for all topics on the server
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
| `dashboard.html` | Main overview: country/institution/author rankings, temporal trends, network metrics, author position analysis, study-type breakdown |
| `citation_network_cytoscape.html` | Citation network explorer: BFS influence tracing, funder highlighting, author/funder autocomplete search, shortest-path finder |
| `network_interactive.html` | Interactive co-authorship network (year slider, author search, community colours) |
| `world_map.html` | Institution-level bubble map; PNG version also written |
| `study_type.html` | Study-type breakdown: donut chart and stacked bar (2000–present) |
| `network_study_type.html` | Co-authorship network with nodes coloured by dominant study type |
| `journal_dashboard.html` | Journal analysis: top journals, impact vs volume, study-type mix, publication trends |

---

## Scripts

### `make_dashboard.py` — main entry point

Builds the main dashboard and `index.html`. With `--fetch` it first calls `bibliometrics.py`
to download data, then runs all other `make_*.py` scripts automatically.

```bash
python make_dashboard.py              # rebuild HTML from existing data
python make_dashboard.py --fetch      # fetch data then build everything
```

All other `make_*.py` scripts can also be run individually if you only need to regenerate one dashboard.

---

### `bibliometrics.py` — data retrieval

Queries OpenAlex using the keywords from `config.json`. Handles cursor pagination,
rate limiting, and retries. Writes all CSV tables to `data/{prefix}/`.

```bash
python bibliometrics.py               # default: data/{prefix}/
python bibliometrics.py --dry-run     # first 2 pages only (testing)
```

**Outputs** (written to `data/{prefix}/`):

| CSV | Contents |
|-----|----------|
| `papers_by_year.csv` | Annual paper counts |
| `papers_by_country.csv` | Papers and citations per country |
| `papers_by_country_year.csv` | Country × year matrix |
| `papers_by_institution.csv` | Papers and citations per institution |
| `papers_by_author.csv` | Per-author counts, institution, author-position breakdown |
| `papers_by_author_study_type.csv` | Per-author breakdown by study type |
| `papers_by_journal.csv` | Papers and citations per journal |
| `papers_by_journal_year.csv` | Journal × year matrix |
| `papers_by_journal_study_type.csv` | Journal × study-type matrix |
| `papers_by_study_type.csv` | Overall study-type distribution |
| `papers_by_study_type_year.csv` | Study-type × year matrix |
| `papers_by_funder.csv` | Funder names and paper counts |
| `funding_by_country.csv` | Funding coverage per country |
| `papers_by_department.csv` | Departmental affiliation strings (experimental) |
| `coauthorship_edges.csv` | All-time co-authorship edge list |
| `coauthorship_edges_by_year.csv` | Edge list with year annotation |
| `coauthorship_edges_primary.csv` | First/last-author-only edges |
| `coauthorship_edges_by_year_primary.csv` | Primary edges with year annotation |
| `network_metrics_by_year.csv` | Annual network size, density, clustering |
| `citation_edges_author.csv` | Author-level citation graph |
| `funders_by_author.csv` | Per-author funder strings |

**Study-type classification** uses three sources in priority order:
1. PubMed MeSH publication-type tags (most reliable; covers ~65–70 % of papers)
2. OpenAlex work type
3. Title-keyword heuristics

Categories: `RCT`, `Clinical Trial`, `Observational`, `Systematic Review / Meta-analysis`, `Review`, `Other`.

---

### `make_citation_network.py` — citation network explorer

Builds a Cytoscape.js-based citation network with:
- Node sizing by PageRank or in-degree; community detection
- **Influence explorer**: enter an author or funder, BFS traces downstream (who they influenced) or upstream (who influenced them) up to 3 hops, colour-coded by direction and depth
- **Funder highlight**: highlight all authors funded by a given organisation
- **Shortest path** between any two authors
- Autocomplete on all search boxes (authors with institution sub-label; funders with badge)
- CSV export of the visible network

---

## Deployment

`deploy.sh` rsyncs the current topic's output to a web server:

```bash
./deploy.sh
```

The prefix is read from `config.json` automatically. It deploys `output/{prefix}/` to
`~/misc/{prefix}/` on the server, and `server_index.html` to `~/misc/index.html`.

`server_index.html` is a landing page linking to all deployed topics.

---

## Syncing between machines

Since `data/` and `output/` are not in git, use rsync to transfer them:

```bash
rsync -avz --exclude='.git/' --exclude='venv/' \
  ~/Documents/upf_bibliography/ \
  WorkLinux:~/Projects/upf_bibliography/
```

On the receiving machine, recreate the venv if needed:

```bash
python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt
```

---

## Data source and caveats

- **Source:** [OpenAlex](https://openalex.org) — open, free, no API key required. All requests include a `mailto` parameter for polite usage.
- **Coverage:** papers indexed by OpenAlex through the retrieval date; earlier years may be under-represented for some topics.
- **Author disambiguation:** as provided by OpenAlex; occasional mis-attribution may occur, particularly for authors with common names.
- **Institution assignment:** most-frequent affiliation across all of an author's papers; still imperfect when an author has moved institutions.
- **Funding data:** incomplete — many papers carry no recorded funder.
- **Study-type classification:** automated and based on MeSH tags and title heuristics; edge cases will be mis-classified.
- **Interpretation:** dashboards present data as retrieved. No editorial interpretation is implied.

---

## Requirements

```
requests
pandas
matplotlib
networkx
plotly
kaleido
scipy
```

Python 3.9 or later. No API key is needed for OpenAlex.

---

## Licence

GPL-3.0 — see [LICENSE](LICENSE).

*Analysis by G. Kuhnle · Data: [OpenAlex](https://openalex.org)*
