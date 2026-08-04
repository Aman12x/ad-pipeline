"""Orchestrator: extract -> load -> dbt build -> quality, with run metadata.

Usage:
    python -m pipeline.run [--as-of YYYY-MM-DD]

--as-of controls the observation date (passed through to the simulator so a
run can be replayed "as of" any past day). Defaults to today. Every run
re-pulls the trailing WINDOW_DAYS and upserts, so runs are idempotent and
late-restated conversions are picked up automatically.

The transform stage is dbt (models + schema/singular tests); the warehouse
connection is closed around it because dbt opens its own.
"""

import argparse
import logging
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb

from . import config, dbt_runner, extract, load, quality

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("pipeline.run")


def _connect():
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(config.DB_PATH)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    args = parser.parse_args(argv)

    run_id = f"{args.as_of.isoformat()}-{uuid.uuid4().hex[:8]}"
    log.info("run %s starting (as_of=%s, window=%dd)",
             run_id, args.as_of, config.WINDOW_DAYS)

    con = _connect()
    con.execute(load.DDL)
    con.execute(
        "INSERT INTO pipeline_runs (run_id, as_of, started_at, status) "
        "VALUES (?, ?, ?, 'running')",
        [run_id, args.as_of, datetime.now(timezone.utc)],
    )
    con.close()

    def finish(status: str, counts: dict | None = None):
        con = _connect()
        con.execute(
            "UPDATE pipeline_runs SET status=?, finished_at=?, rows_ad_stats=?, "
            "rows_orders=?, rows_rejected=? WHERE run_id=?",
            [status, datetime.now(timezone.utc),
             (counts or {}).get("ad_stats"), (counts or {}).get("orders"),
             (counts or {}).get("rejected"), run_id],
        )
        con.close()

    try:
        landed = extract.extract_all(args.as_of)

        con = _connect()
        try:
            counts = load.load_all(con, landed, run_id)
        finally:
            con.close()  # dbt needs the file

        dbt_runner.dbt_build()

        con = _connect()
        try:
            quality.assert_quality(con, args.as_of)
        finally:
            con.close()
    except Exception:
        finish("failed")
        log.exception("run %s FAILED", run_id)
        return 1

    finish("succeeded", counts)
    log.info("run %s succeeded: %s", run_id, counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
