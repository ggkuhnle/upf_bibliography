#!/usr/bin/env python3
"""
make_world_map.py
Global research map — circles at institution level, sized by publication count.

Outputs:
  output/<prefix>_world_map.html   interactive
  output/<prefix>_world_map.png    1600x900 static image
"""

import argparse
import json as _json
import os
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio

OUTPUT_DIR = "output"

def _load_cfg():
    for _p in [os.path.join(os.path.dirname(__file__), "config.json"), "config.json"]:
        if os.path.exists(_p):
            with open(_p, encoding="utf-8") as _f:
                return _json.load(_f)
    return {}

_CFG    = _load_cfg()
_PREFIX = _CFG.get("prefix", "")
_TITLE  = _CFG.get("title", "Research")

def _pf(name):
    return name

# ── Institution lat/lon (top institutions hand-coded; rest fall back to country) ─
INST_COORDS = {
    "Universidade de São Paulo":                         (-23.55, -46.63),
    "Universidade Federal de Minas Gerais":              (-19.87, -43.97),
    "Harvard University":                                 (42.37, -71.12),
    "Deakin University":                                  (-37.85, 144.35),
    "Universidade do Estado do Rio de Janeiro":           (-22.91, -43.18),
    "Universidade Federal de São Paulo":                  (-23.60, -46.73),
    "University of North Carolina at Chapel Hill":        (35.90, -79.05),
    "Inserm":                                             (48.85,   2.35),
    "Instituto de Salud Carlos III":                      (40.47,  -3.70),
    "Universidade Federal do Rio Grande do Sul":          (-30.03, -51.22),
    "Johns Hopkins University":                           (39.33, -76.62),
    "Institut National de Recherche pour l'Agriculture, l'Alimentation et l'Environnement":
                                                          (47.00,   2.00),
    "Universidade Federal do Rio de Janeiro":             (-22.86, -43.23),
    "Fundação Oswaldo Cruz":                              (-22.88, -43.23),
    "Wageningen University & Research":                   (51.97,   5.67),
    "Imperial College London":                            (51.50,  -0.18),
    "Université Sorbonne Paris Nord":                     (48.95,   2.36),
    "Instituto de Saúde":                                 (-23.54, -46.65),
    "Université Paris Cité":                              (48.85,   2.35),
    "Universidade Estadual de Campinas (UNICAMP)":        (-22.82, -47.07),
    "Universidade Federal de Santa Catarina":             (-27.60, -48.55),
    "Centre de Recherche Épidémiologie et Statistique":   (48.87,   2.35),
    "Tufts University":                                   (42.41, -71.12),
    "Universidade Federal de Pelotas":                    (-31.77, -52.34),
    "Universidade Federal de Viçosa":                     (-20.76, -42.88),
    "Universidade Federal de Ouro Preto":                 (-20.38, -43.50),
    "The University of Sydney":                           (-33.89, 151.19),
    "Brigham and Women's Hospital":                       (42.34, -71.11),
    "Centro de Investigación Biomédica en Red de Epidemiología y Salud Pública":
                                                          (40.47,  -3.70),
    "New York University":                                (40.73, -73.99),
    "University of Melbourne":                            (-37.80, 144.96),
    "University of Queensland":                           (-27.50, 153.01),
    "Monash University":                                   (-37.91, 145.13),
    "Columbia University":                                 (40.81, -73.96),
    "University of Toronto":                               (43.66, -79.40),
    "University College London":                           (51.52,  -0.13),
    "University of Cambridge":                             (52.21,   0.12),
    "University of Oxford":                               (51.76,  -1.26),
    "London School of Hygiene & Tropical Medicine":        (51.52,  -0.13),
    "King's College London":                               (51.51,  -0.11),
    "University of Edinburgh":                             (55.95,  -3.19),
    "CNRS":                                                (48.85,   2.35),
    "Sorbonne Université":                                 (48.85,   2.35),
    "Équipe de Recherche en Épidémiologie Nutritionnelle": (48.95,   2.36),
    "Equipe de Recherche en Epidémiologie Nutritionnelle": (48.95,   2.36),
    "Instituto Nacional de Salud Pública":                 (18.87, -99.23),
    "Universidad Nacional Autónoma de México":             (19.33, -99.19),
    "Universidad de Chile":                                (-33.46, -70.66),
    "Pontificia Universidad Católica de Chile":            (-33.50, -70.62),
    "University of Auckland":                              (-36.85, 174.77),
    "The Chinese University of Hong Kong":                 (22.42, 114.21),
    "University of Hong Kong":                             (22.28, 114.14),
    "Peking University":                                   (39.99, 116.31),
    "Seoul National University":                           (37.46, 126.95),
    "University of Cape Town":                             (-33.96,  18.46),
    "Tehran University of Medical Sciences":               (35.70,  51.34),
    "Isfahan University of Medical Sciences":              (32.65,  51.65),
    "Shiraz University of Medical Sciences":               (29.64,  52.55),
    "University of Warsaw":                                (52.24,  21.02),
    "Maastricht University":                               (50.84,   5.69),
    "Utrecht University":                                  (52.09,   5.12),
    "Erasmus University Rotterdam":                        (51.91,   4.47),
    "University of Groningen":                             (53.22,   6.57),
    "Karolinska Institutet":                               (59.35,  18.03),
    "Uppsala University":                                  (59.85,  17.64),
    "Lund University":                                     (55.71,  13.19),
    "University of Oslo":                                  (59.94,  10.72),
    "University of Copenhagen":                            (55.68,  12.57),
    "University of Helsinki":                              (60.17,  24.95),
    "Ludwig Maximilian University of Munich":              (48.15,  11.58),
    "Charité – Universitätsmedizin Berlin":                (52.52,  13.38),
    "University of Bologna":                               (44.50,  11.35),
    "University of Milan":                                 (45.46,   9.19),
    "University of Naples Federico II":                    (40.85,  14.25),
    "University of Barcelona":                             (41.39,   2.16),
    "Autonomous University of Madrid":                     (40.55,  -3.69),
    "University of Navarra":                               (42.80,  -1.65),
    "University of Lisbon":                                (38.74,  -9.14),
    "University of Porto":                                 (41.18,  -8.61),
    "Ghent University":                                    (51.05,   3.72),
    "KU Leuven":                                           (50.88,   4.70),
    "ETH Zurich":                                          (47.38,   8.55),
    "University of Zurich":                                (47.37,   8.55),
    "University of Geneva":                                (46.20,   6.14),
    "McGill University":                                   (45.50, -73.58),
    "University of British Columbia":                      (49.26, -123.25),
    "Stanford University":                                  (37.43, -122.17),
    "University of California, Los Angeles":               (34.07, -118.44),
    "University of California, Berkeley":                  (37.87, -122.26),
    "University of Michigan":                              (42.28, -83.74),
    "University of Minnesota":                             (44.97, -93.23),
    "Northwestern University":                             (42.05, -87.68),
    "Yale University":                                     (41.31, -72.92),
    "Duke University":                                     (36.00, -78.94),
    "Boston University":                                   (42.35, -71.11),
    "Emory University":                                    (33.79, -84.32),
    "University of Washington":                            (47.65, -122.31),
    "University of Florida":                               (29.65, -82.35),
    "University of Texas":                                 (30.28, -97.74),
}

# ── Country centroid fallback ─────────────────────────────────────────────────
COUNTRY_CENTROIDS = {
    "AF": (33.9, 67.7), "AL": (41.2, 20.2), "DZ": (28.0, 1.7), "AO": (-11.2, 17.9),
    "AG": (17.1, -61.8), "AR": (-38.4, -63.6), "AM": (40.1, 45.0), "AU": (-25.3, 133.8),
    "AT": (47.5, 14.6), "AZ": (40.1, 47.6), "BS": (25.0, -77.4), "BB": (13.2, -59.6),
    "BD": (23.7, 90.4), "BY": (53.7, 28.0), "BE": (50.5, 4.5), "BJ": (9.3, 2.3),
    "BO": (-16.3, -63.6), "BA": (43.9, 17.7), "BW": (-22.3, 24.7), "BR": (-14.2, -51.9),
    "BG": (42.7, 25.5), "CM": (3.9, 11.5), "CA": (56.1, -106.3), "CF": (6.6, 20.9),
    "CL": (-35.7, -71.5), "CN": (35.9, 104.2), "CO": (4.6, -74.1), "CR": (9.7, -83.8),
    "CI": (7.5, -5.5), "HR": (45.1, 15.2), "CU": (21.5, -79.5), "CY": (35.1, 33.4),
    "CZ": (49.8, 15.5), "DK": (56.3, 9.5), "DO": (18.7, -70.2), "EC": (-1.8, -78.2),
    "EG": (26.8, 30.8), "SV": (13.8, -88.9), "EE": (58.6, 25.0), "ET": (9.1, 40.5),
    "FJ": (-17.7, 178.1), "FI": (61.9, 25.7), "FR": (46.2, 2.2), "GE": (42.3, 43.4),
    "DE": (51.2, 10.5), "GH": (7.9, -1.0), "GR": (39.1, 21.8), "GD": (12.1, -61.7),
    "GU": (13.4, 144.8), "GT": (15.8, -90.2), "GY": (5.0, -58.9), "HN": (15.2, -86.2),
    "HK": (22.4, 114.1), "HU": (47.2, 19.5), "IS": (64.9, -18.5), "IN": (20.6, 79.0),
    "ID": (-0.8, 113.9), "IQ": (33.2, 43.7), "IR": (32.4, 53.7), "IE": (53.4, -8.2),
    "IL": (31.0, 34.9), "IT": (41.9, 12.6), "JM": (18.1, -77.3), "JP": (36.2, 138.3),
    "JO": (30.6, 36.2), "KZ": (48.0, 68.0), "KE": (-0.0, 37.9), "KI": (1.9, -157.4),
    "KW": (29.5, 47.8), "KH": (12.6, 104.9), "KR": (35.9, 127.8), "LA": (19.9, 102.5),
    "LV": (56.9, 24.6), "LB": (33.9, 35.9), "LK": (7.9, 80.8), "LT": (55.2, 23.9),
    "LU": (49.8, 6.1), "MG": (-18.8, 46.9), "MW": (-13.3, 34.3), "MY": (4.2, 108.0),
    "MV": (3.2, 73.2), "ML": (17.6, -2.0), "MT": (35.9, 14.5), "MQ": (14.6, -61.0),
    "MR": (21.0, -10.9), "MX": (23.6, -102.6), "MD": (47.4, 28.4), "MC": (43.7, 7.4),
    "MN": (46.9, 103.8), "ME": (42.7, 19.4), "MA": (31.8, -7.1), "MZ": (-18.7, 35.5),
    "MM": (21.9, 95.9), "MP": (15.1, 145.7), "NC": (-20.9, 165.6), "NP": (28.4, 84.1),
    "NL": (52.1, 5.3), "NZ": (-40.9, 174.9), "NI": (12.9, -85.2), "NE": (17.6, 8.1),
    "NG": (9.1, 8.7), "NO": (60.5, 8.5), "OM": (21.5, 55.9), "PK": (30.4, 69.3),
    "PA": (8.5, -80.8), "PY": (-23.4, -58.4), "PE": (-9.2, -75.0), "PH": (12.9, 121.8),
    "PL": (51.9, 19.1), "PT": (39.4, -8.2), "PR": (18.2, -66.6), "PS": (31.9, 35.3),
    "PW": (7.5, 134.6), "QA": (25.4, 51.2), "RO": (45.9, 24.9), "RU": (61.5, 105.3),
    "RW": (-1.9, 29.9), "SA": (23.9, 45.1), "SN": (14.5, -14.5), "RS": (44.0, 21.0),
    "SD": (12.9, 30.2), "SG": (1.4, 103.8), "SK": (48.7, 19.7), "SI": (46.2, 15.0),
    "SO": (5.2, 46.2), "ZA": (-30.6, 22.9), "ES": (40.5, -3.7),
    "ST": (0.2, 6.6), "SE": (60.1, 18.6), "CH": (46.8, 8.2), "TW": (23.7, 121.0),
    "TJ": (38.9, 71.3), "TZ": (-6.4, 34.9), "TH": (15.9, 100.9), "TT": (10.7, -61.2),
    "TN": (33.9, 9.5), "TR": (38.9, 35.2), "UA": (48.4, 31.2), "UG": (1.4, 32.3),
    "AE": (23.4, 53.8), "GB": (55.4, -3.4), "US": (38.0, -97.0), "UY": (-32.5, -55.8),
    "UZ": (41.4, 64.6), "VU": (-15.4, 166.9), "VE": (6.4, -66.6), "VN": (14.1, 108.3),
    "YE": (15.6, 48.5), "ZM": (-13.1, 27.8), "ZW": (-19.0, 29.2),
    "XK": (42.6, 20.9), "GP": (16.3, -61.5), "GF": (4.0, -53.1), "AS": (-14.3, -170.7),
}

# ── Region colour map ─────────────────────────────────────────────────────────
def region_of(country_code):
    latin_america = {"BR","MX","AR","CL","CO","PE","VE","EC","BO","UY","PY",
                     "CR","GT","HN","SV","NI","PA","CU","DO","JM","TT","BB",
                     "GY","SR","PY","GD","AG","BS","LC","VU","BZ"}
    north_america = {"US","CA"}
    europe = {"GB","FR","DE","IT","ES","NL","PT","BE","SE","NO","DK","FI",
              "CH","AT","PL","CZ","SK","HU","RO","BG","GR","HR","SI","RS",
              "BA","ME","MK","AL","IE","IS","CY","MT","LU","LV","LT","EE",
              "MD","UA","BY","RU","GE","AM","AZ","XK","MC"}
    asia_pacific = {"CN","JP","KR","AU","NZ","IN","ID","PH","TH","VN","MY",
                    "SG","BD","PK","LK","NP","TW","HK","MN","KH","LA","MM",
                    "KZ","UZ","TJ","AZ","FJ","PW","KI","VU","NC","GU","MP"}
    mena = {"IR","SA","AE","TR","IL","EG","MA","TN","DZ","LB","JO","IQ",
            "QA","KW","YE","OM","BH","SD","LY","SY","PS"}
    africa = {"NG","ZA","KE","ET","GH","TZ","UG","SN","CM","CI","AO","MZ",
              "ZM","ZW","RW","MG","MW","BJ","NE","SO","CF","ST","GM","GW"}
    if country_code in north_america: return "North America"
    if country_code in latin_america: return "Latin America"
    if country_code in europe:        return "Europe"
    if country_code in asia_pacific:  return "Asia-Pacific"
    if country_code in mena:          return "Middle East & N. Africa"
    if country_code in africa:        return "Sub-Saharan Africa"
    return "Other"

REGION_COLORS = {
    "Latin America":           "#e84393",
    "North America":           "#e74c3c",
    "Europe":                  "#2980b9",
    "Asia-Pacific":            "#27ae60",
    "Middle East & N. Africa": "#f39c12",
    "Sub-Saharan Africa":      "#8e44ad",
    "Other":                   "#95a5a6",
}

LABEL_INSTS = {
    "Universidade de São Paulo":                  ("USP",       0.0),
    "Harvard University":                         ("Harvard",   0.0),
    "Deakin University":                          ("Deakin",    0.0),
    "Inserm":                                     ("Inserm",    0.0),
    "Instituto de Salud Carlos III":              ("ISCIII",    0.0),
    "Imperial College London":                    ("Imperial",  0.0),
    "Universidade Federal do Rio Grande do Sul":  ("UFRGS",     0.0),
    "University of North Carolina at Chapel Hill":("UNC",       0.0),
    "Universidade Federal de Minas Gerais":       ("UFMG",      0.0),
    "Wageningen University & Research":           ("Wageningen",0.0),
}

SHORT_NAMES = {
    "Universidade de São Paulo":                "Univ. São Paulo (USP)",
    "Universidade Federal de Minas Gerais":     "UFMG",
    "Harvard University":                       "Harvard",
    "Deakin University":                        "Deakin",
    "Universidade do Estado do Rio de Janeiro": "UERJ",
    "Universidade Federal de São Paulo":        "UNIFESP",
    "University of North Carolina at Chapel Hill": "UNC Chapel Hill",
    "Inserm":                                   "Inserm (France)",
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--prefix", default=None, help="Override config prefix")
    parser.add_argument("--title", default=None, help="Override config title")
    args = parser.parse_args()

    global _PREFIX, _TITLE
    if args.prefix is not None:
        _PREFIX = args.prefix
    if args.title is not None:
        _TITLE = args.title
    out  = args.output_dir or (os.path.join(OUTPUT_DIR, _PREFIX) if _PREFIX else OUTPUT_DIR)
    data = args.data_dir or (os.path.join("data", _PREFIX) if _PREFIX else "data")

    print("Loading data …")
    inst_df = pd.read_csv(os.path.join(data, _pf("papers_by_institution.csv")))
    inst_df = inst_df[inst_df["institution"].notna() & (inst_df["institution"] != "")]
    inst_df = inst_df[inst_df["country"].notna() & (inst_df["country"] != "")]

    def resolve_coords(row):
        coords = INST_COORDS.get(row["institution"])
        if coords:
            return coords
        return COUNTRY_CENTROIDS.get(row["country"], (None, None))

    inst_df[["lat", "lon"]] = inst_df.apply(
        lambda r: pd.Series(resolve_coords(r)), axis=1
    )
    inst_df = inst_df[inst_df["lat"].notna()]

    rng = np.random.default_rng(42)
    inst_df = inst_df.copy()
    inst_df["lat"] = inst_df["lat"] + rng.uniform(-0.08, 0.08, len(inst_df))
    inst_df["lon"] = inst_df["lon"] + rng.uniform(-0.12, 0.12, len(inst_df))
    inst_df["region"] = inst_df["country"].map(region_of)

    max_p = inst_df["papers"].max()
    inst_df["marker_size"] = (np.sqrt(inst_df["papers"] / max_p) * 40).clip(lower=3)

    print("Building map …")
    fig = go.Figure()

    region_order = ["Latin America", "North America", "Europe",
                    "Asia-Pacific", "Middle East & N. Africa",
                    "Sub-Saharan Africa", "Other"]

    for region in region_order:
        sub = inst_df[inst_df["region"] == region]
        if sub.empty:
            continue
        fig.add_trace(go.Scattergeo(
            lat=sub["lat"],
            lon=sub["lon"],
            mode="markers",
            name=region,
            marker=dict(
                size=sub["marker_size"],
                color=REGION_COLORS[region],
                opacity=0.75,
                line=dict(width=0.4, color="white"),
                sizemode="diameter",
            ),
            text=sub.apply(
                lambda r: (
                    f"<b>{r['institution']}</b><br>"
                    f"{r['country']}  ·  {r['region']}<br>"
                    f"Papers: {r['papers']:,}   Citations: {r['citations']:,}"
                ), axis=1
            ),
            hoverinfo="text",
            showlegend=True,
        ))

    label_rows = inst_df[inst_df["institution"].isin(LABEL_INSTS)].copy()
    label_rows["label_text"] = label_rows["institution"].map(
        {k: v[0] for k, v in LABEL_INSTS.items()}
    )
    fig.add_trace(go.Scattergeo(
        lat=label_rows["lat"],
        lon=label_rows["lon"],
        mode="text",
        text=label_rows["label_text"],
        textfont=dict(size=10.5, color="#1a2742", family="Arial"),
        textposition="top right",
        hoverinfo="none",
        showlegend=False,
        name="",
    ))

    fig.update_layout(
        title=dict(
            text=(
                f"<b>Global {_TITLE} Landscape</b><br>"
                "<sup style='font-size:13px;color:#666'>Circles show research institutions — "
                "size proportional to publications · colour by region</sup>"
            ),
            x=0.5, xanchor="center",
            y=0.97, yanchor="top",
            font=dict(size=22, color="#1a2742", family="Arial"),
        ),
        geo=dict(
            projection_type="natural earth",
            showland=True,        landcolor="#f0f2f0",
            showocean=True,       oceancolor="#d6e8f5",
            showlakes=False,
            showcountries=True,   countrycolor="#c8cfd0",
            showcoastlines=True,  coastlinecolor="#b0bcc0",
            showframe=False,
            bgcolor="white",
        ),
        legend=dict(
            title=dict(text="Region", font=dict(size=12, color="#1a2742")),
            x=0.01, y=0.99,
            xanchor="left", yanchor="top",
            bgcolor="rgba(255,255,255,0.88)",
            bordercolor="#ddd",
            borderwidth=1,
            font=dict(size=11),
            itemsizing="constant",
        ),
        paper_bgcolor="white",
        margin=dict(l=0, r=0, t=70, b=10),
        height=680,
    )

    top8 = inst_df.nlargest(8, "papers")
    callout_lines = ["<b>Top institutions</b>"]
    for rank, (_, r) in enumerate(top8.iterrows(), 1):
        name = SHORT_NAMES.get(r["institution"], r["institution"][:30])
        callout_lines.append(f"<b>{rank}.</b> {name}  —  {r['papers']:,}")

    fig.add_annotation(
        text="<br>".join(callout_lines),
        x=0.99, y=0.01, xref="paper", yref="paper",
        xanchor="right", yanchor="bottom",
        showarrow=False,
        font=dict(size=10.5, color="#1a2742", family="Arial"),
        bgcolor="rgba(255,255,255,0.92)",
        bordercolor="#cccccc",
        borderwidth=1,
        align="left",
    )

    os.makedirs(out, exist_ok=True)

    html_path = os.path.join(out, _pf("world_map.html"))
    fig.write_html(
        html_path,
        include_plotlyjs="cdn",
        config={"displayModeBar": True, "responsive": True,
                "modeBarButtonsToRemove": ["select2d", "lasso2d"]},
        full_html=True,
    )
    print(f"HTML → {html_path}  ({os.path.getsize(html_path)//1024} KB)")

    png_path = os.path.join(out, _pf("world_map.png"))
    try:
        pio.write_image(fig, png_path, width=1600, height=900, scale=2)
        print(f"PNG  → {png_path}  ({os.path.getsize(png_path)//1024} KB)")
    except Exception as e:
        print(f"PNG export skipped: {e}")

    print("Done.")


if __name__ == "__main__":
    main()
