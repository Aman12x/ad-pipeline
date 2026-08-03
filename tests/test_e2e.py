"""End-to-end tests against the real simulator: restatement, idempotency,
retry survival, quality gates, and run tracking.
"""

from datetime import date, timedelta

import duckdb
import pytest

from pipeline import config, run

AS_OF_1 = date(2026, 8, 2)
AS_OF_2 = date(2026, 8, 3)


def _db():
    return duckdb.connect(config.DB_PATH, read_only=True)


def _conversions(con, source, campaign, stat_date):
    return con.execute(
        "SELECT platform_conversions FROM stg_ad_stats "
        "WHERE source=? AND campaign_id=? AND stat_date=?",
        [source, campaign, stat_date],
    ).fetchone()[0]


def test_full_pipeline_restatement_and_idempotency(isolated_env):
    # Run 1: observe as of Aug 2 -- recent conversions are immature.
    assert run.main(["--as-of", AS_OF_1.isoformat()]) == 0
    con = _db()
    early = _conversions(con, "google", "g-102", AS_OF_1 - timedelta(days=2))
    rows_after_1 = con.execute("SELECT count(*) FROM stg_ad_stats").fetchone()[0]
    con.close()
    assert rows_after_1 > 0

    # Run 2: one day later -- the SAME stat_date must be restated upward.
    assert run.main(["--as-of", AS_OF_2.isoformat()]) == 0
    con = _db()
    restated = _conversions(con, "google", "g-102", AS_OF_1 - timedelta(days=2))
    assert restated > early, "conversion restatement was not picked up"

    # Upsert, not append: only the one new stat date's rows were added.
    rows_after_2 = con.execute("SELECT count(*) FROM stg_ad_stats").fetchone()[0]
    n_campaigns = con.execute(
        "SELECT count(DISTINCT source || campaign_id) FROM stg_ad_stats"
    ).fetchone()[0]
    assert rows_after_2 == rows_after_1 + n_campaigns
    con.close()

    # Run 3: exact repeat -- fully idempotent, and 4th meta call hits a 429
    # inside this sequence, so surviving it proves the retry path too.
    assert run.main(["--as-of", AS_OF_2.isoformat()]) == 0
    con = _db()
    assert con.execute("SELECT count(*) FROM stg_ad_stats").fetchone()[0] == rows_after_2
    assert con.execute(
        "SELECT count(*) FROM (SELECT order_id FROM stg_orders "
        "GROUP BY 1 HAVING count(*) > 1)").fetchone()[0] == 0

    # Every run recorded and successful.
    runs = con.execute(
        "SELECT status, rows_rejected FROM pipeline_runs ORDER BY started_at"
    ).fetchall()
    con.close()
    assert [s for s, _ in runs] == ["succeeded"] * 3
    assert all(rej == 0 for _, rej in runs)


def test_quality_checks_pass_on_real_output(isolated_env):
    from pipeline import quality

    assert run.main(["--as-of", AS_OF_1.isoformat()]) == 0
    con = duckdb.connect(config.DB_PATH)
    results = quality.run_checks(con, AS_OF_1)
    con.close()
    assert all(ok for _, ok, _ in results), results


def test_marts_keep_platform_and_firstparty_separate(isolated_env):
    assert run.main(["--as-of", AS_OF_1.isoformat()]) == 0
    con = _db()
    cols = {r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='mart_campaign_daily'").fetchall()}
    con.close()
    assert {"platform_conversions", "fp_conversions", "overclaim_ratio"} <= cols
