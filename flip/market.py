"""flip/market.py — za kolik se v okolí nabízí podobné nemovitosti a kolik by stála po opravě.

Dvě čísla pro každý inzerát:
  * medián Kč/m² srovnatelných (stejný typ, podobná plocha) -> "je to levné?"
  * ARV (cena po rekonstrukci) = medián Kč/m² srovnatelných, které JSOU hotové,
    × plocha -> základ odhadu marže flipu.

Geografie (od nejbližšího): průměr TÉŽE OBCE -> sledované obce téhož okresu ->
celý kraj (Sreality ukládá celý kraj právě kvůli tomu). Další úroveň se použije,
jen když je v předchozí podobných méně než MIN_COMPS. Olomouc a vesnice mají
úplně jiné ceny, proto se obec srovnává hlavně sama se sebou.

⚠️ Jsou to NABÍDKOVÉ ceny, ne prodejní (ty portály nezveřejňují). U domů Kč/m²
ignoruje pozemek. Je to vodítko "kam se podívat", ne ocenění.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass

from flip.model import Listing
from flip.text import condition_from_text
from flip.towns import okres_of

RENOVATED_CONDITIONS = {"Po rekonstrukci", "Novostavba", "Velmi dobrý"}
NEEDS_RENO_CONDITIONS = {"Před rekonstrukcí", "Špatný", "K demolici"}

AREA_BAND = {"byt": 0.30, "dum": 0.40}
# Kč/m² mimo tohle pásmo = chyba v datech (špatně přečtená plocha, cena za měsíc…)
SANE_PPM2 = (3_000, 200_000)


def row_condition(building_condition: str | None, description: str | None) -> str | None:
    """"renovated" | "needs_reno" | None — strukturovaný stav má přednost před textem."""
    if building_condition in RENOVATED_CONDITIONS:
        return "renovated"
    if building_condition in NEEDS_RENO_CONDITIONS:
        return "needs_reno"
    if building_condition:
        return None  # "Dobrý", "Po částečné rekonstrukci"… = ani hotové, ani k opravě
    text = condition_from_text(description)
    if text == "renovated":
        return "renovated"
    if text in ("needs_reno", "original"):
        return "needs_reno"
    return None


@dataclass(frozen=True, slots=True)
class Comp:
    key: tuple[str, str]
    area_m2: float
    ppm2: float
    renovated: bool
    town: str | None
    okres: str | None              # jen u sledovaných obcí


class MarketData:
    """Srovnatelné pro jeden typ (byt/dům), načtené z DB JEDNOU za běh zdroje.
    Stav (hotové / k opravě) se z popisu čte tady jednou, ne pro každý inzerát znovu."""

    def __init__(self, rows: list[sqlite3.Row]) -> None:
        self.comps: list[Comp] = []
        for r in rows:
            ppm2 = r["price"] / r["area_m2"]
            if not (SANE_PPM2[0] <= ppm2 <= SANE_PPM2[1]):
                continue
            self.comps.append(Comp(
                key=(r["source"], r["external_id"]), area_m2=r["area_m2"], ppm2=ppm2,
                renovated=row_condition(r["building_condition"], r["description"]) == "renovated",
                town=r["town"], okres=okres_of(r["town"]),
            ))


@dataclass
class MarketView:
    median_ppm2: float | None = None
    comps_n: int = 0
    scope: str | None = None          # "town" (stejná obec) | "okres" (sledované obce okresu) | "region" (kraj)
    arv_ppm2: float | None = None
    arv_n: int = 0
    arv_scope: str | None = None


def _pick(values_by_scope: list[tuple[str, list[float]]], min_comps: int) -> tuple[float | None, int, str | None]:
    for scope, values in values_by_scope:
        if len(values) >= min_comps:
            return statistics.median(values), len(values), scope
    return None, 0, None


def analyze(
    lst: Listing,
    market: MarketData,
    *,
    min_comps: int,
    cluster: set[tuple[str, str]] | None = None,
) -> MarketView:
    """Medián a ARV pro jeden inzerát. `cluster` = klíče téže nemovitosti (sebe nesrovnávat se sebou)."""
    view = MarketView()
    if not lst.area_m2:
        return view
    cluster = cluster or {lst.key}
    band = AREA_BAND.get(lst.kind, 0.30)
    lo, hi = lst.area_m2 * (1 - band), lst.area_m2 * (1 + band)
    similar = [c for c in market.comps if c.key not in cluster and lo <= c.area_m2 <= hi]

    okres = okres_of(lst.town)

    def scopes(comps: list[Comp]) -> list[tuple[str, list[float]]]:
        return [
            ("town", [c.ppm2 for c in comps if lst.town and c.town == lst.town]),
            ("okres", [c.ppm2 for c in comps if okres and c.okres == okres]),
            ("region", [c.ppm2 for c in comps]),
        ]

    view.median_ppm2, view.comps_n, view.scope = _pick(scopes(similar), min_comps)
    view.arv_ppm2, view.arv_n, view.arv_scope = _pick(scopes([c for c in similar if c.renovated]), min_comps)
    return view


def scope_label(scope: str | None, town: str | None) -> str:
    """Pro zprávu: kde se srovnávalo."""
    if scope == "town" and town:
        return town
    if scope == "okres" and okres_of(town):
        return f"okres {okres_of(town)}"
    return "Olomoucký kraj"


@dataclass
class TownAverage:
    town: str
    kind: str
    n: int
    median_ppm2: float
    median_price: float
    renovated_ppm2: float | None      # hotové byty/domy (jen když je jich aspoň pár)
    needs_reno_ppm2: float | None     # k rekonstrukci


def town_averages(rows: list[sqlite3.Row], *, min_n: int = 3) -> list[TownAverage]:
    """Typická cena za m² v každé sledované obci (medián), zvlášť byty a domy.
    Rozdíl "hotové" vs "k rekonstrukci" je hrubý odhad prostoru pro flip."""
    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for r in rows:
        if r["town"] and r["price"] and r["area_m2"]:
            ppm2 = r["price"] / r["area_m2"]
            if SANE_PPM2[0] <= ppm2 <= SANE_PPM2[1]:
                groups.setdefault((r["town"], r["kind"]), []).append(r)
    out: list[TownAverage] = []
    for (town, kind), items in groups.items():
        if len(items) < min_n:
            continue
        cond = [row_condition(r["building_condition"], r["description"]) for r in items]
        ppm2 = [r["price"] / r["area_m2"] for r in items]
        reno = [p for p, c in zip(ppm2, cond) if c == "renovated"]
        raw = [p for p, c in zip(ppm2, cond) if c == "needs_reno"]
        out.append(TownAverage(
            town=town, kind=kind, n=len(items), median_ppm2=statistics.median(ppm2),
            median_price=statistics.median(r["price"] for r in items),
            renovated_ppm2=statistics.median(reno) if len(reno) >= min_n else None,
            needs_reno_ppm2=statistics.median(raw) if len(raw) >= min_n else None,
        ))
    out.sort(key=lambda a: (a.kind, -a.n))
    return out
