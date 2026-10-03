"""investment/sources/base.py — sdílený tvar inzerátu napříč 4 zdroji + rozhraní modulu.

`Listing` je NEZÁVISLÝ na konkrétním portálu (Sreality/Realingo/Bazoš/reality-sumperk.cz).
Pozor, tohle je JINÝ (jednodušší) tvar než `sources.base.Listing` používaný hlavním
monitoringem (main.py) — investiční multi-zdrojový režim má vlastní schéma, ať se
oba světy nepletou a hlavní monitoring zůstává nedotčený.

Konvence pojmenování: zadání používá `fetchListings` (JS styl), tady je to
`fetch_listings` — projekt je Python a drží se PEP 8 (snake_case) všude jinde.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from investment.config import InvestmentConfig


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class Listing:
    """Normalizovaný inzerát napříč 4 zdroji. Všechna obsahová pole jsou Optional
    (None = zdroj hodnotu nedal / nešlo ji spolehlivě rozparsovat — nikdy nehádáme)."""

    source: str                       # "sreality" | "realingo" | "bazos" | "reality_sumperk"
    external_id: str                  # ID inzerátu na daném portálu (string)
    url: str

    price: int | None = None          # CZK
    size_m2: float | None = None
    layout: str | None = None         # "2+1", "2+kk", "3+1", …
    location: str | None = None       # obec/město (bez ulice)
    description: str | None = None

    scraped_at: str = field(default_factory=utc_now_iso)  # ISO 8601 UTC

    # Bazoš má nestrukturované popisy -> size_m2/layout se parsují regexem z textu.
    # "low" = aspoň jedno z nich se nepodařilo spolehlivě určit; inzerát se PŘESTO
    # nechává projít (nefiltruje se pryč), jen se to poznačí v alertu.
    parse_confidence: str = "high"    # "high" | "low"

    # Volitelné strukturované pole, které má jen Sreality (building_condition) —
    # když je k dispozici, je spolehlivější než keyword scan popisu.
    building_condition: str | None = None

    # Vyplní investment/dedup.py — (source, url) DALŠÍCH inzerátů napříč zdroji,
    # co jsou (podle price/size/layout/lokalita tolerance) stejný byt jako tenhle.
    duplicate_sources: list[tuple[str, str]] = field(default_factory=list)

    def dedup_key(self) -> tuple[str, str]:
        return (self.source, self.external_id)

    @property
    def price_per_m2(self) -> float | None:
        if self.price and self.size_m2 and self.size_m2 > 0:
            return self.price / self.size_m2
        return None


class SourceModule(Protocol):
    """Rozhraní, které implementuje každý ze 4 zdrojových modulů."""

    name: str

    def fetch_listings(self, config: "InvestmentConfig") -> list[Listing]: ...


if __name__ == "__main__":
    import sys

    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    sample = Listing(
        source="bazos", external_id="221376860", url="https://reality.bazos.cz/inzerat/221376860/x.php",
        price=1_900_000, size_m2=47.0, layout="2+1", location="Maletín",
        description="Prodej bytu 2+1 v Maletíně, po částečné rekonstrukci.",
        parse_confidence="low",
    )
    print("dedup_key:", sample.dedup_key())
    print(sample)
    print("\n✅ investment/sources/base.py OK")
