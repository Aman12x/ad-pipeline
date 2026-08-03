"""Orchestrator: extract -> load -> transform, with run metadata.

Usage:
    python -m pipeline.run [--as-of YYYY-MM-DD]

--as-of controls the observation date (passed through to the simulator so a
run can be replayed "as of" any past day). Defaults to today. Every run
re-pulls the trailing WINDOW_DAYS and upserts, so runs are idempotent and
late-restated conversions are picked up automatically.
"""

import argparse
import logging
import sys
import uuid
from datetime import date, datetime, timezone

import duckdb

from . import config, extract, load, quality, transform

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("pipeline.run")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    args = parser.parse_args(argv)

    run_id = f"{args.as_of.isoformat()}-{uuid.uuid4().hex[:8]}"
    started = datetime.now(timezone.utc)
    log.info("run %s starting (as_of=%s, window=%dd)",
             run_id, args.as_of, config.WINDOW_DAYS)

    con = duckdb.connect(config.DB_PATH)
    con.execute(load.DDL)
    con.execute(
        "INSERT INTO pipeline_runs (run_id, as_of, started_at, status) "
        "VALUES (?, ?, ?, 'running')",
        [run_id, args.as_of, started],
    )
    try:
        landed = extract.extract_all(args.as_of)
        counts = load.load_all(con, landed, run_id)
        transform.transform(con)
        quality.assert_quality(con, args.as_of)
    except Exception:
        con.execute(
            "UPDATE pipeline_runs SET status='failed', finished_at=? WHERE run_id=?",
            [datetime.now(timezone.utc), run_id],
        )
        log.exception("run %s FAILED", run_id)
        return 1
    finally:
        con.close()

    con = duckdb.connect(config.DB_PATH)
    con.execute(
        "UPDATE pipeline_runs SET status='succeeded', finished_at=?, "
        "rows_ad_stats=?, rows_orders=?, rows_rejected=? WHERE run_id=?",
        [datetime.now(timezone.utc), counts["ad_stats"], counts["orders"],
         counts["rejected"], run_id],
    )
    con.close()
    log.info("run %s succeeded: %s", run_id, counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
