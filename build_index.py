#!/usr/bin/env python3
"""
Green-Transition Talent Feeder Index
====================================

Question: if building electrification and EV charging are constrained by the
supply of electricians, which occupations are the most realistic pools to
retrain *from*, and what exactly would each need to learn?

The index answers that with two numbers per occupation:

  1. Feeder Readiness (0-100) - how much of the electrician skill profile a
     worker in that occupation already has.
  2. Wage Gain ($) - how much more the median electrician earns than the
     median worker in that occupation.

Run:  python3 build_index.py
Data: O*NET 30.1 (skills) + BLS OES May 2025 (employment & wages). Both public,
      both downloaded automatically on first run.
"""

import io
import os
import re
import sys
import zipfile
import urllib.request

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

# ---------------------------------------------------------------------------
# PARAMETERS  (every judgment call in the analysis is on this screen)
# ---------------------------------------------------------------------------

TARGET_SOC = "47-2111"      # Electricians - the job we are staffing
TARGET_NAME = "Electricians"

# O*NET rates Importance 1-5, where 3 = "Important". We keep only the elements
# an electrician rates at least "Important" and ignore the rest. This is the
# core methodological choice: we measure readiness FOR THIS JOB, not general
# similarity between two occupations.
IMPORTANCE_MIN = 3.0

MIN_EMPLOYMENT = 50_000     # pool must be thick enough to matter nationally

# BLS "All Other" codes are residual buckets - whatever did not fit the named
# occupations in that group. They are heterogeneous by construction, so a single
# skill profile does not describe them and no curriculum can target them. Drop.
DROP_RESIDUAL = True
TOP_N = 15                  # length of the ranked table
N_GAP_FEEDERS = 3           # how many feeders get a curriculum breakdown
N_GAPS_SHOWN = 8            # skill gaps listed per feeder

DOMAINS = ["Skills", "Knowledge", "Abilities", "Work Activities"]

ONET_URL = "https://www.onetcenter.org/dl_files/database/db_30_1_text.zip"
OES_URL = "https://www.bls.gov/oes/special-requests/oesm25nat.zip"
RAW = "data/raw"
ONET_DIR = f"{RAW}/db_30_1_text"
OES_XLSX = f"{RAW}/oesm25nat/national_M2025_dl.xlsx"
OUT = "outputs"
FIGS = f"{OUT}/figures"

# Design-system palette (validated for colour-vision deficiency, see README)
C_BLUE, C_ORANGE, C_AQUA = "#2a78d6", "#eb6834", "#1baf7a"
INK, INK_2, INK_3 = "#0b0b0b", "#52514e", "#8a8880"
GRID, SURFACE = "#e6e5e1", "#fcfcfb"
SERIES = [C_BLUE, C_ORANGE, C_AQUA]


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# 0. DATA
# ---------------------------------------------------------------------------

def fetch(url, marker, label):
    """Download+unzip `url` into data/raw unless `marker` already exists."""
    if os.path.exists(marker):
        return
    os.makedirs(RAW, exist_ok=True)
    log(f"  downloading {label} ...")
    # bls.gov rejects bare urllib requests, so send a full browser header set.
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.bls.gov/oes/tables.htm",
    })
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            blob = r.read()
    except Exception as e:
        sys.exit(f"could not download {label} ({e}).\n"
                 f"  Download {url} by hand and unzip it into {RAW}/, then re-run.")
    zipfile.ZipFile(io.BytesIO(blob)).extractall(RAW)


def load_onet():
    """Long table of every O*NET rating we care about, keyed to 6-digit SOC."""
    frames = []
    for dom in DOMAINS:
        d = pd.read_csv(f"{ONET_DIR}/{dom}.txt", sep="\t", dtype=str)
        d["domain"] = dom
        frames.append(d)
    o = pd.concat(frames, ignore_index=True)
    o["value"] = pd.to_numeric(o["Data Value"], errors="coerce")
    # O*NET-SOC (47-2152.04) -> SOC (47-2152) so it joins to BLS wage data.
    o["soc"] = o["O*NET-SOC Code"].str[:7]
    return o.rename(columns={"Element ID": "eid", "Element Name": "element",
                             "Scale ID": "scale"})[
        ["soc", "eid", "element", "domain", "scale", "value"]]


def load_oes():
    """National employment and median annual wage, one row per detailed SOC."""
    d = pd.read_excel(OES_XLSX, dtype=str)
    d = d[d.O_GROUP == "detailed"].copy()
    d["employment"] = pd.to_numeric(d.TOT_EMP, errors="coerce")
    d["wage"] = pd.to_numeric(d.A_MEDIAN, errors="coerce")
    # A handful of occupations report only an hourly median; annualise them.
    hourly = pd.to_numeric(d.H_MEDIAN, errors="coerce") * 2080
    d["wage"] = d.wage.fillna(hourly)
    return d.rename(columns={"OCC_CODE": "soc", "OCC_TITLE": "title"})[
        ["soc", "title", "employment", "wage"]].dropna(subset=["employment", "wage"])


# ---------------------------------------------------------------------------
# 1-2. THE MEASURE
# ---------------------------------------------------------------------------

def build_matrices(onet, keep_socs):
    """Occupation x element matrix of O*NET Level ratings (native 0-7 scale).

    Some SOCs cover several O*NET occupations (e.g. 47-2152 contains both
    Plumbers and Solar Thermal Installers); we average them.
    """
    lv = (onet[(onet.scale == "LV") & (onet.soc.isin(keep_socs))]
          .pivot_table(index="soc", columns="eid", values="value", aggfunc="mean"))
    lv = lv.dropna(axis=1, how="any")
    # z-score each element across occupations: a "level 4" means very different
    # things for Reading Comprehension vs Equipment Maintenance, so we compare
    # each occupation to the spread of the element, not to its raw number.
    z = (lv - lv.mean()) / lv.std(ddof=0)
    return lv, z


def importance_weights(onet, elements):
    """Electrician Importance rating per element, zeroed below the threshold."""
    imp = (onet[(onet.soc == TARGET_SOC) & (onet.scale == "IM")]
           .groupby("eid").value.mean().reindex(elements))
    w = imp.where(imp >= IMPORTANCE_MIN, 0.0).fillna(0.0)
    return imp, w


def readiness(z, w):
    """Importance-weighted, deficit-only distance from the electrician profile.

    Deficit-only is deliberate. If a machinist is BETTER than an electrician at
    something, that is not a barrier to becoming an electrician - it is
    irrelevant, or an asset. Only the elements where a candidate falls SHORT
    represent training that must actually happen. A symmetric distance would
    punish an occupation for being over-qualified; this one does not.
    """
    target = z.loc[TARGET_SOC]
    deficit = (target - z).clip(lower=0)               # occupations x elements
    wv = w.values
    d = np.sqrt((deficit.values ** 2 * wv).sum(axis=1) / wv.sum())
    d = pd.Series(d, index=z.index, name="distance")
    # Rescale to a 0-100 score anchored on the whole economy: 100 = already
    # clears every important bar, 0 = the occupation furthest from the trade.
    score = 100 * (1 - d / d.max())
    return d, score.rename("readiness")


def skill_gaps(lv, w, imp, meta, soc):
    """Curriculum list for one feeder, in native O*NET Level points (0-7)."""
    gap = (lv.loc[TARGET_SOC] - lv.loc[soc]).clip(lower=0)
    g = pd.DataFrame({"gap": gap, "importance": imp, "weight": w,
                      "target_level": lv.loc[TARGET_SOC],
                      "feeder_level": lv.loc[soc]})
    g = g[g.weight > 0].copy()
    # Priority = how far behind you are, scaled by how much the job needs it.
    g["priority"] = g.gap * g.importance
    g = g.join(meta)
    return g.sort_values("priority", ascending=False)


# ---------------------------------------------------------------------------
# FIGURES
# ---------------------------------------------------------------------------

def style_axes(ax):
    ax.set_facecolor(SURFACE)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=INK_2, labelsize=9, length=0)


# Occupation titles are written for a codebook, not a chart axis. These
# substitutions shorten them without changing what they refer to.
ABBREV = [
    (r"Heating, Air Conditioning, and Refrigeration", "HVAC"),
    (r"Hazardous Materials", "Hazmat"),
    (r"Security and Fire Alarm Systems", "Security & Fire Alarm"),
    (r"Maintenance and Repair Workers, General", "General Maintenance & Repair"),
    (r"Electrical Power-Line Installers and Repairers", "Electrical Power-Line Workers"),
    (r"Water and Wastewater Treatment Plant and System", "Water Treatment"),
    (r"Multiple Machine Tool Setters, Operators, and Tenders", "Machine Tool Setters"),
    (r"First-Line Supervisors of", "Supervisors:"),
    (r"Helpers--", "Helpers: "),
    (r", Metal and Plastic", " (Metal/Plastic)"),
    (r"Technicians and Mechanics", "Technicians"),
    (r"Mechanics and Installers", "Mechanics"),
    (r", Lawn Service, and Groundskeeping Workers", ""),
    (r", General$", ""),
    (r", Except .*$", ""),
    (r"\s{2,}", " "),
]


def short(t, n=30):
    for pat, rep in ABBREV:
        t = re.sub(pat, rep, t)
    t = t.strip().rstrip(",")
    return t if len(t) <= n else t[: n - 1] + "…"


# O*NET element names are written for a codebook too.
ELEMENT_ABBREV = [
    (r"Drafting, Laying Out, and Specifying Technical Devices, Parts, and Equipment",
     "Drafting & Specifying Equipment"),
    (r"Communicating with Supervisors, Peers, or Subordinates", "Communicating with Coworkers"),
    (r"Resolving Conflicts and Negotiating with Others", "Resolving Conflicts"),
    (r"Guiding, Directing, and Motivating Subordinates", "Guiding & Motivating Staff"),
    (r"Repairing and Maintaining Electronic Equipment", "Repairing Electronic Equipment"),
    (r"Repairing and Maintaining Mechanical Equipment", "Repairing Mechanical Equipment"),
    (r"Providing Consultation and Advice to Others", "Providing Consultation & Advice"),
    (r"Organizing, Planning, and Prioritizing Work", "Organizing & Prioritizing Work"),
    (r"Performing General Physical Activities", "General Physical Activities"),
    (r"Monitoring and Controlling Resources", "Monitoring Resources"),
    (r"Inspecting Equipment, Structures, or Materials", "Inspecting Equipment"),
    (r"Evaluating Information to Determine Compliance with Standards",
     "Evaluating Compliance"),
    (r"Updating and Using Relevant Knowledge", "Using Relevant Knowledge"),
]


def short_element(t, n=34):
    for pat, rep in ELEMENT_ABBREV:
        t = t.replace(pat, rep)
    return t if len(t) <= n else t[: n - 1] + "\u2026"


def bubble(emp):
    """Marker area from employment - sublinear so a 1.5M pool stays on screen."""
    return 10 + (emp / 1000) ** 0.62 * 2.1


def fig_scatter(cand, top, elec_wage, path):
    fig, ax = plt.subplots(figsize=(12.8, 7.8), facecolor=SURFACE)
    style_axes(ax)
    ax.grid(True, color=GRID, lw=0.7, zorder=0)
    ax.set_axisbelow(True)

    rest = cand[~cand.soc.isin(top.soc)]
    ax.scatter(rest.readiness, rest.wage_gain, s=bubble(rest.employment),
               facecolor="#dbd9d3", edgecolor="#c2c0b8", lw=0.5, zorder=2)
    ax.scatter(top.readiness, top.wage_gain, s=bubble(top.employment),
               facecolor=C_BLUE, edgecolor=SURFACE, lw=1.4, zorder=4)

    # Reserve a gutter on the right so every label gets its own line.
    xhi = cand.readiness.max()
    gutter = xhi + 4
    ax.set_xlim(cand.readiness.min() - 3, gutter + 31)
    ax.set_xticks(list(range(20, int(xhi) + 1, 10)))
    ymax = cand.wage_gain.max()
    ax.set_ylim(-1600, ymax * 1.14)

    # Stack the labels down the gutter, keeping their vertical order but
    # forcing a minimum gap, then draw a leader line back to each bubble.
    lab = top.sort_values("wage_gain", ascending=False)
    y0, y1 = ax.get_ylim()
    span = y1 - y0
    gap = span / 37.0
    ys = []
    for v in lab.wage_gain:
        ys.append(min(v, ys[-1] - gap) if ys else v)
    lift = (y0 + span * 0.03) - ys[-1]
    if lift > 0:
        ys = [y + lift for y in ys]

    for (_, r), ly in zip(lab.iterrows(), ys):
        ax.annotate(f"{int(r['rank'])}. {short(r.title, 30)}",
                    xy=(r.readiness, r.wage_gain), xytext=(gutter, ly),
                    textcoords="data", va="center", ha="left",
                    fontsize=8.9, color=INK, zorder=5,
                    arrowprops=dict(arrowstyle="-", color="#bcbab2", lw=0.7,
                                    shrinkA=0, shrinkB=2.5))

    # Size key, parked in the empty top of the gutter.
    ax.text(gutter, ymax * 1.085, "Workers", fontsize=8.8, color=INK_2, weight="bold")
    for i, emp in enumerate([1_500_000, 500_000, 100_000]):
        yk = ymax * (1.015 - i * 0.058)
        ax.scatter([gutter + 1.6], [yk], s=bubble(emp), facecolor="#dbd9d3",
                   edgecolor="#c2c0b8", lw=0.5, zorder=4)
        ax.text(gutter + 4.2, yk, f"{emp/1e6:.1f}M" if emp >= 1e6 else f"{emp//1000:.0f}k",
                fontsize=8.4, color=INK_2, va="center")

    ax.set_xlabel("Feeder Readiness  \u2192  how much of the electrician skill profile the worker already has",
                  fontsize=10.5, color=INK_2, labelpad=11)
    ax.set_ylabel("Pay rise from switching  ($/year, at the median)",
                  fontsize=10.5, color=INK_2, labelpad=11)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v/1000:.0f}k"))

    fig.text(0.045, 0.955, "Who could become an electrician \u2014 and who has the most to gain",
             ha="left", fontsize=16, color=INK, weight="bold")
    fig.text(0.045, 0.915,
             f"Each bubble is a US occupation paying below the ${elec_wage:,.0f} electrician median "
             f"with at least {MIN_EMPLOYMENT//1000:,}k workers.  Blue = the top {TOP_N}.",
             ha="left", fontsize=10.4, color=INK_2)
    fig.text(0.045, 0.022, "O*NET 30.1 \u00b7 BLS OES May 2025", fontsize=8.6, color=INK_3)
    fig.tight_layout(rect=[0.035, 0.045, 0.99, 0.895])
    fig.savefig(path, dpi=200, facecolor=SURFACE)
    plt.close(fig)


def fig_gaps(gapsets, path):
    n = len(gapsets)
    fig, axes = plt.subplots(1, n, figsize=(15.8, 6.4), facecolor=SURFACE)
    # Elements are CHOSEN by priority (gap x importance) but DRAWN by raw gap,
    # so the bars read top-to-bottom longest-to-shortest.
    shown = [(t, g.head(N_GAPS_SHOWN).sort_values("gap")) for t, g in gapsets]
    xmax = max(g.gap.max() for _, g in shown) * 1.30   # one shared scale

    for i, (ax, (title, g)) in enumerate(zip(np.atleast_1d(axes), shown)):
        style_axes(ax)
        y = np.arange(len(g))
        ax.barh(y, g.gap, color=SERIES[i % len(SERIES)], height=0.64, zorder=3)
        ax.set_yticks(y)
        ax.set_yticklabels([short_element(e) for e in g.element],
                           fontsize=9.3, color=INK)
        ax.set_xlim(0, xmax)
        ax.grid(True, axis="x", color=GRID, lw=0.7, zorder=0)
        ax.set_axisbelow(True)
        # Direct value labels double as the contrast relief for the aqua slot.
        for yi, v in zip(y, g.gap):
            ax.text(v + xmax * 0.022, yi, f"{v:.1f}", va="center",
                    fontsize=8.8, color=INK_2)
        ax.set_title(short(title, 34), loc="left", fontsize=11.8,
                     color=INK, weight="bold", pad=10)
        ax.set_xlabel("O*NET Level points behind electricians",
                      fontsize=9.3, color=INK_2)

    fig.text(0.028, 0.955, "What each feeder would actually have to learn",
             ha="left", fontsize=16, color=INK, weight="bold")
    fig.text(0.028, 0.905,
             "The biggest shortfalls on the elements electricians rate as important. "
             "Longer bar = more training. Shared scale.",
             ha="left", fontsize=10.4, color=INK_2)
    fig.text(0.028, 0.02, "O*NET 30.1 \u00b7 Level is measured 0\u20137",
             fontsize=8.6, color=INK_3)
    fig.tight_layout(rect=[0.01, 0.045, 0.99, 0.875])
    fig.savefig(path, dpi=200, facecolor=SURFACE)
    plt.close(fig)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    global ELEC_WAGE
    os.makedirs(FIGS, exist_ok=True)

    log("1/6  data")
    fetch(ONET_URL, ONET_DIR, "O*NET 30.1")
    fetch(OES_URL, OES_XLSX, "BLS OES May 2025")
    onet, oes = load_onet(), load_oes()

    socs = sorted(set(onet.soc) & set(oes.soc))
    if TARGET_SOC not in socs:
        sys.exit(f"target {TARGET_SOC} missing from joined data")
    log(f"     {len(socs)} occupations with both skill ratings and wage data")

    log("2/6  occupation x element matrix")
    lv, z = build_matrices(onet, socs)
    meta = (onet.drop_duplicates("eid").set_index("eid")[["element", "domain"]]
            .reindex(lv.columns))
    log(f"     {lv.shape[0]} occupations x {lv.shape[1]} elements")

    log("3/6  importance weights")
    imp, w = importance_weights(onet, lv.columns)
    log(f"     {int((w > 0).sum())} of {len(w)} elements rated >= {IMPORTANCE_MIN} "
        f"(\"Important\") by electricians")

    log("4/6  readiness")
    dist, score = readiness(z, w)

    df = (pd.DataFrame({"readiness": score, "distance": dist})
          .join(oes.set_index("soc")).reset_index().rename(columns={"index": "soc"}))
    ELEC_WAGE = float(df.loc[df.soc == TARGET_SOC, "wage"].iloc[0])
    elec_emp = float(df.loc[df.soc == TARGET_SOC, "employment"].iloc[0])
    log(f"     {TARGET_NAME}: ${ELEC_WAGE:,.0f} median, {elec_emp:,.0f} employed")

    df["wage_gain"] = ELEC_WAGE - df.wage
    df["wage_gain_pct"] = 100 * df.wage_gain / df.wage

    log("5/6  filter and rank")
    cand = df[(df.soc != TARGET_SOC) &
              (df.wage_gain > 0) &
              (df.employment >= MIN_EMPLOYMENT)].copy()
    if DROP_RESIDUAL:
        n0 = len(cand)
        cand = cand[~cand.title.str.contains("All Other", case=False, na=False)]
        log(f"     dropped {n0 - len(cand)} residual \"All Other\" categories")
    cand = cand.sort_values("readiness", ascending=False).reset_index(drop=True)
    cand.insert(0, "rank", cand.index + 1)
    top = cand.head(TOP_N)
    log(f"     {len(cand)} occupations pass the filters")

    cols = ["rank", "soc", "title", "readiness", "employment", "wage",
            "wage_gain", "wage_gain_pct"]
    cand[cols].to_csv(f"{OUT}/feeder_index.csv", index=False)
    df.to_csv(f"{OUT}/readiness_all_occupations.csv", index=False)

    gapsets, gap_rows = [], []
    for _, r in top.head(N_GAP_FEEDERS).iterrows():
        g = skill_gaps(lv, w, imp, meta, r.soc)
        gapsets.append((r.title, g))
        gg = g.head(N_GAPS_SHOWN).reset_index().rename(columns={"eid": "element_id"})
        gg.insert(0, "feeder", r.title)
        gap_rows.append(gg)
    pd.concat(gap_rows).to_csv(f"{OUT}/skill_gaps_top3.csv", index=False)

    log("6/6  figures")
    fig_scatter(cand, top, ELEC_WAGE, f"{FIGS}/fig1_readiness_vs_wage_gain.png")
    fig_gaps(gapsets, f"{FIGS}/fig2_skill_gaps.png")

    print(f"\n{'':2}TOP {TOP_N} FEEDER OCCUPATIONS INTO {TARGET_NAME.upper()}\n")
    show = top[["rank", "title", "readiness", "employment", "wage", "wage_gain"]].copy()
    show["title"] = show.title.map(lambda t: short(t, 44))
    show.columns = ["#", "Occupation", "Ready", "Employed", "Median $", "Gain $"]
    print(show.to_string(index=False, formatters={
        "Ready": lambda v: f"{v:5.1f}",
        "Employed": lambda v: f"{v:,.0f}",
        "Median $": lambda v: f"{v:,.0f}",
        "Gain $": lambda v: f"+{v:,.0f}"}))
    print(f"\n  wrote {OUT}/feeder_index.csv, {OUT}/skill_gaps_top3.csv, and 2 figures")


if __name__ == "__main__":
    main()
