"""JLCPCB / LCSC component catalog, via the jlcparts mirror (https://yaqwsx.github.io/jlcparts/).

jlcparts republishes JLCPCB's assembly catalog daily as gzip'd JSONL shards, one
set per subcategory. Each component row carries the LCSC number, MPN, price
tiers, stock, datasheet URL and parametric attributes, which are indices into a
shared lookup table of already-normalized values (SI units).
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass, field
from typing import Any

import requests

from cvhack.scrape import CACHE

BASE = "https://yaqwsx.github.io/jlcparts/data/"
SOURCE_PAGE = "https://yaqwsx.github.io/jlcparts/"
DIR = CACHE / "jlcparts"


@dataclass
class Component:
    lcsc: str
    mpn: str
    description: str
    datasheet: str | None
    price_tiers: list[dict[str, Any]]
    stock: int
    joints: int | None
    url: str | None
    subcategory_id: int
    category: str
    subcategory: str
    # attribute name -> {"text": formatted value, "values": {key: (value, kind)}}
    attributes: dict[str, dict[str, Any]] = field(default_factory=dict)

    def attr_text(self, name: str) -> str | None:
        a = self.attributes.get(name)
        return a["text"] if a else None

    def attr_num(self, name: str, key: str | None = None) -> float | None:
        """Numeric value of an attribute; `key` picks one field of a ranged/compound attribute."""
        a = self.attributes.get(name)
        if not a:
            return None
        vals = a["values"]
        v = vals.get(key) if key else vals.get(a["primary"])
        if v is None or not isinstance(v[0], (int, float)) or isinstance(v[0], bool):
            return None
        return float(v[0])

    @property
    def manufacturer(self) -> str | None:
        return self.attr_text("Manufacturer")

    @property
    def package(self) -> str | None:
        return self.attr_text("Package")

    @property
    def is_basic(self) -> bool:
        return (self.attr_text("Basic/Extended") or "").lower() == "basic"

    @property
    def is_preferred(self) -> bool:
        return (self.attr_text("Basic/Extended") or "").lower() == "preferred"

    @property
    def status(self) -> str | None:
        return self.attr_text("Status")


def _get(name: str) -> bytes:
    path = DIR / name
    if path.exists():
        return path.read_bytes()
    r = requests.get(BASE + name, timeout=120)
    r.raise_for_status()
    DIR.mkdir(parents=True, exist_ok=True)
    path.write_bytes(r.content)
    return r.content


def _json_gz(name: str) -> Any:
    return json.loads(gzip.decompress(_get(name)))


class Catalog:
    def __init__(self, refresh: bool = False):
        if refresh and (DIR / "manifest.json").exists():
            for p in DIR.iterdir():
                p.unlink()
        self.manifest = json.loads(_get("manifest.json"))
        self.created = self.manifest["created"]
        self.subcategories = {c["id"]: c for c in self.manifest["categories"]}
        self._lut: list | None = None

    @property
    def lut(self) -> list:
        if self._lut is None:
            self._lut = _json_gz(self.manifest["attributesLut"])
        return self._lut

    def _decode_attr(self, idx: int) -> tuple[str, dict[str, Any]]:
        name, spec = self.lut[idx]
        values = {k: tuple(v) for k, v in spec["values"].items()}
        text = spec["format"]
        for k, (v, kind) in values.items():
            text = text.replace("${" + k + "}", _fmt(v, kind))
        return name, {"text": text, "values": values, "primary": spec.get("primary")}

    def components(self, subcategory_id: int) -> list[Component]:
        sub = self.subcategories[subcategory_id]
        out: list[Component] = []
        for shard in sub["shards"]:
            lines = gzip.decompress(_get(shard)).decode("utf-8").splitlines()
            header = json.loads(lines[0])
            for line in lines[1:]:
                row = json.loads(line)
                g = {k: row[i] for k, i in header.items()}
                attrs = dict(self._decode_attr(i) for i in g.get("attributes") or [])
                out.append(Component(
                    lcsc=g["lcsc"], mpn=g["mfr"], description=g.get("description") or "",
                    datasheet=g.get("datasheet") or None, price_tiers=g.get("price") or [],
                    stock=int(g.get("stock") or 0), joints=g.get("joints"), url=g.get("url"),
                    subcategory_id=subcategory_id, category=sub["category"],
                    subcategory=sub["subcategory"], attributes=attrs,
                ))
        return out

    def search_index(self) -> dict[str, list[tuple[str, str]]]:
        """Lowercased MPN -> [(lcsc, shard)], for looking up exact part numbers."""
        text = gzip.decompress(_get(self.manifest["searchIndex"])).decode("utf-8")
        idx: dict[str, list[tuple[str, str]]] = {}
        for line in text.splitlines():
            lcsc, shard, words = line.split("\t", 2)
            tokens = words.split(" ")
            if len(tokens) > 1:
                idx.setdefault(tokens[1], []).append((lcsc, shard))
        return idx

    def subcategory_of_shard(self, shard: str) -> int | None:
        for sid, sub in self.subcategories.items():
            if shard in sub["shards"]:
                return sid
        return None


def _fmt(v: Any, kind: str) -> str:
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return str(v)
    units = {"voltage": "V", "current": "A", "resistance": "Ω", "capacitance": "F",
             "inductance": "H", "frequency": "Hz", "power": "W", "temperature": "°C",
             "time": "s", "charge": "C"}
    unit = units.get(kind)
    if unit is None or kind == "temperature":
        return f"{v:g}{unit or ''}"
    for scale, sym in [(1e9, "G"), (1e6, "M"), (1e3, "k"), (1, ""), (1e-3, "m"),
                       (1e-6, "µ"), (1e-9, "n"), (1e-12, "p")]:
        if abs(v) >= scale * 0.999 or scale == 1e-12:
            return f"{round(v / scale, 4):g}{sym}{unit}" if v else f"0{unit}"
    return f"{v:g}{unit}"
