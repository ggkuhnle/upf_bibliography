# UPF Bibliometrics

Open bibliometric analysis of the global ultra-processed food (UPF) research literature, using [OpenAlex](https://openalex.org).

**→ [View the live dashboards](https://ggkuhnle.github.io/upf_bibliography/)**

---

## What this produces

| Output | Description |
|--------|-------------|
| [Research Overview](output/upf_dashboard.html) | Publications by country, institution, author; temporal trends; network metrics |
| [Co-authorship Network](output/network_interactive.html) | Interactive year-by-year network explorer with author search |
| [Influence Analysis](output/influence_dashboard.html) | Most-cited works and authors; inside-vs-outside citation scatter |
| [World Map](output/world_map.html) | Institution-level bubble map; [PNG version](output/world_map.png) |

## Quick start

```bash
git clone https://github.com/ggkuhnle/upf_bibliography
cd upf_bibliography
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 1. Fetch all papers and write CSVs (~12 min for ~7 000 papers)
python upf_bibliometrics.py

# 2. Build the dashboards
python make_interactive_network.py
python make_influence_dashboard.py
python make_world_map.py

# 3. Open index.html in a browser
open index.html
```

Use `--dry-run` on any script to fetch only the first 2 pages for testing.

## Scripts

### `upf_bibliometrics.py`
Queries OpenAlex for works matching `ultra-processed`, `ultraprocessed`, or `NOVA classification` in title/abstract. Handles cursor pagination and retries. Writes CSV tables to `output/` and prints a summary report.

**Outputs:** `papers_by_country.csv`, `papers_by_institution.csv`, `papers_by_author.csv`, `papers_by_year.csv`, `papers_by_country_year.csv`, `coauthorship_edges.csv`, `coauthorship_edges_by_year.csv`, `network_metrics_by_year.csv`

### `make_interactive_network.py`
Builds a standalone HTML co-authorship network explorer from the edge CSVs. Year slider, author search, minimum-papers filter, community colours.

**Output:** `output/network_interactive.html`

### `influence_analysis.py`
Fetches the full reference lists of every UPF paper and counts which external works and authors are cited most often. Optionally fetches Altmetric attention scores.

```bash
python influence_analysis.py                         # reference network only
python influence_analysis.py --altmetric-key KEY     # + Altmetric scores
python influence_analysis.py --skip-refs --altmetric-key KEY  # Altmetric only
```

Requires a free [Altmetric API key](https://www.altmetric.com/solutions/altmetric-api/) for attention scores; set it in `.env` as `ALTMETRIC_KEY=yourkey`.

**Outputs:** `output/most_cited_works.csv`, `output/most_cited_authors.csv`, `output/altmetric_scores.csv`

### `make_influence_dashboard.py`
Renders the influence analysis results as an interactive HTML dashboard.

**Output:** `output/influence_dashboard.html`

### `make_world_map.py`
Generates a global bubble map at institution level (falls back to country centroids). Exports interactive HTML and a 1 600 × 900 px PNG suitable for publication.

**Output:** `output/world_map.html`, `output/world_map.png`

## Project layout

```
upf_bibliography/
├── index.html                    # landing page
├── upf_bibliometrics.py          # data retrieval
├── influence_analysis.py         # reference network + Altmetric
├── make_interactive_network.py   # network explorer
├── make_influence_dashboard.py   # influence dashboard
├── make_world_map.py             # world map
├── requirements.txt
├── notebooks/
│   └── author_network.ipynb      # exploratory network notebook
└── output/                       # generated HTML, PNG, CSV
```

## Data source & caveats

- **Source:** [OpenAlex](https://openalex.org) — open, free, no key required. All requests include a `mailto` parameter for polite usage.
- **Coverage:** papers through mid-2025; ~2 % lack affiliation data.
- **Disambiguation:** author and institution matching is as provided by OpenAlex; occasional mis-attribution may occur.
- **Books & grey literature:** poorly indexed in OpenAlex and under-represented in the influence analysis.
- **Funding data:** incomplete — many papers have no recorded funder.
