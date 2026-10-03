"""investment/candidate.py — InvestmentCandidate: Listing + dopočtené investiční veličiny.

`build_candidate()` předpokládá, že tvrdé strukturální filtry (lokalita, velikost,
dispozice, cena) už proběhly dřív (uvnitř zdrojových modulů / investment_main.py) —
tady se jen počítá výnos a skóre pro kandidáta, který už filtry prošel.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from investment.config import InvestmentConfig
from investment.scoring import score_candidate
from investment.sources.base import Listing
from investment.yield_estimator import (
    estimated_monthly_costs,
    estimated_mortgage,
    estimated_rent,
    gross_yield_pct,
    net_monthly_cashflow,
)
from scoring import _normalize  # sdílená normalizace textu (bez diakritiky, lowercase)


@dataclass(slots=True)
class InvestmentCandidate:
    source: str
    external_id: str
    url: str
    layout: str | None
    size_m2: float | None
    location: str | None

    price: int | None
    price_per_m2: float | None
    avg_price_per_m2: float | None
    discount_pct: float | None          # % pod průměrem (kladné = levnější než průměr)

    estimated_rent: int | None
    gross_yield_pct: float | None
    estimated_mortgage: int | None
    ltv: float
    interest_rate_pct: float
    mortgage_years: int
    net_monthly_cashflow: int | None

    renovation_flag: bool
    score: int | None

    parse_confidence: str = "high"
    duplicate_sources: list[tuple[str, str]] = field(default_factory=list)


def detect_renovation_flag(description: str | None, keywords: list[str]) -> bool:
    """True = popis zmiňuje rekonstrukci/původní stav = investiční příležitost, ne diskvalifikace."""
    if not description:
        return False
    text = _normalize(description)
    return any(_normalize(kw) in text for kw in keywords)


def build_candidate(
    listing: Listing,
    *,
    config: InvestmentConfig,
    avg_price_per_m2: float | None,
    renovation_flag: bool,
) -> InvestmentCandidate:
    price_per_m2 = listing.price_per_m2

    rent = estimated_rent(listing.size_m2, config.rent_per_m2) if listing.size_m2 else None
    yield_pct = gross_yield_pct(rent, listing.price) if rent is not None and listing.price else None
    mortgage = (
        estimated_mortgage(listing.price, config.ltv, config.interest_rate_pct, config.mortgage_years)
        if listing.price else None
    )
    cashflow = None
    if rent is not None and mortgage is not None:
        costs = estimated_monthly_costs(
            config.fond_oprav, config.insurance, config.tax, config.maintenance_reserve
        )
        cashflow = net_monthly_cashflow(rent, costs, mortgage)

    discount_pct = None
    if price_per_m2 is not None and avg_price_per_m2:
        discount_pct = (avg_price_per_m2 - price_per_m2) / avg_price_per_m2 * 100

    score_res = score_candidate(
        price_per_m2=price_per_m2,
        avg_price_per_m2=avg_price_per_m2,
        gross_yield_pct=yield_pct,
        renovation_flag=renovation_flag,
        price=listing.price,
        price_target_max=config.price_target_max,
    )

    return InvestmentCandidate(
        source=listing.source, external_id=listing.external_id, url=listing.url,
        layout=listing.layout, size_m2=listing.size_m2, location=listing.location,
        price=listing.price, price_per_m2=price_per_m2, avg_price_per_m2=avg_price_per_m2,
        discount_pct=discount_pct,
        estimated_rent=rent, gross_yield_pct=yield_pct, estimated_mortgage=mortgage,
        ltv=config.ltv, interest_rate_pct=config.interest_rate_pct, mortgage_years=config.mortgage_years,
        net_monthly_cashflow=cashflow,
        renovation_flag=renovation_flag, score=score_res.score,
        parse_confidence=listing.parse_confidence, duplicate_sources=list(listing.duplicate_sources),
    )


if __name__ == "__main__":
    import sys

    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    listing = Listing(
        source="sreality", external_id="42",
        url="https://www.sreality.cz/detail/prodej/byt/2+1/sumperk-sumperk/42",
        price=1_790_000, size_m2=58.0, layout="2+1", location="Šumperk",
        building_condition="Před rekonstrukcí",
        description="Byt 2+1 v původním stavu, vhodný k rekonstrukci.",
        duplicate_sources=[("bazos", "https://reality.bazos.cz/inzerat/123/x.php")],
    )
    cfg = InvestmentConfig()

    reno = detect_renovation_flag(listing.description, cfg.condition_keywords)
    print("renovation_flag:", reno)
    assert reno is True

    candidate = build_candidate(listing, config=cfg, avg_price_per_m2=34700.0, renovation_flag=reno)
    print(candidate)
    assert candidate.score is not None and candidate.score >= 40  # projde default prahem
    assert candidate.duplicate_sources == [("bazos", "https://reality.bazos.cz/inzerat/123/x.php")]

    print("\n✅ investment/candidate.py OK")
