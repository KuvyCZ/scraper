"""investment/filters.py — tvrdé strukturální filtry sdílené VŠEMI 4 zdroji.

Používá se na dvou místech:
  1) uvnitř HTML zdrojových modulů (Realingo, reality-sumperk.cz) — ať se detail
     (popis, dražší request) tahá jen pro kandidáty, co už stejně projdou;
  2) v orchestrátoru (investment_main.py) jako finální autoritativní brána
     (po cross-source deduplikaci, kdy se pole kandidáta můžou mírně změnit).

Skóre (investment/scoring.py) se sem NEPLETE — score je volné řazení, tohle jsou
natvrdo dané meze ze zadání (lokalita, velikost, dispozice, cena).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from investment.config import InvestmentConfig
    from investment.sources.base import Listing

# Obce okresu Šumperk — ověřeno živě (cs.wikipedia.org/wiki/Kategorie:Obce_v_okrese_Šumperk,
# 2026-07-24), 77 obcí. Sreality a Realingo mají okres už omezený na úrovni fetche
# (district_id / "Okres_Šumperk" URL) — tenhle seznam se reálně využije hlavně u
# Bazoše, který žádné okresové pole nedává, jen text obce z inzerátu.
OKRES_SUMPERK_TOWNS: frozenset[str] = frozenset({
    "Bludov", "Bohdíkov", "Bohuslavice", "Bohutín", "Branná", "Bratrušov", "Brníčko",
    "Bušín", "Chromeč", "Dlouhomilov", "Dolní Studénky", "Drozdov", "Dubicko",
    "Hanušovice", "Horní Studénky", "Hoštejn", "Hrabišín", "Hrabová", "Hraběšice",
    "Hynčina", "Jakubovice", "Janoušov", "Jedlí", "Jestřebí", "Jindřichov", "Kamenná",
    "Klopina", "Kolšov", "Kopřivná", "Kosov", "Krchleby", "Lesnice", "Leština",
    "Libina", "Loučná nad Desnou", "Loštice", "Lukavice", "Líšnice", "Maletín",
    "Malá Morava", "Mohelnice", "Moravičany", "Mírov", "Nemile", "Nový Malín",
    "Olšany", "Oskava", "Palonín", "Pavlov", "Petrov nad Desnou", "Police",
    "Postřelmov", "Postřelmůvek", "Písařov", "Rapotín", "Rejchartice", "Rohle",
    "Rovensko", "Ruda nad Moravou", "Rájec", "Sobotín", "Staré Město", "Stavenice",
    "Sudkov", "Svébohov", "Třeština", "Velké Losiny", "Vernířovice", "Vikantice",
    "Vikýřovice", "Vyšehoří", "Zborov", "Zvole", "Zábřeh", "Úsov", "Šléglov",
    "Štíty", "Šumperk",
})


def passes_structural_filters(listing: "Listing", location: str | None, config: "InvestmentConfig") -> bool:
    if listing.price is None or listing.price > config.price_max:
        return False
    if listing.size_m2 is None or not (config.size_min <= listing.size_m2 <= config.size_max):
        return False
    if listing.layout not in config.allowed_layouts:
        return False
    if config.include_district:
        # Sreality/Realingo mají okres omezený už při fetchi -> location tam vždy sedí.
        # Bazoš dává jen text obce -> ověř proti reálnému seznamu obcí okresu.
        return location == config.city_filter or location in OKRES_SUMPERK_TOWNS
    return location == config.city_filter
