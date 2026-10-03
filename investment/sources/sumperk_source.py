"""investment/sources/sumperk_source.py — reality-sumperk.cz (místní RK agregátor).

Ověřeno živě (2026-07-24):
  * Zadáním navržené `/byty/nejlevnejsi/` a `/prodej/byty/obec-sumperk/` na webu
    NEEXISTUJÍ (404 při přímém ověření) — reálné kategorie jsou dispozice-scoped:
    `/prodej/byty/byty-2-1/`, `/prodej/byty/byty-2-kk/`, `/prodej/byty/byty-3-1/`, …
    Díky tomu je `layout` u každého inzerátu jistý už z URL (ne z regexu).
  * Stránkování `?strana=N` — robots.txt ho nezakazuje (jen /moje-oblibene/,
    /moje-filtry/, /realitni-kancelare/ajax_search/, /mapa/).
  * Formulářový `sortBy=1` ("od nejlevnějšího") POSTuje na /hledani/ — ověřeno, že
    prosté přidání `?sortBy=1` na kategorii pořadí NEMĚNÍ. Řadíme proto až po
    stažení (v paměti), stránkování je omezené stropem (config.sumperk_max_pages).
  * Celá doména je editorsky omezená na okres Šumperk (vidět v listing datech —
    Zábřeh, Bludov, Mohelnice, …) — location filtr se přesto aplikuje jako
    dodatečná pojistka (sdílený `investment.filters`).
  * Popis na kartě je zkrácený ("…") — plný text je v `<meta property="og:description">`
    na detailu; tahá se JEN pro nové kandidáty, co projdou strukturálním filtrem.
"""

from __future__ import annotations

import html
import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from investment.filters import passes_structural_filters
from investment.scraping_utils import RateLimiter, RobotsChecker, polite_get
from investment.sources.base import Listing, utc_now_iso

if TYPE_CHECKING:
    from investment.config import InvestmentConfig

log = logging.getLogger("investment.sources.sumperk")

name = "reality_sumperk"

_SELECTORS_PATH = Path(__file__).parent / "selectors" / "sumperk.json"
_AREA_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*m(?:&sup2;|²|2)\b", re.IGNORECASE)
_PRICE_DIGITS_RE = re.compile(r"\d+")
_ID_RE = re.compile(r"-(\d+)\.html$")


def _load_selectors() -> dict[str, Any]:
    with open(_SELECTORS_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _extract_city(address: str | None) -> str | None:
    if not address:
        return None
    return address.split(",")[-1].strip() or None


def _parse_price(text: str) -> int | None:
    digits = "".join(_PRICE_DIGITS_RE.findall(text))
    return int(digits) if digits else None


def _parse_page(html_text: str, selectors: dict[str, Any]) -> list[dict[str, Any]]:
    marker = selectors["listing_block_marker"]
    blocks = html_text.split(marker)[1:]
    title_re = re.compile(selectors["title_link_re"])
    addr_re = re.compile(selectors["address_re"])
    desc_re = re.compile(selectors["description_snippet_re"])
    price_re = re.compile(selectors["price_re"], re.DOTALL)

    parsed = []
    for block in blocks:
        title_m = title_re.search(block)
        if not title_m:
            continue
        href, title = title_m.group(1), html.unescape(title_m.group(2).strip())
        addr_m = addr_re.search(block)
        desc_m = desc_re.search(block)
        price_m = price_re.search(block)
        parsed.append({
            "href": href, "title": title,
            "address": html.unescape(addr_m.group(1).strip()) if addr_m else "",
            "description_snippet": html.unescape(desc_m.group(1).strip()) if desc_m else "",
            "price_text": price_m.group(1).strip() if price_m else "",
        })
    return parsed


def _fetch_full_description(
    client: httpx.Client, url: str, selectors: dict[str, Any], *, robots: RobotsChecker, rate_limiter: RateLimiter
) -> str | None:
    resp = polite_get(client, url, robots=robots, rate_limiter=rate_limiter)
    if resp is None:
        return None
    m = re.search(selectors["og_description_re"], resp.text, re.DOTALL)
    return html.unescape(m.group(1)).strip() if m else None


def fetch_listings(config: "InvestmentConfig", known_ids: set[str] | None = None) -> list[Listing]:
    selectors = _load_selectors()
    robots = RobotsChecker()
    rate_limiter = RateLimiter()
    known_ids = known_ids or set()
    out: list[Listing] = []

    layout_paths = {
        layout: selectors["layout_path_map"][layout]
        for layout in config.allowed_layouts
        if layout in selectors["layout_path_map"]
    }
    skipped = set(config.allowed_layouts) - set(layout_paths)
    if skipped:
        log.warning("reality-sumperk.cz: dispozice bez známé kategorie (přeskočeno): %s", skipped)

    with httpx.Client(timeout=20.0, follow_redirects=True) as client:
        for layout, path in layout_paths.items():
            for page in range(1, config.sumperk_max_pages + 1):
                url = f'{selectors["base_url"]}{path}'
                if page > 1:
                    url += f'?{selectors["page_query_param"]}={page}'

                resp = polite_get(client, url, robots=robots, rate_limiter=rate_limiter)
                if resp is None:
                    log.warning("reality-sumperk.cz: %s strana %d se nepodařilo stáhnout, další dispozice", layout, page)
                    break

                items = _parse_page(resp.text, selectors)
                if not items:
                    break

                for item in items:
                    id_m = _ID_RE.search(item["href"])
                    if not id_m:
                        continue
                    external_id = id_m.group(1)
                    price = _parse_price(item["price_text"])
                    size_m2 = None
                    area_m = _AREA_RE.search(item["title"])
                    if area_m:
                        size_m2 = float(area_m.group(1).replace(",", "."))
                    location = _extract_city(item["address"])
                    detail_url = selectors["base_url"] + item["href"] if item["href"].startswith("/") else item["href"]

                    listing = Listing(
                        source=name, external_id=external_id, url=detail_url,
                        price=price, size_m2=size_m2, layout=layout, location=location,
                        description=item["description_snippet"] or None,
                        scraped_at=utc_now_iso(),
                    )

                    if passes_structural_filters(listing, location, config) and external_id not in known_ids:
                        full_desc = _fetch_full_description(
                            client, detail_url, selectors, robots=robots, rate_limiter=rate_limiter
                        )
                        if full_desc:
                            listing.description = full_desc

                    if passes_structural_filters(listing, location, config):
                        out.append(listing)

    log.info("reality-sumperk.cz: %d kandidátů po strukturálních filtrech", len(out))
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

    cfg = InvestmentConfig(sumperk_max_pages=2)
    items = fetch_listings(cfg)
    print(f"\n=== {len(items)} Listing (reality-sumperk.cz) ===")
    for it in items[:10]:
        print(it.layout, it.size_m2, "m2", it.price, "Kč", it.location,
              "| desc:", "ano" if it.description else "ne", "|", it.url)
    print("\n✅ investment/sources/sumperk_source.py OK")
