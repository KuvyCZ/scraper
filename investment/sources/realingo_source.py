"""investment/sources/realingo_source.py — realingo.cz (Next.js aggregator).

Ověřeno živě (2026-07-24):
  * Next.js `__NEXT_DATA__` JSON blob v HTML nese strukturovaná data
    (props.pageProps.store.offer.list.data) — spolehlivější než CSS třídy
    (ty jsou u Realinga hashované, `rjss408` apod., mění se s buildem).
  * Lokalita jde přímo URL cestou: /prodej_reality/{Obec}/ nebo
    /prodej_reality/Okres_{Obec}/ (celý okres).
  * Řazení od nejlevnějšího: segment /nejlevnejsi/ (ověřeno — ceny vzestupně).
  * Stránkování: segment /{N}_strana/ (lze kombinovat s nejlevnejsi/).
  * `purpose` pole je AUTORITATIVNÍ pro prodej/pronájem — NE URL (viděn inzerát
    s purpose=SELL, ale url obsahující "/pronajem/" — zjevná chyba dat webu).
  * Popis inzerátu je JEN na detailu (store.offer.details[id].offer.detail.description).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from investment.filters import passes_structural_filters
from investment.scraping_utils import RateLimiter, RobotsChecker, polite_get
from investment.sources.base import Listing, utc_now_iso

if TYPE_CHECKING:
    from investment.config import InvestmentConfig

log = logging.getLogger("investment.sources.realingo")

name = "realingo"

_SELECTORS_PATH = Path(__file__).parent / "selectors" / "realingo.json"
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)


def _load_selectors() -> dict[str, Any]:
    with open(_SELECTORS_PATH, encoding="utf-8") as fh:
        return json.load(fh)


_POSTAL_CODE_RE = re.compile(r"^\d{3}\s?\d{2}$")


def _extract_city(address: str | None) -> str | None:
    """Realingo staví address nekonzistentně — vídáno '{ulice}, {obec}' i
    '{obec}, {PSČ}' (ověřeno živě, PSČ formát 'NNN NN'). Bereme poslední
    segment, co NENÍ tvarem PSČ, ne prostě poslední segment za čárkou."""
    if not address:
        return None
    parts = [p.strip() for p in address.split(",")]
    for part in reversed(parts):
        if part and not _POSTAL_CODE_RE.match(part):
            return part
    return parts[-1].strip() or None if parts else None


def _parse_next_data(html: str) -> dict[str, Any] | None:
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _build_list_url(selectors: dict[str, Any], config: "InvestmentConfig", page: int) -> str:
    template = selectors["district_path_template"] if config.include_district else selectors["city_path_template"]
    path = template.format(location=quote(config.city_filter))
    if page > 1:
        path += selectors["page_path_suffix"].format(n=page)
    return selectors["base_url"] + path


def _fetch_description(
    client: httpx.Client, url: str, external_id: str, *, robots: RobotsChecker, rate_limiter: RateLimiter
) -> str | None:
    resp = polite_get(client, url, robots=robots, rate_limiter=rate_limiter)
    if resp is None:
        return None
    data = _parse_next_data(resp.text)
    if not data:
        return None
    try:
        details = data["props"]["pageProps"]["store"]["offer"]["details"]
        return details[external_id]["offer"]["detail"].get("description")
    except (KeyError, TypeError):
        return None


def fetch_listings(config: "InvestmentConfig", known_ids: set[str] | None = None) -> list[Listing]:
    selectors = _load_selectors()
    robots = RobotsChecker()
    rate_limiter = RateLimiter()
    known_ids = known_ids or set()
    out: list[Listing] = []

    with httpx.Client(timeout=20.0, follow_redirects=True) as client:
        for page in range(1, config.realingo_max_pages + 1):
            url = _build_list_url(selectors, config, page)
            resp = polite_get(client, url, robots=robots, rate_limiter=rate_limiter)
            if resp is None:
                log.warning("Realingo: stránka %d se nepodařilo stáhnout, končím stránkování", page)
                break

            data = _parse_next_data(resp.text)
            if not data:
                log.error("Realingo: __NEXT_DATA__ nenalezeno na %s (možná redesign — zkontroluj selectors/realingo.json)", url)
                break

            try:
                items = data["props"]["pageProps"]["store"]["offer"]["list"]["data"]
            except (KeyError, TypeError):
                log.error("Realingo: neočekávaná struktura NEXT_DATA na %s", url)
                break

            if not items:
                break

            page_had_in_range = False
            for item in items:
                if item.get("purpose") != "SELL" or item.get("property") != "FLAT":
                    continue
                price = (item.get("price") or {}).get("total")
                if price is not None and price <= config.price_max:
                    page_had_in_range = True

                layout = selectors["category_layout_map"].get(item.get("category"))
                location = _extract_city((item.get("location") or {}).get("address"))
                external_id = str(item["id"])

                listing = Listing(
                    source=name, external_id=external_id, url=selectors["base_url"] + item["url"],
                    price=price, size_m2=(item.get("area") or {}).get("main"),
                    layout=layout, location=location, description=None,
                    scraped_at=utc_now_iso(),
                )

                if not passes_structural_filters(listing, location, config):
                    continue

                if external_id not in known_ids:
                    listing.description = _fetch_description(
                        client, listing.url, external_id, robots=robots, rate_limiter=rate_limiter
                    )

                out.append(listing)

            # Řazeno vzestupně cenou (nejlevnejsi/) -> jakmile celá stránka je nad
            # price_max, další stránky budou jen dražší -> šetři requesty a skonči.
            if not page_had_in_range and page > 1:
                log.info("Realingo: strana %d celá nad price_max, končím stránkování", page)
                break

    log.info("Realingo: %d kandidátů (FLAT/SELL, po strukturálních filtrech)", len(out))
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

    cfg = InvestmentConfig(realingo_max_pages=2)
    items = fetch_listings(cfg)
    print(f"\n=== {len(items)} Listing (realingo) ===")
    for it in items[:10]:
        print(it.layout, it.size_m2, "m2", it.price, "Kč", it.location,
              "| desc:", "ano" if it.description else "ne", "|", it.url)
    print("\n✅ investment/sources/realingo_source.py OK")
