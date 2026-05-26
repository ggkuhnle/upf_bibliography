# Keyword Bibliometrics

A configurable, open bibliometric analysis pipeline built on [OpenAlex](https://openalex.org).
Point it at any set of search terms and it produces a full suite of interactive dashboards:
publication trends, country and institution rankings, author networks, study-type breakdown,
journal analysis, and a world map — all as self-contained HTML files.

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
open output/index.html
```

`--fetch` retrieves data from OpenAlex before building the dashboards.
Omit it on subsequent runs to rebuild the HTML from already-downloaded CSVs.

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
| `prefix` | Short string prepended to every output file (`upf_dashboard.html`, `upf_papers_by_year.csv`, …). Lets multiple topics share one `output/` directory without collisions. |
| `keywords` | Search terms sent to OpenAlex `title_and_abstract.search`. Multi-word phrases are quoted and matched exactly; single words are stemmed by OpenAlex (so `flavanol` also matches `flavanols`). Terms are OR-combined. |

### Multi-topic use

Switch configs to analyse a different topic without losing existing data:

```bash
cp config.json config.json.upf         # save current topic
cp config.json.flav config.json        # switch to flavanols
python make_dashboard.py --fetch
```

Because every output file carries the prefix, the two datasets coexist in `output/`.

---

## Dashboards produced

| File | Description |
|------|-------------|
| `output/index.html` | Landing page with live stats and links to all dashboards |
| `output/<prefix>_dashboard.html` | Main overview: country/institution/author rankings, temporal trends, network metrics, author position analysis, study-type breakdown |
| `output/<prefix>_network_interactive.html` | Interactive co-authorship network explorer (year slider, author search, community colours) |
| `output/<prefix>_world_map.html` | Institution-level bubble map; PNG version also written |
| `output/<prefix>_study_type.html` | Study-type breakdown: donut chart and stacked bar (2000–2024) |
| `output/<prefix>_network_study_type.html` | Co-authorship network with nodes coloured/shaped by dominant study type |
| `output/<prefix>_journal_dashboard.html` | Journal analysis: top journals, impact vs volume, study-type mix, publication trends |

---

## Scripts

### `bibliometrics.py` — data retrieval

Queries OpenAlex using the keywords from `config.json`. Handles cursor pagination,
rate limiting, and retries. Writes all CSV tables to `output/`.

```bash
python bibliometrics.py                       # default output dir: output/
python bibliometrics.py --output-dir results  # custom dir
python bibliometrics.py --dry-run             # first 2 pages only (testing)
```

**Outputs** (all prefixed):

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
| `papers_by_department.csv` | Departmental affiliation strings (experimental) |
| `papers_by_funder.csv` | Funder names and paper counts |
| `funding_by_country.csv` | Funding coverage per country |
| `coauthorship_edges.csv` | All-time co-authorship edge list |
| `coauthorship_edges_by_year.csv` | Edge list with year annotation |
| `coauthorship_edges_primary.csv` | First/last-author-only edges |
| `coauthorship_edges_by_year_primary.csv` | Primary edges with year annotation |
| `network_metrics_by_year.csv` | Annual network size, density, clustering |

**Study-type classification** uses three sources in priority order:
1. PubMed MeSH publication-type tags (most reliable; covers ~65–70 % of papers)
2. OpenAlex work type
3. Title-keyword heuristics

Categories: `RCT`, `Clinical Trial`, `Observational`, `Systematic Review / Meta-analysis`, `Review`, `Other`.

**Institution assignment** uses the most frequently recorded affiliation across all of an author's papers rather than a single paper's affiliation, reducing mis-attribution from occasional guest or historical affiliations.

---

### `make_dashboard.py` — main overview dashboard

Builds the main dashboard and `output/index.html`. With `--fetch` it also runs `bibliometrics.py` first, making it the single entry point for the full pipeline.

```bash
python make_dashboard.py                      # rebuild HTML from existing CSVs
python make_dashboard.py --fetch              # fetch data then build
python make_dashboard.py --output-dir results # custom output directory
```

---

### `make_interactive_network.py` — co-authorship network

Interactive network explorer. Adjust the minimum-papers threshold, step through years, or search for a specific author.

```bash
python make_interactive_network.py
```

---

### `make_world_map.py` — world map

Institution-level bubble map (falls back to country centroids when coordinates are unavailable). Exports both an interactive HTML file and a 1 600 × 900 px PNG.

```bash
python make_world_map.py
```

---

### `make_study_type_dashboard.py` — study-type dashboard

Standalone page with a donut chart (overall distribution) and a stacked bar chart (2000–2024 trends).

```bash
python make_study_type_dashboard.py
```

---

### `make_study_type_network.py` — study-type network

Co-authorship network where node colour and shape encode each author's dominant study type.

```bash
python make_study_type_network.py
```

---

### `make_journal_dashboard.py` — journal analysis

Four charts: top journals by paper count, citations-per-paper vs volume (bubble), study-type mix by journal (100 % stacked bar), and publication trends for the top 10 journals. Pre-print servers and institutional repositories (medRxiv, Figshare, Zenodo, LA Referencia, etc.) are excluded automatically.

```bash
python make_journal_dashboard.py
```

---

## Project layout

```
keyword-bibliometrics/
├── config.json                      # topic title, prefix, search keywords
├── bibliometrics.py                 # data retrieval from OpenAlex
├── make_dashboard.py                # main overview + index.html
├── make_interactive_network.py      # co-authorship network explorer
├── make_world_map.py                # world map (HTML + PNG)
├── make_study_type_dashboard.py     # study-type charts
├── make_study_type_network.py       # study-type co-authorship network
├── make_journal_dashboard.py        # journal analysis
├── requirements.txt
└── output/                          # generated HTML, PNG, CSV (git-tracked)
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
```

Python 3.9 or later. No API key is needed for OpenAlex.

---

## Licence

GPL-3.0 — see [LICENSE](LICENSE).

*Analysis by G. Kuhnle · Data: [OpenAlex](https://openalex.org)*
