# UPF Bibliometrics

Retrieves the full ultra-processed food literature from [OpenAlex](https://openalex.org) and produces ranked CSV tables, a summary report, and an author co-authorship network analysis.

## Quick start

```bash
pip install -r requirements.txt

# Fetch all papers and write CSVs (~12 min for ~143k papers)
python upf_bibliometrics.py

# Then open the network notebook
jupyter lab notebooks/author_network.ipynb
```

Use `--dry-run` to fetch only the first 2 pages (400 papers) for testing.

## What it does

### `upf_bibliometrics.py`

Queries OpenAlex for works matching any of the default search terms in title or abstract:

- `ultra-processed`
- `ultraprocessed`
- `NOVA classification`

Handles cursor-based pagination, retries on errors, and writes to `output/`:

| File | Contents |
|------|----------|
| `papers_by_country.csv` | Country, paper count, total citations |
| `papers_by_institution.csv` | Institution, country, paper count, total citations |
| `papers_by_author.csv` | Author, institution, country, paper count, total citations |
| `coauthorship_edges.csv` | Author pairs with shared paper count (input for network analysis) |

Also prints a summary report to stdout with top-10 countries and institutions, and a concentration metric for the top-5 institutions.

### `notebooks/author_network.ipynb`

Builds and analyses the co-authorship network using NetworkX and Matplotlib:

- Filters to productive authors (`MIN_PAPERS`) and strong ties (`MIN_EDGE_WEIGHT`)
- Reports global network statistics (density, average degree, clustering, diameter)
- Computes per-author centrality: degree, betweenness, PageRank, clustering coefficient
- Detects research communities via Louvain algorithm
- Produces four figures saved to `output/`:
  - `degree_distribution.png` — linear and log-log degree distributions
  - `author_network.png` — network graph coloured by community
  - `betweenness_vs_degree.png` — scatter plot identifying bridges/gatekeepers
  - `country_collaboration_heatmap.png` — cross-national co-authorship counts
- Saves `author_centrality.csv` with all centrality metrics and community assignments

## CLI options

```
python upf_bibliometrics.py [--terms TERM [TERM ...]]
                             [--output-dir DIR]
                             [--dry-run]
```

## Project layout

```
upf_bibliography/
├── upf_bibliometrics.py   # data retrieval and aggregation
├── requirements.txt
├── notebooks/
│   └── author_network.ipynb
└── output/                # generated files (git-ignored)
```

## Data source

[OpenAlex](https://openalex.org) — open, free, no API key required.  
All requests include a `mailto` parameter for polite API usage.
