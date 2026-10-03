"""
test_api.py – krok 1: ověření reality Sreality API (prodej bytů, Olomoucký kraj).

Cíl skriptu (nic víc – je to průzkumný/ověřovací krok, ne produkční scraper):
  1. Zavolá Sreality API pro prodej bytů v Olomouckém kraji.
  2. Stáhne ~20 inzerátů ze SEZNAMU.
  3. Pro každý vypíše: source_id, cena, plocha, dispozice, lokalita, GPS.
     Energetický štítek + prvních 200 znaků popisu jsou až v DETAILU, proto
     se detail stahuje POUZE u prvních 3 inzerátů (ukázka, ne u všech 20).
  4. Uloží surovou JSON odpověď (seznam + 3 detaily) do raw_sample.json pro
     offline vývoj scoringu.

Co bylo ověřeno živým voláním API (15. 6. 2026) – NEhádá se:
  * Starý veřejný JSON `/api/cs/v2/estates` z původního zadání je MRTVÝ (404).
    Web Sreality je dnes Next.js a data tahá klientsky z:
        SEARCH : https://www.sreality.cz/api/v1/estates/search   (HTTP 200, bez přihlášení)
        DETAIL : https://www.sreality.cz/api/v1/estates/{hash_id} (HTTP 200, bez přihlášení)
    Pozn.: `/api/v1/estates` BEZ `/search` vrací 401 (to je něco jiného – nesahat).
  * Olomoucký kraj = locality_region_id = 8  (ověřeno z /estates/filter_page,
    ne z tipu). V Olomouckém kraji je aktuálně ~733 bytů na prodej.
  * Stránkování: parametry `limit` a `offset` (NE per_page/page). Default limit=100.
  * Dispozice je strukturované pole `category_sub_cb.name` ("2+kk", "3+1", …).
  * Plocha NENÍ v seznamu jako pole – dopočítá se z `price_czk / price_czk_m2`
    (fallback: regex z `advert_name`).
  * GPS, okres i kraj jsou uvnitř objektu `locality` (gps_lat, gps_lon,
    district, district_id, region, region_id).
  * Popis (`advert_description`), energetický štítek (`energy_efficiency_rating_cb`)
    a hlavně STAV (`building_condition`, enum – 8 = "Před rekonstrukcí") jsou
    JEN v detailu. To rozhoduje o počtu requestů (1 detail / inzerát).
  * Některé inzeráty mají price_czk = 0 ("Info o ceně u RK") -> bereme jako None.

Prostředí: Python 3.11, jen httpx (žádný Selenium), cílově ARM/1 GB RAM na NAS.
Rate-limit >= 1,5 s mezi requesty + slušný identifikující User-Agent (ToS/etika).
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from typing import Any

import httpx

# Windows konzole jede defaultně v cp1250 a spadla by při tisku "m²"/emoji
# (UnicodeEncodeError). Na NASu (Linux) je UTF-8 nativně; tohle jen sjednotí
# chování i na Windows PC, kde se vyvíjí. Musí proběhnout PŘED logging.basicConfig,
# ať i log handler (stderr) píše v UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

# --------------------------------------------------------------------------- #
# Konfigurace (v produkci půjde do config.py / .env – tady natvrdo pro test).  #
# --------------------------------------------------------------------------- #

SEARCH_URL = "https://www.sreality.cz/api/v1/estates/search"
DETAIL_URL = "https://www.sreality.cz/api/v1/estates/{hash_id}"

# Filtry: byty (category_main_cb=1), prodej (category_type_cb=1), Olomoucký kraj (8).
QUERY_PARAMS: dict[str, int] = {
    "category_main_cb": 1,   # 1 = Byty
    "category_type_cb": 1,   # 1 = Prodej
    "locality_region_id": 8,  # 8 = Olomoucký kraj (ověřeno z filter_page)
    "limit": 20,             # ~20 inzerátů ze seznamu
    "offset": 0,
}

DETAIL_SAMPLE = 3            # detail stahujeme jen u prvních N inzerátů
RATE_LIMIT_S = 1.6          # pauza mezi requesty (>= 1,5 s)
HTTP_TIMEOUT = 20.0
RAW_SAMPLE_FILE = "raw_sample.json"

# Slušný, identifikující User-Agent (ne maskování za prohlížeč). API ho přijímá.
USER_AGENT = "realitni-scraper/0.1 (osobni nekomercni projekt)"
HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("test_api")

# Sdílený stav pro rate-limiting napříč voláními.
_last_request_ts: float = 0.0


# --------------------------------------------------------------------------- #
# HTTP vrstva                                                                  #
# --------------------------------------------------------------------------- #

def _respect_rate_limit() -> None:
    """Uspí vlákno tak, aby mezi dvěma requesty bylo aspoň RATE_LIMIT_S."""
    global _last_request_ts
    elapsed = time.monotonic() - _last_request_ts
    if elapsed < RATE_LIMIT_S:
        time.sleep(RATE_LIMIT_S - elapsed)
    _last_request_ts = time.monotonic()


def polite_get(client: httpx.Client, url: str, params: dict | None = None) -> dict[str, Any]:
    """
    GET s rate-limitingem a JSON parsováním. Vrací dict; při chybě vyhodí výjimku,
    kterou volající ošetří (jeden chybný request nesmí shodit celý běh).
    """
    _respect_rate_limit()
    log.info("GET %s %s", url, params or "")
    resp = client.get(url, params=params)
    resp.raise_for_status()
    return resp.json()


def fetch_search(client: httpx.Client) -> dict[str, Any]:
    """Stáhne seznam inzerátů (surová odpověď API)."""
    return polite_get(client, SEARCH_URL, QUERY_PARAMS)


def fetch_detail(client: httpx.Client, hash_id: str | int) -> dict[str, Any]:
    """Stáhne detail jednoho inzerátu (surová odpověď API)."""
    return polite_get(client, DETAIL_URL.format(hash_id=hash_id))


# --------------------------------------------------------------------------- #
# Normalizace (vše přes .get() – API se mění, nikdy přímý index)               #
# --------------------------------------------------------------------------- #

_AREA_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*m(?:²|2)\b", re.IGNORECASE)
_DISP_RE = re.compile(r"\b(\d\s*\+\s*(?:kk|\d))\b", re.IGNORECASE)


def _derive_area_m2(estate: dict[str, Any]) -> float | None:
    """
    Plocha v seznamu chybí jako pole. Dopočítáme z ceny: price_czk / price_czk_m2.
    Fallback: vytáhnout "55 m²" z advert_name. Když nic, vrať None (nehádej).
    """
    price = estate.get("price_czk")
    price_m2 = estate.get("price_czk_m2")
    if price and price_m2:  # oba nenulové
        return round(price / price_m2, 1)
    name = estate.get("advert_name") or ""
    m = _AREA_RE.search(name)
    if m:
        return float(m.group(1).replace(",", "."))
    return None


def _derive_disposition(estate: dict[str, Any]) -> str | None:
    """Dispozice ze strukturovaného pole; fallback regex z názvu."""
    sub = estate.get("category_sub_cb") or {}
    if isinstance(sub, dict) and sub.get("name"):
        return sub["name"]
    m = _DISP_RE.search(estate.get("advert_name") or "")
    return m.group(1).replace(" ", "") if m else None


def normalize_summary(estate: dict[str, Any]) -> dict[str, Any]:
    """Z jednoho prvku search.results udělá ploché, čitelné shrnutí."""
    locality = estate.get("locality") or {}
    price = estate.get("price_czk")
    return {
        "source_id": str(estate.get("hash_id")),
        "price_czk": price if price else None,  # 0 = "info u RK" -> None
        "price_czk_m2": estate.get("price_czk_m2") or None,
        "area_m2": _derive_area_m2(estate),
        "disposition": _derive_disposition(estate),
        "locality_text": ", ".join(
            p for p in (locality.get("city"), locality.get("district")) if p
        ) or None,
        "region": locality.get("region"),
        "region_id": locality.get("region_id"),
        "gps_lat": locality.get("gps_lat"),
        "gps_lon": locality.get("gps_lon"),
        "discount_show": estate.get("discount_show"),
        "advert_name": estate.get("advert_name"),
    }


def extract_detail_fields(detail: dict[str, Any]) -> dict[str, Any]:
    """Z detailu vytáhne to, co v seznamu není: štítek, stav, popis, plochu, starou cenu."""
    result = detail.get("result") or {}
    energy = result.get("energy_efficiency_rating_cb") or {}
    condition = result.get("building_condition") or {}
    description = result.get("advert_description") or ""
    return {
        "energy_rating": energy.get("name"),          # napr. "G - Mimořádně nehospodárná"
        "building_condition": condition.get("name"),  # napr. "Před rekonstrukcí"  <-- klíčový signál
        "usable_area": result.get("usable_area"),
        "price_old_czk": result.get("price_summary_old_czk"),  # stará cena = pokles ceny bez historie
        "description_200": description[:200],
        "description_len": len(description),
    }


# --------------------------------------------------------------------------- #
# Výpis                                                                        #
# --------------------------------------------------------------------------- #

def _fmt_price(czk: int | None) -> str:
    return f"{czk:,} Kč".replace(",", " ") if czk else "—  (info u RK)"


def print_listing(idx: int, summary: dict[str, Any], detail: dict[str, Any] | None) -> None:
    """Vypíše jeden inzerát do konzole (čitelný report – účel tohoto skriptu)."""
    gps = (
        f"{summary['gps_lat']}, {summary['gps_lon']}"
        if summary.get("gps_lat") is not None else "—"
    )
    area = f"{summary['area_m2']} m²" if summary.get("area_m2") else "—"
    ppm2 = f" ({summary['price_czk_m2']:,} Kč/m²)".replace(",", " ") if summary.get("price_czk_m2") else ""

    print(f"\n[{idx:>2}] #{summary['source_id']}  {summary.get('disposition') or '?'}  {area}")
    print(f"     💰 {_fmt_price(summary['price_czk'])}{ppm2}")
    print(f"     📍 {summary.get('locality_text') or '—'}  ({summary.get('region') or '?'})")
    print(f"     🛰  GPS: {gps}   |  sleva zobrazena: {summary.get('discount_show')}")

    if detail is not None:
        print(f"     🔨 Stav (building_condition): {detail.get('building_condition') or '—'}")
        print(f"     ⚡ Štítek: {detail.get('energy_rating') or '—'}"
              f"   |  plocha z detailu: {detail.get('usable_area') or '—'} m²"
              f"   |  stará cena: {_fmt_price(detail.get('price_old_czk'))}")
        desc = (detail.get("description_200") or "").replace("\n", " ").strip()
        print(f"     📝 Popis ({detail.get('description_len', 0)} zn.): {desc or '—'}")
    else:
        print("     (detail nestahován – štítek/stav/popis jsou jen v detailu)")


# --------------------------------------------------------------------------- #
# Uložení surového vzorku                                                      #
# --------------------------------------------------------------------------- #

def save_raw_sample(search_raw: dict[str, Any], details_raw: dict[str, Any]) -> None:
    """Uloží surová data do raw_sample.json pro offline vývoj scoringu."""
    payload = {
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": "sreality",
        "note": "Surova ukazka z /api/v1/estates/search + /api/v1/estates/{hash_id}.",
        "endpoint_search": SEARCH_URL,
        "endpoint_detail": DETAIL_URL,
        "query": QUERY_PARAMS,
        "search": search_raw,
        "details": details_raw,
    }
    with open(RAW_SAMPLE_FILE, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    log.info("Surová data uložena do %s", RAW_SAMPLE_FILE)


# --------------------------------------------------------------------------- #
# Orchestrace                                                                  #
# --------------------------------------------------------------------------- #

def main() -> None:
    log.info("Start: prodej bytů, Olomoucký kraj (region_id=%s)", QUERY_PARAMS["locality_region_id"])

    with httpx.Client(headers=HEADERS, timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        # 1) seznam
        try:
            search_raw = fetch_search(client)
        except Exception as exc:  # noqa: BLE001 – jeden výpadek nesmí shodit běh
            log.error("Nepodařilo se stáhnout seznam: %s", exc)
            return

        estates = search_raw.get("results") or []
        total = (search_raw.get("pagination") or {}).get("total")
        log.info("Staženo %d inzerátů ze seznamu (celkem v kraji: %s)", len(estates), total)
        if not estates:
            log.warning("Seznam je prázdný – končím.")
            return

        # 2) detail jen u prvních DETAIL_SAMPLE (štítek/stav/popis jsou jen tam)
        details_raw: dict[str, Any] = {}
        for i, estate in enumerate(estates):
            summary = normalize_summary(estate)
            detail_fields = None

            if i < DETAIL_SAMPLE:
                hash_id = estate.get("hash_id")
                try:
                    detail_raw = fetch_detail(client, hash_id)
                    details_raw[str(hash_id)] = detail_raw
                    detail_fields = extract_detail_fields(detail_raw)
                except Exception as exc:  # noqa: BLE001 – chybný detail přeskoč
                    log.error("Detail #%s selhal: %s", hash_id, exc)

            print_listing(i + 1, summary, detail_fields)

    # 3) ulož surový vzorek
    save_raw_sample(search_raw, details_raw)
    print(f"\n✅ Hotovo. {len(estates)} inzerátů vypsáno, "
          f"{len(details_raw)} detailů staženo, vzorek v {RAW_SAMPLE_FILE}.")


if __name__ == "__main__":
    main()
