from __future__ import annotations
import os
from pathlib import Path

PROJECT_NAME = "Dayseal"
API_PORT = 10700
_DATA_ENV = os.environ.get("DAYSEAL_DATA", "").strip()
DATA_DIR = Path(_DATA_ENV) if _DATA_ENV else Path(__file__).resolve().parents[2] / "data"
DB_PATH = DATA_DIR / "dayseal.db"
