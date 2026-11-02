"""flip/market.py — za kolik se v okolí nabízí podobné nemovitosti a kolik by stála po opravě.

Dvě čísla pro každý inzerát:
  * medián Kč/m² srovnatelných (stejný typ, podobná plocha) -> "je to levné?"
  * ARV (cena po rekonstrukci) = medián Kč/m² srovnatelných, které JSOU hotové,
    × plocha -> základ odhadu marže flipu.

Geografie (od nejbližšího): průměr TÉŽE OBCE -> sledované obce téhož okresu ->
celý kraj (Sreality ukládá celý kraj právě kvůli tomu). Další úroveň se použije,
jen když je v předchozí podobných méně než MIN_COMPS. Olomouc a vesnice mají
úplně jiné ceny, proto se obec srovnává hlavně sama se sebou.

Historie (od zavedení gone_at) to zpřesňuje:
  * VÁHY: inzerát, který zmizel do 60 dní (nejspíš prodaný), váží 1,5×; ten, který
    visí 120+ dní a nikdo ho nekoupil (předražený), jen 0,5×. Medián je vážený.
  * SLEVA PŘI PRODEJI: z inzerátů, které zmizely, se měří, o kolik průměrně klesla
    nabídková cena, než zmizely (okres -> kraj, aspoň MIN_SALES kusů). Bez dost dat
    platí pevný SALE_FACTOR z configu.
  * DOMY: když má dům známý pozemek, nejdřív se srovnává s domy s podobným pozemkem
    (0,5–2×), teprve pak se všemi podobně velkými.

⚠️ Jsou to NABÍDKOVÉ ceny, ne prodejní (ty portály nezveřejňují). Je to vodítko
"kam se podívat", ne ocenění.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone

from flip.model import Listing
from flip.text import condition_from_text
from flip.towns import okres_of

RENOVATED_CONDITIONS = {"Po rekonstrukci", "Novostavba", "Velmi dobrý"}
NEEDS_RENO_CONDITIONS = {"Před rekonstrukcí", "Špatný", "K demolici"}

AREA_BAND = {"byt": 0.30, "dum": 0.40}
LAND_BAND = (0.5, 2.0)            # dům s pozemkem 400 m² se srovnává s pozemky 200–800 m²
QUICK_SALE_DAYS = 60              # zmizel do 60 dní -> nejspíš prodaný za rozumnou cenu
STALE_DAYS = 120                  # visí 120+ dní -> nejspíš předražený
QUICK_WEIGHT, STALE_WEIGHT = 1.5, 0.5
MIN_SALES = 30                    # z kolika zmizelých inzerátů měřit slevu
FINAL_HAGGLE = 0.05               # i poslední nabídková cena se ještě usmlouvá (~5 %, odhad)
SALE_FACTOR_RANGE = (0.75, 1.0)
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
    weight: float = 1.0            # 1,5 = rychle zmizel (prodaný), 0,5 = dlouho visí (předražený)
    land_m2: float | None = None


def comp_weight(row: sqlite3.Row, now: datetime) -> float:
    """Jak moc věřit ceně: rychle zmizelý inzerát víc, dlouho neprodaný míň."""
    keys = row.keys()
    if "first_seen" not in keys or not row["first_seen"]:
        return 1.0
    try:
        first = datetime.fromisoformat(row["first_seen"])
        if row["gone_at"]:
            quick = (datetime.fromisoformat(row["gone_at"]) - first).days <= QUICK_SALE_DAYS
            return QUICK_WEIGHT if quick and not row["seen_at_start"] else 1.0
        return STALE_WEIGHT if (now - first).days >= STALE_DAYS else 1.0
    except (TypeError, ValueError):
        return 1.0


@dataclass(frozen=True, slots=True)
class SaleRatio:
    ratio: float                   # průměr poslední/první nabídková cena u zmizelých
    n: int
    scope: str                     # "okres" | "region"

    @property
    def sale_factor(self) -> float:
        lo, hi = SALE_FACTOR_RANGE
        return max(lo, min(hi, self.ratio * (1 - FINAL_HAGGLE)))

    @property
    def drop_pct(self) -> float:
        return (1 - self.ratio) * 100


class MarketData:
    """Srovnatelné pro jeden typ (byt/dům), načtené z DB JEDNOU za běh zdroje.
    Stav (hotové / k opravě) se z popisu čte tady jednou, ne pro každý inzerát znovu.
    `sales` = zmizelé inzeráty s první a poslední cenou (Store.sale_rows) pro měření slevy."""

    def __init__(self, rows: list[sqlite3.Row], *, now: datetime | None = None,
                 sales: list[sqlite3.Row] | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        self.comps: list[Comp] = []
        for r in rows:
            ppm2 = r["price"] / r["area_m2"]
            if not (SANE_PPM2[0] <= ppm2 <= SANE_PPM2[1]):
                continue
            self.comps.append(Comp(
                key=(r["source"], r["external_id"]), area_m2=r["area_m2"], ppm2=ppm2,
                renovated=row_condition(r["building_condition"], r["description"]) == "renovated",
                town=r["town"], okres=okres_of(r["town"]), weight=comp_weight(r, now),
                land_m2=r["land_m2"] if "land_m2" in r.keys() else None,
            ))
        self.sale_ratios: dict[str | None, SaleRatio] = {}   # okres -> …, None = celý kraj
        by_scope: dict[str | None, list[float]] = {}
        for r in sales or []:
            if not (r["first_price"] and r["price"]):
                continue
            ratio = min(r["price"] / r["first_price"], 1.0)    # zdražení se nepočítá jako "sleva"
            if ratio < 0.5:
                continue                                       # chyba v datech, ne sleva
            by_scope.setdefault(None, []).append(ratio)
            if okres := okres_of(r["town"]):
                by_scope.setdefault(okres, []).append(ratio)
        for scope, ratios in by_scope.items():
            if len(ratios) >= MIN_SALES:
                self.sale_ratios[scope] = SaleRatio(statistics.fmean(ratios), len(ratios),
                                                    "region" if scope is None else "okres")

    def sale_ratio(self, town: str | None) -> SaleRatio | None:
        return self.sale_ratios.get(okres_of(town)) or self.sale_ratios.get(None)


@dataclass
class MarketView:
    median_ppm2: float | None = None
    comps_n: int = 0
    scope: str | None = None          # "town" (stejná obec) | "okres" (sledované obce okresu) | "region" (kraj)
    arv_ppm2: float | None = None
    arv_n: int = 0
    arv_scope: str | None = None
    land_matched: bool = False        # dům: srovnáno s domy s podobným pozemkem
    sale: SaleRatio | None = None     # změřená sleva v okolí (None = pevný SALE_FACTOR)


def weighted_median(values: list[tuple[float, float]]) -> float:
    """[(hodnota, váha)] -> vážený medián (se stejnými vahami = obyčejný medián)."""
    items = sorted(values)
    if len({w for _, w in items}) <= 1:
        return statistics.median(v for v, _ in items)
    half = sum(w for _, w in items) / 2
    acc = 0.0
    for value, weight in items:
        acc += weight
        if acc >= half:
            return value
    return items[-1][0]


Pool = list[tuple[float, float]]      # [(Kč/m², váha)]


def _pick(values_by_scope: list[tuple[str, Pool, bool]], min_comps: int) -> tuple[float | None, int, str | None, bool]:
    for scope, values, land in values_by_scope:
        if len(values) >= min_comps:
            return weighted_median(values), len(values), scope, land
    return None, 0, None, False


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
    land = lst.land_m2 if lst.kind == "dum" else None

    def land_ok(c: Comp) -> bool:
        return bool(land and c.land_m2) and LAND_BAND[0] <= c.land_m2 / land <= LAND_BAND[1]

    def scopes(comps: list[Comp]) -> list[tuple[str, Pool, bool]]:
        out: list[tuple[str, Pool, bool]] = []
        for scope, members in (
            ("town", [c for c in comps if lst.town and c.town == lst.town]),
            ("okres", [c for c in comps if okres and c.okres == okres]),
            ("region", comps),
        ):
            if land:      # nejdřív domy s podobným pozemkem, pak všechny podobně velké
                out.append((scope, [(c.ppm2, c.weight) for c in members if land_ok(c)], True))
            out.append((scope, [(c.ppm2, c.weight) for c in members], False))
        return out

    view.median_ppm2, view.comps_n, view.scope, view.land_matched = _pick(scopes(similar), min_comps)
    view.arv_ppm2, view.arv_n, view.arv_scope, _ = _pick(scopes([c for c in similar if c.renovated]), min_comps)
    view.sale = market.sale_ratio(lst.town)
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
