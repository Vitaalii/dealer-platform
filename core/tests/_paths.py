"""Пути пакетов монорепозитория для тестов (в образе они лежат рядом в /app)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for sub in ("core", "leads", "dealer-cabinet", "billing"):
    path = str(ROOT / sub)
    if path not in sys.path:
        sys.path.insert(0, path)
