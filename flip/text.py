"""flip/text.py — čtení signálů z textu inzerátu (stav, motivace prodejce, rizika, typ domu).

Keywordy se porovnávají bez diakritiky a velikosti písmen. Výstupy jsou vždy
pravděpodobnostní ("pravděpodobně před rekonstrukcí") — popis píše prodejce.
"""

from __future__ import annotations

import re

from flip.towns import normalize


def _norm_all(items: list[str]) -> tuple[str, ...]:
    return tuple(normalize(x) for x in items)


# --- stav --- #
NEEDS_RENO_KW = _norm_all([
    "k rekonstrukci", "nutná rekonstrukce", "před rekonstrukcí", "vyžaduje rekonstrukci",
    "určeno k rekonstrukci", "k celkové rekonstrukci", "nutnost rekonstrukce",
    "holobyt", "původní bytové jádro", "umakartové jádro", "k demolici", "ke zbourání",
    "nutné opravy", "vyžaduje opravy", "horší technický stav", "špatný technický stav",
    "zchátralý", "vhodný k rekonstrukci", "vhodné k rekonstrukci",
])
ORIGINAL_STATE_KW = _norm_all([
    "původní stav", "původním stavu", "původní zachovalý", "k modernizaci",
    "vhodný k úpravám", "stav před modernizací", "částečné rekonstrukci", "částečná rekonstrukce",
])
RENOVATED_KW = _norm_all([
    "po rekonstrukci", "po kompletní rekonstrukci", "nově zrekonstruovan", "kompletně zrekonstruovan",
    "novostavba", "zrekonstruováno", "nová kuchyňská linka", "luxusní", "designov", "na klíč",
])

# --- motivace prodejce --- #
MOTIVATED_KW = _norm_all([
    "dědictví", "z pozůstalosti", "po zesnul", "nutný prodej", "nutno prodat", "rychlé jednání",
    "při rychlém jednání", "sleva při rychl", "stěhování do zahraničí", "spěchá", "spěšný prodej",
    "rozvod", "výrazná sleva", "zlevněno", "cena k jednání", "cena dohodou",
])

# --- rizika: neblokují, ale MUSÍ být ve zprávě vidět --- #
RISK_KW: dict[str, tuple[str, ...]] = {
    "share": _norm_all(["spoluvlastnický podíl", "ideální podíl", "id. podíl", "podíl 1/2", "podílu 1/2",
                        "podíl o velikosti", "prodej podílu", "id. 1/2", "ideální 1/2"]),
    "auction": _norm_all(["dražba", "dražby", "dražební", "aukce", "aukční"]),
    "tenant": _norm_all(["s nájemníkem", "nájemní smlouva na dobu", "pronajatý", "obsazený nájemník"]),
    "easement": _norm_all(["věcné břemeno", "věcným břemenem", "doživotní užívání", "služebnost"]),
    "coop": _norm_all(["družstevní", "převod členských práv", "převod družstevního podílu"]),
    "attic": _norm_all(["půdní prostor", "půdní prostory", "k vybudování bytu", "nebytový prostor"]),
}
RISK_LABELS: dict[str, str] = {
    "share": "only a share of the property (podíl)",
    "auction": "auction (dražba/aukce)",
    "tenant": "sitting tenant",
    "easement": "easement / lifelong right of use (věcné břemeno)",
    "coop": "co-op flat (družstevní) — mortgage harder",
    "attic": "attic / non-residential space, not a finished flat",
}

# --- typ domu --- #
HOUSE_SUBTYPE_KW: list[tuple[str, tuple[str, ...]]] = [
    ("Chata", _norm_all(["chata", "chaty", "chatka", "chatku", "chatu", "zahradní chat"])),
    ("Chalupa", _norm_all(["chalupa", "chalupu", "chalupy"])),
    ("Zemědělská usedlost", _norm_all(["usedlost", "statek", "zemědělská", "hospodářská stavení", "grunt"])),
    ("Vila", _norm_all(["vila", "vilu", "vily"])),
    ("Vícegenerační dům", _norm_all(["vícegenerační"])),
]
EXCLUDED_HOUSE_SUBTYPES = {"Chata", "Na klíč"}     # malá chata / novostavba na klíč = není flip
TAGGED_HOUSE_SUBTYPES = {"Chalupa"}                # může být celoroční dům, ale ověř

LAYOUT_RE = re.compile(r"\b(\d)\s*\+\s*(kk|\d)\b", re.IGNORECASE)
AREA_RE = re.compile(r"(\d{1,4}(?:[.,]\d+)?)\s*m(?:&sup2;|²|2)(?![\w])", re.IGNORECASE)
LAND_RE = re.compile(r"pozem\w*[^0-9]{0,40}?(\d[\d\s.]{1,8})\s*m(?:&sup2;|²|2)(?![\w])", re.IGNORECASE)


def _any(keywords: tuple[str, ...], text_norm: str) -> bool:
    return any(kw in text_norm for kw in keywords)


def condition_from_text(text: str | None) -> str | None:
    """Hrubý stav z popisu: "needs_reno" | "original" | "renovated" | None (nic nevíme)."""
    if not text:
        return None
    t = normalize(text)
    if _any(NEEDS_RENO_KW, t):
        return "needs_reno"
    if _any(RENOVATED_KW, t):
        return "renovated"
    if _any(ORIGINAL_STATE_KW, t):
        return "original"
    return None


def is_motivated(text: str | None) -> bool:
    return bool(text) and _any(MOTIVATED_KW, normalize(text))


def risks(text: str | None) -> list[str]:
    """Klíče rizik nalezených v textu (pořadí dle RISK_KW)."""
    if not text:
        return []
    t = normalize(text)
    return [key for key, kws in RISK_KW.items() if _any(kws, t)]


def house_subtype_from_text(text: str | None) -> str:
    """Typ domu z titulku/popisu; výchozí "Rodinný"."""
    if text:
        t = normalize(text)
        for subtype, kws in HOUSE_SUBTYPE_KW:
            # začátek slova: "vila" nesmí chytit cizí slovo, "chata" ne "chatová oblast" uprostřed
            if any(re.search(r"\b" + re.escape(kw), t) for kw in kws):
                return subtype
    return "Rodinný"


def parse_layout(text: str | None) -> str | None:
    if not text:
        return None
    m = LAYOUT_RE.search(text)
    return f"{m.group(1)}+{m.group(2).lower()}" if m else None


def parse_area(text: str | None) -> float | None:
    """První plocha v textu, která NENÍ plocha pozemku."""
    if not text:
        return None
    land_spans = [m.span(1) for m in LAND_RE.finditer(text)]
    for m in AREA_RE.finditer(text):
        if any(start <= m.start(1) < end for start, end in land_spans):
            continue
        value = float(m.group(1).replace(",", "."))
        if 10 <= value <= 2000:
            return value
    return None


def parse_land(text: str | None) -> float | None:
    if not text:
        return None
    m = LAND_RE.search(text)
    if not m:
        return None
    digits = re.sub(r"[\s.]", "", m.group(1))
    return float(digits) if digits.isdigit() else None


def parse_price(text: str | None) -> int | None:
    """"1 290 000 Kč" -> 1290000. Text bez číslic ("Dohodou", "V RK") -> None."""
    if not text:
        return None
    digits = "".join(re.findall(r"\d+", text))
    if not digits:
        return None
    value = int(digits)
    return value if value >= 10_000 else None  # "1 Kč" / "0 Kč" = cena na dotaz
