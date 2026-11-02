"""flip/dedup.py — je to stejná nemovitost na jiném portálu?

Duplicita = stejný typ (byt/dům) AND stejná obec AND cena do 2 % AND plocha do
2 m² (domy do 3 %, plochu domu portály uvádějí méně přesně) AND u bytů stejná
dispozice, pokud ji znají obě strany. Bez plochy se neshoduje nic — dva různé
byty v Šumperku za stejnou cenu jsou běžné, falešná shoda by schovala nabídku.

Větší rozdíl ceny (do 15 %) projde, jen když obě nabídky uvádějí STEJNOU ULICI
(Sreality, Realingo; Bazoš ulici nemá) a u bytů obě znají a shodují se v dispozici.
Právě tak se pozná "stejný byt je jinde levnější" (📉 Cheaper on another site).
V datech z října 2026 byly podobně velké byty s rozdílnou cenou vždy na jiných
ulicích — bez ulice by takové spojení schovalo jinou nabídku.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from flip.model import Listing
from flip.towns import _PSC_RE, match_town, normalize

PRICE_TOLERANCE = 0.02
SAME_STREET_PRICE_TOLERANCE = 0.15
_NUMBER_RE = re.compile(r"\b\d+[a-z]?(?:/\d+[a-z]?)?\b")
FLAT_AREA_TOLERANCE_M2 = 2.0
HOUSE_AREA_TOLERANCE_PCT = 0.03


def streets(locality: str | None) -> set[str]:
    """'Šumperk, Šumperk, Zábřežská' / 'Jeremenkova 653, Šumperk' -> {'zabrezska'} / {'jeremenkova'}.
    Části, které jsou obcí nebo její částí, PSČ a čísla domů se vynechají."""
    out = set()
    for part in (locality or "").split(","):
        part = _PSC_RE.sub(" ", part).strip()
        if not part or match_town(part):
            continue
        name = normalize(_NUMBER_RE.sub(" ", part)).strip(" .-/")
        if len(name) >= 3 and not name.isdigit():
            out.add(name)
    return out


def is_duplicate(lst: Listing, other: Mapping[str, Any]) -> bool:
    if lst.source == other["source"]:
        return False
    if lst.kind != other["kind"] or lst.town is None or lst.town != other["town"]:
        return False
    if not (lst.price and other["price"] and lst.area_m2 and other["area_m2"]):
        return False
    gap = abs(lst.price - other["price"]) / min(lst.price, other["price"])
    if gap > PRICE_TOLERANCE:
        same_street = bool(streets(lst.locality) & streets(other["locality"]))
        layout_ok = lst.kind != "byt" or bool(lst.subtype and other["subtype"] and lst.subtype == other["subtype"])
        if not (gap <= SAME_STREET_PRICE_TOLERANCE and same_street and layout_ok):
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
