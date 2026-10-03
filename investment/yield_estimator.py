"""investment/yield_estimator.py — čisté výpočetní funkce pro odhad výnosu.

Žádný I/O, žádná závislost na configu jako objektu — všechny parametry
(sazba nájmu, úroková sazba, ...) se předávají explicitně, ať jsou funkce
snadno testovatelné a laditelné nezávisle na zbytku modulu.
"""

from __future__ import annotations


def estimated_rent(area_m2: float, rent_per_m2: float) -> int:
    """Odhad měsíčního nájmu = plocha × sazba (Kč/m²/měsíc)."""
    return round(area_m2 * rent_per_m2)


def gross_yield_pct(estimated_rent_czk: int, asking_price: int) -> float | None:
    """Hrubý roční výnos v % ceny. None, když cena chybí/je nulová (nehádáme)."""
    if not asking_price:
        return None
    return (estimated_rent_czk * 12) / asking_price * 100


def estimated_monthly_costs(
    fond_oprav: int, insurance: int, tax: int, maintenance_reserve: int
) -> int:
    """Fond oprav + pojištění + daň z nemovitosti (měsíční díl) + rezerva na údržbu."""
    return fond_oprav + insurance + tax + maintenance_reserve


def mortgage_payment(principal: float, annual_rate_pct: float, years: int) -> int:
    """Měsíční anuitní splátka hypotéky (standardní vzorec amortizace)."""
    n = years * 12
    if n <= 0:
        return 0
    monthly_rate = annual_rate_pct / 100 / 12
    if monthly_rate == 0:
        return round(principal / n)
    payment = principal * monthly_rate / (1 - (1 + monthly_rate) ** -n)
    return round(payment)


def estimated_mortgage(asking_price: int, ltv: float, annual_rate_pct: float, years: int) -> int:
    """Měsíční splátka hypotéky při daném LTV (podíl ceny financovaný úvěrem)."""
    principal = asking_price * ltv
    return mortgage_payment(principal, annual_rate_pct, years)


def net_monthly_cashflow(rent: int, monthly_costs: int, mortgage: int) -> int:
    return rent - monthly_costs - mortgage


if __name__ == "__main__":
    import sys

    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    # Byt 58 m², 1 790 000 Kč — hodnoty ze zadání (očekávaný výstup v komentářích).
    area, price = 58.0, 1_790_000
    rent = estimated_rent(area, rent_per_m2=200.0)
    print("estimated_rent:", rent, "Kč/měsíc (čekám 11 600)")
    assert rent == 11_600

    gy = gross_yield_pct(rent, price)
    print("gross_yield_pct:", round(gy, 1), "% (čekám ~7.8)")
    assert gy is not None and 7.7 <= gy <= 7.9

    costs = estimated_monthly_costs(fond_oprav=2000, insurance=300, tax=300, maintenance_reserve=1000)
    print("estimated_monthly_costs:", costs, "(čekám 3 600)")
    assert costs == 3600

    mortgage = estimated_mortgage(price, ltv=0.90, annual_rate_pct=4.6, years=30)
    print("estimated_mortgage:", mortgage, "Kč/měsíc (čekám ~8 260)")
    assert 8100 <= mortgage <= 8400

    cashflow = net_monthly_cashflow(rent, costs, mortgage)
    print("net_monthly_cashflow:", cashflow, "Kč/měsíc")

    # cena bez info -> žádný hrubý výnos (nehádáme)
    assert gross_yield_pct(rent, 0) is None

    print("\n✅ investment/yield_estimator.py OK")
