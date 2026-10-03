"""investment/dedup.py — cross-source deduplikace (stejný byt na víc portálech).

Dva inzeráty se považují za duplicitu, když: cena do `dedup_price_pct` od sebe
(relativně, ke levnější z dvojice) AND plocha do `dedup_size_m2` m² od sebe
AND stejná dispozice AND stejná obec. Shlukuje se přes union-find (tranzitivně
— A~B a B~C => A,B,C jeden shluk), ne jen párové porovnání sousedů.

Z každého shluku se nechá NEJLEVNĚJŠÍ verze (ta jde do skórování/alertu), zbylé
URL se přidají do `duplicate_sources`, ať uživatel může porovnat nabídky.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from investment.config import InvestmentConfig
    from investment.sources.base import Listing


def is_duplicate_pair(a: "Listing", b: "Listing", config: "InvestmentConfig") -> bool:
    if a.layout != b.layout or a.layout is None:
        return False
    if a.location != b.location or a.location is None:
        return False
    if a.price is None or b.price is None or a.size_m2 is None or b.size_m2 is None:
        return False
    price_ref = min(a.price, b.price)
    if price_ref <= 0:
        return False
    if abs(a.price - b.price) / price_ref > config.dedup_price_pct:
        return False
    if abs(a.size_m2 - b.size_m2) > config.dedup_size_m2:
        return False
    return True


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def deduplicate(listings: list["Listing"], config: "InvestmentConfig") -> list["Listing"]:
    """Vrací jeden reprezentativní (nejlevnější) `Listing` na shluk, s vyplněným
    `duplicate_sources`. Pořadí výstupu neodpovídá vstupu (skupiny se přeuspořádají)."""
    n = len(listings)
    if n <= 1:
        return list(listings)

    uf = _UnionFind(n)
    for i in range(n):
        for j in range(i + 1, n):
            if is_duplicate_pair(listings[i], listings[j], config):
                uf.union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(uf.find(i), []).append(i)

    out: list["Listing"] = []
    for indices in groups.values():
        group = [listings[i] for i in indices]
        group.sort(key=lambda lst: lst.price if lst.price is not None else float("inf"))
        canonical = group[0]
        canonical.duplicate_sources = [(lst.source, lst.url) for lst in group[1:]]
        out.append(canonical)
    return out


if __name__ == "__main__":
    import sys

    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    from investment.config import InvestmentConfig
    from investment.sources.base import Listing

    cfg = InvestmentConfig()

    a = Listing(source="sreality", external_id="1", url="https://sreality/1",
                price=1_900_000, size_m2=55.0, layout="2+1", location="Šumperk")
    b = Listing(source="realingo", external_id="2", url="https://realingo/2",
                price=1_910_000, size_m2=55.5, layout="2+1", location="Šumperk")  # ~0.5% cena, 0.5 m2 -> duplikát
    c = Listing(source="bazos", external_id="3", url="https://bazos/3",
                price=2_500_000, size_m2=60.0, layout="2+1", location="Šumperk")  # jiná cena -> NE duplikát
    d = Listing(source="reality_sumperk", external_id="4", url="https://sumperk/4",
                price=1_895_000, size_m2=54.8, layout="2+1", location="Šumperk")  # taky duplikát a+b

    result = deduplicate([a, b, c, d], cfg)
    print(f"{len(result)} kandidátů po dedup (čekám 2: [a,b,d] shluk + c samostatně)")
    for r in result:
        print(" ", r.source, r.price, "duplicate_sources=", r.duplicate_sources)
    assert len(result) == 2
    canonical = next(r for r in result if r.price == 1_895_000)
    assert canonical.source == "reality_sumperk"  # nejlevnější ze shluku a/b/d
    assert len(canonical.duplicate_sources) == 2

    print("\n✅ investment/dedup.py OK")
