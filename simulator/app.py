"""Ad platform API simulator.

Fakes three upstream sources for the ETL pipeline:
  GET /google/campaign_stats   - Google Ads-shaped report (camelCase, costMicros)
  GET /meta/insights           - Meta Marketing API-shaped report (actions array),
                                 rate-limited: every 4th call returns 429
  GET /firstparty/orders       - first-party order truth (utm-tagged)

All data is deterministic given (campaign, date): the same query always returns
the same base numbers. Conversions "mature": a report pulled with as_of close to
the stat date under-reports conversions, and later as_of dates report more --
mimicking real platforms restating attributed conversions for up to ~7 days.
Pass ?as_of=YYYY-MM-DD to control the observation date (defaults to today).
"""

import hashlib
from datetime import date, timedelta

from fastapi import FastAPI, HTTPException, Response

app = FastAPI(title="Ad Platform Simulator")

CAMPAIGNS = {
    "google": [
        ("g-101", "Brand Search", 9000, 0.055, 0.065, 1.9),
        ("g-102", "Generic Search", 24000, 0.028, 0.021, 2.6),
        ("g-103", "Shopping - Core", 15000, 0.034, 0.030, 1.4),
        ("g-104", "YouTube Prospecting", 60000, 0.006, 0.007, 0.5),
    ],
    "meta": [
        ("m-201", "Prospecting - Broad", 42000, 0.011, 0.012, 0.9),
        ("m-202", "Retargeting - 30d", 8000, 0.042, 0.055, 1.1),
        ("m-203", "Lookalike 1pct", 20000, 0.016, 0.018, 1.0),
    ],
}

WEEKDAY_FACTOR = [1.0, 1.02, 1.05, 1.04, 1.0, 0.82, 0.78]  # Mon..Sun
AOV = 68.0  # average order value for first-party revenue


def _rand(*parts) -> float:
    """Deterministic pseudo-random float in [0, 1) from the given key parts."""
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(h[:8], "big") / 2**64


def _daily(platform: str, cid: str, base_impr: int, ctr: float, cvr: float,
           cpc: float, d: date) -> dict:
    wf = WEEKDAY_FACTOR[d.weekday()]
    noise = 0.8 + 0.4 * _rand(platform, cid, d, "impr")
    impressions = int(base_impr * wf * noise)
    clicks = int(impressions * ctr * (0.85 + 0.3 * _rand(platform, cid, d, "ctr")))
    spend = round(clicks * cpc * (0.9 + 0.2 * _rand(platform, cid, d, "cpc")), 2)
    final_conversions = clicks * cvr * (0.8 + 0.4 * _rand(platform, cid, d, "cvr"))
    return {
        "impressions": impressions,
        "clicks": clicks,
        "spend": spend,
        "final_conversions": final_conversions,
    }


def _maturity(stat_date: date, as_of: date) -> float:
    """Fraction of final conversions visible when observed on as_of.

    Day 0: ~50% attributed. Fully mature after 7 days.
    """
    days = (as_of - stat_date).days
    if days < 0:
        return 0.0
    return min(1.0, 0.5 + 0.5 * days / 7.0)


def _parse_dates(start: str, end: str, as_of: str | None):
    try:
        s, e = date.fromisoformat(start), date.fromisoformat(end)
        a = date.fromisoformat(as_of) if as_of else date.today()
    except ValueError:
        raise HTTPException(400, "dates must be YYYY-MM-DD")
    if s > e:
        raise HTTPException(400, "start after end")
    return s, e, a


def _date_range(s: date, e: date):
    d = s
    while d <= e:
        yield d
        d += timedelta(days=1)


@app.get("/google/campaign_stats")
def google_stats(start: str, end: str, as_of: str | None = None):
    s, e, a = _parse_dates(start, end, as_of)
    results = []
    for cid, name, impr, ctr, cvr, cpc in CAMPAIGNS["google"]:
        for d in _date_range(s, e):
            if d > a:
                continue
            m = _daily("google", cid, impr, ctr, cvr, cpc, d)
            results.append({
                "campaign": {"resourceName": f"customers/123/campaigns/{cid}",
                             "id": cid, "name": name},
                "metrics": {
                    "impressions": str(m["impressions"]),  # Google returns int64 as string
                    "clicks": str(m["clicks"]),
                    "costMicros": str(int(m["spend"] * 1_000_000)),
                    "conversions": round(m["final_conversions"] * _maturity(d, a), 2),
                },
                "segments": {"date": d.isoformat()},
            })
    return {"results": results, "fieldMask": "campaign.id,metrics,segments.date"}


_meta_calls = {"n": 0}


@app.get("/meta/insights")
def meta_insights(since: str, until: str, as_of: str | None = None):
    _meta_calls["n"] += 1
    if _meta_calls["n"] % 4 == 0:
        return Response(
            status_code=429,
            content='{"error":{"message":"User request limit reached","code":17}}',
            media_type="application/json",
            headers={"Retry-After": "1"},
        )
    s, e, a = _parse_dates(since, until, as_of)
    data = []
    for cid, name, impr, ctr, cvr, cpc in CAMPAIGNS["meta"]:
        for d in _date_range(s, e):
            if d > a:
                continue
            m = _daily("meta", cid, impr, ctr, cvr, cpc, d)
            conv = round(m["final_conversions"] * _maturity(d, a), 2)
            data.append({
                "campaign_id": cid,
                "campaign_name": name,
                "impressions": str(m["impressions"]),
                "clicks": str(m["clicks"]),
                "spend": f'{m["spend"]:.2f}',
                "actions": [
                    {"action_type": "purchase", "value": str(conv)},
                    {"action_type": "link_click", "value": str(m["clicks"])},
                ],
                "date_start": d.isoformat(),
                "date_stop": d.isoformat(),
            })
    return {"data": data, "paging": {"cursors": {}}}


@app.get("/firstparty/orders")
def firstparty_orders(start: str, end: str, as_of: str | None = None):
    """Ground-truth orders from 'our own backend', UTM-tagged.

    Roughly 85% of a campaign's final conversions become real orders (the rest
    is platform over-attribution) -- this is the gap the pipeline should expose.
    Orders appear with a 1-day lag and don't restate after that.
    """
    s, e, a = _parse_dates(start, end, as_of)
    orders = []
    for platform, campaigns in CAMPAIGNS.items():
        for cid, name, impr, ctr, cvr, cpc in campaigns:
            for d in _date_range(s, e):
                if (a - d).days < 1:  # 1-day ingestion lag
                    continue
                m = _daily(platform, cid, impr, ctr, cvr, cpc, d)
                n_orders = int(m["final_conversions"] * 0.85)
                for i in range(n_orders):
                    orders.append({
                        "order_id": f"{cid}-{d.isoformat()}-{i:04d}",
                        "order_date": d.isoformat(),
                        "utm_source": platform,
                        "utm_campaign": cid,
                        "revenue": round(AOV * (0.5 + _rand(cid, d, i, "rev")), 2),
                    })
    return {"orders": orders, "count": len(orders)}


@app.get("/health")
def health():
    return {"ok": True}
