"""flip/sources/reality_sumperk.py — reality-sumperk.cz (místní agregátor realitek).

Ověřeno živě (2026-07-24) pro byty:
  * kategorie jsou podle dispozice: /prodej/byty/byty-2-1/ … (dispozice je jistá z URL),
  * stránkování ?strana=N (robots.txt ho nezakazuje),
  * `?sortBy=1` na kategorii řazení NEMĚNÍ -> neřadí se, prochází se strop stránek,
  * karta má zkrácený popis, plný je v <meta property="og:description"> detailu.

⚠️ NEOVĚŘENO: kategorie domů /prodej/domy/ (předpokládá se stejná HTML struktura).
`python main.py check` ukáže, jestli vrací inzeráty; cesta je v selectors/reality_sumperk.json.
"""

from __future__ import annotations

import html
import json
import logging
import re
from pathlib import Path
from typing import Any

import httpx

from flip.config import FlipConfig
from flip.http import RateLimiter, RobotsChecker, polite_get
from flip.model import FetchResult, Listing, utc_now_iso
from flip.text import EXCLUDED_HOUSE_SUBTYPES, house_subtype_from_text, parse_area, parse_land, parse_price
from flip.towns import match_town

log = logging.getLogger("flip.sources.reality_sumperk")

name = "reality_sumperk"

_SELECTORS_PATH = Path(__file__).parent / "selectors" / "reality_sumperk.json"
_ID_RE = re.compile(r"-(\d+)\.html$")


def load_selectors() -> dict[str, Any]:
    with open(_SELECTORS_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def parse_page(html_text: str, selectors: dict[str, Any]) -> list[dict[str, str]]:
    blocks = html_text.split(selectors["listing_block_marker"])[1:]
    title_re = re.compile(selectors["title_link_re"])
    addr_re = re.compile(selectors["address_re"])
    desc_re = re.compile(selectors["description_snippet_re"])
    price_re = re.compile(selectors["price_re"], re.DOTALL)
    parsed = []
    for block in blocks:
        title_m = title_re.search(block)
        if not title_m:
            continue
        addr_m, desc_m, price_m = addr_re.search(block), desc_re.search(block), price_re.search(block)
        parsed.append({
            "href": title_m.group(1),
            "title": html.unescape(title_m.group(2).strip()),
            "address": html.unescape(addr_m.group(1).strip()) if addr_m else "",
            "description": html.unescape(desc_m.group(1).strip()) if desc_m else "",
            "price_text": price_m.group(1).strip() if price_m else "",
        })
    return parsed


def to_listing(item: dict[str, str], kind: str, layout: str | None, base_url: str) -> Listing | None:
    id_m = _ID_RE.search(item["href"])
    if not id_m:
        return None
    if kind == "dum":
        subtype = house_subtype_from_text(item["title"])
        if subtype in EXCLUDED_HOUSE_SUBTYPES:
            return None
    else:
        subtype = layout
    href = base_url + item["href"] if item["href"].startswith("/") else item["href"]
    return Listing(
        source=name, external_id=id_m.group(1), url=href, kind=kind, subtype=subtype,
        price=parse_price(item["price_text"]), area_m2=parse_area(item["title"]),
        land_m2=parse_land(item["title"]) if kind == "dum" else None,
        town=match_town(item["address"]), locality=item["address"] or None,
        description=item["description"] or None, scraped_at=utc_now_iso(),
    )


def _full_description(client: httpx.Client, url: str, selectors: dict[str, Any], **kw: Any) -> str | None:
    resp = polite_get(client, url, **kw)
    if resp is None:
        return None
    m = re.search(selectors["og_description_re"], resp.text, re.DOTALL)
    return html.unescape(m.group(1)).strip() if m else None


def fetch_listings(cfg: FlipConfig, known_ids: set[str]) -> FetchResult:
    selectors = load_selectors()
    kw = {"robots": RobotsChecker(), "rate_limiter": RateLimiter()}
    out: list[Listing] = []
    raw_count = 0
    base = selectors["base_url"]

    categories: list[tuple[str, str, str | None]] = []   # (kind, path, layout)
    if cfg.include_flats:
        categories += [("byt", path, layout) for layout, path in selectors["flat_layout_paths"].items()]
    if cfg.include_houses:
        categories += [("dum", path, None) for path in selectors["house_paths"]]

    with httpx.Client(timeout=20.0, follow_redirects=True) as client:
        for kind, path, layout in categories:
            seen_on_previous: set[str] = set()
            for page in range(1, cfg.reality_sumperk_max_pages + 1):
                url = base + path + (f'?{selectors["page_query_param"]}={page}' if page > 1 else "")
                resp = polite_get(client, url, **kw)
                if resp is None:
                    break
                items = parse_page(resp.text, selectors)
                if not items:
                    break
                ids = {i["href"] for i in items}
                if ids == seen_on_previous:   # web vrací poslední stranu pořád dokola
                    break
                seen_on_previous = ids
                raw_count += len(items)
                for item in items:
                    lst = to_listing(item, kind, layout, base)
                    if lst is None or not lst.in_area:
                        continue
                    if cfg.fetch_details and lst.external_id not in known_ids:
                        full = _full_description(client, lst.url, selectors, **kw)
                        if full:
                            lst.description = full
                    out.append(lst)

    log.info("reality-sumperk.cz: %d inzerátů ve sledovaných obcích (z %d prošlých)", len(out), raw_count)
    return FetchResult(out, raw_count)
