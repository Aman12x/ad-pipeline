"""Build a demo warehouse without any running servers.

Routes the pipeline's HTTP fetches through FastAPI's TestClient (in-process,
no network), then replays the last three daily runs so the warehouse contains
realistic restatement history. Used by the dashboard on Streamlit Cloud,
where the simulator/pipeline terminals don't exist; also handy locally:

    python scripts/seed_demo.py
"""

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Bump when the simulator's data generation changes materially: the dashboard
# reseeds any warehouse stamped with an older version.
SEED_VERSION = 2


def seed() -> str:
    from fastapi.testclient import TestClient

    from pipeline import config, extract, run
    from simulator.app import app

    client = TestClient(app)

    def in_process_fetch(url: str, params: dict) -> dict:
        path = url.replace(config.SIM_BASE_URL, "")
        for _ in range(config.MAX_RETRIES):
            resp = client.get(path, params=params)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code != 429:
                resp.raise_for_status()
        raise RuntimeError(f"giving up on {path}")

    original = extract.fetch_with_retry
    extract.fetch_with_retry = in_process_fetch
    try:
        today = date.today()
        for offset in (2, 1, 0):  # 3 daily runs -> restatement history
            as_of = today - timedelta(days=offset)
            if run.main(["--as-of", as_of.isoformat()]) != 0:
                raise RuntimeError(f"seed run for {as_of} failed")
    finally:
        extract.fetch_with_retry = original

    import duckdb
    con = duckdb.connect(config.DB_PATH)
    con.execute("CREATE OR REPLACE TABLE seed_meta AS SELECT ? AS version",
                [SEED_VERSION])
    con.close()
    return config.DB_PATH


if __name__ == "__main__":
    print(f"seeded {seed()}")
