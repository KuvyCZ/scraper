"""flip/sources/realingo.py — realingo.cz (Next.js agregátor).

Ověřeno živě (2026-07-24) pro byty:
  * data jsou v `__NEXT_DATA__` JSON (props.pageProps.store.offer.list.data),
    CSS třídy jsou hashované -> na ně se nespoléhá;
  * okres: /prodej_reality/Okres_{okres}/ ; od nejlevnějšího: /nejlevnejsi/ ;
    stránkování: /{N}_strana/ ;
  * `purpose` je autoritativní pro prodej/pronájem (ne URL);
  * popis je jen na detailu (store.offer.details[id].offer.detail.description).

⚠️ NEOVĚŘENO pro domy: hodnota `property` u domů ("HOUSE") a kódy `category`.
Neznámé hodnoty se logují (`python main.py check` je vypíše), ať jde mapování
doplnit v selectors/realingo.json bez zásahu do kódu.

Vrací jen inzeráty ze sledovaných obcí (data o trhu z okresu dodává Sreality —
tady by se bez detailu a deduplikace jen zdvojovala).
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from flip.config import FlipConfig
from flip.http import RateLimiter, RobotsChecker, polite_get
from flip.model import FetchResult, Listing, utc_now_iso
from flip.text import EXCLUDED_HOUSE_SUBTYPES, parse_layout
from flip.towns import match_town

log = logging.getLogger("flip.sources.realingo")

name = "realingo"

_SELECTORS_PATH = Path(__file__).parent / "selectors" / "realingo.json"
_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)


def load_selectors() -> dict[str, Any]:
    with open(_SELECTORS_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def parse_next_data(html: str) -> dict[str, Any] | None:
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def list_items(data: dict[str, Any]) -> list[dict[str, Any]] | None:
    try:
        return data["props"]["pageProps"]["store"]["offer"]["list"]["data"]
    except (KeyError, TypeError):
        return None


def house_subtype(category: str | None, selectors: dict[str, Any]) -> str:
    """Kód kategorie -> typ domu. Přesná mapa z configu, jinak podle podřetězce."""
    if not category:
        return "Rodinný"
    exact = selectors.get("house_category_map", {}).get(category)
    if exact:
        return exact
    for needle, subtype in selectors.get("house_category_contains", {}).items():
        if needle in category.upper():
            return subtype
    return "Rodinný"


def normalize_item(item: dict[str, Any], selectors: dict[str, Any]) -> Listing | None:
    """Prvek seznamu -> Listing; None = pronájem, jiný typ nemovitosti, nebo chata."""
    if item.get("purpose") != "SELL":
        return None
    kind = selectors["property_kind_map"].get(item.get("property"))
    if kind is None:
        return None
    category = item.get("category")
    if kind == "byt":
        subtype = selectors["category_layout_map"].get(category) or parse_layout(category)
    else:
        subtype = house_subtype(category, selectors)
        if subtype in EXCLUDED_HOUSE_SUBTYPES:
            return None
    area = item.get("area") or {}
    address = (item.get("location") or {}).get("address")
    land = (area.get("plot") or area.get("land")) if kind == "dum" else None
    return Listing(
        source=name, external_id=str(item["id"]), url=selectors["base_url"] + item["url"], kind=kind,
        subtype=subtype, price=(item.get("price") or {}).get("total"), area_m2=area.get("main"),
        land_m2=float(land) if land else None, town=match_town(address), locality=address,
        scraped_at=utc_now_iso(),
    )


def build_list_url(selectors: dict[str, Any], cfg: FlipConfig, page: int) -> str:
    path = selectors["district_path_template"].format(location=quote(cfg.district_name))
    if page > 1:
        path += selectors["page_path_suffix"].format(n=page)
    return selectors["base_url"] + path


def _fetch_description(client: httpx.Client, url: str, external_id: str, **kw: Any) -> str | None:
    resp = polite_get(client, url, **kw)
    data = parse_next_data(resp.text) if resp is not None else None
    if not data:
        return None
    try:
        return data["props"]["pageProps"]["store"]["offer"]["details"][external_id]["offer"]["detail"].get("description")
    except (KeyError, TypeError):
        return None


def fetch_listings(cfg: FlipConfig, known_ids: set[str]) -> FetchResult:
    selectors = load_selectors()
    kw = {"robots": RobotsChecker(), "rate_limiter": RateLimiter()}
    out: list[Listing] = []
    raw_count = 0
    unknown: Counter[str] = Counter()

    with httpx.Client(timeout=20.0, follow_redirects=True) as client:
        for page in range(1, cfg.realingo_max_pages + 1):
            url = build_list_url(selectors, cfg, page)
            resp = polite_get(client, url, **kw)
            if resp is None:
                break
            data = parse_next_data(resp.text)
            items = list_items(data) if data else None
            if items is None:
                log.error("Realingo: data stránky nenalezena na %s (redesign? zkontroluj selectors/realingo.json)", url)
                break
            if not items:
                break
            raw_count += len(items)

            page_in_budget = False
            for item in items:
                price = (item.get("price") or {}).get("total")
                if price is not None and price <= cfg.price_max:
                    page_in_budget = True
                if item.get("purpose") == "SELL" and item.get("property") not in selectors["property_kind_map"]:
                    unknown[f"property={item.get('property')}"] += 1
                lst = normalize_item(item, selectors)
                if lst is None or not lst.in_area or not cfg.wants_kind(lst.kind):
                    continue
                if lst.kind == "dum" and item.get("category") not in selectors.get("house_category_map", {}):
                    unknown[f"house category={item.get('category')}"] += 1
                if lst.price is not None and lst.price > cfg.price_max:
                    continue
                if lst.external_id not in known_ids:
                    lst.description = _fetch_description(client, lst.url, lst.external_id, **kw)
                out.append(lst)

            # řazeno od nejlevnějšího: celá strana nad rozpočtem -> dál už jen dražší
            if page > 1 and not page_in_budget:
                break

    if unknown:
        log.info("Realingo: neznámé hodnoty (doplň do selectors/realingo.json): %s", dict(unknown.most_common(10)))
    log.info("Realingo: %d inzerátů ve sledovaných obcích (z %d prošlých)", len(out), raw_count)
    return FetchResult(out, raw_count)
