"""flip/model.py — jednotný tvar inzerátu napříč všemi 4 zdroji (byty i domy).

Všechna obsahová pole jsou Optional: None = zdroj hodnotu nedal nebo ji nešlo
spolehlivě přečíst. Nikdy se nehádá.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class Listing:
    source: str                        # "sreality" | "realingo" | "bazos" | "reality_sumperk"
    external_id: str                   # ID na portálu
    url: str
    kind: str                          # "byt" | "dum"

    subtype: str | None = None         # byt: "2+1" …; dům: "Rodinný", "Chalupa", "Zemědělská usedlost" …
    price: int | None = None           # Kč; None = "cena na dotaz"
    area_m2: float | None = None       # užitná plocha
    land_m2: float | None = None       # pozemek (jen domy)
    town: str | None = None            # kanonický název sledované obce (flip/towns.py), None = mimo oblast
    locality: str | None = None        # lokalita tak, jak ji psal portál (pro zprávu)
    description: str | None = None
    building_condition: str | None = None  # strukturovaný stav (jen Sreality): "Před rekonstrukcí" …
    energy_rating: str | None = None   # A–G
    gps_lat: float | None = None
    gps_lon: float | None = None

    # Bazoš nemá strukturovaná pole -> plocha/dispozice z textu; "low" = něco chybí.
    parse_confidence: str = "high"
    scraped_at: str = field(default_factory=utc_now_iso)

    # vyplní pipeline: (source, url) dalších portálů se stejnou nemovitostí
    duplicate_sources: list[tuple[str, str]] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str]:
        return (self.source, self.external_id)

    @property
    def in_area(self) -> bool:
        return self.town is not None

    @property
    def price_per_m2(self) -> float | None:
        if self.price and self.area_m2 and self.area_m2 > 0:
            return self.price / self.area_m2
        return None


@dataclass
class FetchResult:
    """Výsledek jednoho běhu zdroje.

    `raw_count` = kolik inzerátů zdroj vůbec rozparsoval (PŘED filtry). Když je 0,
    nejspíš se změnil web (redesign) — to se počítá jako selhání, ne jako "nic nového".
    `complete` = False, když zdroj část inzerátů odložil na další běh (Sreality: strop
    detailů). Při prvním běhu se pak shrnutí posílá až s kompletním seznamem.
    """

    listings: list[Listing]
    raw_count: int
    complete: bool = True
