"""investment/config.py — konfigurace investičního režimu (samostatná od config.py).

Sdílí jen `.env` soubor a jeho typové parsery (config.py:_get*, load_dotenv) —
žádné pole hlavního `Config` se nečte ani neupravuje.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from config import _get, _get_bool, _get_float, _get_int, _get_list_str, load_dotenv

log = logging.getLogger("investment.config")


@dataclass
class InvestmentConfig:
    enabled: bool = False          # INVESTMENT_ENABLED — jistící pojistka proti omylem spuštěnému běhu

    # --- Telegram (samostatný chat, stejný bot token jako hlavní monitoring) --- #
    telegram_token: str = ""
    telegram_chat_id: str = ""     # TELEGRAM_INVESTMENT_CHAT_ID
    dry_run: bool = False

    # --- zdroje a nezávislé plánování (každý zdroj má svůj vlastní interval) --- #
    active_sources: list[str] = field(default_factory=lambda: ["sreality", "realingo", "bazos", "reality_sumperk"])
    sreality_poll_interval_minutes: float = 30.0   # 0 = jednorázově
    realingo_poll_interval_minutes: float = 60.0   # HTML zdroje jedou šetrněji (delší interval)
    bazos_poll_interval_minutes: float = 60.0
    sumperk_poll_interval_minutes: float = 60.0

    # --- kolik stránek smí každý HTML zdroj max. projít za jeden běh --- #
    realingo_max_pages: int = 5    # cenově řazeno -> pár stránek pokryje celý cenový rozsah
    bazos_max_pages: int = 25      # neřazeno/celostátní -> potřebuje víc stránek na šanci najít Šumperk
    sumperk_max_pages: int = 5     # na dispozici; malý regionální inventář

    # --- selhání zdroje: po kolika bězích v kuse poslat JEDNO varování (ne každý běh) --- #
    source_failure_alert_threshold: int = 3

    # --- lokalita: Šumperk (obec), s přepínačem na celý okres --- #
    district_id: int = 44                 # Sreality locality_district_id (ověřeno: 44 = okres Šumperk)
    city_filter: str = "Šumperk"           # obec, na kterou se filtruje, když include_district=False
    include_district: bool = False         # True = celý okres Šumperk, False = jen obec Šumperk

    # --- velikost / dispozice / cena --- #
    size_min: float = 40.0
    size_max: float = 70.0
    layouts: list[str] = field(default_factory=lambda: ["2+1", "2+kk", "3+1"])
    price_max: int = 2_800_000
    price_target_max: int = 2_000_000  # pod touhle hranicí je to skutečný nákupní rozpočet -> bonus ve skóre

    # --- rolling průměr cena/m² --- #
    avg_window_n: int = 30         # kolik posledních srovnatelných inzerátů (lokalita+dispozice) zprůměrovat
    avg_min_sample: int = 5        # pod tímto vzorkem se cenový signál přeskočí (cold-start, nehádá se)

    # --- cross-source deduplikace (stejný byt na víc portálech) --- #
    dedup_price_pct: float = 0.02  # cena do 2 % od sebe
    dedup_size_m2: float = 2.0     # plocha do 2 m² od sebe

    # --- klíčová slova stavu (příležitost, NE diskvalifikace) --- #
    condition_keywords: list[str] = field(default_factory=lambda: [
        "před rekonstrukcí", "původní stav", "k rekonstrukci", "nutná rekonstrukce",
    ])

    # --- odhad výnosu --- #
    rent_per_m2: float = 200.0     # Kč/m²/měsíc
    fond_oprav: int = 2000         # Kč/měsíc
    insurance: int = 300           # Kč/měsíc
    tax: int = 300                 # Kč/měsíc
    maintenance_reserve: int = 1000  # Kč/měsíc
    ltv: float = 0.90              # podíl hypotéky na ceně
    mortgage_years: int = 30
    interest_rate_pct: float = 4.6

    # --- skóre (volné — jen nízký práh, ne tvrdý filtr) --- #
    score_threshold: int = 40

    # --- úložiště --- #
    db_path: str = "investment.db"

    @property
    def allowed_layouts(self) -> list[str]:
        return list(self.layouts)

    def poll_interval_minutes(self, source: str) -> float:
        return {
            "sreality": self.sreality_poll_interval_minutes,
            "realingo": self.realingo_poll_interval_minutes,
            "bazos": self.bazos_poll_interval_minutes,
            "reality_sumperk": self.sumperk_poll_interval_minutes,
        }[source]


def load_investment_config(env_file: str = ".env") -> InvestmentConfig:
    """Načte .env (stejný soubor jako hlavní config) a sestaví InvestmentConfig.

    Bez tokenu/investičního chat_id vynutí dry_run — stejná konvence jako u
    hlavního config.py, ať omylem nepřijde zpráva do neexistujícího chatu.
    """
    load_dotenv(env_file)

    cfg = InvestmentConfig(
        enabled=_get_bool("INVESTMENT_ENABLED", False),
        telegram_token=_get("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_get("TELEGRAM_INVESTMENT_CHAT_ID"),
        dry_run=_get_bool("INVESTMENT_DRY_RUN", False),
        active_sources=_get_list_str(
            "INVESTMENT_ACTIVE_SOURCES", ["sreality", "realingo", "bazos", "reality_sumperk"]
        ),
        sreality_poll_interval_minutes=_get_float("INVESTMENT_SREALITY_POLL_INTERVAL_MINUTES", 30.0),
        realingo_poll_interval_minutes=_get_float("INVESTMENT_REALINGO_POLL_INTERVAL_MINUTES", 60.0),
        bazos_poll_interval_minutes=_get_float("INVESTMENT_BAZOS_POLL_INTERVAL_MINUTES", 60.0),
        sumperk_poll_interval_minutes=_get_float("INVESTMENT_SUMPERK_POLL_INTERVAL_MINUTES", 60.0),
        realingo_max_pages=_get_int("INVESTMENT_REALINGO_MAX_PAGES", 5),
        bazos_max_pages=_get_int("INVESTMENT_BAZOS_MAX_PAGES", 25),
        sumperk_max_pages=_get_int("INVESTMENT_SUMPERK_MAX_PAGES", 5),
        source_failure_alert_threshold=_get_int("INVESTMENT_SOURCE_FAILURE_ALERT_THRESHOLD", 3),
        district_id=_get_int("INVESTMENT_DISTRICT_ID", 44),
        city_filter=_get("INVESTMENT_CITY_FILTER", "Šumperk"),
        include_district=_get_bool("INVESTMENT_INCLUDE_DISTRICT", False),
        size_min=_get_float("INVESTMENT_SIZE_MIN", 40.0),
        size_max=_get_float("INVESTMENT_SIZE_MAX", 70.0),
        layouts=_get_list_str("INVESTMENT_LAYOUTS", ["2+1", "2+kk", "3+1"]),
        price_max=_get_int("INVESTMENT_PRICE_MAX", 2_800_000),
        price_target_max=_get_int("INVESTMENT_PRICE_TARGET_MAX", 2_000_000),
        avg_window_n=_get_int("INVESTMENT_AVG_WINDOW_N", 30),
        avg_min_sample=_get_int("INVESTMENT_AVG_MIN_SAMPLE", 5),
        dedup_price_pct=_get_float("INVESTMENT_DEDUP_PRICE_PCT", 0.02),
        dedup_size_m2=_get_float("INVESTMENT_DEDUP_SIZE_M2", 2.0),
        condition_keywords=_get_list_str(
            "INVESTMENT_CONDITION_KEYWORDS",
            ["před rekonstrukcí", "původní stav", "k rekonstrukci", "nutná rekonstrukce"],
        ),
        rent_per_m2=_get_float("INVESTMENT_RENT_PER_M2", 200.0),
        fond_oprav=_get_int("INVESTMENT_FOND_OPRAV", 2000),
        insurance=_get_int("INVESTMENT_INSURANCE", 300),
        tax=_get_int("INVESTMENT_TAX", 300),
        maintenance_reserve=_get_int("INVESTMENT_MAINTENANCE_RESERVE", 1000),
        ltv=_get_float("INVESTMENT_LTV", 0.90),
        mortgage_years=_get_int("INVESTMENT_MORTGAGE_YEARS", 30),
        interest_rate_pct=_get_float("INVESTMENT_INTEREST_RATE_PCT", 4.6),
        score_threshold=_get_int("INVESTMENT_SCORE_THRESHOLD", 40),
        db_path=_get("INVESTMENT_DB_PATH", "investment.db"),
    )

    if not cfg.dry_run and not (cfg.telegram_token and cfg.telegram_chat_id):
        log.warning(
            "Chybí TELEGRAM_BOT_TOKEN/TELEGRAM_INVESTMENT_CHAT_ID → vynucuji dry_run "
            "pro investiční režim (zprávy se jen zalogují)."
        )
        cfg.dry_run = True

    return cfg


if __name__ == "__main__":
    import sys

    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = load_investment_config()
    print("Načtený investment config:")
    for k, v in cfg.__dict__.items():
        shown = "***" if "token" in k and v else v
        print(f"  {k:34}= {shown!r}")
    print("\n✅ investment/config.py OK")
