"""Extract: pull the trailing window from each source and land raw JSON.

Raw files are landed exactly as received (no parsing) under
data/raw/<source>/<as_of>/<start>_<end>.json so any downstream bug can be
replayed without re-hitting the APIs.
"""

import json
import logging
import time
from datetime import date, timedelta
from pathlib import Path

import requests

from . import config

log = logging.getLogger("pipeline.extract")


def fetch_with_retry(url: str, params: dict) -> dict:
    """GET with exponential backoff on 429/5xx, honoring Retry-After."""
    last_err = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=30)
        except requests.RequestException as e:
            last_err = e
            log.warning("attempt %d: connection error %s", attempt, e)
        else:
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = float(resp.headers.get("Retry-After", min(2 ** attempt, 8)))
                last_err = RuntimeError(f"HTTP {resp.status_code}")
                log.warning("attempt %d: HTTP %d from %s, backing off %.1fs",
                            attempt, resp.status_code, url, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
        time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"giving up on {url} after {config.MAX_RETRIES} attempts: {last_err}")


def _land(source: str, as_of: date, start: date, end: date, payload: dict) -> Path:
    out_dir = config.RAW_DIR / source / as_of.isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{start.isoformat()}_{end.isoformat()}.json"
    path.write_text(json.dumps(payload, indent=1))
    return path


def extract_all(as_of: date) -> dict[str, Path]:
    """Pull all sources for the trailing window ending at as_of."""
    start = as_of - timedelta(days=config.WINDOW_DAYS - 1)
    end = as_of
    base = config.SIM_BASE_URL
    pulls = {
        "google": (f"{base}/google/campaign_stats",
                   {"start": start.isoformat(), "end": end.isoformat(),
                    "as_of": as_of.isoformat()}),
        "meta": (f"{base}/meta/insights",
                 {"since": start.isoformat(), "until": end.isoformat(),
                  "as_of": as_of.isoformat()}),
        "firstparty": (f"{base}/firstparty/orders",
                       {"start": start.isoformat(), "end": end.isoformat(),
                        "as_of": as_of.isoformat()}),
    }
    landed = {}
    for source, (url, params) in pulls.items():
        payload = fetch_with_retry(url, params)
        path = _land(source, as_of, start, end, payload)
        landed[source] = path
        log.info("landed %s -> %s", source, path)
    return landed
