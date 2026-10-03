"""
sources/sreality.py — scraper Sreality nad OVĚŘENÝM veřejným v1 API.

Endpointy (ověřeno živě, bez přihlášení):
  SEARCH  https://www.sreality.cz/api/v1/estates/search   (stránkování limit/offset)
  DETAIL  https://www.sreality.cz/api/v1/estates/{hash_id} (data pod klíčem `result`)

Dvoufázový sběr:
  1) SEARCH — levné, vrátí vše ze seznamu (cena, plocha-dopočet, dispozice, lokalita, GPS, URL).
  2) DETAIL — JEN když je potřeba (popis, STAV, štítek). Detail se tahá pro inzeráty,
     které ještě nejsou v DB (`known_source_ids`) — popis ani stav se nemění, takže
     známé inzeráty znovu netáhneme. To drží počet requestů (a tím i běh) nízko.

Mapování na Listing (vše přes .get(), nikdy přímý index — API se mění):
  source_id          = hash_id (str)
  url                = /detail/{prodej}/{byt}/{sub}/{city-citypart-street}/{hash_id}  (ověřený slug)
  price              = price_czk (0 => None, "Info o ceně u RK")
  area_m2            = price_czk / price_czk_m2 (search) → přepsáno usable_area (detail)
  disposition        = category_sub_cb.name
  locality_text      = "{city}, {district}"
  district / region  = locality.district / locality.region
  gps_lat/lon        = locality.gps_lat / gps_lon
  description        = result.advert_description            (jen detail)
  building_condition = result.building_condition.name       (jen detail; hlavní signál)
  energy_rating      = result.energy_efficiency_rating_cb   (jen detail; A–G)
  listed_date        = result.since                         (jen detail; = poslední úprava, viz base.py)
"""

from __future__ import annotations

import logging
import time
import unicodedata
from datetime import date, datetime
from typing import Any, Iterator

import httpx

from .base import Listing, Source

log = logging.getLogger("sources.sreality")

SEARCH_URL = "https://www.sreality.cz/api/v1/estates/search"
DETAIL_URL = "https://www.sreality.cz/api/v1/estates/{hash_id}"

# Pro stavbu veřejné URL na detail (seo segmenty). Zatím řešíme jen byty/prodej.
_MAIN_SEO = {1: "byt", 2: "dum", 3: "pozemek", 4: "komercni-nemovitost"}
_TYPE_SEO = {1: "prodej", 2: "pronajem", 3: "drazba"}
_ENERGY_LETTERS = "ABCDEFG"  # energy_efficiency_rating_cb.value 1..7 -> A..G
# category_main_cb.value -> Listing.property_type (segmentace mediánů; domy ≠ byty)
_PROPERTY_TYPE = {1: "byt", 2: "dum"}


class SrealitySource(Source):
    """Zdroj Sreality. Vrací `list[Listing]` přes jednotné schéma z base.py."""

    name = "sreality"

    def __init__(
        self,
        region_ids: list[int] | None = None,
        *,
        district_ids: list[int] | None = None,  # okres-level filtr (locality_district_id); přebije region_ids
        category_main_cb: int | list[int] = 1,  # 1 = Byty, 2 = Domy (i seznam: [1, 2])
        category_type_cb: int = 1,        # 1 = Prodej
        page_size: int = 100,             # limit na stránku (API max default 100)
        max_listings: int | None = None,  # strop inzerátů na region (None = vše); hl. pro testy
        fetch_detail: bool = True,        # tahat detail (popis/stav/štítek)?
        detail_limit: int | None = None,  # strop detailů na běh (None = bez stropu)
        known_source_ids: set[str] | None = None,  # ID už v DB -> detail nepřetahuj
        rate_limit_s: float = 1.6,        # pauza mezi requesty (>= 1,5 s)
        timeout: float = 20.0,
        user_agent: str = "realitni-scraper/0.1 (osobni nekomercni projekt)",
    ) -> None:
        self.region_ids = region_ids or [8]  # 8 = Olomoucký kraj (ověřeno)
        self.district_ids = district_ids  # None = použij region_ids (chování beze změny)
        # int i seznam -> vždy seznam kategorií (byty a domy se stahují jako oddělené dotazy)
        self.category_main_cbs = (
            [category_main_cb] if isinstance(category_main_cb, int) else list(category_main_cb)
        )
        self.category_type_cb = category_type_cb
        self.page_size = page_size
        self.max_listings = max_listings
        self.fetch_detail = fetch_detail
        self.detail_limit = detail_limit
        self.known_source_ids = known_source_ids
        self.rate_limit_s = rate_limit_s
        self.timeout = timeout
        self.headers = {"User-Agent": user_agent, "Accept": "application/json"}
        self._last_request_ts = 0.0

    # ------------------------------------------------------------------ #
    # HTTP vrstva s rate-limitingem                                      #
    # ------------------------------------------------------------------ #

    def _respect_rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_ts
        if elapsed < self.rate_limit_s:
            time.sleep(self.rate_limit_s - elapsed)
        self._last_request_ts = time.monotonic()

    def _get(self, client: httpx.Client, url: str, params: dict | None = None) -> dict[str, Any]:
        self._respect_rate_limit()
        resp = client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------ #
    # SEARCH (stránkování)                                               #
    # ------------------------------------------------------------------ #

    def _locality_filters(self) -> list[tuple[str, int]]:
        """Páry (query param, hodnota) pro lokalitní filtr. district_ids (okres) má přednost
        před region_ids (kraj) — ověřeno živě: locality_district_id funguje stejně jako
        locality_region_id, jen na jemnější úrovni (např. 44 = okres Šumperk, 135 inzerátů)."""
        if self.district_ids:
            return [("locality_district_id", d) for d in self.district_ids]
        return [("locality_region_id", r) for r in self.region_ids]

    def _iter_search(
        self, client: httpx.Client, param_key: str, loc_id: int, category_main_cb: int
    ) -> Iterator[dict[str, Any]]:
        """Postupně vrací surové inzeráty ze seznamu, stránkuje přes limit/offset."""
        offset = 0
        fetched = 0
        while True:
            if self.max_listings is not None and fetched >= self.max_listings:
                return
            limit = self.page_size
            if self.max_listings is not None:
                limit = min(limit, self.max_listings - fetched)
            params = {
                "category_main_cb": category_main_cb,
                "category_type_cb": self.category_type_cb,
                param_key: loc_id,
                "limit": limit,
                "offset": offset,
            }
            try:
                data = self._get(client, SEARCH_URL, params)
            except Exception as exc:  # noqa: BLE001 – výpadek stránky nesmí zahodit už stažené
                log.error("SEARCH selhal (%s=%s, offset=%s): %s", param_key, loc_id, offset, exc)
                return

            results = data.get("results") or []
            if not results:
                return
            for estate in results:
                yield estate
                fetched += 1

            total = (data.get("pagination") or {}).get("total") or 0
            offset += len(results)
            log.info("%s=%s (kat. %s): %d/%s inzerátů ze seznamu",
                     param_key, loc_id, category_main_cb, offset, total)
            if offset >= total:
                return

    # ------------------------------------------------------------------ #
    # Normalizace                                                        #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _area_from_search(estate: dict[str, Any]) -> float | None:
        price = estate.get("price_czk")
        price_m2 = estate.get("price_czk_m2")
        if price and price_m2:
            return round(price / price_m2, 1)
        return None

    @staticmethod
    def _slug_sub(name: str) -> str:
        """Podkategorie do URL. OVĚŘENO: u domů musí být bez diakritiky a malými
        ("Rodinný" -> "rodinny", jinak 404); u bytů ("2+kk") transformace nic nemění."""
        decomposed = unicodedata.normalize("NFKD", name.lower())
        ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
        return "-".join(ascii_only.split())

    def _build_url(self, estate: dict[str, Any]) -> str:
        loc = estate.get("locality") or {}
        sub = self._slug_sub((estate.get("category_sub_cb") or {}).get("name") or "")
        main_seo = _MAIN_SEO.get((estate.get("category_main_cb") or {}).get("value"), "byt")
        type_seo = _TYPE_SEO.get((estate.get("category_type_cb") or {}).get("value"), "prodej")
        slug = "-".join(
            part for part in (
                loc.get("city_seo_name"),
                loc.get("citypart_seo_name"),
                loc.get("street_seo_name"),
            ) if part
        )
        return f"https://www.sreality.cz/detail/{type_seo}/{main_seo}/{sub}/{slug}/{estate.get('hash_id')}"

    def _normalize_search(self, estate: dict[str, Any]) -> Listing:
        """Z prvku search.results udělá Listing (bez polí, co jsou jen v detailu)."""
        loc = estate.get("locality") or {}
        price = estate.get("price_czk") or None  # 0 => None
        return Listing(
            source=self.name,
            source_id=str(estate.get("hash_id")),
            url=self._build_url(estate),
            price=price,
            area_m2=self._area_from_search(estate),
            property_type=_PROPERTY_TYPE.get((estate.get("category_main_cb") or {}).get("value"), "byt"),
            disposition=(estate.get("category_sub_cb") or {}).get("name"),
            locality_text=", ".join(
                p for p in (loc.get("city"), loc.get("district")) if p
            ) or None,
            district=loc.get("district"),
            region=loc.get("region"),
            gps_lat=loc.get("gps_lat"),
            gps_lon=loc.get("gps_lon"),
        )

    @staticmethod
    def _energy_letter(cb: dict[str, Any]) -> str | None:
        value = cb.get("value")
        if isinstance(value, int) and 1 <= value <= 7:
            return _ENERGY_LETTERS[value - 1]
        name = cb.get("name") or ""
        return name[0] if name[:1] in _ENERGY_LETTERS else None

    @staticmethod
    def _parse_date(value: Any) -> date | None:
        if not value:
            return None
        try:
            return datetime.strptime(str(value), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None

    def _enrich_detail(self, listing: Listing, raw_detail: dict[str, Any]) -> None:
        """Doplní do Listing pole, která jsou jen v detailu (popis, stav, štítek, datum)."""
        result = raw_detail.get("result") or {}
        listing.description = result.get("advert_description") or None
        listing.building_condition = (result.get("building_condition") or {}).get("name")
        listing.energy_rating = self._energy_letter(result.get("energy_efficiency_rating_cb") or {})
        usable = result.get("usable_area")
        if usable:  # přesnější než dopočet ze search
            listing.area_m2 = float(usable)
        # Pozemek (domy): estate_area. U bytů chybí -> zůstane None.
        estate_area = result.get("estate_area")
        if estate_area:
            listing.land_area_m2 = float(estate_area)
        listing.listed_date = self._parse_date(result.get("since"))

    def _needs_detail(self, source_id: str) -> bool:
        """Detail tahat jen pro neznámé inzeráty (známé už mají detail uložený v DB)."""
        if self.known_source_ids is None:
            return True
        return source_id not in self.known_source_ids

    # ------------------------------------------------------------------ #
    # Veřejné API zdroje                                                 #
    # ------------------------------------------------------------------ #

    def fetch(self) -> list[Listing]:
        """Stáhne inzeráty (search + řízeně detail) a vrátí je jako Listing."""
        listings: list[Listing] = []
        detail_count = 0
        locality_filters = self._locality_filters()
        log.info("Sreality fetch: lokalita=%s, kategorie=%s, fetch_detail=%s",
                 locality_filters, self.category_main_cbs, self.fetch_detail)

        with httpx.Client(headers=self.headers, timeout=self.timeout, follow_redirects=True) as client:
            for category_main_cb in self.category_main_cbs:
                for param_key, loc_id in locality_filters:
                    for estate in self._iter_search(client, param_key, loc_id, category_main_cb):
                        try:
                            listing = self._normalize_search(estate)
                        except Exception as exc:  # noqa: BLE001 – jeden vadný inzerát přeskoč
                            log.warning("Přeskakuji inzerát %s: %s", estate.get("hash_id"), exc)
                            continue

                        want_detail = (
                            self.fetch_detail
                            and self._needs_detail(listing.source_id)
                            and (self.detail_limit is None or detail_count < self.detail_limit)
                        )
                        if want_detail:
                            try:
                                raw_detail = self._get(client, DETAIL_URL.format(hash_id=listing.source_id))
                                self._enrich_detail(listing, raw_detail)
                                detail_count += 1
                            except Exception as exc:  # noqa: BLE001 – chybný detail nezahodí inzerát
                                log.error("Detail %s selhal: %s", listing.source_id, exc)

                        listings.append(listing)

        log.info("Sreality fetch hotovo: %d inzerátů, %d detailů staženo", len(listings), detail_count)
        return listings


if __name__ == "__main__":
    # Smoke test proti živému API s malými stropy (1 search + pár detailů).
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

    src = SrealitySource(region_ids=[8], max_listings=8, detail_limit=4)
    items = src.fetch()

    print(f"\n=== {len(items)} Listing objektů ===")
    for i, lst in enumerate(items, 1):
        ppm2 = f"{lst.price_per_m2:,} Kč/m²".replace(",", " ") if lst.price_per_m2 else "—"
        price = f"{lst.price:,} Kč".replace(",", " ") if lst.price else "— (info u RK)"
        print(f"\n[{i}] #{lst.source_id}  {lst.disposition or '?'}  "
              f"{lst.area_m2 or '?'} m²  |  {price} ({ppm2})")
        print(f"    📍 {lst.locality_text} · okres {lst.district} · {lst.region}")
        print(f"    🔨 stav: {lst.building_condition or '— (bez detailu)'}  |  ⚡ {lst.energy_rating or '—'}"
              f"  |  vystaveno: {lst.listed_date or '—'}")
        print(f"    🔗 {lst.url}")
        if lst.description:
            print(f"    📝 {lst.description[:110].replace(chr(10), ' ')}…")

    # kolik má kompletní data pro scoring (stav)
    s_stav = sum(1 for x in items if x.building_condition)
    print(f"\n✅ sreality.py OK — {s_stav}/{len(items)} má building_condition (detail), zbytek jen search-level.")
