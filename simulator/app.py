"""Ad platform API simulator with realistic data distributions.

Fakes three upstream sources for the ETL pipeline:
  GET /google/campaign_stats   - Google Ads-shaped report (camelCase, costMicros)
  GET /meta/insights           - Meta Marketing API-shaped report (actions array),
                                 rate-limited: every 4th call returns 429
  GET /firstparty/orders       - first-party order truth (utm-tagged)

All data is deterministic given (campaign, date): the same query always returns
the same base numbers. On top of that determinism, the generator reproduces the
statistical texture of real ad data:

  - log-normal daily noise with AR(1) persistence (a truncated moving-average
    of per-day seeded gaussians), so good/bad days cluster like real series
  - campaign lifecycle: a short learning ramp after each creative refresh,
    then CTR fatigue decay, refreshing on a ~75-day cycle
  - rare shock days per channel (~2% promo spikes, ~1% tracking outages) --
    the heavy tail that spend-anomaly monitoring exists for
  - front-loaded conversion maturity (~55% visible same day, ~88% by day 2,
    fully restated by ~day 7+), mimicking real attribution windows
  - campaign-type-specific over-attribution: retargeting/view-through-heavy
    campaigns overclaim far more vs first-party orders than brand search
  - log-normal order values (many small orders, occasional large ones)

Pass ?as_of=YYYY-MM-DD to control the observation date (defaults to today).
"""

import hashlib
import math
from datetime import date, timedelta

from fastapi import FastAPI, HTTPException, Response

app = FastAPI(title="Ad Platform Simulator")

# (id, name, base_impressions, ctr, cvr, cpc, fp_share)
# fp_share: fraction of platform-claimed conversions that are real first-party
# orders. Retargeting/view-through campaigns overclaim most (low share);
# brand search barely overclaims.
CAMPAIGNS = {
    "google": [
        ("g-101", "Brand Search", 9000, 0.055, 0.065, 1.9, 0.95),
        ("g-102", "Generic Search", 24000, 0.028, 0.021, 2.6, 0.88),
        ("g-103", "Shopping - Core", 15000, 0.034, 0.030, 1.4, 0.90),
        ("g-104", "YouTube Prospecting", 60000, 0.006, 0.007, 0.5, 0.55),
    ],
    "meta": [
        ("m-201", "Prospecting - Broad", 42000, 0.011, 0.012, 0.9, 0.72),
        ("m-202", "Retargeting - 30d", 8000, 0.042, 0.055, 1.1, 0.60),
        ("m-203", "Lookalike 1pct", 20000, 0.016, 0.018, 1.0, 0.75),
    ],
}

WEEKDAY_FACTOR = [1.0, 1.02, 1.05, 1.04, 1.0, 0.82, 0.78]  # Mon..Sun
MEDIAN_ORDER_VALUE = 55.0
ORDER_VALUE_SIGMA = 0.65  # log-normal spread: mean AOV ~ $68, right-skewed

AR_PHI = 0.55   # day-to-day persistence of the latent demand/auction factor
AR_DEPTH = 10   # MA truncation depth (phi^10 ~ 0.25% -- negligible tail)
CREATIVE_CYCLE_DAYS = 75  # creative refresh cadence per campaign


def _rand(*parts) -> float:
    """Deterministic pseudo-random float in [0, 1) from the given key parts."""
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(h[:8], "big") / 2**64


def _gauss(*parts) -> float:
    """Deterministic standard normal (Box-Muller over two hash uniforms)."""
    u1 = max(_rand(*parts, "u1"), 1e-12)
    u2 = _rand(*parts, "u2")
    return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)


def _ar1(key: str, cid: str, d: date) -> float:
    """AR(1) latent factor at date d, computed statelessly as a truncated
    MA(inf): sum(phi^k * eps_{d-k}). Rescaled to unit variance so callers can
    multiply by their own sigma. Same date -> same value, always."""
    total = sum(AR_PHI ** k * _gauss(key, cid, d - timedelta(days=k))
                for k in range(AR_DEPTH + 1))
    return total * math.sqrt(1.0 - AR_PHI ** 2)


def _lognorm(sigma: float, z: float) -> float:
    """Mean-preserving log-normal multiplier for a given z-score."""
    return math.exp(sigma * z - sigma * sigma / 2.0)


def _lifecycle(cid: str, d: date) -> float:
    """CTR multiplier over the creative's life: short learning ramp after each
    refresh, then fatigue decay toward ~0.7 of peak until the next refresh.
    Cycle position derives from the absolute date, so it needs no state."""
    offset = int(_rand("launch", cid) * CREATIVE_CYCLE_DAYS)
    age = (d.toordinal() + offset) % CREATIVE_CYCLE_DAYS
    ramp = min(1.0, 0.85 + 0.03 * age)          # ~5-day learning phase
    fatigue = 0.68 + 0.32 * math.exp(-age / 45)  # slow creative wearout
    return ramp * fatigue


def _shock(platform: str, d: date) -> float:
    """Rare channel-wide shock days: ~2% promo spikes (1.6-2.8x volume),
    ~1% tracking/delivery outages (~0.45x). The heavy tail."""
    r = _rand("event", platform, d)
    if r < 0.02:
        return 1.6 + 1.2 * _rand("event-size", platform, d)
    if r < 0.03:
        return 0.45
    return 1.0


def _daily(platform: str, cid: str, base_impr: int, ctr: float, cvr: float,
           cpc: float, fp_share: float, d: date) -> dict:
    wf = WEEKDAY_FACTOR[d.weekday()]
    shock = _shock(platform, d)

    demand = _ar1("demand", cid, d)    # persistent volume factor
    auction = _ar1("auction", cid, d)  # persistent CPC pressure

    impressions = int(base_impr * wf * shock * _lognorm(0.18, demand))
    eff_ctr = ctr * _lifecycle(cid, d) * _lognorm(0.08, _gauss("ctr", cid, d))
    clicks = int(impressions * eff_ctr)
    eff_cpc = cpc * _lognorm(0.10, auction)
    spend = round(clicks * eff_cpc, 2)
    eff_cvr = cvr * _lognorm(0.12, _gauss("cvr", cid, d))
    final_conversions = clicks * eff_cvr
    return {
        "impressions": impressions,
        "clicks": clicks,
        "spend": spend,
        "final_conversions": final_conversions,
        "fp_share": fp_share,
    }


def _maturity(stat_date: date, as_of: date) -> float:
    """Fraction of final conversions visible when observed on as_of.

    Front-loaded like real attribution windows: ~55% same day, ~77% day 1,
    ~88% day 2, then a shrinking tail; fully restated from day 10.
    """
    days = (as_of - stat_date).days
    if days < 0:
        return 0.0
    if days >= 10:
        return 1.0
    return 1.0 - 0.45 * math.exp(-days / 1.5)


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
    for cid, name, impr, ctr, cvr, cpc, fp in CAMPAIGNS["google"]:
        for d in _date_range(s, e):
            if d > a:
                continue
            m = _daily("google", cid, impr, ctr, cvr, cpc, fp, d)
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
    for cid, name, impr, ctr, cvr, cpc, fp in CAMPAIGNS["meta"]:
        for d in _date_range(s, e):
            if d > a:
                continue
            m = _daily("meta", cid, impr, ctr, cvr, cpc, fp, d)
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

    Each campaign's fp_share of platform-claimed conversions become real
    orders (the rest is over-attribution -- worst for retargeting and
    view-through-heavy campaigns). Order values are log-normal: many small
    orders, occasional large ones. Orders appear with a 1-day lag and don't
    restate after that.
    """
    s, e, a = _parse_dates(start, end, as_of)
    orders = []
    for platform, campaigns in CAMPAIGNS.items():
        for cid, name, impr, ctr, cvr, cpc, fp in campaigns:
            for d in _date_range(s, e):
                if (a - d).days < 1:  # 1-day ingestion lag
                    continue
                m = _daily(platform, cid, impr, ctr, cvr, cpc, fp, d)
                # mean-preserving rounding: plain int() would bias small
                # campaigns' order counts far below fp_share of conversions
                expected = m["final_conversions"] * m["fp_share"]
                n_orders = int(expected)
                if _rand("order-frac", cid, d) < expected - n_orders:
                    n_orders += 1
                for i in range(n_orders):
                    value = MEDIAN_ORDER_VALUE * math.exp(
                        ORDER_VALUE_SIGMA * _gauss(cid, d, i, "rev"))
                    orders.append({
                        "order_id": f"{cid}-{d.isoformat()}-{i:04d}",
                        "order_date": d.isoformat(),
                        "utm_source": platform,
                        "utm_campaign": cid,
                        "revenue": round(value, 2),
                    })
    return {"orders": orders, "count": len(orders)}


@app.get("/health")
def health():
    return {"ok": True}
