"""
comps.py — srovnatelné nemovitosti v okolí ("comps") + výpočet, jestli se vyplatí rekonstrukce.

Odpovídá na dvě otázky, které chce člověk vidět hned v notifikaci:
  1) "Za kolik se prodávají podobné nemovitosti v okolí?"  -> medián a rozptyl (p25–p75)
  2) "Vyplatí se to koupit a opravit?"                     -> ARV − cena − náklady = marže

⚠️ ZÁSADNÍ OMEZENÍ DAT (nepřehlédnout při čtení výsledků):
  * Portály zveřejňují jen NABÍDKOVÉ ceny, ne realizované. Comps tedy říkají
    "za kolik se nabízí", ne "za kolik se prodalo". Reálná prodejní cena bývá nižší
    -> proto `sale_factor` (default 0.92), kterým se ARV sráží na střízlivější odhad.
  * Vzorek = jen to, co máme v DB (aktuálně inzerované). Není to katastr.
  * U DOMŮ je Kč/m² stavby zavádějící (velký vliv pozemku a lokality) -> proto se
    u domů reportuje i medián pozemku, ať je vidět, s čím se to vlastně srovnává.
  * Náklady na rekonstrukci jsou HRUBÝ ODHAD (Kč/m² × plocha), ne rozpočet.
Výstup je tedy vodítko "kam se podívat", ne ocenění. Formulace v textu to drží.

ARV (After Repair Value) = odhad ceny po rekonstrukci: medián Kč/m² u srovnatelných
nemovitostí, které JSOU v dobrém stavu (po rekonstrukci / novostavba / velmi dobrý),
× plocha hodnocené nemovitosti. Když takových comps není dost, ARV = None (nehádáme).
"""

from __future__ import annotations

import math
import sqlite3
import statistics
from dataclasses import dataclass
from typing import Any

from sources.base import Listing

# Stavy, které považujeme za "hotové" -> z nich se počítá ARV (cena po rekonstrukci).
RENOVATED_CONDITIONS = ("Po rekonstrukci", "Novostavba", "Velmi dobrý")
# Stavy, u kterých má smysl počítat náklady na opravu a marži.
NEEDS_RENOVATION_CONDITIONS = ("Před rekonstrukcí", "Špatný", "K demolici")


def needs_renovation(listing: Listing) -> bool:
    """Má u téhle nemovitosti smysl počítat ekonomiku rekonstrukce?"""
    return listing.building_condition in NEEDS_RENOVATION_CONDITIONS

MIN_COMPS = 5           # míň srovnatelných = nespolehlivé, radši nic neříkat
AREA_BAND = 0.30        # srovnávej ±30 % plochy

RADIUS_KM = 15.0        # "blízké okolí" pro comps; u domů rozhoduje lokalita víc než plocha

# Kdy výsledku NEVĚŘIT bez očí. Prahy jsou nastavené z reálných dat tak, aby varování
# NEBYLO u každého druhého inzerátu (jinak ho člověk přestane číst):
#  * Nemovitost před rekonstrukcí BĚŽNĚ stojí 60–75 % pod mediánem (ten obsahuje hotové)
#    -> samotná nízká cena/m² varování NESPOUŠTÍ.
#  * Ověřeno na datech: 203m² "byt" za 4,85 M v Olomouci vyšel na 47% marži, ale byl to
#    PŮDNÍ PROSTOR k vybudování bytu -> extrémně nízká cena/m² tam byla příznakem
#    nesrovnatelné nemovitosti, ne příležitosti.
SUSPICIOUS_DISCOUNT = 0.40    # pod 40 % mediánu = spíš ruina/podíl/chyba než sleva
SUSPICIOUS_MARGIN_PCT = 60.0  # marže nad 60 % = nerealistické i na papíře
DEFAULT_RENO_COST_M2 = 20_000   # Kč/m² hrubý odhad celkové rekonstrukce (konfigurovatelné)
DEFAULT_SALE_FACTOR = 0.92      # nabídková -> očekávaná realizovaná cena


@dataclass
class Comps:
    """Výsledek srovnávací analýzy. Pole = None znamená 'nemáme dost dat' (nikdy se nehádá)."""

    scope: str                      # "okres Šumperk" / "Olomoucký kraj"
    count: int                      # kolik srovnatelných nemovitostí
    median_price: int | None
    median_ppm2: int | None
    p25_ppm2: int | None
    p75_ppm2: int | None
    median_land_m2: int | None      # jen domy — s čím se to vlastně srovnává
    subject_ppm2: int | None = None   # Kč/m² hodnocené nemovitosti (ať je vidět poměr)
    vs_median_pct: float | None = None  # o kolik % je levnější (−) / dražší (+) než medián
    warning: str | None = None        # varování, když výsledek vypadá "moc dobře"
    # --- ARV a ekonomika rekonstrukce ---
    arv_count: int = 0              # kolik z comps je "hotových" (základ ARV)
    arv_ppm2: int | None = None     # medián Kč/m² hotových comps
    arv_value: int | None = None    # ARV pro TUHLE nemovitost (arv_ppm2 × plocha)
    reno_cost: int | None = None    # hrubý odhad nákladů na rekonstrukci
    margin: int | None = None       # ARV×sale_factor − cena − reno_cost
    margin_pct: float | None = None  # marže v % z celkové investice (cena + reno)

    @property
    def has_data(self) -> bool:
        return self.count >= MIN_COMPS and self.median_ppm2 is not None

    def as_dict(self) -> dict[str, Any]:
        """Pro uložení do scores.breakdown (JSON) — ať je vidět, z čeho se počítalo."""
        return {
            "scope": self.scope, "count": self.count,
            "median_price": self.median_price, "median_ppm2": self.median_ppm2,
            "p25_ppm2": self.p25_ppm2, "p75_ppm2": self.p75_ppm2,
            "arv_count": self.arv_count, "arv_ppm2": self.arv_ppm2, "arv_value": self.arv_value,
            "reno_cost": self.reno_cost, "margin": self.margin, "margin_pct": self.margin_pct,
        }


def _rows_to_stats(rows: list[sqlite3.Row]) -> tuple[int | None, int | None, int | None, int | None]:
    """(medián ceny, medián Kč/m², p25 Kč/m², p75 Kč/m²) z výběru comps."""
    if not rows:
        return None, None, None, None
    prices = [r["price"] for r in rows]
    ppm2 = [r["price"] / r["area_m2"] for r in rows]
    p25 = p75 = None
    if len(ppm2) >= 4:  # kvartily dávají smysl až u trochu většího vzorku
        quartiles = statistics.quantiles(ppm2, n=4)
        p25, p75 = round(quartiles[0]), round(quartiles[2])
    return round(statistics.median(prices)), round(statistics.median(ppm2)), p25, p75


def _select_comps(
    conn: sqlite3.Connection,
    listing: Listing,
    *,
    geo: str,                       # "radius" | "district" | "region"
    renovated_only: bool = False,
    radius_km: float = RADIUS_KM,
) -> list[sqlite3.Row]:
    """Srovnatelné: stejný typ, podobná plocha, daná geografická úroveň; sebe sama vyloučit."""
    if not listing.area_m2:
        return []
    clauses = [
        "price IS NOT NULL", "price > 0", "area_m2 IS NOT NULL", "area_m2 > 0",
        "property_type = ?", "area_m2 BETWEEN ? AND ?",
        "NOT (source = ? AND source_id = ?)",
    ]
    params: list[Any] = [
        listing.property_type,
        listing.area_m2 * (1 - AREA_BAND), listing.area_m2 * (1 + AREA_BAND),
        listing.source, listing.source_id,
    ]
    if geo == "radius":
        if not (listing.gps_lat and listing.gps_lon):
            return []
        # Bounding box v SQL (rychlé, využije index), přesný kruh se dořeže v Pythonu níž.
        dlat = radius_km / 111.0
        dlon = radius_km / (111.0 * max(0.2, math.cos(math.radians(listing.gps_lat))))
        clauses += ["gps_lat BETWEEN ? AND ?", "gps_lon BETWEEN ? AND ?"]
        params += [listing.gps_lat - dlat, listing.gps_lat + dlat,
                   listing.gps_lon - dlon, listing.gps_lon + dlon]
    elif geo == "district" and listing.district:
        clauses.append("district = ?")
        params.append(listing.district)
    elif geo == "region" and listing.region:
        clauses.append("region = ?")
        params.append(listing.region)
    else:
        return []
    if renovated_only:
        placeholders = ",".join("?" * len(RENOVATED_CONDITIONS))
        clauses.append(f"building_condition IN ({placeholders})")
        params.extend(RENOVATED_CONDITIONS)

    sql = (f"SELECT price, area_m2, land_area_m2, gps_lat, gps_lon "
           f"FROM listings WHERE {' AND '.join(clauses)}")
    rows = conn.execute(sql, params).fetchall()
    if geo == "radius":  # z bounding boxu na skutečný kruh
        rows = [r for r in rows
                if r["gps_lat"] and r["gps_lon"]
                and _haversine_km(listing.gps_lat, listing.gps_lon, r["gps_lat"], r["gps_lon"]) <= radius_km]
    return rows


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Vzdálenost dvou GPS bodů v km."""
    r_lat1, r_lat2 = math.radians(lat1), math.radians(lat2)
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(r_lat1) * math.cos(r_lat2) * math.sin(dlon / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def analyze(
    conn: sqlite3.Connection,
    listing: Listing,
    *,
    reno_cost_per_m2: int = DEFAULT_RENO_COST_M2,
    sale_factor: float = DEFAULT_SALE_FACTOR,
    needs_renovation: bool = False,
) -> Comps | None:
    """
    Srovnávací analýza pro jednu nemovitost. Vrací None, když není dost dat.

    `needs_renovation` (typicky building_condition == "Před rekonstrukcí"): jen tehdy
    má smysl počítat náklady na opravu a marži — u hotového bytu by to byl nesmysl.
    """
    if not (listing.area_m2 and listing.price):
        return None

    # Od nejbližšího k nejhrubšímu: okruh (skutečné "okolí") -> okres -> kraj.
    # U domů je kraj hodně hrubý (míchá drahé město s levným pohraničím) -> `coarse_scope`.
    candidates = [
        ("radius", f"do {RADIUS_KM:g} km"),
        ("district", f"okres {listing.district}"),
        ("region", listing.region or "kraj"),
    ]
    rows: list[sqlite3.Row] = []
    scope = ""
    geo_used = ""
    for geo, label in candidates:
        found = _select_comps(conn, listing, geo=geo)
        if len(found) >= MIN_COMPS:
            rows, scope, geo_used = found, label, geo
            break
    if not rows:
        return None

    median_price, median_ppm2, p25, p75 = _rows_to_stats(rows)
    lands = [r["land_area_m2"] for r in rows if r["land_area_m2"]]
    median_land = round(statistics.median(lands)) if lands and listing.property_type == "dum" else None

    subject_ppm2 = round(listing.price / listing.area_m2)
    comps = Comps(
        scope=scope, count=len(rows), median_price=median_price, median_ppm2=median_ppm2,
        p25_ppm2=p25, p75_ppm2=p75, median_land_m2=median_land,
        subject_ppm2=subject_ppm2,
        vs_median_pct=round((subject_ppm2 / median_ppm2 - 1) * 100, 1) if median_ppm2 else None,
    )

    # --- ARV: kolik stojí SROVNATELNÁ nemovitost, která už je v pořádku ---
    renovated: list[sqlite3.Row] = []
    for geo, _label in candidates:
        found = _select_comps(conn, listing, geo=geo, renovated_only=True)
        if len(found) >= MIN_COMPS:
            renovated = found
            break
    if renovated:
        comps.arv_count = len(renovated)
        comps.arv_ppm2 = round(statistics.median(r["price"] / r["area_m2"] for r in renovated))
        comps.arv_value = round(comps.arv_ppm2 * listing.area_m2)

        # --- ekonomika rekonstrukce (jen když ji nemovitost vůbec potřebuje) ---
        if needs_renovation:
            comps.reno_cost = round(reno_cost_per_m2 * listing.area_m2)
            expected_sale = comps.arv_value * sale_factor
            comps.margin = round(expected_sale - listing.price - comps.reno_cost)
            investment = listing.price + comps.reno_cost
            comps.margin_pct = round(comps.margin / investment * 100, 1) if investment > 0 else None

    # Varování jen tam, kde to nese informaci (ne u každého druhého inzerátu).
    warnings: list[str] = []
    if geo_used == "region":
        warnings.append("srovnání jen na úrovni kraje (v okolí málo podobných) — ber s rezervou")
    if median_ppm2 and subject_ppm2 < median_ppm2 * SUSPICIOUS_DISCOUNT:
        warnings.append("extrémně nízká cena/m² — ověř, jestli je nemovitost srovnatelná "
                        "(ruina, podíl, věcné břemeno, chyba v inzerátu)")
    if comps.margin_pct is not None and comps.margin_pct > SUSPICIOUS_MARGIN_PCT:
        warnings.append("marže je nerealisticky vysoká — nejspíš nesedí odhad nákladů nebo srovnání")
    comps.warning = "; ".join(warnings) or None

    return comps
