"""Data quality checks, run as a distinct stage after transform.

Each check returns (name, passed, detail). fail_on_error controls whether a
failure raises (Airflow task failure / non-zero exit) or just logs -- freshness
is a warning-level check, integrity checks are hard failures.
"""

import logging
from datetime import date, timedelta

import duckdb

log = logging.getLogger("pipeline.quality")


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

    for name, ok, detail in checks:
        log.log(logging.INFO if ok else logging.ERROR,
                "check %-24s %s (%s)", name, "PASS" if ok else "FAIL", detail)
    return checks


def assert_quality(con: duckdb.DuckDBPyConnection, as_of: date):
    """Raise if any hard check fails (freshness is warn-only)."""
    hard_failures = [name for name, ok, _ in run_checks(con, as_of)
                     if not ok and name != "freshness_within_1d"]
    if hard_failures:
        raise RuntimeError(f"data quality checks failed: {hard_failures}")
