"""Locations inside the checker folder. Everything the tool creates stays in this one repo."""

from pathlib import Path

CHECKER_ROOT = Path(__file__).resolve().parents[1]
CACHE_ROOT = CHECKER_ROOT / "cache"  # git-ignored: DigiKey answers, datasheet PDFs, PDF text
