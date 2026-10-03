"""investment/scoring.py — volné skóre 0-100 pro investiční kandidáty.

Na rozdíl od hlavního scoring.py se tohle skóre NIKDY nepoužívá jako tvrdý filtr
(uživatel chce vidět i hraniční nabídky a sám je zahodit) — jen řadí, co poslat
na Telegram nad nízkým prahem (config.score_threshold, výchozí 40).

Váhy (ze zadání, pevné — ne konfigurovatelné):
  cena/m² vs průměr        40 %
  hrubý výnos               25 %
  renovation_flag           20 %
  pod price_target_max      15 %  (skutečný nákupní rozpočet -> bonus)

Cold-start: chybí-li průměr cena/m² (malý vzorek), signál se vynechá a váha se
přerozdělí mezi zbylé — stejná filozofie jako v hlavním scoring.py.
"""

from __future__ import annotations

from dataclasses import dataclass

WEIGHTS: dict[str, float] = {
    "cena_m2": 0.40,
    "vynos": 0.25,
    "renovace": 0.20,
    "pod_cilovou_cenou": 0.15,
}

CENA_FULL_DISCOUNT = 0.30   # 30 % pod průměrem = plné body za cenu
YIELD_FULL_PCT = 8.0        # 8% hrubý výnos = plné body za výnos


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


@dataclass
class InvestmentScoreResult:
    score: int | None
    signals: dict[str, float | None]
    weights_used: dict[str, float]


def score_candidate(
    *,
    price_per_m2: float | None,
    avg_price_per_m2: float | None,
    gross_yield_pct: float | None,
    renovation_flag: bool,
    price: int | None,
    price_target_max: int,
) -> InvestmentScoreResult:
    s_cena: float | None = None
    if price_per_m2 is not None and avg_price_per_m2:
        ratio = price_per_m2 / avg_price_per_m2
        s_cena = _clamp((1.0 - ratio) / CENA_FULL_DISCOUNT)

    s_vynos: float | None = None
    if gross_yield_pct is not None:
        s_vynos = _clamp(gross_yield_pct / YIELD_FULL_PCT)

    s_renovace = 1.0 if renovation_flag else 0.0

    s_pod_cilovou: float | None = None
    if price is not None:
        s_pod_cilovou = 1.0 if price <= price_target_max else 0.0

    signals: dict[str, float | None] = {
        "cena_m2": s_cena, "vynos": s_vynos, "renovace": s_renovace, "pod_cilovou_cenou": s_pod_cilovou,
    }
    present = {k: v for k, v in signals.items() if v is not None}
    total_weight = sum(WEIGHTS[k] for k in present)
    if total_weight <= 0:
        return InvestmentScoreResult(None, signals, {})

    weighted = sum(WEIGHTS[k] * v for k, v in present.items())
    score = round(weighted / total_weight * 100)
    weights_used = {k: round(WEIGHTS[k] / total_weight, 3) for k in present}
    return InvestmentScoreResult(score, signals, weights_used)


if __name__ == "__main__":
    import sys

    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    # A) příklad ze zadání: 30 862 Kč/m², 11 % pod průměrem, výnos 7.8 %, renovace, pod cílovou cenou
    avg = 30862 / 0.89
    res_a = score_candidate(
        price_per_m2=30862, avg_price_per_m2=avg, gross_yield_pct=7.8, renovation_flag=True,
        price=1_790_000, price_target_max=2_000_000,
    )
    print("A (ilustrativní, dobré skóre):", res_a)
    assert res_a.score is not None and res_a.score >= 60
    assert res_a.signals["pod_cilovou_cenou"] == 1.0

    # A2) stejné, ale NAD cílovou cenou -> nižší skóre
    res_a2 = score_candidate(
        price_per_m2=30862, avg_price_per_m2=avg, gross_yield_pct=7.8, renovation_flag=True,
        price=2_700_000, price_target_max=2_000_000,
    )
    print("A2 (stejné, nad cílovou cenou):", res_a2)
    assert res_a2.signals["pod_cilovou_cenou"] == 0.0
    assert res_a2.score < res_a.score

    # B) cold-start: bez průměru (malý vzorek) -> cena_m2=None, váha jde na zbylé signály
    res_b = score_candidate(
        price_per_m2=30862, avg_price_per_m2=None, gross_yield_pct=6.0, renovation_flag=False,
        price=1_950_000, price_target_max=2_000_000,
    )
    print("B (cold-start, bez cena_m2):", res_b)
    assert res_b.signals["cena_m2"] is None

    # C) nic nevíme kromě renovace (False) a ceny neznámé -> jen renovace jako signál,
    #    skóre proto nikdy není None, ale u prázdných dat spadne na 0 (loose, ne tvrdý filtr).
    res_c = score_candidate(
        price_per_m2=None, avg_price_per_m2=None, gross_yield_pct=None, renovation_flag=False,
        price=None, price_target_max=2_000_000,
    )
    print("C (žádná cenová/výnosová data):", res_c)
    assert res_c.score == 0

    print("\n✅ investment/scoring.py OK")
