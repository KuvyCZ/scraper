"""flip/dedup.py — je to stejná nemovitost na jiném portálu?

Duplicita = stejný typ (byt/dům) AND stejná obec AND cena do 2 % AND plocha do
2 m² (domy do 3 %, plochu domu portály uvádějí méně přesně) AND u bytů stejná
dispozice, pokud ji znají obě strany. Bez plochy se neshoduje nic — dva různé
byty v Šumperku za stejnou cenu jsou běžné, falešná shoda by schovala nabídku.
"""

from __future__ import annotations

from typing import Any, Mapping

from flip.model import Listing

PRICE_TOLERANCE = 0.02
FLAT_AREA_TOLERANCE_M2 = 2.0
HOUSE_AREA_TOLERANCE_PCT = 0.03


def is_duplicate(lst: Listing, other: Mapping[str, Any]) -> bool:
    if lst.source == other["source"]:
        return False
    if lst.kind != other["kind"] or lst.town is None or lst.town != other["town"]:
        return False
    if not (lst.price and other["price"] and lst.area_m2 and other["area_m2"]):
        return False
    if abs(lst.price - other["price"]) / min(lst.price, other["price"]) > PRICE_TOLERANCE:
        return False
    if lst.kind == "byt":
        if abs(lst.area_m2 - other["area_m2"]) > FLAT_AREA_TOLERANCE_M2:
            return False
        if lst.subtype and other["subtype"] and lst.subtype != other["subtype"]:
            return False
    else:
        if abs(lst.area_m2 - other["area_m2"]) / min(lst.area_m2, other["area_m2"]) > HOUSE_AREA_TOLERANCE_PCT:
            return False
    return True
