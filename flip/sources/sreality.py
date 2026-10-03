"""flip/sources/sreality.py — Sreality přes ověřené veřejné JSON API v1.

  SEARCH  https://www.sreality.cz/api/v1/estates/search   (limit/offset)
  DETAIL  https://www.sreality.cz/api/v1/estates/{hash_id} (popis, stav, štítek, pozemek)

Stahuje byty (category_main_cb=1) i domy (2) z CELÉHO okresu Šumperk
(locality_district_id=44, ověřeno živě) a vrací všechny — i mimo 6 obcí a nad
rozpočtem. Ty slouží jako data o trhu (medián, ARV); alertuje se jen to, co je
ve sledovaných obcích a v rozpočtu (rozhoduje pipeline).

Detail (dražší request) jen pro inzeráty, které ještě nejsou v DB, a přednostně
pro sledované obce — strop na běh drží první běh rozumně krátký, zbytek se
dotáhne v dalších bězích.
"""

from __future__ import annotations

import logging
import time
import unicodedata
from typing import Any, Iterator

import httpx

from flip.config import FlipConfig
from flip.http import USER_AGENT
from flip.model import FetchResult, Listing, utc_now_iso
from flip.text import EXCLUDED_HOUSE_SUBTYPES
from flip.towns import match_town

log = logging.getLogger("flip.sources.sreality")

name = "sreality"

SEARCH_URL = "https://www.sreality.cz/api/v1/estates/search"
DETAIL_URL = "https://www.sreality.cz/api/v1/estates/{hash_id}"

_MAIN = {1: ("byt", "byt"), 2: ("dum", "dum")}   # category_main_cb -> (kind, URL segment)
_ENERGY_LETTERS = "ABCDEFG"
RATE_LIMIT_S = 1.6
DETAIL_LIMIT_PER_RUN = 300


def _slug(text: str) -> str:
    """"Rodinný" -> "rodinny" (ověřeno: URL domu s diakritikou vrací 404)."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "-".join("".join(c for c in decomposed if not unicodedata.combining(c)).split())


def build_url(estate: dict[str, Any]) -> str:
    loc = estate.get("locality") or {}
    sub = _slug((estate.get("category_sub_cb") or {}).get("name") or "")
    main_seo = _MAIN.get((estate.get("category_main_cb") or {}).get("value"), ("byt", "byt"))[1]
    slug = "-".join(p for p in (loc.get("city_seo_name"), loc.get("citypart_seo_name"), loc.get("street_seo_name")) if p)
    return f"https://www.sreality.cz/detail/prodej/{main_seo}/{sub}/{slug}/{estate.get('hash_id')}"


def normalize_search(estate: dict[str, Any]) -> Listing | None:
    """Prvek search.results -> Listing (bez polí z detailu). None = typ, který nesledujeme."""
    main_value = (estate.get("category_main_cb") or {}).get("value")
    if main_value not in _MAIN:
        return None
    kind = _MAIN[main_value][0]
    subtype = (estate.get("category_sub_cb") or {}).get("name")
    if kind == "dum" and subtype in EXCLUDED_HOUSE_SUBTYPES:
        return None
    loc = estate.get("locality") or {}
    price = estate.get("price_czk") or None
    price_m2 = estate.get("price_czk_m2")
    area = round(price / price_m2, 1) if price and price_m2 else None
    return Listing(
        source=name, external_id=str(estate.get("hash_id")), url=build_url(estate), kind=kind,
        subtype=subtype, price=price, area_m2=area,
        town=match_town(loc.get("city"), loc.get("citypart")),
        locality=", ".join(p for p in (loc.get("city"), loc.get("citypart"), loc.get("street")) if p) or None,
        gps_lat=loc.get("gps_lat"), gps_lon=loc.get("gps_lon"), scraped_at=utc_now_iso(),
    )


def _energy_letter(cb: dict[str, Any]) -> str | None:
    value = cb.get("value")
    if isinstance(value, int) and 1 <= value <= 7:
        return _ENERGY_LETTERS[value - 1]
    label = cb.get("name") or ""
    return label[0] if label and label[0] in _ENERGY_LETTERS else None


def enrich_detail(lst: Listing, raw: dict[str, Any]) -> None:
    result = raw.get("result") or {}
    lst.description = result.get("advert_description") or None
    lst.building_condition = (result.get("building_condition") or {}).get("name")
    lst.energy_rating = _energy_letter(result.get("energy_efficiency_rating_cb") or {})
    if result.get("usable_area"):
        lst.area_m2 = float(result["usable_area"])
    if lst.kind == "dum" and result.get("estate_area"):
        lst.land_m2 = float(result["estate_area"])


class _Client:
    def __init__(self) -> None:
        self.http = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=20.0, follow_redirects=True
        )
        self._last = 0.0

    def get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        wait = RATE_LIMIT_S - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        resp = self.http.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        self.http.close()


def _iter_search(client: _Client, district_id: int, category: int) -> Iterator[dict[str, Any]]:
    offset = 0
    while True:
        data = client.get(SEARCH_URL, {
            "category_main_cb": category, "category_type_cb": 1,
            "locality_district_id": district_id, "limit": 100, "offset": offset,
        })
        results = data.get("results") or []
        if not results:
            return
        yield from results
        offset += len(results)
        total = (data.get("pagination") or {}).get("total") or 0
        if offset >= total:
            return


def fetch_listings(cfg: FlipConfig, known_ids: set[str]) -> FetchResult:
    categories = [c for c, (kind, _) in _MAIN.items() if cfg.wants_kind(kind)]
    client = _Client()
    raw_count = 0
    listings: list[Listing] = []
    try:
        for category in categories:
            for estate in _iter_search(client, cfg.district_id, category):
                raw_count += 1
                try:
                    lst = normalize_search(estate)
                except Exception as exc:  # noqa: BLE001 – jeden vadný inzerát přeskoč
                    log.warning("Sreality: přeskakuji %s: %s", estate.get("hash_id"), exc)
                    continue
                if lst is not None:
                    listings.append(lst)

        # detail jen pro nové, sledované obce první
        todo = sorted((x for x in listings if x.external_id not in known_ids), key=lambda x: not x.in_area)
        for lst in todo[:DETAIL_LIMIT_PER_RUN]:
            try:
                enrich_detail(lst, client.get(DETAIL_URL.format(hash_id=lst.external_id)))
            except Exception as exc:  # noqa: BLE001 – chybný detail nezahodí inzerát
                log.warning("Sreality: detail %s selhal: %s", lst.external_id, exc)
        if len(todo) > DETAIL_LIMIT_PER_RUN:
            log.info("Sreality: %d detailů odloženo na další běh", len(todo) - DETAIL_LIMIT_PER_RUN)
            # odložené tenhle běh vůbec nevracej -> nebudou "známé" a detail se jim příště dotáhne
            deferred = {x.key for x in todo[DETAIL_LIMIT_PER_RUN:]}
            listings = [x for x in listings if x.key not in deferred]
    finally:
        client.close()

    in_area = sum(1 for x in listings if x.in_area)
    log.info("Sreality: %d inzerátů v okrese, %d ve sledovaných obcích", len(listings), in_area)
    return FetchResult(listings, raw_count)
