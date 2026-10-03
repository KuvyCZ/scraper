"""investment/sources/bazos_source.py — reality.bazos.cz (soukromé inzeráty, bez RK).

robots.txt DISALLOWuje `hledat=`, `hlokalita=`, `humkreis`, `order=`, `cenaod=`,
`cenado=`, `rubriky=`, `type=`, `category=` (ověřeno živě) — přesně tyhle query
parametry Bazoš normálně používá pro vyhledávání/filtrování/třídění. Nejde tedy
URL vyfiltrovat na Šumperk ani seřadit od nejlevnějšího po serveru.

Řešení respektující robots.txt: prosté procházení kategorie `/prodam/byt/{offset}/`
(stránkování PATH segmentem, ne query stringem — to disallow nezakazuje), lokalita
i řazení se řeší AŽ po stažení, na naší straně (filtr přes text lokality v inzerátu,
třídění podle ceny v paměti).

Popisy jsou nestrukturované -> size_m2/layout se parsují regexem defenzivně.
Když se nepodaří určit ani jedno, inzerát se STEJNĚ nechá projít (na rozdíl od
ostatních zdrojů — Bazoš je z podstaty nejméně strukturovaný), jen s
`parse_confidence="low"`, ať se neztratí soukromé nabídky jen kvůli chybějícímu
regex matchi v popisu.
"""

from __future__ import annotations

import html
import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from investment.filters import OKRES_SUMPERK_TOWNS
from investment.scraping_utils import RateLimiter, RobotsChecker, polite_get
from investment.sources.base import Listing, utc_now_iso

if TYPE_CHECKING:
    from investment.config import InvestmentConfig

log = logging.getLogger("investment.sources.bazos")

name = "bazos"

_SELECTORS_PATH = Path(__file__).parent / "selectors" / "bazos.json"

_AREA_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*m(?:&sup2;|²|2)\b", re.IGNORECASE)
_LAYOUT_RE = re.compile(r"\b(\d)\s*\+\s*(kk|\d)\b", re.IGNORECASE)
_PRICE_DIGITS_RE = re.compile(r"\d+")


def _load_selectors() -> dict[str, Any]:
    with open(_SELECTORS_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _parse_price(text: str) -> int | None:
    digits = "".join(_PRICE_DIGITS_RE.findall(text))
    return int(digits) if digits else None


def _parse_area(text: str) -> float | None:
    m = _AREA_RE.search(text)
    return float(m.group(1).replace(",", ".")) if m else None


def _parse_layout(text: str) -> str | None:
    m = _LAYOUT_RE.search(text)
    return f"{m.group(1)}+{m.group(2).lower()}" if m else None


def _passes_relaxed_filters(listing: Listing, location: str | None, config: "InvestmentConfig") -> bool:
    """Stejná natvrdo daná pravidla jako investment/filters.py, ALE chybějící
    (neparsovatelné) size_m2/layout se nepočítá jako selhání — jen jako
    neznámé (benefit of the doubt), viz docstring modulu."""
    if listing.price is None or listing.price > config.price_max:
        return False
    if listing.size_m2 is not None and not (config.size_min <= listing.size_m2 <= config.size_max):
        return False
    if listing.layout is not None and listing.layout not in config.allowed_layouts:
        return False
    if config.include_district:
        return location == config.city_filter or location in OKRES_SUMPERK_TOWNS
    return location == config.city_filter


def _parse_page(html_text: str, selectors: dict[str, Any]) -> list[dict[str, Any]]:
    marker = selectors["listing_block_marker"]
    blocks = html_text.split(marker)[1:]
    title_re = re.compile(selectors["title_link_re"])
    price_re = re.compile(selectors["price_re"])
    location_re = re.compile(selectors["location_re"])
    desc_re = re.compile(selectors["description_re"], re.DOTALL)

    parsed = []
    for block in blocks:
        title_m = title_re.search(block)
        if not title_m:
            continue
        href, title = title_m.group(1), html.unescape(title_m.group(2).strip())
        price_m = price_re.search(block)
        loc_m = location_re.search(block)
        desc_m = desc_re.search(block)
        parsed.append({
            "href": href, "title": title,
            "price_text": price_m.group(1).strip() if price_m else "",
            "location_text": html.unescape(loc_m.group(1).strip()) if loc_m else "",
            "description": html.unescape(desc_m.group(1).strip()) if desc_m else "",
        })
    return parsed


def fetch_listings(config: "InvestmentConfig", known_ids: set[str] | None = None) -> list[Listing]:
    selectors = _load_selectors()
    robots = RobotsChecker()
    rate_limiter = RateLimiter()
    out: list[Listing] = []

    with httpx.Client(timeout=20.0, follow_redirects=True) as client:
        for page_idx in range(config.bazos_max_pages):
            offset = page_idx * selectors["page_offset_step"]
            path = selectors["category_path"] if offset == 0 else f'{selectors["category_path"]}{offset}/'
            url = selectors["base_url"] + path

            resp = polite_get(client, url, robots=robots, rate_limiter=rate_limiter)
            if resp is None:
                log.warning("Bazoš: strana offset=%d se nepodařilo stáhnout, končím stránkování", offset)
                break

            items = _parse_page(resp.text, selectors)
            if not items:
                break

            for item in items:
                price = _parse_price(item["price_text"])
                combined_text = f'{item["title"]} {item["description"]}'
                size_m2 = _parse_area(combined_text)
                layout = _parse_layout(combined_text)
                location = item["location_text"].strip() or None
                external_id_m = re.search(r"/inzerat/(\d+)/", item["href"])
                if not external_id_m:
                    continue
                external_id = external_id_m.group(1)

                listing = Listing(
                    source=name, external_id=external_id,
                    url=selectors["base_url"] + item["href"],
                    price=price, size_m2=size_m2, layout=layout, location=location,
                    description=item["description"] or None,
                    scraped_at=utc_now_iso(),
                    parse_confidence="high" if (size_m2 is not None and layout is not None) else "low",
                )
                if _passes_relaxed_filters(listing, location, config):
                    out.append(listing)

    log.info("Bazoš: %d kandidátů po (relaxovaných) strukturálních filtrech", len(out))
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

    cfg = InvestmentConfig(bazos_max_pages=5)
    items = fetch_listings(cfg)
    print(f"\n=== {len(items)} Listing (bazos) ===")
    for it in items[:10]:
        print(it.layout, it.size_m2, "m2", it.price, "Kč", it.location, "| confidence:", it.parse_confidence, "|", it.url)
    print("\n✅ investment/sources/bazos_source.py OK")
