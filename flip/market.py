"""flip/market.py — za kolik se v okolí nabízí podobné nemovitosti a kolik by stála po opravě.

Dvě čísla pro každý inzerát:
  * medián Kč/m² srovnatelných (stejný typ, podobná plocha) -> "je to levné?"
  * ARV (cena po rekonstrukci) = medián Kč/m² srovnatelných, které JSOU hotové,
    × plocha -> základ odhadu marže flipu.

Geografie: nejdřív jen 6 sledovaných obcí; když je tam podobných méně než
MIN_COMPS, rozšíří se na celý okres Šumperk (Sreality ukládá celý okres právě
kvůli tomu). Šest obcí je malý trh — bez fallbacku by medián většinou chyběl.

⚠️ Jsou to NABÍDKOVÉ ceny, ne prodejní (ty portály nezveřejňují). U domů Kč/m²
ignoruje pozemek. Je to vodítko "kam se podívat", ne ocenění.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass

from flip.model import Listing
from flip.text import condition_from_text

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
    in_area: bool


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
                in_area=r["town"] is not None,
            ))


@dataclass
class MarketView:
    median_ppm2: float | None = None
    comps_n: int = 0
    scope: str | None = None          # "area" (6 obcí) | "district" (okres)
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

    view.median_ppm2, view.comps_n, view.scope = _pick([
        ("area", [c.ppm2 for c in similar if c.in_area]),
        ("district", [c.ppm2 for c in similar]),
    ], min_comps)
    view.arv_ppm2, view.arv_n, view.arv_scope = _pick([
        ("area", [c.ppm2 for c in similar if c.in_area and c.renovated]),
        ("district", [c.ppm2 for c in similar if c.renovated]),
    ], min_comps)
    return view
