"""
sources/bezrealitky.py — scraper Bezrealitky nad OVĚŘENÝM veřejným GraphQL API.

Endpoint (ověřeno živě, bez přihlášení, introspekce povolená):
  POST https://api.bezrealitky.cz/graphql/   query `listAdverts`

Bezrealitky = inzeráty OD MAJITELŮ (bez RK). Menší inventář než Sreality, ale přesně
naše cílovka: motivovaní soukromí prodejci, často byty před rekonstrukcí.

Proč je to jednodušší než Sreality:
  * Seznam `listAdverts` vrací rovnou VŠE včetně `description` a strukturovaného
    `condition` → žádné per-inzerát detail cally (jeden GraphQL request stáhne stránku).
  * Proto se tu neřeší `known_source_ids` (nic se „nedotahuje" zvlášť).

Mapování na Listing (jednotné schéma z base.py):
  source_id          = id
  url                = /nemovitosti-byty-domy/{uri}                 (ověřeno 200)
  price              = price (0/None => None)
  area_m2            = surface
  disposition        = Disposition enum -> "2+kk", "3+1", …          (sjednoceno se Sreality)
  building_condition = Condition enum   -> "Před rekonstrukcí", …    (sjednoceno se Sreality)
  energy_rating      = penb (A–G)
  description        = description
  district / region  = regionTree: "okres Olomouc" -> "Olomouc" / "Olomoucký kraj"
                       (shodné s okresy Sreality => medián cena/m² se pooluje napříč zdroji)
  gps_lat/lon        = gps.lat / gps.lng
  listed_date        = None (veřejné API datum aktivace nevystavuje; „doba inzerce" z DB seen_first)

Pozn.: API vrací i `originalPrice` (původní cena před zlevněním). Zatím se nepoužívá;
nabízí se jako levný den-1 signál poklesu ceny (viz scoring._signal_pokles).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterator

import httpx

from .base import Listing, Source

log = logging.getLogger("sources.bezrealitky")

GQL_URL = "https://api.bezrealitky.cz/graphql/"
DETAIL_URL = "https://www.bezrealitky.cz/nemovitosti-byty-domy/{uri}"

# Dispozice (Bezrealitky enum -> stejný tvar jako Sreality, ať se medián pooluje per dispozice).
_DISPOSITION: dict[str, str] = {
    "GARSONIERA": "Garsoniéra",
    "DISP_1_KK": "1+kk", "DISP_1_1": "1+1",
    "DISP_2_KK": "2+kk", "DISP_2_1": "2+1",
    "DISP_3_KK": "3+kk", "DISP_3_1": "3+1",
    "DISP_4_KK": "4+kk", "DISP_4_1": "4+1",
    "DISP_5_KK": "5+kk", "DISP_5_1": "5+1",
    "DISP_6_KK": "6+kk", "DISP_6_1": "6+1",
    "DISP_7_KK": "7+kk", "DISP_7_1": "7+1",
    "OSTATNI": "Ostatní",
}

# Stav (Bezrealitky Condition -> české názvy, na které reaguje scoring.COND_SCORE).
_CONDITION: dict[str, str] = {
    "BEFORE_RECONSTRUCTION": "Před rekonstrukcí",
    "BAD": "Špatný",
    "DEMOLITION": "K demolici",
    "IN_RECONSTRUCTION": "V rekonstrukci",
    "AFTER_PARTIAL_RECONSTRUCTION": "Po částečné rekonstrukci",
    "GOOD": "Dobrý",
    "VERY_GOOD": "Velmi dobrý",
    "AFTER_RECONSTRUCTION": "Po rekonstrukci",
    "NEW": "Novostavba",
    "CONSTRUCTION": "Ve výstavbě",
    "PROJECT": "Projekt",
    # UNDEFINED -> None (stav neznámý)
}

_ENERGY = {"A", "B", "C", "D", "E", "F", "G"}

# GraphQL dotaz. offerType/estateType/order jsou konstanty (inline); proměnné jen region/stránkování.
_QUERY = """
query Listings($region: ID, $limit: Int, $offset: Int) {
  listAdverts(
    offerType: [PRODEJ]
    estateType: [BYT]
    regionId: $region
    limit: $limit
    offset: $offset
    order: TIMEORDER_DESC
  ) {
    totalCount
    list {
      id
      uri
      price
      surface
      disposition
      condition
      penb
      gps { lat lng }
      description
      regionTree(locale: CS) { name }
    }
  }
}
"""


class BezrealitkySource(Source):
    """Zdroj Bezrealitky (GraphQL). Vrací `list[Listing]` přes jednotné schéma z base.py."""

    name = "bezrealitky"

    def __init__(
        self,
        region_ids: list[int] | None = None,   # Bezrealitky regionId (495 = Olomoucký kraj)
        *,
        page_size: int = 100,
        max_listings: int | None = None,        # strop inzerátů na region (None = vše); hl. pro testy
        rate_limit_s: float = 1.6,              # pauza mezi requesty (>= 1,5 s)
        timeout: float = 25.0,
        user_agent: str = "realitni-scraper/0.1 (osobni nekomercni projekt)",
        **_ignored: Any,                        # snese i kwargs společné se Sreality (např. known_source_ids)
    ) -> None:
        self.region_ids = region_ids or [495]   # 495 = Olomoucký kraj (ověřeno přes czechRegions)
        self.page_size = page_size
        self.max_listings = max_listings
        self.rate_limit_s = rate_limit_s
        self.timeout = timeout
        self.headers = {
            "User-Agent": user_agent,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self._last_request_ts = 0.0

    # ------------------------------------------------------------------ #
    # HTTP vrstva (GraphQL POST) s rate-limitingem                       #
    # ------------------------------------------------------------------ #

    def _respect_rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_ts
        if elapsed < self.rate_limit_s:
            time.sleep(self.rate_limit_s - elapsed)
        self._last_request_ts = time.monotonic()

    def _post(self, client: httpx.Client, variables: dict[str, Any]) -> dict[str, Any]:
        self._respect_rate_limit()
        resp = client.post(GQL_URL, json={"query": _QUERY, "variables": variables})
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("errors"):
            raise RuntimeError(f"GraphQL errors: {payload['errors']}")
        return (payload.get("data") or {}).get("listAdverts") or {}

    # ------------------------------------------------------------------ #
    # Stránkování                                                        #
    # ------------------------------------------------------------------ #

    def _iter_adverts(self, client: httpx.Client, region_id: int) -> Iterator[dict[str, Any]]:
        offset = 0
        fetched = 0
        while True:
            if self.max_listings is not None and fetched >= self.max_listings:
                return
            limit = self.page_size
            if self.max_listings is not None:
                limit = min(limit, self.max_listings - fetched)
            try:
                data = self._post(client, {"region": str(region_id), "limit": limit, "offset": offset})
            except Exception as exc:  # noqa: BLE001 – výpadek stránky nezahodí už stažené
                log.error("listAdverts selhal (region=%s, offset=%s): %s", region_id, offset, exc)
                return

            adverts = data.get("list") or []
            if not adverts:
                return
            for advert in adverts:
                yield advert
                fetched += 1

            total = data.get("totalCount") or 0
            offset += len(adverts)
            log.info("region=%s: %d/%s inzerátů ze seznamu", region_id, offset, total)
            if offset >= total:
                return

    # ------------------------------------------------------------------ #
    # Normalizace                                                        #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _location(region_tree: list[dict[str, Any]]) -> tuple[str | None, str | None, str | None]:
        """Z regionTree vytáhne (region kraj, okres, město). 'okres Olomouc' -> 'Olomouc'."""
        region = district = city = None
        for node in region_tree or []:
            name = (node.get("name") or "").strip()
            if not name:
                continue
            if name.lower().endswith("kraj"):
                region = name
            elif name.lower().startswith("okres "):
                district = name[len("okres "):].strip()
            else:
                city = name  # nejspecifičtější (poslední vyhrává)
        return region, district, city

    def _normalize(self, advert: dict[str, Any]) -> Listing:
        gps = advert.get("gps") or {}
        region, district, city = self._location(advert.get("regionTree") or [])
        penb = (advert.get("penb") or "").upper()
        surface = advert.get("surface")
        return Listing(
            source=self.name,
            source_id=str(advert.get("id")),
            url=DETAIL_URL.format(uri=advert.get("uri")),
            price=advert.get("price") or None,                       # 0 => None
            area_m2=float(surface) if surface else None,
            disposition=_DISPOSITION.get(advert.get("disposition")),
            building_condition=_CONDITION.get(advert.get("condition")),
            energy_rating=penb if penb in _ENERGY else None,
            description=advert.get("description") or None,
            locality_text=", ".join(p for p in (city, district) if p) or None,
            district=district,
            region=region,
            gps_lat=gps.get("lat"),
            gps_lon=gps.get("lng"),
            listed_date=None,  # veřejné API datum aktivace nevystavuje; doba inzerce z DB seen_first
        )

    # ------------------------------------------------------------------ #
    # Veřejné API zdroje                                                 #
    # ------------------------------------------------------------------ #

    def fetch(self) -> list[Listing]:
        listings: list[Listing] = []
        log.info("Bezrealitky fetch: regiony=%s", self.region_ids)
        with httpx.Client(headers=self.headers, timeout=self.timeout, follow_redirects=True) as client:
            for region_id in self.region_ids:
                for advert in self._iter_adverts(client, region_id):
                    try:
                        listings.append(self._normalize(advert))
                    except Exception as exc:  # noqa: BLE001 – jeden vadný inzerát přeskoč
                        log.warning("Přeskakuji inzerát %s: %s", advert.get("id"), exc)
        log.info("Bezrealitky fetch hotovo: %d inzerátů", len(listings))
        return listings


if __name__ == "__main__":
    # Smoke test proti živému GraphQL (malý strop).
    import sys

    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    src = BezrealitkySource(region_ids=[495], max_listings=8)
    items = src.fetch()

    print(f"\n=== {len(items)} Listing objektů ===")
    for i, lst in enumerate(items, 1):
        ppm2 = f"{lst.price_per_m2:,} Kč/m²".replace(",", " ") if lst.price_per_m2 else "—"
        price = f"{lst.price:,} Kč".replace(",", " ") if lst.price else "— (info)"
        print(f"\n[{i}] #{lst.source_id}  {lst.disposition or '?'}  {lst.area_m2 or '?'} m²  |  {price} ({ppm2})")
        print(f"    📍 {lst.locality_text} · okres {lst.district} · {lst.region}")
        print(f"    🔨 stav: {lst.building_condition or '—'}  |  ⚡ {lst.energy_rating or '—'}  |  vystaveno: {lst.listed_date or '—'}")
        print(f"    🔗 {lst.url}")
        if lst.description:
            print(f"    📝 {lst.description[:110].replace(chr(10), ' ')}…")

    s_stav = sum(1 for x in items if x.building_condition)
    s_okres = sum(1 for x in items if x.district)
    print(f"\n✅ bezrealitky.py OK — {s_stav}/{len(items)} má stav, {s_okres}/{len(items)} má okres (pro medián).")
