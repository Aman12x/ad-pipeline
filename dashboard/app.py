"""Streamlit dashboard over the DuckDB marts.

Run:  streamlit run dashboard/app.py

Reads data/warehouse.duckdb (read-only). Channel colors are fixed to the
entity -- Google is always blue, Meta is always orange -- regardless of
filters or sort order. CAC/ROAS aggregates exclude the most recent day,
where first-party orders haven't landed yet (1-day lag) and platform
conversions are still immature.
"""

import os
import tempfile
from pathlib import Path

import duckdb
import pandas as pd
import plotly.graph_objects as go
import streamlit as st


def _writable(d: Path) -> bool:
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".write_probe"
        probe.touch()
        probe.unlink()
        return True
    except OSError:
        return False


# Prefer the repo's data/ dir; on hosts where the source mount is read-only
# (some cloud deploys), fall back to the system temp dir. Must be decided
# before any pipeline module is imported -- config reads DATA_DIR from env.
_repo_data = Path(__file__).resolve().parent.parent / "data"
DATA_DIR = (_repo_data if _writable(_repo_data)
            else Path(tempfile.gettempdir()) / "ad-pipeline-demo")
os.environ.setdefault("DATA_DIR", str(DATA_DIR))
DB_PATH = Path(os.environ["DATA_DIR"]) / "warehouse.duckdb"

# Channel identity colors (validated palette, light mode) -- fixed per entity.
CHANNEL_COLOR = {"google": "#2a78d6", "meta": "#eb6834"}
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
SURFACE = "#fcfcfb"
BASELINE = "#c3c2b7"

LAYOUT = dict(
    paper_bgcolor=SURFACE,
    plot_bgcolor=SURFACE,
    font=dict(family='system-ui, -apple-system, "Segoe UI", sans-serif',
              color=INK_2, size=13),
    margin=dict(l=8, r=8, t=8, b=8),
    hovermode="x unified",
    legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
    xaxis=dict(gridcolor=GRID, linecolor=BASELINE, tickcolor=BASELINE,
               tickfont=dict(color=MUTED), zeroline=False),
    yaxis=dict(gridcolor=GRID, linecolor=BASELINE, tickcolor=BASELINE,
               tickfont=dict(color=MUTED), zeroline=False),
)


@st.cache_data(ttl=60)
def load(query: str) -> pd.DataFrame:
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        return con.execute(query).df()
    finally:
        con.close()


st.set_page_config(page_title="Ad Analytics", page_icon="📈", layout="wide")

# The dashboard needs these; a warehouse from an older code version may lack
# the newer marts (cloud hot-updates keep the old DB file), so seed on any
# missing table, not just on a missing file.
REQUIRED_TABLES = {"fact_ad_performance_daily", "mart_campaign_daily",
                   "mart_channel_daily", "mart_campaign_rolling",
                   "mart_channel_pacing"}


def _needs_seed() -> bool:
    if not DB_PATH.exists():
        return True
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        tables = {r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables").fetchall()}
        if not REQUIRED_TABLES <= tables:
            return True
        if "seed_meta" in tables:
            from scripts.seed_demo import SEED_VERSION
            version = con.execute("SELECT version FROM seed_meta").fetchone()[0]
            return version < SEED_VERSION
        # seeded warehouse from before versioning existed (cloud) -> refresh;
        # locally-built warehouses without the stamp also just get refreshed
        return True
    finally:
        con.close()


import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if _needs_seed():
    # Build/refresh the warehouse in-process by replaying the last 3 daily
    # pipeline runs against the simulator (includes dbt build -> all marts).
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts.seed_demo import seed
    with st.spinner("Seeding demo warehouse (~15s)…"):
        seed()
    load.clear()  # drop any cached queries from the stale warehouse

channel = load("SELECT * FROM mart_channel_daily ORDER BY stat_date")
campaign = load("SELECT * FROM mart_campaign_daily ORDER BY stat_date")
if channel.empty:
    st.error("Warehouse is empty — run the pipeline first.")
    st.stop()

# Most recent day has no first-party orders yet (1-day lag): keep it on the
# spend chart but exclude it from CAC/ROAS/overclaim, which would read as 0.
latest = channel["stat_date"].max()
mature_ch = channel[channel["stat_date"] < latest]
mature_cp = campaign[campaign["stat_date"] < latest]

latest_str = pd.Timestamp(latest).strftime("%b %d, %Y")
st.title("Ad performance")
st.caption(f"Data through {latest_str}. Efficiency metrics exclude {latest_str} "
           "(first-party orders land with a 1-day lag).")

# Spend anomalies recorded by the pipeline's quality stage for the current day.
try:
    alerts = load("""
        SELECT source, stat_date, observed_spend, baseline_mean, z_score
        FROM quality_alerts
        WHERE stat_date = (SELECT max(stat_date) FROM fact_ad_performance_daily)
        ORDER BY abs(z_score) DESC
    """)
except Exception:  # older warehouse without the alerts table
    alerts = pd.DataFrame()
for _, a in alerts.iterrows():
    st.warning(
        f"**Spend anomaly** — {a['source'].capitalize()} spent "
        f"${a['observed_spend']:,.0f} on {pd.Timestamp(a['stat_date']):%b %d}, "
        f"vs a trailing-14-day average of ${a['baseline_mean']:,.0f} "
        f"(z = {a['z_score']:+.1f}). Investigate before trusting today's numbers.",
        icon="🚨")

# ---- headline tiles ---------------------------------------------------------
spend = mature_ch["spend"].sum()
fp_conv = mature_ch["fp_conversions"].sum()
fp_rev = mature_ch["fp_revenue"].sum()
plat_conv = mature_ch["platform_conversions"].sum()
c1, c2, c3, c4 = st.columns(4)
c1.metric("Spend", f"${spend:,.0f}")
c2.metric("Blended CAC", f"${spend / fp_conv:,.2f}" if fp_conv else "—")
c3.metric("Blended ROAS", f"{fp_rev / spend:,.2f}" if spend else "—")
c4.metric("Platform overclaim", f"{plat_conv / fp_conv:,.2f}×" if fp_conv else "—",
          help="Platform-reported conversions ÷ first-party orders. "
               ">1 means the platforms claim more conversions than actually happened.")

# ---- budget pacing ----------------------------------------------------------
pacing = load("""
    SELECT source, mtd_spend, monthly_budget, expected_mtd_spend,
           pacing_ratio, pacing_status
    FROM mart_channel_pacing
    WHERE stat_date = (SELECT max(stat_date) FROM mart_channel_pacing)
    ORDER BY source
""")
if not pacing.empty:
    st.subheader("Budget pacing — month to date")
    status_word = {"over": "over plan", "under": "under plan",
                   "on_track": "on track", "no_budget": "no budget set"}
    cols = st.columns(len(pacing) + 2)
    for col, (_, row) in zip(cols, pacing.iterrows()):
        col.metric(
            f"{row['source'].capitalize()} — {status_word[row['pacing_status']]}",
            f"{row['pacing_ratio']:.0%}" if pd.notna(row["pacing_ratio"]) else "—",
            delta=f"${row['mtd_spend'] - row['expected_mtd_spend']:+,.0f} vs plan",
            help=f"${row['mtd_spend']:,.0f} spent of a "
                 f"${row['monthly_budget']:,.0f} monthly budget; "
                 f"prorated plan to date is ${row['expected_mtd_spend']:,.0f}.",
        )
    st.caption("100% = exactly on the prorated monthly budget. "
               "Bands: over >110%, under <90%.")

# ---- spend over time --------------------------------------------------------
st.subheader("Daily spend by channel")
fig = go.Figure()
for ch, grp in channel.groupby("source"):
    fig.add_scatter(x=grp["stat_date"], y=grp["spend"], name=ch.capitalize(),
                    mode="lines", line=dict(color=CHANNEL_COLOR[ch], width=2),
                    hovertemplate="%{y:$,.0f}<extra>" + ch.capitalize() + "</extra>")
fig.update_layout(**LAYOUT, height=320)
fig.update_yaxes(tickprefix="$", rangemode="tozero")
st.plotly_chart(fig, width='stretch')

# ---- campaign efficiency ----------------------------------------------------
totals = (mature_cp.groupby(["source", "campaign_name"], as_index=False)
          .agg(spend=("spend", "sum"), fp_conversions=("fp_conversions", "sum"),
               fp_revenue=("fp_revenue", "sum"),
               platform_conversions=("platform_conversions", "sum")))
totals["cac"] = totals["spend"] / totals["fp_conversions"]
totals["roas"] = totals["fp_revenue"] / totals["spend"]
totals["overclaim"] = totals["platform_conversions"] / totals["fp_conversions"]


def campaign_bar(df, value_col, fmt, title, refline=None, ascending=True):
    df = df.sort_values(value_col, ascending=ascending)
    fig = go.Figure()
    for ch in ("google", "meta"):
        grp = df[df["source"] == ch]
        fig.add_bar(y=grp["campaign_name"], x=grp[value_col], orientation="h",
                    name=ch.capitalize(), marker_color=CHANNEL_COLOR[ch],
                    text=[fmt.format(v) for v in grp[value_col]],
                    textposition="outside", textfont=dict(color=INK_2),
                    hovertemplate="%{x:,.2f}<extra>%{y}</extra>")
    if refline is not None:
        fig.add_vline(x=refline, line_width=1, line_dash="dot", line_color=MUTED,
                      annotation_text="break-even", annotation_font_color=MUTED)
    fig.update_layout(**LAYOUT, height=300, bargap=0.35,
                      yaxis_categoryorder="trace")
    st.subheader(title)
    st.plotly_chart(fig, width='stretch')


left, right = st.columns(2)
with left:
    campaign_bar(totals, "cac", "${:,.0f}",
                 "CAC by campaign (first-party)", ascending=True)
with right:
    campaign_bar(totals, "roas", "{:,.2f}",
                 "ROAS by campaign (first-party)", refline=1.0, ascending=False)

# ---- creative fatigue -------------------------------------------------------
fatigue = load("""
    SELECT source, campaign_name, campaign_age_days, ctr_7d, ctr_vs_peak, cac_7d
    FROM mart_campaign_rolling
    WHERE stat_date = (SELECT max(stat_date) FROM mart_campaign_rolling)
    ORDER BY ctr_vs_peak
""")
if not fatigue.empty:
    st.subheader("Creative fatigue")
    st.caption("Each campaign's rolling 7-day CTR as a fraction of its own "
               "best. A sustained slide below the 0.8 line means the creative "
               "is wearing out — refresh the ads.")
    fig = go.Figure()
    for ch in ("google", "meta"):
        grp = fatigue[fatigue["source"] == ch]
        fig.add_bar(y=grp["campaign_name"], x=grp["ctr_vs_peak"],
                    orientation="h", name=ch.capitalize(),
                    marker_color=CHANNEL_COLOR[ch],
                    text=[f"{v:,.2f}" for v in grp["ctr_vs_peak"]],
                    textposition="outside", textfont=dict(color=INK_2),
                    hovertemplate="%{x:,.2f} of peak CTR<extra>%{y}</extra>")
    fig.add_vline(x=0.8, line_width=1, line_dash="dot", line_color=MUTED,
                  annotation_text="fatigue threshold",
                  annotation_font_color=MUTED)
    fig.update_layout(**LAYOUT, height=320, bargap=0.35,
                      yaxis_categoryorder="trace")
    fig.update_xaxes(range=[0, 1.12])
    st.plotly_chart(fig, width='stretch')

st.subheader("Platform overclaim by campaign")
st.caption("Platform-attributed conversions ÷ first-party orders. Everything "
           "above the 1.0 line is over-attribution.")
oc = totals.sort_values("overclaim", ascending=True)
fig = go.Figure()
for ch in ("google", "meta"):
    grp = oc[oc["source"] == ch]
    fig.add_bar(y=grp["campaign_name"], x=grp["overclaim"], orientation="h",
                name=ch.capitalize(), marker_color=CHANNEL_COLOR[ch],
                text=[f"{v:,.2f}×" for v in grp["overclaim"]],
                textposition="outside", textfont=dict(color=INK_2),
                hovertemplate="%{x:,.2f}×<extra>%{y}</extra>")
fig.add_vline(x=1.0, line_width=1, line_dash="dot", line_color=MUTED)
fig.update_layout(**LAYOUT, height=320, bargap=0.35, yaxis_categoryorder="trace")
fig.update_xaxes(range=[0, max(2.0, oc["overclaim"].max() * 1.25)])
st.plotly_chart(fig, width='stretch')

# ---- table view (accessibility + drill-down) --------------------------------
with st.expander("Data table — campaign daily"):
    st.dataframe(
        campaign.sort_values(["stat_date", "source", "campaign_id"],
                             ascending=[False, True, True]),
        width='stretch', hide_index=True)
