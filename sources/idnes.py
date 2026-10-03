"""
sources/idnes.py — scraper iDnes Reality (reality.idnes.cz). HTML, ne JSON API.

iDnes nemá veřejné JSON/GraphQL API → parsuje se HTML přes BeautifulSoup
(backend stdlib "html.parser", žádný lxml → na ARM se nic nekompiluje).

Dvoufázový sběr (jako Sreality):
  1) SEARCH  https://reality.idnes.cz/s/prodej/byty/{kraj}/?page=N
     Kartička `div.c-products__item` dá: id, url, dispozici, plochu, cenu, lokalitu.
     POZOR: okres je na kartě jen občas (u měst chybí) → bere se z detailu.
  2) DETAIL  /detail/prodej/byt/{...}/{hash}/
     Tabulka `dl/dt/dd` dá stav (Stav bytu/budovy), PENB, přesnou plochu;
     `.b-detail__info` dá okres; `.b-desc` dá popis. Detail jen pro NEznámé (known_source_ids).

GDPR: kontaktní blok (jméno/telefon prodejce) se NEparsuje. Ukládá se jen popis nemovitosti.

Mapování na Listing (sjednocené se Sreality kvůli mediánu cena/m² per okres):
  source_id          = hash z URL detailu
  building_condition = "Stav bytu" | "Stav budovy"  -> "Velmi dobrý", "Před rekonstrukcí", …
  district           = z `.b-detail__info` ("…, okres Šumperk" -> "Šumperk")
  region             = název kraje dle slugu (kvůli párování se Sreality)
  listed_date        = None (iDnes na seznamu datum nedává; doba inzerce z DB seen_first)
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from collections import Counter
from typing import Any, Iterator

import httpx
from bs4 import BeautifulSoup

from .base import Listing, Source

log = logging.getLogger("sources.idnes")

SEARCH_URL = "https://reality.idnes.cz/s/prodej/byty/{region}/"
SAFETY_PAGE_CAP = 200  # pojistka proti nekonečnému stránkování

# Slug kraje -> přesný název (musí sedět na Sreality `region` kvůli fallbacku mediánu na kraj).
_REGION_NAME: dict[str, str] = {
    "olomoucky-kraj": "Olomoucký kraj",
}

# Okres slug -> název (sjednoceno s okresy Sreality kvůli mediánu). Zatím Olomoucký kraj (5 okresů);
# pro další kraje doplnit jejich okresy. Slug se bere z SEO odkazů detailu "…-okres-{slug}/".
_OKRES_NAME: dict[str, str] = {
    "olomouc": "Olomouc",
    "prostejov": "Prostějov",
    "prerov": "Přerov",
    "sumperk": "Šumperk",
    "jesenik": "Jeseník",
}

# Stav z iDnes ("velmi dobrý stav", "před rekonstrukcí") -> názvy, na které reaguje scoring.COND_SCORE.
_COND_NORMALIZE: dict[str, str] = {
    "velmi dobry": "Velmi dobrý",
    "dobry": "Dobrý",
    "spatny": "Špatný",
    "pred rekonstrukci": "Před rekonstrukcí",
    "po rekonstrukci": "Po rekonstrukci",
    "po castecne rekonstrukci": "Po částečné rekonstrukci",
    "v rekonstrukci": "V rekonstrukci",
    "novostavba": "Novostavba",
    "ve vystavbe": "Ve výstavbě",
    "k demolici": "K demolici",
    "projekt": "Projekt",
}

_ENERGY = "ABCDEFG"


def _strip_diacritics(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c)).strip()


def _digits_to_int(text: str | None) -> int | None:
    """'3 190 000 Kč' -> 3190000; vrátí None, když tam číslo není."""
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


class IdnesSource(Source):
    """Zdroj iDnes Reality (HTML). Vrací `list[Listing]` přes jednotné schéma z base.py."""

    name = "idnes"

    def __init__(
        self,
        region_slugs: list[str] | None = None,   # slugy krajů v URL (olomoucky-kraj)
        *,
        max_listings: int | None = None,         # strop inzerátů na region (None = vše); hl. pro testy
        fetch_detail: bool = True,               # tahat detail (stav/popis/štítek)?
        detail_limit: int | None = None,         # strop detailů na běh
        known_source_ids: set[str] | None = None,  # ID už v DB -> detail nepřetahuj
        rate_limit_s: float = 1.6,
        timeout: float = 25.0,
        user_agent: str = "realitni-scraper/0.1 (osobni nekomercni projekt)",
    ) -> None:
        self.region_slugs = region_slugs or ["olomoucky-kraj"]
        self.max_listings = max_listings
        self.fetch_detail = fetch_detail
        self.detail_limit = detail_limit
        self.known_source_ids = known_source_ids
        self.rate_limit_s = rate_limit_s
        self.timeout = timeout
        self.headers = {"User-Agent": user_agent, "Accept": "text/html"}
        self._last_request_ts = 0.0

    # ------------------------------------------------------------------ #
    # HTTP vrstva s rate-limitingem                                      #
    # ------------------------------------------------------------------ #

    def _respect_rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_ts
        if elapsed < self.rate_limit_s:
            time.sleep(self.rate_limit_s - elapsed)
        self._last_request_ts = time.monotonic()

    def _get_soup(self, client: httpx.Client, url: str, params: dict | None = None) -> BeautifulSoup:
        self._respect_rate_limit()
        resp = client.get(url, params=params)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")

    # ------------------------------------------------------------------ #
    # SEARCH (stránkování ?page=N)                                       #
    # ------------------------------------------------------------------ #

    def _iter_search(self, client: httpx.Client, region_slug: str) -> Iterator[Any]:
        fetched = 0
        for page in range(1, SAFETY_PAGE_CAP + 1):
            if self.max_listings is not None and fetched >= self.max_listings:
                return
            try:
                soup = self._get_soup(client, SEARCH_URL.format(region=region_slug), {"page": page})
            except Exception as exc:  # noqa: BLE001 – výpadek stránky nezahodí už stažené
                log.error("SEARCH selhal (region=%s, page=%s): %s", region_slug, page, exc)
                return

            cards = soup.select("div.c-products__item")
            if not cards:
                return
            yielded_on_page = 0
            for card in cards:
                if self.max_listings is not None and fetched >= self.max_listings:
                    break
                if card.select_one("a.c-products__link"):  # přeskoč reklamní/prázdné dlaždice
                    yield card
                    fetched += 1
                    yielded_on_page += 1
            log.info("region=%s: page %d (+%d, celkem %d)", region_slug, page, yielded_on_page, fetched)
            if yielded_on_page == 0:
                return

    # ------------------------------------------------------------------ #
    # Normalizace karty (seznam)                                         #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _source_id(href: str) -> str | None:
        m = re.search(r"([0-9a-f]{20,})", href)
        return m.group(1) if m else None

    @staticmethod
    def _disposition(title: str) -> str | None:
        m = re.search(r"\d\+(?:kk|\d)", title)
        if m:
            return m.group(0)
        low = _strip_diacritics(title)
        if "garsonier" in low:
            return "Garsoniéra"
        return None

    @staticmethod
    def _parse_area(text: str | None) -> float | None:
        """'74 m²' / '52,72 m 2' -> float m². Bere číslo TĚSNĚ před 'm' (ne dispozici z '3+1'
        ani '2' z rozbitého <sup>2</sup>). Zvládne i desetinnou čárku."""
        if not text:
            return None
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*m", text)
        return float(m.group(1).replace(",", ".")) if m else None

    def _normalize_card(self, card: Any, region_name: str) -> Listing | None:
        link = card.select_one("a.c-products__link")
        href = (link.get("href") or "").strip() if link else ""
        source_id = self._source_id(href)
        if not (href and source_id):
            return None
        title = card.select_one(".c-products__title")
        title_text = title.get_text(" ", strip=True) if title else ""
        info = card.select_one(".c-products__info")
        price = card.select_one(".c-products__price")
        return Listing(
            source=self.name,
            source_id=source_id,
            url=href,
            price=_digits_to_int(price.get_text(" ", strip=True)) if price else None,
            area_m2=self._parse_area(title_text),
            disposition=self._disposition(title_text),
            locality_text=info.get_text(" ", strip=True) if info else None,
            region=region_name,
        )

    # ------------------------------------------------------------------ #
    # Detail (stav, PENB, okres, popis)                                  #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _params(soup: BeautifulSoup) -> dict[str, str]:
        """Parametry z `dl/dt/dd` jako {label: value}."""
        params: dict[str, str] = {}
        for dl in soup.select("dl"):
            dts, dds = dl.select("dt"), dl.select("dd")
            for dt, dd in zip(dts, dds):
                params[dt.get_text(" ", strip=True)] = dd.get_text(" ", strip=True)
        return params

    @staticmethod
    def _condition(params: dict[str, str]) -> str | None:
        raw = params.get("Stav bytu") or params.get("Stav budovy")
        if not raw:
            return None
        key = _strip_diacritics(raw).replace(" stav", "").strip()
        return _COND_NORMALIZE.get(key)

    @staticmethod
    def _energy(params: dict[str, str]) -> str | None:
        raw = (params.get("PENB") or "").strip().upper()
        return raw[0] if raw[:1] in _ENERGY else None

    @staticmethod
    def _district(soup: BeautifulSoup) -> str | None:
        # Primárně okres ze SEO odkazů "…-okres-{slug}/" (spolehlivé i u městských bytů).
        slugs = [
            m.group(1)
            for a in soup.find_all("a", href=True)
            if (m := re.search(r"okres-([a-z]+)", a["href"]))
        ]
        for slug, _ in Counter(slugs).most_common():
            if slug in _OKRES_NAME:
                return _OKRES_NAME[slug]
        # Fallback: "…, okres X" v podtitulku (.b-detail__info).
        info = soup.select_one(".b-detail__info")
        text = info.get_text(" ", strip=True) if info else ""
        m = re.search(r"okres\s+([^,]+)$", text)
        return m.group(1).strip() if m else None

    def _enrich_detail(self, listing: Listing, soup: BeautifulSoup) -> None:
        params = self._params(soup)
        listing.building_condition = self._condition(params)
        listing.energy_rating = self._energy(params)
        area = self._parse_area(params.get("Užitná plocha"))
        if area:
            listing.area_m2 = area
        listing.district = self._district(soup)
        desc = soup.select_one(".b-desc")
        if desc:
            listing.description = desc.get_text(" ", strip=True) or None

    def _needs_detail(self, source_id: str) -> bool:
        if self.known_source_ids is None:
            return True
        return source_id not in self.known_source_ids

    # ------------------------------------------------------------------ #
    # Veřejné API zdroje                                                 #
    # ------------------------------------------------------------------ #

    def fetch(self) -> list[Listing]:
        listings: list[Listing] = []
        detail_count = 0
        seen: set[str] = set()  # iDnes opakuje inzeráty napříč stránkami -> ať netaháme detail 2x
        log.info("iDnes fetch: regiony=%s, fetch_detail=%s", self.region_slugs, self.fetch_detail)

        with httpx.Client(headers=self.headers, timeout=self.timeout, follow_redirects=True) as client:
            for slug in self.region_slugs:
                region_name = _REGION_NAME.get(slug, slug.replace("-", " ").title())
                for card in self._iter_search(client, slug):
                    try:
                        listing = self._normalize_card(card, region_name)
                    except Exception as exc:  # noqa: BLE001 – jedna vadná karta nezhasne zbytek
                        log.warning("Přeskakuji kartu: %s", exc)
                        continue
                    if listing is None or listing.source_id in seen:
                        continue
                    seen.add(listing.source_id)

                    want_detail = (
                        self.fetch_detail
                        and self._needs_detail(listing.source_id)
                        and (self.detail_limit is None or detail_count < self.detail_limit)
                    )
                    if want_detail:
                        try:
                            soup = self._get_soup(client, listing.url)
                            self._enrich_detail(listing, soup)
                            detail_count += 1
                        except Exception as exc:  # noqa: BLE001 – chybný detail nezahodí inzerát
                            log.error("Detail %s selhal: %s", listing.source_id, exc)

                    listings.append(listing)

        log.info("iDnes fetch hotovo: %d inzerátů, %d detailů staženo", len(listings), detail_count)
        return listings


if __name__ == "__main__":
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

    src = IdnesSource(region_slugs=["olomoucky-kraj"], max_listings=6, detail_limit=4)
    items = src.fetch()

    print(f"\n=== {len(items)} Listing objektů ===")
    for i, lst in enumerate(items, 1):
        ppm2 = f"{lst.price_per_m2:,} Kč/m²".replace(",", " ") if lst.price_per_m2 else "—"
        price = f"{lst.price:,} Kč".replace(",", " ") if lst.price else "— (info)"
        print(f"\n[{i}] #{lst.source_id}  {lst.disposition or '?'}  {lst.area_m2 or '?'} m²  |  {price} ({ppm2})")
        print(f"    📍 {lst.locality_text} · okres {lst.district} · {lst.region}")
        print(f"    🔨 stav: {lst.building_condition or '— (bez detailu)'}  |  ⚡ {lst.energy_rating or '—'}")
        print(f"    🔗 {lst.url}")
        if lst.description:
            print(f"    📝 {lst.description[:110].replace(chr(10), ' ')}…")

    s_stav = sum(1 for x in items if x.building_condition)
    s_okres = sum(1 for x in items if x.district)
    print(f"\n✅ idnes.py OK — {s_stav}/{len(items)} má stav, {s_okres}/{len(items)} má okres (z detailu).")
