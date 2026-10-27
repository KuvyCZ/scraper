"""flip/towns.py — sledované obce a rozpoznání obce z textu lokality.

Sledujeme 6 obcí okresu Šumperk. Každá může mít v inzerátu místo názvu obce
uvedenou ČÁST OBCE (např. "Králec" = Dolní Studénky) — proto aliasy.
Části obcí: Nový Malín (Mladoňov, Plechy), Dolní Studénky (Králec),
Šumperk (Dolní a Horní Temenice). Vikýřovice, Rapotín a Sudkov části nemají.

Shoda je PŘESNÁ na celý segment adresy (rozděleno čárkou/pomlčkou), ne podřetězec:
  * "Bludov, okres Šumperk" NESMÍ projít jako Šumperk (proto ne podřetězec),
  * "Horní Studénky" NESMÍ projít jako Dolní Studénky.
"""

from __future__ import annotations

import logging
import re
import unicodedata

log = logging.getLogger("flip.towns")

# kanonický název obce -> části obce / jiné zápisy, které k ní patří
TOWNS: dict[str, tuple[str, ...]] = {
    "Šumperk": ("Dolní Temenice", "Horní Temenice", "Temenice"),
    "Nový Malín": ("Mladoňov", "Plechy"),
    "Vikýřovice": (),
    "Rapotín": (),
    "Dolní Studénky": ("Králec",),
    "Sudkov": (),
}

# Bazoš: obec se pozná podle PSČ, ne podle názvu (Bazoš u inzerátu občas ukáže
# "Šumperk" s PSČ jiné obce). PSČ -> kanonický název obce. Jedno PSČ může pokrývat
# i sousední obce (788 14 = Rapotín i Petrov nad Desnou) — ty pak projdou jako tahle obec.
# Přepíše se v .env proměnnou BAZOS_PSC (viz parse_psc_map).
DEFAULT_PSC: dict[str, str] = {
    "78701": "Šumperk",
    "78803": "Nový Malín",
    "78813": "Vikýřovice",
    "78814": "Rapotín",
    "78820": "Dolní Studénky",
    "78821": "Sudkov",
}

_SPLIT_RE = re.compile(r"\s*[,–/]\s*|\s+-\s+|(?<=\w)-(?=\w)")
_PSC_RE = re.compile(r"\b\d{3}\s?\d{2}\b")


def normalize(text: str) -> str:
    """lowercase, bez diakritiky, jedna mezera — pro robustní porovnání názvů."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(ascii_only.split())


def _build_index() -> dict[str, str]:
    index: dict[str, str] = {}
    for town, aliases in TOWNS.items():
        index[normalize(town)] = town
        for alias in aliases:
            index[normalize(alias)] = town
    return index


_INDEX = _build_index()


def match_town(*texts: str | None) -> str | None:
    """Vrátí kanonický název sledované obce, nebo None.

    Bere víc kandidátních textů (např. Sreality `city` a `citypart`) a vrátí první shodu.
    Každý text se rozdělí na segmenty (čárka, pomlčka, lomítko) a porovná se přesně.
    """
    for text in texts:
        if not text:
            continue
        cleaned = _PSC_RE.sub(" ", text)
        segments = [cleaned, *_SPLIT_RE.split(cleaned)]
        for segment in segments:
            hit = _INDEX.get(normalize(segment))
            if hit:
                return hit
    return None


def watched_towns() -> list[str]:
    return list(TOWNS)


def normalize_psc(text: str | None) -> str | None:
    """'787 01' / '78701' / 'PSČ 787 01' -> '78701'; jinak None."""
    if not text:
        return None
    m = _PSC_RE.search(text)
    return m.group(0).replace(" ", "") if m else None


def parse_psc_map(raw: str) -> dict[str, str]:
    """BAZOS_PSC z .env: '787 01=Šumperk, 788 03=Nový Malín' -> {'78701': 'Šumperk', ...}.

    Název obce musí být jedna ze sledovaných obcí (nebo její část); jinak se položka
    přeskočí s varováním. Prázdný text -> DEFAULT_PSC.
    """
    if not raw.strip():
        return dict(DEFAULT_PSC)
    out: dict[str, str] = {}
    for part in raw.split(","):
        psc_text, _, town_text = part.partition("=")
        psc, town = normalize_psc(psc_text), match_town(town_text)
        if psc is None or town is None:
            log.warning("BAZOS_PSC: nerozumím položce %r (má být např. '787 01=Šumperk'), přeskakuji", part.strip())
            continue
        out[psc] = town
    return out or dict(DEFAULT_PSC)


def match_town_by_psc(psc: str | None, psc_map: dict[str, str]) -> str | None:
    """Kanonická obec pro PSČ z inzerátu, nebo None (mimo sledovaná PSČ)."""
    key = normalize_psc(psc)
    return psc_map.get(key) if key else None
