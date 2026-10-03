"""
scoring.py — skóre 0–100 pro inzerát. Priorita projektu = byty PŘED rekonstrukcí.

Signály a váhy (výchozí, ne svaté — proto se skóre loguje u všech inzerátů a po
pár týdnech se doladí):

  stav     30 %  strukturovaný building_condition + keywordy popisu + štítek F/G
  cena_m2  25 %  cena/m² vs MEDIÁN (okres+dispozice; fallback kraj při malém vzorku)
  urgent   20 %  regex/keyword scan (dědictví, spěšný prodej)
  pokles   15 %  z price_history – % a rychlost poklesu
  doba     10 %  jak dlouho visí (z DB seen_first) + jestli i zlevnil

COLD-START (zásadní — nikdy nehádej):
  Když pro signál chybí data, vrátí se None a jeho váha se PŘERODĚLÍ mezi přítomné
  signály (renormalizace), ne náhodné číslo. Konkrétně:
    * vzorek pro medián < ~8 a ani kraj nestačí  -> cena_m2 = None
    * < 2 ceny v historii (nový inzerát)          -> pokles  = None
    * bez popisu/detailu                          -> stav/urgent = None

STAV: building_condition (strukturované pole z API) má PŘEDNOST před keywordy —
"Po rekonstrukci"/novostavba přebíjí vše (stav=0). Pozor: štítek a stav si můžou
odporovat (reálně viděn byt "Po rekonstrukci" se štítkem G), proto je štítek jen
sekundární bonus a NEpřičítá se k rekonstruovaným bytům.

Formulace v reasons jsou VŽDY jako pravděpodobnost ("možný", "pravděpodobně").
"""

from __future__ import annotations

import statistics
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sources.base import Listing

# --------------------------------------------------------------------------- #
# Konstanty                                                                    #
# --------------------------------------------------------------------------- #

WEIGHTS: dict[str, float] = {
    "stav": 0.30,
    "cena_m2": 0.25,
    "urgent": 0.20,
    "pokles": 0.15,
    "doba": 0.10,
}

MIN_SAMPLE = 8           # minimální vzorek pro spolehlivý medián cena/m²
CENA_FULL_DISCOUNT = 0.40  # ~40 % pod mediánem = plné body za cenu
POKLES_FULL = 0.15       # 15% pokles ceny = plné body
DOBA_FULL_DAYS = 90      # 90+ dní na trhu = plné body za dobu inzerce


def _normalize(text: str) -> str:
    """lowercase + odstranění diakritiky (pro robustní keyword matching)."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


# Keyword skupiny ze zadání, předem normalizované (bez diakritiky, lowercase).
def _norm_all(items: list[str]) -> list[str]:
    return [_normalize(x) for x in items]


REKO_NUTNA = _norm_all([
    "k rekonstrukci", "nutná rekonstrukce", "před rekonstrukcí",
    "vyžaduje rekonstrukci", "určeno k rekonstrukci", "k celkové rekonstrukci",
    "možnost rekonstrukce", "holobyt", "původní bytové jádro", "umakartové jádro",
])
STAV_PUVODNI = _norm_all([
    "původní stav", "původní zachovalý", "k modernizaci",
    "vhodný k úpravám", "stav před modernizací",
])
REKO_HOTOVO = _norm_all([
    "po rekonstrukci", "kompletní rekonstrukce", "nově zrekonstruovaný",
    "novostavba", "po kompletní", "zrekonstruováno", "nová kuchyňská linka",
    "luxusní", "designový",
])
DEDICTVI = _norm_all(["dědictví", "po zesnul", "uvolněn", "nutný prodej", "z pozůstalosti"])
SPECH = _norm_all([
    "rychlé jednání", "při rychlém", "nutno prodat",
    "stěhování do zahraničí", "sleva při rychl",
])

# Strukturovaný stav z API -> body za stav (1.0 = ideální příležitost).
COND_SCORE: dict[str, float] = {
    "Před rekonstrukcí": 1.00,
    "Špatný": 0.90,
    "K demolici": 0.80,
    "V rekonstrukci": 0.45,
    "Dobrý": 0.30,
    "Po částečné rekonstrukci": 0.30,  # část práce hotová -> menší příležitost (zdroj: Bezrealitky)
    "Velmi dobrý": 0.15,
    "Po rekonstrukci": 0.00,
    "Novostavba": 0.00,
    "Projekt": 0.00,
    "Ve výstavbě": 0.00,
}
# Stavy, které "přebijí vše" (hotovo/nové) -> stav = 0, bez štítkového bonusu.
RENOVATED_CONDITIONS = {"Po rekonstrukci", "Novostavba", "Projekt", "Ve výstavbě"}


# --------------------------------------------------------------------------- #
# Pomocné                                                                      #
# --------------------------------------------------------------------------- #

def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _any_kw(keywords: list[str], text_norm: str) -> bool:
    return any(kw in text_norm for kw in keywords)


def _days_between(iso_a: str, iso_b: str) -> int | None:
    try:
        return abs((datetime.fromisoformat(iso_b) - datetime.fromisoformat(iso_a)).days)
    except (ValueError, TypeError):
        return None


def resolve_median_ppm2(
    district_samples: list[float],
    region_samples: list[float],
    min_sample: int = MIN_SAMPLE,
) -> tuple[float | None, str | None]:
    """Medián cena/m²: nejdřív okres+disp, fallback kraj+disp; jinak None (nehádáme)."""
    if len(district_samples) >= min_sample:
        return statistics.median(district_samples), "okresu"
    if len(region_samples) >= min_sample:
        return statistics.median(region_samples), "kraje"
    return None, None


# --------------------------------------------------------------------------- #
# Jednotlivé signály -> (hodnota 0..1 nebo None, lidský důvod nebo None)       #
# --------------------------------------------------------------------------- #

def _signal_stav(listing: Listing) -> tuple[float | None, str | None]:
    bc = listing.building_condition
    energy = (listing.energy_rating or "").upper()
    text = _normalize(listing.description or "")

    # 1) "po rekonstrukci"/novostavba přebíjí vše
    if bc in RENOVATED_CONDITIONS or _any_kw(REKO_HOTOVO, text):
        return 0.0, None

    base: float | None = None
    parts: list[str] = []
    if bc in COND_SCORE:
        base = COND_SCORE[bc]
        parts.append(f"stav: {bc.lower()}")

    # 2) keywordy mohou body zvednout (prodejce sám zmiňuje rekonstrukci)
    if _any_kw(REKO_NUTNA, text):
        base = max(base or 0.0, 1.0)
        parts.append("text: k rekonstrukci")
    elif _any_kw(STAV_PUVODNI, text):
        base = max(base or 0.0, 0.5)
        parts.append("text: původní stav")

    if base is None:
        return None, None  # žádná informace o stavu (např. bez detailu)

    # 3) štítek F/G = tvrdý sekundární signál nerekonstruovaného (jen když není hotovo)
    if energy in ("F", "G") and base > 0:
        base = _clamp(base + 0.15)
        parts.append(f"štítek {energy}")

    reason = f"Pravděpodobně před rekonstrukcí ({', '.join(parts)})" if base >= 0.6 else None
    return base, reason


def _signal_cena(listing: Listing, median_ppm2: float | None, scope: str | None) -> tuple[float | None, str | None]:
    if median_ppm2 is None or not listing.price_per_m2:
        return None, None
    ratio = listing.price_per_m2 / median_ppm2
    score = _clamp((1.0 - ratio) / CENA_FULL_DISCOUNT)
    if score <= 0:
        return 0.0, None
    pct = round((1.0 - ratio) * 100)
    return score, f"Cena/m² ~{pct} % pod mediánem {scope}"


def _signal_urgent(listing: Listing) -> tuple[float | None, str | None]:
    if listing.description is None:
        return None, None  # bez popisu nelze skenovat
    text = _normalize(listing.description)
    has_ded = _any_kw(DEDICTVI, text)
    has_spe = _any_kw(SPECH, text)
    groups = int(has_ded) + int(has_spe)
    score = {0: 0.0, 1: 0.7, 2: 1.0}[groups]
    if groups == 0:
        return 0.0, None
    labels = []
    if has_ded:
        labels.append("dědictví/pozůstalost")
    if has_spe:
        labels.append("spěšný prodej")
    return score, f"Možný motivovaný prodej ({', '.join(labels)} – formulace v textu)"


def _signal_pokles(price_history: list[tuple[int, str]]) -> tuple[float | None, str | None]:
    prices = [(p, ts) for p, ts in price_history if p]
    if len(prices) < 2:
        return None, None  # nový inzerát / bez historie
    peak = max(p for p, _ in prices)
    last = prices[-1][0]
    if peak <= 0:
        return None, None
    pct = (peak - last) / peak
    if pct <= 0:
        return 0.0, None
    score = _clamp(pct / POKLES_FULL)
    days = _days_between(prices[0][1], prices[-1][1])
    suffix = f" za {days} dní" if days else ""
    return score, f"Cena dolů o ~{round(pct * 100)} %{suffix}"


def _signal_doba(days_on_market: int | None, price_dropped: bool) -> tuple[float | None, str | None]:
    if days_on_market is None:
        return None, None
    base = _clamp(days_on_market / DOBA_FULL_DAYS)
    score = base if price_dropped else base * 0.6  # dlouho visí + sleva = motivace
    reason = f"Visí {days_on_market} dní" if days_on_market >= 30 else None
    return score, reason


# --------------------------------------------------------------------------- #
# Agregace                                                                     #
# --------------------------------------------------------------------------- #

@dataclass
class ScoreResult:
    score: int | None
    signals: dict[str, float | None]
    reasons: dict[str, str]          # signal -> lidský důvod (pro Telegram)
    median_scope: str | None
    weights_used: dict[str, float] = field(default_factory=dict)

    def breakdown(self) -> dict[str, Any]:
        """Pro uložení do scores.breakdown (JSON)."""
        return {
            "signals": self.signals,
            "reasons": self.reasons,
            "median_scope": self.median_scope,
            "weights_used": self.weights_used,
        }


def score_listing(
    listing: Listing,
    *,
    district_samples: list[float] | None = None,
    region_samples: list[float] | None = None,
    price_history: list[tuple[int, str]] | None = None,
    days_on_market: int | None = None,
) -> ScoreResult:
    """Spočítá skóre 0–100 s přerozdělením vah u chybějících signálů."""
    median_ppm2, scope = resolve_median_ppm2(district_samples or [], region_samples or [])

    s_stav, r_stav = _signal_stav(listing)
    s_cena, r_cena = _signal_cena(listing, median_ppm2, scope)
    s_urg, r_urg = _signal_urgent(listing)
    s_pok, r_pok = _signal_pokles(price_history or [])
    s_doba, r_doba = _signal_doba(days_on_market, price_dropped=bool(s_pok and s_pok > 0))

    signals: dict[str, float | None] = {
        "stav": s_stav, "cena_m2": s_cena, "urgent": s_urg, "pokles": s_pok, "doba": s_doba,
    }
    raw_reasons = {"stav": r_stav, "cena_m2": r_cena, "urgent": r_urg, "pokles": r_pok, "doba": r_doba}
    reasons = {k: v for k, v in raw_reasons.items() if v}

    present = {k: v for k, v in signals.items() if v is not None}
    total_weight = sum(WEIGHTS[k] for k in present)
    if total_weight <= 0:
        return ScoreResult(None, signals, reasons, scope, {})

    weighted = sum(WEIGHTS[k] * v for k, v in present.items())
    score = round(weighted / total_weight * 100)
    weights_used = {k: round(WEIGHTS[k] / total_weight, 3) for k in present}  # renormalizované
    return ScoreResult(score, signals, reasons, scope, weights_used)


if __name__ == "__main__":
    import sys
    from datetime import date

    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    def show(title: str, res: ScoreResult) -> None:
        print(f"\n=== {title} ===")
        print("skóre:", res.score, "| medián:", res.median_scope)
        print("signály:", {k: (round(v, 2) if v is not None else None) for k, v in res.signals.items()})
        print("váhy_použité:", res.weights_used)
        for sig, txt in res.reasons.items():
            print(f"   [{sig}] {txt}")

    # A) plná data – byt před rekonstrukcí, štítek G, dědictví, levný, po poklesu, dlouho visí
    a = Listing(
        source="sreality", source_id="A", url="x", price=1_890_000, area_m2=54.0,
        disposition="2+kk", building_condition="Před rekonstrukcí", energy_rating="G",
        description="Byt v původním stavu, z dědictví, nutný rychlý prodej.",
        district="Šumperk", region="Olomoucký kraj",
    )
    med_district = [55000.0] * 10  # medián okresu 55k/m²; byt má 35k/m² => ~36 % pod
    ph = [(2_050_000, "2026-05-20T08:00:00+00:00"), (1_890_000, "2026-06-03T08:00:00+00:00")]
    show("A: plná data (čekám vysoké skóre)", score_listing(
        a, district_samples=med_district, region_samples=med_district, price_history=ph, days_on_market=41))

    # B) cold-start – nový inzerát bez detailu, malý vzorek okresu => jen doba present
    b = Listing(source="sreality", source_id="B", url="x", price=3_000_000, area_m2=60.0,
                disposition="2+kk", district="Jeseník", region="Olomoucký kraj")
    show("B: cold-start (stav/urgent/cena/pokles=None -> váha jde na doba)", score_listing(
        b, district_samples=[50000.0] * 3, region_samples=[50000.0] * 4,
        price_history=[(3_000_000, "2026-06-15T08:00:00+00:00")], days_on_market=0))

    # C) po rekonstrukci se štítkem G => stav MUSÍ být 0 (štítek nepřebíjí hotovo)
    c = Listing(source="sreality", source_id="C", url="x", price=4_990_000, area_m2=63.0,
                disposition="3+1", building_condition="Po rekonstrukci", energy_rating="G",
                description="Krásný byt po kompletní rekonstrukci.",
                district="Olomouc", region="Olomoucký kraj")
    res_c = score_listing(c, district_samples=[70000.0] * 10, region_samples=[70000.0] * 10,
                          price_history=[(4_990_000, "2026-06-15T08:00:00+00:00")], days_on_market=5)
    show("C: po rekonstrukci + štítek G (čekám stav=0)", res_c)
    assert res_c.signals["stav"] == 0.0, "stav po rekonstrukci musí být 0"

    print("\n✅ scoring.py OK — plná data, cold-start i override 'po rekonstrukci' fungují.")
