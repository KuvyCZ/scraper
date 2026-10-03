"""
sources/base.py — jednotné datové schéma + abstraktní rozhraní zdroje.

`Listing` je normalizovaný inzerát NEZÁVISLÝ na portálu. Každý scraper
(sources/sreality.py, sources/bezrealitky.py, …) vrací `list[Listing]` a
DB, scoring i notifier pracují JEN s `Listing` — o konkrétní portál se nestarají.

Deduplikace je GLOBÁLNÍ přes `(source, source_id)` — stejná nemovitost může být
na více portálech a stejný hash_id se na Sreality může recyklovat při reinzerci
(to ošetří seen_first/seen_last v DB, ne tahle vrstva).

Všechna „obsahová" pole jsou Optional (None = portál hodnotu nedal). Povinné je
jen to, bez čeho inzerát nedává smysl: odkud je, jeho ID a URL.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any


@dataclass(slots=True)
class Listing:
    """Normalizovaný inzerát napříč portály. Viz mapování polí v sources/sreality.py."""

    # --- identita / původ (povinné) ---
    source: str                       # identifikátor portálu, např. "sreality"
    source_id: str                    # ID inzerátu na portálu (Sreality hash_id) jako string
    url: str                          # veřejná URL na detail inzerátu

    # --- cena & velikost ---
    price: int | None = None          # CZK za nemovitost; None = neuvedeno ("Info o ceně u RK")
    area_m2: float | None = None      # užitná plocha v m²
    land_area_m2: float | None = None  # plocha POZEMKU (jen domy; Sreality `estate_area`).
                                       # U domů zásadní: cena/m² stavby je bez pozemku zavádějící.

    # --- charakteristika nemovitosti ---
    property_type: str = "byt"            # "byt" | "dum" — MUSÍ segmentovat mediány,
                                          # jinak by Kč/m² domů znečistilo mediány bytů
    disposition: str | None = None        # byty: "2+kk", "3+1"; domy: "Rodinný", "Chalupa", …
    building_condition: str | None = None  # strukturovaný STAV ("Před rekonstrukcí", "Po rekonstrukci", …)
                                           # = hlavní signál scoringu; spolehlivější než keyword scan popisu
    energy_rating: str | None = None      # energetická třída "A"–"G" (nebo None)
    description: str | None = None        # text inzerátu (u Sreality jen z detailu)

    # --- lokalita ---
    locality_text: str | None = None  # člověku čitelné, např. "Olomouc, Olomouc"
    district: str | None = None       # OKRES ("Olomouc", "Šumperk") — klíč pro medián cena/m² per okres
    region: str | None = None         # kraj ("Olomoucký kraj")
    gps_lat: float | None = None
    gps_lon: float | None = None

    # --- čas ---
    listed_date: date | None = None   # "datum inzerce" dle portálu (u Sreality pole `since`).
                                      # POZOR: bývá to datum poslední úpravy/reinzerce, NE původního
                                      # vystavení → spolehlivá "doba inzerce" se počítá z DB seen_first.

    def dedup_key(self) -> tuple[str, str]:
        """Globální deduplikační klíč napříč portály."""
        return (self.source, self.source_id)

    @property
    def price_per_m2(self) -> int | None:
        """Cena za m² (dopočet). None, když chybí cena nebo plocha — nehádáme."""
        if self.price and self.area_m2 and self.area_m2 > 0:
            return round(self.price / self.area_m2)
        return None

    def as_dict(self) -> dict[str, Any]:
        """Plochý dict pro uložení do DB / serializaci (date -> ISO string)."""
        data = asdict(self)
        data["listed_date"] = self.listed_date.isoformat() if self.listed_date else None
        return data


class Source(ABC):
    """
    Abstraktní portálový zdroj. Konkrétní implementace (sources/sreality.py …)
    si v `__init__` vezme svou konfiguraci a implementuje `fetch()`.

    Hranice chyb (důležité pro robustnost celého běhu):
      * Chyby JEDNOTLIVÉHO inzerátu řeš UVNITŘ fetch() — vadný inzerát přeskoč a loguj,
        ať kvůli jednomu nepřijdeš o zbytek.
      * Fatální chybu zdroje (nedostupný portál, změna API) smíš nechat probublat —
        main.py každý zdroj obaluje try/except, takže ostatní zdroje běží dál.
    """

    #: krátký identifikátor zdroje; musí odpovídat Listing.source (např. "sreality")
    name: str

    @abstractmethod
    def fetch(self) -> list[Listing]:
        """Stáhne inzeráty z portálu a vrátí je jako normalizované `Listing`."""
        raise NotImplementedError


if __name__ == "__main__":
    # Smoke test schématu (spustitelné: python sources/base.py).
    # UTF-8 jen pro tenhle výpis na Windows konzoli (cp1250 by spadlo na diakritice).
    import sys

    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    # Hodnoty z reálného běhu test_api.py (#2994335820 — byt "Po rekonstrukci", štítek G).
    sample = Listing(
        source="sreality",
        source_id="2994335820",
        url="https://www.sreality.cz/detail/prodej/byt/3+1/jindrichov-jindrichov/2994335820",
        price=2_490_000,
        area_m2=69.0,
        disposition="3+1",
        building_condition="Po rekonstrukci",
        energy_rating="G",
        description="Jindřichov už dávno není místo, kde lišky dávají dobrou noc…",
        locality_text="Jindřichov, Šumperk",
        district="Šumperk",
        region="Olomoucký kraj",
        gps_lat=50.1001352514941,
        gps_lon=16.993815479172,
        listed_date=date(2026, 6, 15),
    )

    print("dedup_key   :", sample.dedup_key())
    print("price_per_m2:", sample.price_per_m2, "Kč/m²")

    # Inzerát bez ceny i plochy -> price_per_m2 musí být None (cold-start, nehádáme).
    empty = Listing(source="sreality", source_id="0", url="x")
    print("prázdný price_per_m2 (čekám None):", empty.price_per_m2)

    print("\nas_dict():")
    for key, value in sample.as_dict().items():
        print(f"  {key:18}= {value!r}")

    print("\n✅ base.py OK — Listing i Source se importují a chovají dle očekávání.")
