# Keyword Bibliometrics

A configurable, open bibliometric analysis pipeline built on [OpenAlex](https://openalex.org).
Point it at any set of search terms and it produces a full suite of interactive dashboards:
publication trends, country and institution rankings, author networks, study-type breakdown,
journal analysis, a world map, and a citation network explorer — all as self-contained HTML files.

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
├── make_influence_report.py          # paper or author: corpus network + global influence map
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
