"""Transform stage: run `dbt build` (models + tests) over the staging tables.

Runs dbt as a subprocess rather than in-process: dbt-duckdb holds its
warehouse connection open after an in-process invoke, which blocks any later
connection to the same file with different options (e.g. read_only). A
subprocess guarantees the connection closes with it.
"""

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import config

log = logging.getLogger("pipeline.dbt")

DBT_DIR = config.PROJECT_ROOT / "dbt"


def _dbt_executable() -> str:
    candidate = Path(sys.executable).parent / "dbt"
    if candidate.exists():
        return str(candidate)
    found = shutil.which("dbt")
    if found:
        return found
    raise RuntimeError("dbt executable not found; pip install dbt-duckdb")


def dbt_build():
    cmd = [
        _dbt_executable(), "build",
        "--project-dir", str(DBT_DIR),
        "--profiles-dir", str(DBT_DIR),
        "--log-level", "warn",
    ]
    env = {**os.environ, "DB_PATH": str(config.DB_PATH)}
    res = subprocess.run(cmd, env=env, cwd=config.PROJECT_ROOT,
                         capture_output=True, text=True, timeout=600)
    if res.returncode != 0:
        log.error("dbt build failed:\n%s\n%s", res.stdout[-3000:], res.stderr[-1000:])
        raise RuntimeError("dbt build failed (model error or test failure)")
    log.info("dbt build succeeded")
