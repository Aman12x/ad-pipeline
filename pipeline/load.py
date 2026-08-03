"""Load: parse landed raw JSON into typed rows and upsert into staging.

Each platform's schema is normalized here (and only here):
  - Google: camelCase, int64-as-string, costMicros -> spend dollars
  - Meta: actions array -> purchase conversions
Rows that fail validation go to data/rejected/ with a reason instead of
aborting the run or silently loading garbage.

Staging upserts on the natural key (source, campaign_id, stat_date) via
INSERT OR REPLACE, so re-pulling the trailing window restates rows in place --
re-running a day is always safe.
"""

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb

from . import config

log = logging.getLogger("pipeline.load")

DDL = """
CREATE TABLE IF NOT EXISTS stg_ad_stats (
    source            TEXT NOT NULL,
    campaign_id       TEXT NOT NULL,
    campaign_name     TEXT,
    stat_date         DATE NOT NULL,
    impressions       BIGINT,
    clicks            BIGINT,
    spend             DOUBLE,
    platform_conversions DOUBLE,
    run_id            TEXT,
    loaded_at         TIMESTAMP,
    PRIMARY KEY (source, campaign_id, stat_date)
);
CREATE TABLE IF NOT EXISTS stg_orders (
    order_id     TEXT PRIMARY KEY,
    order_date   DATE,
    utm_source   TEXT,
    utm_campaign TEXT,
    revenue      DOUBLE,
    run_id       TEXT,
    loaded_at    TIMESTAMP
);
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id        TEXT PRIMARY KEY,
    as_of         DATE,
    started_at    TIMESTAMP,
    finished_at   TIMESTAMP,
    status        TEXT,
    rows_ad_stats INTEGER,
    rows_orders   INTEGER,
    rows_rejected INTEGER
);
"""


def _reject(bucket: list, row: dict, reason: str):
    bucket.append({"reason": reason, "row": row})


def _parse_google(payload: dict, rejected: list) -> list[tuple]:
    rows = []
    for r in payload.get("results", []):
        try:
            impressions = int(r["metrics"]["impressions"])
            clicks = int(r["metrics"]["clicks"])
            spend = int(r["metrics"]["costMicros"]) / 1_000_000
            conv = float(r["metrics"]["conversions"])
            stat_date = date.fromisoformat(r["segments"]["date"])
            cid, name = r["campaign"]["id"], r["campaign"]["name"]
        except (KeyError, ValueError, TypeError) as e:
            _reject(rejected, r, f"google parse error: {e}")
            continue
        if min(impressions, clicks, spend, conv) < 0 or clicks > impressions:
            _reject(rejected, r, "google sanity check failed")
            continue
        rows.append(("google", cid, name, stat_date, impressions, clicks,
                     round(spend, 2), conv))
    return rows


def _parse_meta(payload: dict, rejected: list) -> list[tuple]:
    rows = []
    for r in payload.get("data", []):
        try:
            actions = {a["action_type"]: float(a["value"])
                       for a in r.get("actions", [])}
            impressions = int(r["impressions"])
            clicks = int(r["clicks"])
            spend = float(r["spend"])
            conv = actions.get("purchase", 0.0)
            stat_date = date.fromisoformat(r["date_start"])
            cid, name = r["campaign_id"], r["campaign_name"]
        except (KeyError, ValueError, TypeError) as e:
            _reject(rejected, r, f"meta parse error: {e}")
            continue
        if min(impressions, clicks, spend, conv) < 0 or clicks > impressions:
            _reject(rejected, r, "meta sanity check failed")
            continue
        rows.append(("meta", cid, name, stat_date, impressions, clicks,
                     round(spend, 2), conv))
    return rows


def _parse_orders(payload: dict, rejected: list) -> list[tuple]:
    rows = []
    for r in payload.get("orders", []):
        try:
            rows.append((r["order_id"], date.fromisoformat(r["order_date"]),
                         r["utm_source"], r["utm_campaign"], float(r["revenue"])))
        except (KeyError, ValueError, TypeError) as e:
            _reject(rejected, r, f"order parse error: {e}")
    return rows


def load_all(con: duckdb.DuckDBPyConnection, landed: dict[str, Path],
             run_id: str) -> dict:
    con.execute(DDL)
    now = datetime.now(timezone.utc)
    rejected: list = []

    ad_rows = []
    ad_rows += _parse_google(json.loads(landed["google"].read_text()), rejected)
    ad_rows += _parse_meta(json.loads(landed["meta"].read_text()), rejected)
    order_rows = _parse_orders(json.loads(landed["firstparty"].read_text()), rejected)

    con.executemany(
        "INSERT OR REPLACE INTO stg_ad_stats VALUES (?,?,?,?,?,?,?,?,?,?)",
        [row + (run_id, now) for row in ad_rows],
    )
    con.executemany(
        "INSERT OR REPLACE INTO stg_orders VALUES (?,?,?,?,?,?,?)",
        [row + (run_id, now) for row in order_rows],
    )

    if rejected:
        config.REJECTED_DIR.mkdir(parents=True, exist_ok=True)
        dead = config.REJECTED_DIR / f"{run_id}.json"
        dead.write_text(json.dumps(rejected, indent=1))
        log.warning("%d rows rejected -> %s", len(rejected), dead)

    log.info("upserted %d ad-stat rows, %d orders (%d rejected)",
             len(ad_rows), len(order_rows), len(rejected))
    return {"ad_stats": len(ad_rows), "orders": len(order_rows),
            "rejected": len(rejected)}
