# Keyword Bibliometrics

A configurable, open bibliometric analysis pipeline built on [OpenAlex](https://openalex.org).
Point it at any set of search terms and it produces a full suite of interactive dashboards:
publication trends, country and institution rankings, author networks, study-type breakdown,
journal analysis, a world map, and a citation network explorer — all as self-contained HTML files.

A standalone influence-map tool (`make_influence_report.py`) generates interactive Cytoscape.js
graphs for individual **authors**, **funders**, and **clinical trial / grant projects**, showing
global citation reach beyond any single corpus.

For full details see [DOCS.md](DOCS.md).

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

## Influence maps (author / funder / project)

`make_influence_report.py` is a standalone tool that crawls OpenAlex live and builds
an interactive Cytoscape.js graph of global citation influence. Three modes are available:

```bash
# Author influence map
python make_influence_report.py --author "Monteiro" --data-dir data/upf

# Funder influence map
python make_influence_report.py --funder "Mars" --prefix flavanol

# Clinical trial (NCT) — combined ClinicalTrials.gov + OpenAlex full-text search
python make_influence_report.py --nct NCT02422745

# Research grant (OpenAlex award ID)
python make_influence_report.py --award-id 312090 --prefix flavanol

# Combine NCT and award sources, optionally embed in an existing corpus
python make_influence_report.py --nct NCT01799005 --also-award-id 312090 \
    --prefix flavanol --data-dir data/flavanol
```

Outputs are written to `reports/authors/{slug}/`, `reports/funders/{slug}/`, or
`reports/projects/{slug}/`. All HTML files are self-contained — open in any browser.

See [section 4.2 of DOCS.md](DOCS.md#42-make_influence_reportpy) for the full CLI reference.

---

## Print-quality PDF (A3)

`make_influence_pdf.py` converts any `*_influence.html` from `make_influence_report.py`
into a standalone A3 landscape PDF: timeline scatter (publication year × citation count),
citation arrows, rotated labels with leader lines, and a numbered legend.

```bash
# Basic — reads title from HTML, writes author_influence_a3.pdf
venv/bin/python3 make_influence_pdf.py reports/authors/kuhnle/author_influence.html

# Press quality (300 DPI), custom output path
venv/bin/python3 make_influence_pdf.py reports/authors/kuhnle/author_influence.html \
    --dpi 300 --output kuhnle_a3.pdf

# Show more citing papers; hide unlabelled focal badges
venv/bin/python3 make_influence_pdf.py <file.html> --max-cite 200 --top-numbered 0
```

See [section 4.3 of DOCS.md](DOCS.md#43-make_influence_pdfpy) for the full CLI reference.

---

## Building all topics at once

```bash
./build_all.sh           # build and deploy all topics
./build_all.sh --fetch   # refetch from OpenAlex, then build and deploy
```

Topics are defined by files in `config/`. Flavonoid is always built last (largest dataset).

---

## Configuration

All topic-specific settings live in `config.json` (active working copy) and in
`config/config.json.*` (saved per-topic configs).

```json
{
  "title":    "Ultra-Processed Food Research",
  "prefix":   "upf",
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
| `prefix` | Short identifier used as subfolder name (`output/upf/`, `data/upf/`) |
| `keywords` | OR-combined search terms sent to OpenAlex |

To switch topics manually:

```bash
cp config/config.json.flav config.json
python make_dashboard.py --fetch
```

---

## Directory layout

```
upf_bibliography/
├── config.json                       # active topic config (working copy)
├── make_dashboard.py                 # main entry point — builds all dashboards
├── make_influence_report.py          # author / funder / project: corpus network + global influence map
├── make_influence_pdf.py             # print-quality A3 PDF from any *_influence.html
├── bibliometrics.py                  # data retrieval from OpenAlex
├── build_all.sh                      # build and deploy all topics in one command
├── deploy.sh                         # rsync one topic's output to the web server
├── requirements.txt
│
├── deploy/                           # deployment assets
│   └── server_index.html             # landing page for all deployed topics
│
├── config/                           # per-topic config files and author aliases
│   ├── config.json.upf
│   ├── config.json.flav
│   ├── config.json.flavonoid
│   ├── config.json.cocoa
│   └── aliases.json
│
├── cache/                            # local API caches (not git-tracked)
│   └── influence_cache.sqlite
│
├── reports/                          # influence reports — selectively committed
│   ├── authors/{slug}/               # author_corpus.html, author_influence.html
│   ├── funders/{slug}/               # funder_influence.html, funder_corpus.html
│   └── projects/{slug}/              # project_influence.html, project_corpus.html
│
├── scripts/                          # sub-scripts called by make_dashboard.py
│   ├── make_citation_network.py
│   ├── make_interactive_network.py
│   ├── make_world_map.py
│   ├── make_study_type_dashboard.py
│   ├── make_study_type_network.py
│   ├── make_journal_dashboard.py
│   ├── make_paper_dashboard.py
│   ├── make_author_report.py         # standalone author profile tool
│   ├── make_network_pdf.py           # standalone print-ready PDF tool
│   └── fix_upf_hardcoded.sh
│
├── data/                             # downloaded data — not git-tracked
│   ├── upf/
│   ├── flavanol/
│   └── ...
│
└── output/                           # generated HTML and PNG — not git-tracked
    ├── upf/
    ├── flavanol/
    └── ...
```

---

## Deployment

`deploy.sh` rsyncs the current topic's output to a web server and updates the landing page:

```bash
./deploy.sh
```

The prefix is read from `config.json` at deploy time.

---

## Data source and caveats

- **Source:** [OpenAlex](https://openalex.org) — open, free, no API key required.
- **Coverage:** papers indexed by OpenAlex through the retrieval date.
- **Author disambiguation:** as provided by OpenAlex; use `config/aliases.json` to correct known split profiles.
- **Funding data:** incomplete — many papers carry no recorded funder.
- **Study-type classification:** automated heuristics; edge cases will be mis-classified.

---

## Requirements

```
requests · pandas · numpy · networkx · plotly · kaleido · python-louvain
```

Python 3.9 or later. No API key needed.

---

## Licence

GPL-3.0 — see [LICENSE](LICENSE).

*Analysis by G. Kuhnle · Data: [OpenAlex](https://openalex.org)*
