"""Export the dashboard's charts as static PNGs (assets/) for the README
or an emailed report. Mirrors dashboard/app.py's queries and styling.

Run:  python scripts/export_report.py
"""

import sys
from pathlib import Path

import duckdb
import plotly.graph_objects as go

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DB = ROOT / "data" / "warehouse.duckdb"
OUT = ROOT / "assets"

CHANNEL_COLOR = {"google": "#2a78d6", "meta": "#eb6834"}
INK_2, MUTED, GRID = "#52514e", "#898781", "#e1e0d9"
SURFACE, BASELINE = "#fcfcfb", "#c3c2b7"

LAYOUT = dict(
    paper_bgcolor=SURFACE, plot_bgcolor=SURFACE,
    font=dict(family="Helvetica, Arial, sans-serif", color=INK_2, size=13),
    margin=dict(l=10, r=30, t=90, b=10),
    title_y=0.97,
    legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
    xaxis=dict(gridcolor=GRID, linecolor=BASELINE, tickcolor=BASELINE,
               tickfont=dict(color=MUTED), zeroline=False),
    yaxis=dict(gridcolor=GRID, linecolor=BASELINE, tickcolor=BASELINE,
               tickfont=dict(color=MUTED), zeroline=False),
)


def main():
    OUT.mkdir(exist_ok=True)
    con = duckdb.connect(str(DB), read_only=True)
    channel = con.execute("SELECT * FROM mart_channel_daily ORDER BY stat_date").df()
    latest = channel["stat_date"].max()
    totals = con.execute("""
        SELECT source, campaign_name,
               sum(spend) AS spend, sum(fp_conversions) AS fp_conversions,
               sum(fp_revenue) AS fp_revenue,
               sum(platform_conversions) AS platform_conversions
        FROM mart_campaign_daily
        WHERE stat_date < (SELECT max(stat_date) FROM mart_campaign_daily)
        GROUP BY 1, 2
    """).df()
    con.close()
    totals["cac"] = totals["spend"] / totals["fp_conversions"]
    totals["roas"] = totals["fp_revenue"] / totals["spend"]
    totals["overclaim"] = totals["platform_conversions"] / totals["fp_conversions"]

    fig = go.Figure()
    for ch, grp in channel.groupby("source"):
        fig.add_scatter(x=grp["stat_date"], y=grp["spend"], name=ch.capitalize(),
                        mode="lines", line=dict(color=CHANNEL_COLOR[ch], width=2))
    fig.update_layout(**LAYOUT, height=340, width=900,
                      title=dict(text="Daily spend by channel", font_color="#0b0b0b"))
    fig.update_yaxes(tickprefix="$", rangemode="tozero")
    fig.write_image(OUT / "spend_by_channel.png", scale=2)

    def bar(df, col, fmt, title, fname, refline=None, ascending=True):
        df = df.sort_values(col, ascending=ascending)
        fig = go.Figure()
        for ch in ("google", "meta"):
            grp = df[df["source"] == ch]
            fig.add_bar(y=grp["campaign_name"], x=grp[col], orientation="h",
                        name=ch.capitalize(), marker_color=CHANNEL_COLOR[ch],
                        text=[fmt.format(v) for v in grp[col]],
                        textposition="outside", textfont=dict(color=INK_2))
        if refline is not None:
            fig.add_vline(x=refline, line_width=1, line_dash="dot",
                          line_color=MUTED)
        fig.update_layout(**LAYOUT, height=340, width=900, bargap=0.35,
                          yaxis_categoryorder="trace",
                          title=dict(text=title, font_color="#0b0b0b"))
        fig.update_xaxes(range=[0, df[col].max() * 1.3])
        fig.write_image(OUT / fname, scale=2)

    bar(totals, "cac", "${:,.0f}", "CAC by campaign (first-party)", "cac.png")
    bar(totals, "roas", "{:,.2f}", "ROAS by campaign (first-party)", "roas.png",
        refline=1.0, ascending=False)
    bar(totals, "overclaim", "{:,.2f}x",
        "Platform overclaim (platform conv ÷ first-party orders)",
        "overclaim.png", refline=1.0)
    print(f"wrote 4 charts to {OUT}/ (data through {latest})")


if __name__ == "__main__":
    main()
