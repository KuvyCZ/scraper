"""flip/towns.py — sledované obce a rozpoznání obce z textu lokality.

Sledujeme větší obce Olomouckého kraje (zhruba od 2 000 obyvatel), malé vesnice
ne. Seznam je ručně psaný níže (TOWN_INFO) — počty obyvatel jsou PŘIBLIŽNÉ (slouží
jen k orientaci), obec přidáš/ubereš úpravou seznamu. Každá obec může mít v
inzerátu místo názvu obce uvedenou ČÁST OBCE (např. "Holice" = Olomouc) — proto aliasy.

Shoda je PŘESNÁ na celý segment adresy (rozděleno čárkou/pomlčkou), ne podřetězec:
  * "Bludov, okres Šumperk" NESMÍ projít jako Šumperk (proto ne podřetězec),
  * "Horní Studénky" NESMÍ projít jako Dolní Studénky.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass

log = logging.getLogger("flip.towns")

OKRESY = ("Jeseník", "Olomouc", "Prostějov", "Přerov", "Šumperk")


@dataclass(frozen=True)
class Town:
    okres: str
    population: int                   # přibližně, jen orientačně
    psc: tuple[str, ...]              # hlavní PSČ (Bazoš); první 3 číslice slouží i jako kontrola názvu
    aliases: tuple[str, ...] = ()     # části obce / jiné zápisy


# ⚠️ Počty obyvatel a PSČ jsou z paměti, ne z oficiálního zdroje (ten nebyl z cloudu
# dostupný). Pokud `python main.py check` hlásí Bazoš PSČ, které nesedí, oprav je tady.
TOWN_INFO: dict[str, Town] = {
    # --- okres Olomouc ---
    "Olomouc": Town("Olomouc", 100_000, ("779 00", "772 00", "783 71"), (
        "Nová Ulice", "Hodolany", "Neředín", "Slavonín", "Nemilany", "Holice", "Chomoutov", "Černovír",
        "Hejčín", "Řepčín", "Lazce", "Klášterní Hradisko", "Nové Sady", "Povel", "Pavlovičky", "Bělidla",
        "Chválkovice", "Droždín", "Lošov", "Radíkov", "Svatý Kopeček", "Týneček", "Topolany", "Nedvězí")),
    "Šternberk": Town("Olomouc", 13_500, ("785 01",)),
    "Uničov": Town("Olomouc", 11_000, ("783 91",)),
    "Litovel": Town("Olomouc", 9_800, ("784 01",)),
    "Hlubočky": Town("Olomouc", 4_000, ("783 61",), ("Mariánské Údolí", "Hrubá Voda")),
    "Velká Bystřice": Town("Olomouc", 3_400, ("783 53",)),
    "Bohuňovice": Town("Olomouc", 3_400, ("783 14",)),
    "Štěpánov": Town("Olomouc", 3_400, ("783 13",)),
    "Velký Týnec": Town("Olomouc", 3_000, ("783 72",)),
    "Moravský Beroun": Town("Olomouc", 3_000, ("793 05",)),
    "Dolany": Town("Olomouc", 2_800, ("783 16",)),
    "Horka nad Moravou": Town("Olomouc", 2_500, ("783 35",)),
    "Náměšť na Hané": Town("Olomouc", 2_000, ("783 44",)),
    # --- okres Prostějov ---
    "Prostějov": Town("Prostějov", 43_000, ("796 01", "796 04"), (
        "Krasice", "Čechovice", "Čechůvky", "Domamyslice", "Vrahovice", "Žešov")),
    "Kostelec na Hané": Town("Prostějov", 2_900, ("798 41",)),
    "Konice": Town("Prostějov", 2_800, ("798 52",)),
    "Plumlov": Town("Prostějov", 2_400, ("798 03",)),
    "Němčice nad Hanou": Town("Prostějov", 2_000, ("798 27",)),
    # --- okres Přerov ---
    "Přerov": Town("Přerov", 41_000, ("750 02", "751 24"), (
        "Předmostí", "Lověšice", "Kozlovice", "Henčlov", "Dluhonice", "Žeravice", "Vinary", "Čekyně",
        "Penčice", "Popovice", "Lýsky", "Újezdec")),
    "Hranice": Town("Přerov", 18_000, ("753 01",), ("Drahotuše", "Slavíč", "Velká", "Lhotka")),
    "Lipník nad Bečvou": Town("Přerov", 8_000, ("751 31",)),
    "Kojetín": Town("Přerov", 6_000, ("752 01",)),
    "Tovačov": Town("Přerov", 2_300, ("751 01",)),
    "Brodek u Přerova": Town("Přerov", 2_000, ("751 03",)),
    # --- okres Šumperk ---
    "Šumperk": Town("Šumperk", 25_500, ("787 01",), ("Dolní Temenice", "Horní Temenice", "Temenice")),
    "Zábřeh": Town("Šumperk", 13_500, ("789 01",)),
    "Mohelnice": Town("Šumperk", 9_000, ("789 85",)),
    "Postřelmov": Town("Šumperk", 3_300, ("789 69",)),
    "Libina": Town("Šumperk", 3_300, ("788 05",)),
    "Bludov": Town("Šumperk", 3_100, ("789 61",)),
    "Nový Malín": Town("Šumperk", 3_100, ("788 03",), ("Mladoňov", "Plechy")),
    "Loštice": Town("Šumperk", 3_000, ("789 83",)),
    "Rapotín": Town("Šumperk", 3_000, ("788 14",)),
    "Hanušovice": Town("Šumperk", 2_900, ("788 33",)),
    "Velké Losiny": Town("Šumperk", 2_700, ("788 15",)),
    "Vikýřovice": Town("Šumperk", 2_500, ("788 13",)),
    "Štíty": Town("Šumperk", 2_000, ("789 91",)),
    # --- okres Jeseník ---
    "Jeseník": Town("Jeseník", 10_500, ("790 01",), ("Bukovice", "Dětřichov", "Seč")),
    "Zlaté Hory": Town("Jeseník", 3_600, ("793 76",)),
    "Javorník": Town("Jeseník", 2_600, ("790 70",)),
    "Mikulovice": Town("Jeseník", 2_400, ("790 84",)),
    "Lipová-lázně": Town("Jeseník", 2_300, ("790 61",), ("Lipová lázně",)),
}

# kanonický název obce -> části obce / jiné zápisy, které k ní patří
TOWNS: dict[str, tuple[str, ...]] = {name: t.aliases for name, t in TOWN_INFO.items()}


def okres_of(town: str | None) -> str | None:
    info = TOWN_INFO.get(town) if town else None
    return info.okres if info else None


# Bazoš: obec se pozná podle PSČ, ne podle názvu (Bazoš u inzerátu občas ukáže
# "Šumperk" s PSČ jiné obce). PSČ -> kanonický název obce.
# Přepíše se v .env proměnnou BAZOS_PSC (viz parse_psc_map).
DEFAULT_PSC: dict[str, str] = {
    p.replace(" ", ""): name for name, t in TOWN_INFO.items() for p in t.psc
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


def psc_fits_town(psc: str | None, town: str | None) -> bool:
    """Sedí PSČ aspoň první 3 číslicemi na obec? (velká města mají víc PSČ než
    TOWN_INFO zná; "Šumperk" s PSČ 789 72 = Bohuslavice ale neprojde)."""
    key = normalize_psc(psc)
    info = TOWN_INFO.get(town) if town else None
    return bool(key and info) and any(p.replace(" ", "")[:3] == key[:3] for p in info.psc)
