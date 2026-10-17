"""Single entry point for the parts DB.

Today this reads the mock JSON. When B's DB lands, point PARTS_SOURCE at it
(or replace _load_from_b) and nothing else in C's code changes.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from cvhack.contracts import Part

MOCK_PATH = Path(__file__).resolve().parent / "mocks" / "parts.json"


def _load_from_b() -> list[Part] | None:
    """Hook for B's parts DB. Return None to fall back to the mock."""
    try:
        from cvhack.data import parts_db  # type: ignore  # B's module, once it exists
    except ImportError:
        return None
    return [p if isinstance(p, Part) else Part.model_validate(p) for p in parts_db.all_parts()]


@lru_cache(maxsize=1)
def load_parts() -> dict[str, Part]:
    src = os.environ.get("PARTS_SOURCE")
    if src:
        parts = [Part.model_validate(p) for p in json.loads(Path(src).read_text())]
    else:
        parts = _load_from_b() or [Part.model_validate(p) for p in json.loads(MOCK_PATH.read_text())]
    return {p.mpn: p for p in parts}
