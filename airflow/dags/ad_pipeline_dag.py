"""Airflow DAG for the ad analytics pipeline.

Stages are separate tasks (extract -> load -> transform -> quality) so a
failure is visible at the stage that caused it and retries don't redo
finished work. The run's logical date becomes the pipeline's as_of, so
`airflow dags backfill` replays history correctly -- the simulator (and a
real ad API) returns data as it looked on that date.

Setup: the project root must be importable, e.g.
    export PYTHONPATH=/path/to/ad-pipeline
and the simulator reachable at SIM_BASE_URL (default http://127.0.0.1:8787).
"""

from datetime import date, datetime, timedelta

from airflow.decorators import dag, task

DEFAULT_ARGS = {
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
}


@dag(
    dag_id="ad_pipeline",
    description="Google/Meta/first-party ad ETL: raw -> staging -> marts",
    schedule="0 6 * * *",  # daily 06:00, after platforms settle overnight stats
    start_date=datetime(2026, 7, 20),
    catchup=False,
    default_args=DEFAULT_ARGS,
    tags=["ads", "etl"],
)
def ad_pipeline():

    @task
    def extract(ds: str | None = None) -> dict:
        from pipeline import extract as ex
        as_of = date.fromisoformat(ds)
        landed = ex.extract_all(as_of)
        return {source: str(path) for source, path in landed.items()}

    @task
    def load(landed: dict, ds: str | None = None) -> dict:
        from pathlib import Path

        import duckdb

        from pipeline import config
        from pipeline import load as ld
        con = duckdb.connect(config.DB_PATH)
        try:
            return ld.load_all(con, {s: Path(p) for s, p in landed.items()},
                               run_id=f"airflow-{ds}")
        finally:
            con.close()

    @task
    def transform(counts: dict) -> dict:
        import duckdb

        from pipeline import config
        from pipeline import transform as tf
        con = duckdb.connect(config.DB_PATH)
        try:
            tf.transform(con)
        finally:
            con.close()
        return counts

    @task
    def quality(counts: dict, ds: str | None = None):
        import duckdb

        from pipeline import config
        from pipeline import quality as qc
        con = duckdb.connect(config.DB_PATH)
        try:
            qc.assert_quality(con, date.fromisoformat(ds))
        finally:
            con.close()

    quality(transform(load(extract())))


ad_pipeline()
