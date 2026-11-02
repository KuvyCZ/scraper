"""flip/scoring.py — skóre 0–100: jak dobře nemovitost vypadá jako flip (koupit, opravit, prodat).

Signály a váhy:
  stav      30 %  potřebuje rekonstrukci? (strukturovaný stav > text popisu; štítek F/G bonus)
  marže     30 %  odhad zisku: ARV × sale_factor − cena − rekonstrukce − vedlejší náklady
  cena      25 %  Kč/m² vs medián srovnatelných v okolí
  motivace  15 %  zlevnění, doba na trhu, "dědictví / rychlé jednání" v popisu

Chybí-li pro signál data, je None a jeho váha se rozdělí mezi ostatní — nikdy
se nehádá. Skóre se ale stáhne podle toho, kolik vah chybí (MIN_COVERAGE_FACTOR):
jeden jediný signál (např. jen "levné za m²") už nedá 100 a 🔥. Vážná rizika (jen podíl, dražba, půdní prostor) skóre sráží na
polovinu a nemovitost nikdy nedostane 🔥 — ve zprávě ale vidět je.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from flip.config import FlipConfig
from flip.market import NEEDS_RENO_CONDITIONS, RENOVATED_CONDITIONS, MarketView
from flip.model import Listing, price_steps
from flip.text import condition_from_text, is_motivated, risks

WEIGHTS: dict[str, float] = {"stav": 0.30, "marze": 0.30, "cena": 0.25, "motivace": 0.15}

COND_SCORE: dict[str, float] = {
    "Před rekonstrukcí": 1.00,
    "Špatný": 0.95,
    "K demolici": 0.70,          # často jen pozemek s ruinou — příležitost, ale riskantní
    "V rekonstrukci": 0.50,
    "Po částečné rekonstrukci": 0.40,
    "Dobrý": 0.30,
    "Velmi dobrý": 0.10,
    "Po rekonstrukci": 0.0,
    "Novostavba": 0.0,
    "Projekt": 0.0,
    "Ve výstavbě": 0.0,
}
TEXT_COND_SCORE = {"needs_reno": 0.90, "original": 0.65, "renovated": 0.0}
NEEDS_WORK_MIN = 0.60            # od tohoto stavu se počítá ekonomika rekonstrukce

CENA_FULL_DISCOUNT = 0.40        # 40 % pod mediánem = plné body
MARGIN_FULL_PCT = 25.0           # 25% marže = plné body
DROP_FULL_PCT = 10.0             # zlevnění o 10 % = plné body za motivaci
DOM_FULL_DAYS = 120              # 4 měsíce na trhu = plné body za motivaci
SERIOUS_RISKS = {"share", "auction", "attic"}
MIN_COVERAGE_FACTOR = 0.5        # se všemi signály ×1.0, s jediným malým signálem ~×0.6


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


@dataclass
class FlipEstimate:
    arv: int                     # odhad prodejní ceny po rekonstrukci (nabídková)
    reno_cost: int
    holding_cost: int
    margin: int                  # Kč
    margin_pct: float            # % z celkové investice (cena + reko + vedlejší)


@dataclass
class ScoreResult:
    score: int | None
    signals: dict[str, float | None]
    condition_label: str | None              # co o stavu víme, lidsky
    needs_work: bool
    market: MarketView
    estimate: FlipEstimate | None
    price_drop_pct: float | None              # od první známé ceny
    days_on_market: int | None
    motivated_text: bool
    risks: list[str] = field(default_factory=list)
    hot: bool = False
    ppm2: float | None = None
    price_steps: list[tuple[int, str]] = field(default_factory=list)   # skutečné změny ceny [(cena, kdy)]
    listed_before_start: bool = False      # byl online už při spuštění sledování -> dny jsou "aspoň"

    @property
    def vs_median_pct(self) -> float | None:
        """+ dražší / − levnější než medián (pro zprávu)."""
        if self.market.median_ppm2 is None or self.ppm2 is None:
            return None
        return (self.ppm2 / self.market.median_ppm2 - 1) * 100

    def breakdown(self) -> dict[str, Any]:
        return {
            "score": self.score, "signals": self.signals, "condition": self.condition_label,
            "market": asdict(self.market), "estimate": asdict(self.estimate) if self.estimate else None,
            "price_drop_pct": self.price_drop_pct, "days_on_market": self.days_on_market,
            "risks": self.risks, "hot": self.hot, "price_steps": self.price_steps,
            "listed_before_start": self.listed_before_start,
        }


def _condition(lst: Listing) -> tuple[float | None, str | None]:
    bc = lst.building_condition
    text_cond = condition_from_text(lst.description)
    value: float | None = None
    label: str | None = None
    if bc in COND_SCORE:
        value, label = COND_SCORE[bc], bc
        # prodejce v textu zmiňuje rekonstrukci, i když stav vyplnil jako "Dobrý"
        if text_cond == "needs_reno" and bc not in RENOVATED_CONDITIONS:
            value, label = max(value, 0.75), f"{bc} (text mentions renovation)"
    elif text_cond is not None:
        value = TEXT_COND_SCORE[text_cond]
        label = {"needs_reno": "probably needs renovation (from text)",
                 "original": "probably original condition (from text)",
                 "renovated": "probably renovated (from text)"}[text_cond]
    if value is not None and value > 0 and (lst.energy_rating or "").upper() in ("F", "G"):
        value = _clamp(value + 0.10)
        label = f"{label}, energy {lst.energy_rating.upper()}"
    return value, label


def _estimate(lst: Listing, market: MarketView, cfg: FlipConfig) -> FlipEstimate | None:
    if not (market.arv_ppm2 and lst.area_m2 and lst.price):
        return None
    arv = market.arv_ppm2 * lst.area_m2
    reno = cfg.reno_cost_m2(lst.kind) * lst.area_m2
    holding = cfg.holding_cost_pct * (lst.price + reno)
    margin = arv * cfg.sale_factor - lst.price - reno - holding
    invested = lst.price + reno + holding
    return FlipEstimate(
        arv=round(arv), reno_cost=round(reno), holding_cost=round(holding),
        margin=round(margin), margin_pct=round(margin / invested * 100, 1),
    )


def _days_between(a_iso: str, b: datetime) -> int | None:
    try:
        return (b - datetime.fromisoformat(a_iso)).days
    except (TypeError, ValueError):
        return None


def score_listing(
    lst: Listing,
    market: MarketView,
    cfg: FlipConfig,
    *,
    price_history: list[tuple[int | None, str]] | None = None,
    first_seen: str | None = None,
    now: datetime | None = None,
    listed_before_start: bool = False,
) -> ScoreResult:
    s_stav, cond_label = _condition(lst)
    needs_work = (s_stav or 0) >= NEEDS_WORK_MIN or lst.building_condition in NEEDS_RENO_CONDITIONS

    # cena vs medián
    s_cena: float | None = None
    ppm2 = lst.price_per_m2
    if ppm2 is not None and market.median_ppm2:
        s_cena = _clamp((1 - ppm2 / market.median_ppm2) / CENA_FULL_DISCOUNT)

    # marže (jen když nemovitost opravu opravdu potřebuje — u hotové by to byl nesmysl)
    estimate = _estimate(lst, market, cfg) if needs_work else None
    s_marze = _clamp(estimate.margin_pct / MARGIN_FULL_PCT) if estimate else None

    # motivace prodejce
    drop_pct: float | None = None
    prices = [p for p, _ in (price_history or []) if p]
    if len(prices) >= 2 and prices[0] > 0 and lst.price:
        drop_pct = round((prices[0] - lst.price) / prices[0] * 100, 1)
    dom = _days_between(first_seen, now) if (first_seen and now) else None
    motivated = is_motivated(lst.description)
    parts: list[float] = []
    if drop_pct is not None:
        parts.append(_clamp(drop_pct / DROP_FULL_PCT))
    if dom is not None and dom > 0:
        parts.append(_clamp(dom / DOM_FULL_DAYS))
    if motivated:
        parts.append(0.6)
    s_motivace = max(parts) if parts else None

    signals = {"stav": s_stav, "marze": s_marze, "cena": s_cena, "motivace": s_motivace}
    present = {k: v for k, v in signals.items() if v is not None}
    score: int | None = None
    if present:
        total = sum(WEIGHTS[k] for k in present)
        coverage = MIN_COVERAGE_FACTOR + (1 - MIN_COVERAGE_FACTOR) * total
        score = round(sum(WEIGHTS[k] * v for k, v in present.items()) / total * coverage * 100)

    found_risks = risks(lst.description)
    serious = bool(SERIOUS_RISKS & set(found_risks))
    if score is not None and serious:
        score = round(score * 0.5)

    hot = False
    if not serious and score is not None:
        hot = score >= cfg.hot_score or (estimate is not None and estimate.margin_pct >= cfg.hot_margin_pct)
    # bez ceny a srovnání je "skóre" jen o stavu — 🔥 jen s aspoň jedním cenovým signálem
    if s_cena is None and s_marze is None:
        hot = False

    return ScoreResult(
        score=score, signals=signals, condition_label=cond_label, needs_work=needs_work, market=market,
        estimate=estimate, price_drop_pct=drop_pct, days_on_market=dom, motivated_text=motivated,
        risks=found_risks, hot=hot, ppm2=ppm2, price_steps=price_steps(price_history or []),
        listed_before_start=listed_before_start,
    )
