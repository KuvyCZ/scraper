"""flip/sources/bazos.py — reality.bazos.cz (často soukromí prodejci, bez RK).

robots.txt DISALLOWuje `hledat=`, `hlokalita=`, `humkreis`, `order=`, `cenaod=`,
`cenado=`, `rubriky=` … (ověřeno živě) — tedy přesně parametry pro hledání a
filtrování. Proto se prochází prostá celostátní kategorie se stránkováním cestou
(`/prodam/byt/20/`), a obec i cena se filtrují až u nás.

Obec se určuje podle PSČ (`cfg.bazos_psc`), ne podle názvu: Bazoš u inzerátu
občas ukáže "Šumperk" s PSČ jiné obce. Název se použije jen když PSČ chybí.

Popisy jsou volný text -> plocha/dispozice/pozemek se čtou regexem. Když něco
chybí, inzerát STEJNĚ projde (parse_confidence="low"), jen se to ve zprávě
označí — soukromá nabídka se nesmí ztratit kvůli chybějícímu "m²" v textu.

✅ ověřeno živě: kategorie /prodam/byt/ a HTML struktura výpisu.
⚠️ NEOVĚŘENO: kategorie /prodam/dum/ (stejná struktura se předpokládá) —
`python main.py check` ukáže, jestli vrací inzeráty.
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
from flip.text import EXCLUDED_HOUSE_SUBTYPES, house_subtype_from_text, parse_area, parse_land, parse_layout, parse_price
from flip.towns import DEFAULT_PSC, match_town, match_town_by_psc

log = logging.getLogger("flip.sources.bazos")

name = "bazos"

_SELECTORS_PATH = Path(__file__).parent / "selectors" / "bazos.json"
_ID_RE = re.compile(r"/inzerat/(\d+)/")


def load_selectors() -> dict[str, Any]:
    with open(_SELECTORS_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def parse_page(html_text: str, selectors: dict[str, Any]) -> list[dict[str, str]]:
    blocks = html_text.split(selectors["listing_block_marker"])[1:]
    title_re = re.compile(selectors["title_link_re"])
    price_re = re.compile(selectors["price_re"])
    location_re = re.compile(selectors["location_re"])
    psc_re = re.compile(selectors["psc_re"])
    desc_re = re.compile(selectors["description_re"], re.DOTALL)
    parsed = []
    for block in blocks:
        title_m = title_re.search(block)
        if not title_m:
            continue
        price_m, loc_m, desc_m = price_re.search(block), location_re.search(block), desc_re.search(block)
        psc_m = psc_re.search(block)
        parsed.append({
            "href": title_m.group(1),
            "title": html.unescape(title_m.group(2).strip()),
            "price_text": price_m.group(1).strip() if price_m else "",
            "location_text": html.unescape(loc_m.group(1).strip()) if loc_m else "",
            "psc": psc_m.group(1).strip() if psc_m else "",
            "description": html.unescape(re.sub(r"<[^>]+>", " ", desc_m.group(1))).strip() if desc_m else "",
        })
    return parsed


def town_for(item: dict[str, str], psc_map: dict[str, str]) -> str | None:
    """Sledovaná obec podle PSČ; bez PSČ (změna webu?) záložně podle názvu."""
    if item.get("psc"):
        return match_town_by_psc(item["psc"], psc_map)
    return match_town(item["location_text"])


def to_listing(item: dict[str, str], kind: str, base_url: str,
               psc_map: dict[str, str] | None = None) -> Listing | None:
    id_m = _ID_RE.search(item["href"])
    if not id_m:
        return None
    text = f'{item["title"]} {item["description"]}'
    if kind == "dum":
        subtype = house_subtype_from_text(item["title"]) if item["title"] else house_subtype_from_text(text)
        if subtype in EXCLUDED_HOUSE_SUBTYPES:
            return None
        land = parse_land(text)
    else:
        subtype, land = parse_layout(text), None
    area = parse_area(text)
    href = item["href"] if item["href"].startswith("http") else base_url + item["href"]
    return Listing(
        source=name, external_id=id_m.group(1), url=href, kind=kind, subtype=subtype,
        price=parse_price(item["price_text"]), area_m2=area, land_m2=land,
        town=town_for(item, DEFAULT_PSC if psc_map is None else psc_map),
        locality=" ".join(x for x in (item["location_text"], item.get("psc", "")) if x) or None,
        description=text.strip() or None, scraped_at=utc_now_iso(),
        parse_confidence="high" if area is not None and subtype is not None else "low",
    )


def fetch_listings(cfg: FlipConfig, known_ids: set[str]) -> FetchResult:
    selectors = load_selectors()
    kw = {"robots": RobotsChecker(), "rate_limiter": RateLimiter()}
    out: list[Listing] = []
    raw_count = 0

    with httpx.Client(timeout=20.0, follow_redirects=True) as client:
        for category in selectors["categories"]:
            kind = category["kind"]
            if not cfg.wants_kind(kind):
                continue
            category_raw = 0
            for page_idx in range(cfg.bazos_max_pages):
                offset = page_idx * selectors["page_offset_step"]
                path = category["path"] if offset == 0 else f'{category["path"]}{offset}/'
                resp = polite_get(client, selectors["base_url"] + path, **kw)
                if resp is None:
                    break
                items = parse_page(resp.text, selectors)
                if not items:
                    break
                category_raw += len(items)
                for item in items:
                    lst = to_listing(item, kind, selectors["base_url"], cfg.bazos_psc)
                    if lst is None or not lst.in_area:
                        continue
                    if lst.price is not None and lst.price > cfg.price_max:
                        continue
                    out.append(lst)
            if category_raw == 0:
                log.warning("Bazoš: kategorie %s nevrátila nic (změna webu?)", category["path"])
            raw_count += category_raw

    log.info("Bazoš: %d inzerátů ve sledovaných obcích (z %d prošlých)", len(out), raw_count)
    return FetchResult(out, raw_count)
