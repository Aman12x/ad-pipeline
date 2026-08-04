"""Data quality checks, run as a distinct stage after transform.

Each check returns (name, passed, detail). Integrity checks are hard failures
(they fail the run); freshness and the spend-anomaly detector are warn-level:
an unusual-but-real spend day is an alert for a human, not bad data, so it is
recorded in quality_alerts and surfaced (dashboard, logs) without blocking
the marts.
"""

import logging
from datetime import date, datetime, timedelta, timezone

import duckdb

log = logging.getLogger("pipeline.quality")

SOFT_CHECKS = {"freshness_within_1d", "spend_anomaly_3sigma"}

ALERT_DDL = """
CREATE TABLE IF NOT EXISTS quality_alerts (
    detected_at    TIMESTAMP,
    run_as_of      DATE,
    source         TEXT,
    stat_date      DATE,
    observed_spend DOUBLE,
    baseline_mean  DOUBLE,
    baseline_std   DOUBLE,
    z_score        DOUBLE
);
"""

ANOMALY_SQL = """
WITH daily AS (
    SELECT source, stat_date, sum(spend) AS spend
    FROM fact_ad_performance_daily
    GROUP BY 1, 2
),
latest AS (SELECT max(stat_date) AS d FROM daily),
base AS (
    SELECT source,
           avg(spend)         AS mu,
           stddev_samp(spend) AS sigma,
           count(*)           AS n
    FROM daily, latest
    WHERE stat_date < d AND stat_date >= d - INTERVAL 14 DAY
    GROUP BY 1
)
SELECT t.source, l.d AS stat_date, round(t.spend, 2) AS spend,
       round(b.mu, 2) AS mu, round(b.sigma, 2) AS sigma,
       round((t.spend - b.mu) / b.sigma, 2) AS z
FROM daily t
JOIN latest l ON t.stat_date = l.d
JOIN base b USING (source)
WHERE b.n >= 7 AND b.sigma > 0
  AND abs((t.spend - b.mu) / b.sigma) > 3
"""


def detect_spend_anomalies(con: duckdb.DuckDBPyConnection) -> list[tuple]:
    """Channels whose latest daily spend sits >3 sigma from their trailing
    14-day baseline. Needs >=7 baseline days, else stays silent (cold start).
    """
    return con.execute(ANOMALY_SQL).fetchall()


def record_alerts(con: duckdb.DuckDBPyConnection, as_of: date,
                  anomalies: list[tuple]):
    con.execute(ALERT_DDL)
    now = datetime.now(timezone.utc)
    con.executemany(
        "INSERT INTO quality_alerts VALUES (?,?,?,?,?,?,?,?)",
        [(now, as_of, src, d, spend, mu, sigma, z)
         for src, d, spend, mu, sigma, z in anomalies],
    )


def run_checks(con: duckdb.DuckDBPyConnection, as_of: date) -> list[tuple[str, bool, str]]:
    checks = []

    n = con.execute("SELECT count(*) FROM fact_ad_performance_daily").fetchone()[0]
    checks.append(("fact_not_empty", n > 0, f"{n} rows"))

    dupes = con.execute("""
        SELECT count(*) FROM (
            SELECT source, campaign_id, stat_date FROM fact_ad_performance_daily
            GROUP BY 1,2,3 HAVING count(*) > 1)
    """).fetchone()[0]
    checks.append(("fact_pk_unique", dupes == 0, f"{dupes} duplicate keys"))

    negatives = con.execute("""
        SELECT count(*) FROM fact_ad_performance_daily
        WHERE least(impressions, clicks, spend, platform_conversions,
                    fp_conversions, fp_revenue) < 0
    """).fetchone()[0]
    checks.append(("no_negative_metrics", negatives == 0, f"{negatives} rows"))

    bad_ctr = con.execute(
        "SELECT count(*) FROM fact_ad_performance_daily WHERE clicks > impressions"
    ).fetchone()[0]
    checks.append(("clicks_lte_impressions", bad_ctr == 0, f"{bad_ctr} rows"))

    latest = con.execute("SELECT max(stat_date) FROM fact_ad_performance_daily").fetchone()[0]
    fresh = latest is not None and latest >= as_of - timedelta(days=1)
    checks.append(("freshness_within_1d", fresh, f"latest stat_date={latest}"))

    anomalies = detect_spend_anomalies(con)
    if anomalies:
        record_alerts(con, as_of, anomalies)
        detail = "; ".join(
            f"{src} spent ${spend:,.0f} on {d} vs ${mu:,.0f}±${sigma:,.0f} (z={z:+.1f})"
            for src, d, spend, mu, sigma, z in anomalies)
    else:
        detail = "all channels within 3 sigma of trailing 14d"
    checks.append(("spend_anomaly_3sigma", not anomalies, detail))

    for name, ok, detail in checks:
        log.log(logging.INFO if ok else logging.ERROR,
                "check %-24s %s (%s)", name, "PASS" if ok else "FAIL", detail)
    return checks


def assert_quality(con: duckdb.DuckDBPyConnection, as_of: date):
    """Raise if any hard check fails (SOFT_CHECKS are warn/alert-only)."""
    hard_failures = [name for name, ok, _ in run_checks(con, as_of)
                     if not ok and name not in SOFT_CHECKS]
    if hard_failures:
        raise RuntimeError(f"data quality checks failed: {hard_failures}")
