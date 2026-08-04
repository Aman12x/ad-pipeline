"""Unit tests for the spend-anomaly detector (3-sigma vs trailing 14 days)."""

from datetime import date, timedelta

import duckdb

from pipeline import quality

FACT_DDL = """
CREATE TABLE fact_ad_performance_daily (
    source TEXT, campaign_id TEXT, campaign_name TEXT, stat_date DATE,
    impressions BIGINT, clicks BIGINT, spend DOUBLE,
    platform_conversions DOUBLE, fp_conversions BIGINT, fp_revenue DOUBLE
)
"""

END = date(2026, 8, 3)


def _seed_fact(con, last_day_spend: float):
    """14 baseline days of ~$1000/day spend (small deterministic wobble),
    then a final day at last_day_spend."""
    con.execute(FACT_DDL)
    rows = []
    for i in range(14):
        d = END - timedelta(days=14 - i)
        spend = 1000 + (i % 5) * 20  # sigma ~ tens of dollars
        rows.append(("google", "g-1", "Campaign", d, 10000, 300, spend, 10.0, 9, 600.0))
    rows.append(("google", "g-1", "Campaign", END, 10000, 300,
                 last_day_spend, 10.0, 9, 600.0))
    con.executemany(
        "INSERT INTO fact_ad_performance_daily VALUES (?,?,?,?,?,?,?,?,?,?)", rows)


def test_spike_fires_and_is_recorded():
    con = duckdb.connect()
    _seed_fact(con, last_day_spend=5000)  # 5x baseline -> huge z
    anomalies = quality.detect_spend_anomalies(con)
    assert len(anomalies) == 1
    src, stat_date, spend, mu, sigma, z = anomalies[0]
    assert src == "google" and spend == 5000 and z > 3

    quality.record_alerts(con, END, anomalies)
    assert con.execute("SELECT count(*) FROM quality_alerts").fetchone()[0] == 1

    # Soft check: reported as failing, but must NOT fail the run.
    results = dict((n, ok) for n, ok, _ in quality.run_checks(con, END))
    assert results["spend_anomaly_3sigma"] is False
    quality.assert_quality(con, END)  # no raise


def test_normal_spend_is_silent():
    con = duckdb.connect()
    _seed_fact(con, last_day_spend=1040)  # within the baseline wobble
    assert quality.detect_spend_anomalies(con) == []


def test_cold_start_is_silent():
    """Fewer than 7 baseline days -> no anomaly claims possible."""
    con = duckdb.connect()
    con.execute(FACT_DDL)
    rows = [("google", "g-1", "C", END - timedelta(days=i), 100, 10,
             1000.0 + i, 1.0, 1, 60.0) for i in range(4)]
    con.executemany(
        "INSERT INTO fact_ad_performance_daily VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    assert quality.detect_spend_anomalies(con) == []
