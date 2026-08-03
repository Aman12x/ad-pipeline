import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def simulator():
    """Boot the real simulator in a subprocess; yield its base URL."""
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "simulator.app:app",
         "--port", str(port), "--log-level", "warning"],
        cwd=PROJECT_ROOT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(50):
            try:
                if requests.get(f"{base}/health", timeout=1).ok:
                    break
            except requests.RequestException:
                time.sleep(0.2)
        else:
            raise RuntimeError("simulator failed to start")
        yield base
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture()
def isolated_env(tmp_path, monkeypatch, simulator):
    """Point the pipeline at the test simulator and a throwaway data dir."""
    from pipeline import config

    monkeypatch.setattr(config, "SIM_BASE_URL", simulator)
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "warehouse.duckdb"))
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(config, "REJECTED_DIR", tmp_path / "rejected")
    return tmp_path
