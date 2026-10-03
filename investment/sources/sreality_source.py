"""investment/sources/sreality_source.py — adaptér nad existujícím sources/sreality.py.

NEDUPLIKUJE fetch logiku — Sreality má ověřené JSON API (search+detail,
rate-limiting, retry) už hotové v `sources.sreality.SrealitySource` (sdílené i
s hlavním monitoringem main.py). Tenhle modul jen zavolá district-scoped fetch
a převede výsledek na sdílený investiční `Listing` tvar (investment/sources/base.py).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from investment.filters import passes_structural_filters
from investment.sources.base import Listing, utc_now_iso
from sources.sreality import SrealitySource

if TYPE_CHECKING:
    from investment.config import InvestmentConfig

log = logging.getLogger("investment.sources.sreality")

name = "sreality"


def fetch_listings(config: "InvestmentConfig", known_ids: set[str] | None = None) -> list[Listing]:
    source = SrealitySource(
        district_ids=[config.district_id],
        category_main_cb=1, category_type_cb=1,
        known_source_ids=known_ids,
    )
    raw = source.fetch()
    log.info("Sreality: %d inzerátů (okres_id=%s)", len(raw), config.district_id)

    out: list[Listing] = []
    for lst in raw:
        location = lst.locality_text.split(",")[0].strip() if lst.locality_text else None
        listing = Listing(
            source=name, external_id=lst.source_id, url=lst.url,
            price=lst.price, size_m2=lst.area_m2, layout=lst.disposition,
            location=location,
            description=lst.description,
            building_condition=lst.building_condition,
            scraped_at=utc_now_iso(),
        )
        if passes_structural_filters(listing, location, config):
            out.append(listing)

    log.info("Sreality: %d kandidátů po strukturálních filtrech", len(out))
    return out


if __name__ == "__main__":
    import sys

    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    from investment.config import InvestmentConfig

    cfg = InvestmentConfig()
    items = fetch_listings(cfg)
    print(f"\n=== {len(items)} Listing (sreality) ===")
    for it in items[:5]:
        print(it.layout, it.size_m2, "m2", it.price, "Kč", it.location, "|", it.url)
    print("\n✅ investment/sources/sreality_source.py OK")
