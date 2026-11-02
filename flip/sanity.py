"""flip/sanity.py — kontrola nesmyslných hodnot, než se inzerát uloží a oskóruje.

Vzniklo z reálných dat z NAS (říjen 2026):
  * pronájem v kategorii prodej (2+1 za "11 000 Kč") dostal skóre 100 a 🔥;
  * Bazoš u domů bral plochu pozemku ("RD Lesnice 691 m²") jako plochu domu,
    takže Kč/m² vyšlo extrémně nízko a dům vypadal jako trhák;
  * Sreality občas vrátí pozemek 1 m²;
  * Realingo u kategorie "ostatní byty" nemá dispozici ani plochu, i když jsou v popisu.

Pravidlo zůstává: nehádat. Podezřelá hodnota se zahodí (None), nevymýšlí se nová.
"""

from __future__ import annotations

import logging

from flip.model import Listing
from flip.text import looks_like_rental, parse_area, parse_layout

log = logging.getLogger("flip.sanity")

MIN_SALE_PRICE = 50_000          # pod tím to není prodejní cena (nájem, poplatek, "1 Kč")
RENTAL_PRICE_MAX = 100_000       # pronájem v textu + cena pod tím = měsíční nájem
MIN_LAND_M2 = 10
FLAT_AREA_RANGE = (12, 300)
HOUSE_AREA_MAX = 600             # "dům 691 m²" na Bazoši je skoro vždy pozemek


def clean(lst: Listing) -> Listing | None:
    """Opraví/zahodí podezřelé hodnoty. None = inzerát vůbec není prodej (pronájem)."""
    if lst.price is not None and lst.price < RENTAL_PRICE_MAX and looks_like_rental(lst.description):
        log.info("%s #%s: vypadá jako pronájem (%s Kč) — přeskočeno", lst.source, lst.external_id, lst.price)
        return None
    if lst.price is not None and lst.price < MIN_SALE_PRICE:
        lst.price = None

    if lst.land_m2 is not None and lst.land_m2 < MIN_LAND_M2:
        lst.land_m2 = None

    # chybějící dispozice/plocha: zkusit popis (Realingo "ostatní byty")
    if lst.kind == "byt" and lst.subtype is None:
        lst.subtype = parse_layout(lst.description)
    if lst.area_m2 is None and lst.description:
        lst.area_m2 = parse_area(lst.description)

    if lst.area_m2 is not None:
        if lst.kind == "byt" and not FLAT_AREA_RANGE[0] <= lst.area_m2 <= FLAT_AREA_RANGE[1]:
            lst.area_m2 = None
        elif lst.kind == "dum":
            if lst.land_m2 is not None and abs(lst.area_m2 - lst.land_m2) < 1:
                lst.area_m2 = None                       # stejné číslo = pozemek
            elif lst.area_m2 > HOUSE_AREA_MAX:
                if lst.land_m2 is None:
                    lst.land_m2 = lst.area_m2
                lst.area_m2 = None
    return lst
