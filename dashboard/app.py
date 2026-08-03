"""Streamlit dashboard over the DuckDB marts.

Run:  streamlit run dashboard/app.py

Reads data/warehouse.duckdb (read-only). Channel colors are fixed to the
entity -- Google is always blue, Meta is always orange -- regardless of
filters or sort order. CAC/ROAS aggregates exclude the most recent day,
where first-party orders haven't landed yet (1-day lag) and platform
conversions are still immature.
"""

import duckdb
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "warehouse.duckdb"

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

if not DB_PATH.exists():
    st.error("No warehouse found. Run the pipeline first: "
             "`python -m pipeline.run` (with the simulator up).")
    st.stop()

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

st.title("Ad performance")
st.caption(f"Data through {latest}. Efficiency metrics exclude {latest} "
           "(first-party orders land with a 1-day lag).")

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
