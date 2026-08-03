import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
REJECTED_DIR = DATA_DIR / "rejected"
DB_PATH = os.environ.get("DB_PATH", str(DATA_DIR / "warehouse.duckdb"))

SIM_BASE_URL = os.environ.get("SIM_BASE_URL", "http://127.0.0.1:8787")

# Trailing re-pull window: conversions restate for up to ~7 days, so re-pull
# 14 days to be safe. Every run upserts this whole window.
WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "14"))

MAX_RETRIES = 5
