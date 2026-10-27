"""flip/config.py — veškerá konfigurace z prostředí / .env (žádné hardcoded tajnosti).

Vlastní mini-parser .env (bez python-dotenv) — NAS má málo paměti. Každá hodnota
má rozumný default, takže .env stačí s Telegram tokenem a chat_id.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from flip.towns import DEFAULT_PSC, OKRESY, parse_psc_map

log = logging.getLogger("flip.config")

ALL_SOURCES = ("sreality", "realingo", "bazos", "reality_sumperk")


def load_dotenv(path: str = ".env") -> None:
    """KEY=VALUE z .env do os.environ (už nastavené proměnné NEPŘEPISUJE).

    Podporuje inline komentář za hodnotou (mezera + '#') stejně jako Docker
    `env_file`; uvozená hodnota se bere celá.
    """
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if value[:1] in ("'", '"'):
                quote = value[0]
                end = value.find(quote, 1)
                value = value[1:end] if end != -1 else value[1:]
            else:
                hash_pos = value.find(" #")
                if hash_pos != -1:
                    value = value[:hash_pos]
                value = value.strip()
            os.environ.setdefault(key, value)


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _get_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "ano", "on")


def _get_int(name: str, default: int) -> int:
    try:
        return int(_get(name, str(default)).replace("_", "").replace(" ", ""))
    except ValueError:
        log.warning("%s: neplatné číslo, používám výchozí %s", name, default)
        return default


def _get_float(name: str, default: float) -> float:
    try:
        return float(_get(name, str(default)))
    except ValueError:
        log.warning("%s: neplatné číslo, používám výchozí %s", name, default)
        return default


def _get_list(name: str, default: list[str]) -> list[str]:
    raw = _get(name).strip()
    if not raw:
        return list(default)
    return [x.strip() for x in raw.split(",") if x.strip()]


@dataclass
class FlipConfig:
    # --- Telegram --- #
    telegram_token: str = ""
    telegram_chat_id: str = ""
    dry_run: bool = False

    # --- zdroje a plánování (každý zdroj má vlastní interval, 0 = jen jednou) --- #
    active_sources: list[str] = field(default_factory=lambda: list(ALL_SOURCES))
    sreality_interval_min: float = 30.0
    realingo_interval_min: float = 60.0
    bazos_interval_min: float = 60.0
    reality_sumperk_interval_min: float = 60.0
    realingo_max_pages: int = 20      # okres, všechny typy nemovitostí, řazeno od nejlevnějšího
    bazos_max_pages: int = 25         # Bazoš nejde filtrovat (robots.txt) -> celostátní kategorie
    reality_sumperk_max_pages: int = 5
    source_failure_alert_threshold: int = 3

    # --- lokalita --- #
    # Sreality locality_region_id: 8 = Olomoucký kraj (NEOVĚŘENO živě; okres Šumperk = district 44 ověřen).
    # Když `main.py check` ukáže 0 inzerátů ve sledovaných obcích, číslo je špatně.
    sreality_region_id: int = 8
    # Realingo: /prodej_reality/Okres_{okres}/ pro každý okres kraje
    realingo_districts: list[str] = field(default_factory=lambda: list(OKRESY))
    # Bazoš: PSČ -> obec; inzerát projde podle PSČ, ne podle názvu obce (BAZOS_PSC v .env)
    bazos_psc: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_PSC))

    # --- co sledujeme --- #
    price_max: int = 5_000_000        # rozpočet; dražší se ukládá (data o trhu), ale nealertuje
    area_min: float = 0.0             # 0 = bez omezení
    area_max: float = 0.0             # 0 = bez omezení
    include_flats: bool = True
    include_houses: bool = True

    # --- ekonomika flipu --- #
    reno_cost_flat_m2: int = 25_000   # Kč/m² užitné plochy, celková rekonstrukce bytu
    reno_cost_house_m2: int = 30_000  # domy dráž: střecha, fasáda, topení, sítě
    sale_factor: float = 0.92         # nabídková cena -> realisticky prodejní (~8 % smlouvání)
    holding_cost_pct: float = 0.05    # poplatky, makléř, úroky, energie po dobu flipu (z ceny + reko)

    # --- trh (mediány) --- #
    market_window_days: int = 90      # z jak starých inzerátů se počítá medián (prodané po čase vypadnou)
    min_comps: int = 5                # pod tímto vzorkem se cenový signál nepočítá (nehádá se)

    # --- notifikace --- #
    hot_score: int = 65               # od tohoto skóre 🔥
    hot_margin_pct: float = 20.0      # nebo když odhad marže >= tolik % -> taky 🔥
    price_drop_alert_pct: float = 3.0  # pokles ceny známého inzerátu o >= % -> zpráva
    baseline_top_n: int = 10          # první běh: místo záplavy jedno shrnutí s top N
    town_report_days: int = 7         # jak často poslat tabulku cen po obcích (0 = nikdy)
    town_report_window_days: int = 30  # z inzerátů viděných za posledních N dní

    # --- úložiště --- #
    db_path: str = "flip.db"

    # False = jen seznamy, bez detailů (rychlé `main.py check`); z .env se nečte
    fetch_details: bool = True

    def interval_minutes(self, source: str) -> float:
        return {
            "sreality": self.sreality_interval_min,
            "realingo": self.realingo_interval_min,
            "bazos": self.bazos_interval_min,
            "reality_sumperk": self.reality_sumperk_interval_min,
        }[source]

    def reno_cost_m2(self, kind: str) -> int:
        return self.reno_cost_house_m2 if kind == "dum" else self.reno_cost_flat_m2

    def wants_kind(self, kind: str) -> bool:
        return (kind == "byt" and self.include_flats) or (kind == "dum" and self.include_houses)

    def area_ok(self, area_m2: float | None) -> bool:
        """Neznámá plocha projde (radši ukázat než omylem zahodit)."""
        if area_m2 is None:
            return True
        if self.area_min and area_m2 < self.area_min:
            return False
        if self.area_max and area_m2 > self.area_max:
            return False
        return True


def load_config(env_file: str = ".env") -> FlipConfig:
    load_dotenv(env_file)
    cfg = FlipConfig(
        telegram_token=_get("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_get("TELEGRAM_CHAT_ID"),
        dry_run=_get_bool("DRY_RUN", False),
        active_sources=[s for s in _get_list("ACTIVE_SOURCES", list(ALL_SOURCES)) if s in ALL_SOURCES],
        sreality_interval_min=_get_float("SREALITY_INTERVAL_MINUTES", 30.0),
        realingo_interval_min=_get_float("REALINGO_INTERVAL_MINUTES", 60.0),
        bazos_interval_min=_get_float("BAZOS_INTERVAL_MINUTES", 60.0),
        reality_sumperk_interval_min=_get_float("REALITY_SUMPERK_INTERVAL_MINUTES", 60.0),
        realingo_max_pages=_get_int("REALINGO_MAX_PAGES", 20),
        bazos_max_pages=_get_int("BAZOS_MAX_PAGES", 25),
        reality_sumperk_max_pages=_get_int("REALITY_SUMPERK_MAX_PAGES", 5),
        source_failure_alert_threshold=_get_int("SOURCE_FAILURE_ALERT_THRESHOLD", 3),
        sreality_region_id=_get_int("SREALITY_REGION_ID", 8),
        realingo_districts=_get_list("REALINGO_DISTRICTS", list(OKRESY)),
        bazos_psc=parse_psc_map(_get("BAZOS_PSC")),
        price_max=_get_int("PRICE_MAX", 5_000_000),
        area_min=_get_float("AREA_MIN", 0.0),
        area_max=_get_float("AREA_MAX", 0.0),
        include_flats=_get_bool("INCLUDE_FLATS", True),
        include_houses=_get_bool("INCLUDE_HOUSES", True),
        reno_cost_flat_m2=_get_int("RENO_COST_FLAT_M2", 25_000),
        reno_cost_house_m2=_get_int("RENO_COST_HOUSE_M2", 30_000),
        sale_factor=_get_float("SALE_FACTOR", 0.92),
        holding_cost_pct=_get_float("HOLDING_COST_PCT", 0.05),
        market_window_days=_get_int("MARKET_WINDOW_DAYS", 90),
        min_comps=_get_int("MIN_COMPS", 5),
        hot_score=_get_int("HOT_SCORE", 65),
        hot_margin_pct=_get_float("HOT_MARGIN_PCT", 20.0),
        price_drop_alert_pct=_get_float("PRICE_DROP_ALERT_PCT", 3.0),
        baseline_top_n=_get_int("BASELINE_TOP_N", 10),
        town_report_days=_get_int("TOWN_REPORT_DAYS", 7),
        town_report_window_days=_get_int("TOWN_REPORT_WINDOW_DAYS", 30),
        db_path=_get("DB_PATH", "flip.db"),
    )
    if not cfg.dry_run and not (cfg.telegram_token and cfg.telegram_chat_id):
        log.warning("Chybí TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID → dry_run (zprávy se jen zalogují).")
        cfg.dry_run = True
    return cfg
