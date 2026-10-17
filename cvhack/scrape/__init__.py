"""Scrapers that turn public catalogs into the real parts dataset.

    python -m cvhack.scrape.build          # fetch, select ~500 parts, write data/parts_catalog.json
    python -m cvhack.parts_graph --write   # rebuild data/parts_graph.json from it

Downloads are cached under .cache/scrape/ (gitignored) and only ever parsed as
data (JSON and KiCad s-expressions), never executed.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
CACHE = ROOT / ".cache" / "scrape"
