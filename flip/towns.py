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

import re
import unicodedata

# kanonický název obce -> části obce / jiné zápisy, které k ní patří
TOWNS: dict[str, tuple[str, ...]] = {
    "Šumperk": ("Dolní Temenice", "Horní Temenice", "Temenice"),
    "Nový Malín": ("Mladoňov", "Plechy"),
    "Vikýřovice": (),
    "Rapotín": (),
    "Dolní Studénky": ("Králec",),
    "Sudkov": (),
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
