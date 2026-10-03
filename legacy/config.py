"""
config.py — veškerá konfigurace přes prostředí / .env (žádné hardcoded hodnoty).

Načítá .env (pokud existuje) do prostředí a sestaví `Config`. Vlastní mini-parser
.env, ať nepřibývá závislost (python-dotenv) — NAS má málo paměti.

Tajné věci (token, chat_id) patří JEN do .env (v gitu ignorováno). .env.example
dokumentuje všechny proměnné.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger("config")


def load_dotenv(path: str = ".env") -> None:
    """Načte KEY=VALUE z .env do os.environ (neprepisuje už nastavené proměnné).

    Podporuje inline komentář za hodnotou (mezera + '#') — stejně jako Docker
    `env_file`. Bez toho by .env zkopírovaný z .env.example (komentáře za hodnotami)
    fungoval v kontejneru, ale spadl při lokálním běhu. Uvozená hodnota se bere celá.
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
            if value[:1] in ("'", '"'):           # uvozená hodnota: vezmi obsah uvozovek
                quote = value[0]
                end = value.find(quote, 1)
                value = value[1:end] if end != -1 else value[1:]
            else:                                  # neuvozená: utni inline komentář ' #...'
                hash_pos = value.find(" #")
                if hash_pos != -1:
                    value = value[:hash_pos]
                value = value.strip()
            os.environ.setdefault(key, value)


# --- typové parsery proměnných prostředí --- #

def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _get_bool(name: str, default: bool = False) -> bool:
    return _get(name, str(default)).lower() in ("1", "true", "yes", "ano", "on")


def _get_int(name: str, default: int) -> int:
    try:
        return int(_get(name, str(default)))
    except ValueError:
        return default


def _get_float(name: str, default: float) -> float:
    try:
        return float(_get(name, str(default)))
    except ValueError:
        return default


def _get_int_opt(name: str) -> int | None:
    value = _get(name).strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _get_list_int(name: str, default: list[int]) -> list[int]:
    value = _get(name).strip()
    if not value:
        return list(default)
    return [int(x) for x in value.replace(";", ",").split(",") if x.strip()]


def _get_list_str(name: str, default: list[str]) -> list[str]:
    value = _get(name).strip()
    if not value:
        return list(default)
    return [x.strip() for x in value.replace(";", ",").split(",") if x.strip()]


@dataclass
class Config:
    # --- Telegram --- #
    telegram_token: str = ""
    telegram_chat_id: str = ""
    dry_run: bool = False          # True = zprávy se jen logují, neodesílají
    heartbeat: bool = True         # poslat denní heartbeat
    startup_notice: bool = True    # po startu (build/restart kontejneru) poslat "nasazeno a běží"
    interval_hours: float = 0.0    # 0 = jednorázový běh; >0 = smyčka každých N h (Container Manager)

    # --- scoring / notifikace --- #
    score_threshold: int = 70      # posílej jen nad prahem (start vysoko, pak sniž)

    # --- zdroje a geografie --- #
    active_sources: list[str] = field(default_factory=lambda: ["sreality"])
    region_ids: list[int] = field(default_factory=lambda: [8])  # 8 = Olomoucký kraj (Sreality)
    bezrealitky_region_ids: list[int] = field(default_factory=lambda: [495])  # 495 = Olomoucký kraj (Bezrealitky)
    idnes_region_slugs: list[str] = field(default_factory=lambda: ["olomoucky-kraj"])  # slug kraje v URL (iDnes)

    # --- srovnávací analýza (comps) --- #
    renovation_cost_per_m2: int = 20_000  # hrubý odhad Kč/m² celkové rekonstrukce
    sale_factor: float = 0.92             # nabídková -> očekávaná realizovaná cena
    comps_radius_km: float = 15.0         # okruh "blízkého okolí" pro srovnatelné

    # --- Sreality scraper --- #
    category_main_cb: list[int] = field(default_factory=lambda: [1])  # 1 = Byty, 2 = Domy
    category_type_cb: int = 1      # 1 = Prodej
    page_size: int = 100
    max_listings: int | None = None   # strop inzerátů/region (None = vše); hl. pro testy
    detail_limit: int | None = None   # strop detailů/běh (None = bez stropu)
    rate_limit_s: float = 1.6
    user_agent: str = "realitni-scraper/0.1 (osobni nekomercni projekt)"

    # --- úložiště --- #
    db_path: str = "realitky.db"

    def sreality_kwargs(self, known_source_ids: set[str] | None = None) -> dict:
        """Kwargs pro SrealitySource (drží config na jednom místě)."""
        return {
            "region_ids": self.region_ids,
            "category_main_cb": self.category_main_cb,
            "category_type_cb": self.category_type_cb,
            "page_size": self.page_size,
            "max_listings": self.max_listings,
            "detail_limit": self.detail_limit,
            "known_source_ids": known_source_ids,
            "rate_limit_s": self.rate_limit_s,
            "user_agent": self.user_agent,
        }

    def bezrealitky_kwargs(self) -> dict:
        """Kwargs pro BezrealitkySource (popis i stav jsou v seznamu -> žádné detail cally)."""
        return {
            "region_ids": self.bezrealitky_region_ids,
            "page_size": self.page_size,
            "max_listings": self.max_listings,
            "rate_limit_s": self.rate_limit_s,
            "user_agent": self.user_agent,
        }

    def idnes_kwargs(self, known_source_ids: set[str] | None = None) -> dict:
        """Kwargs pro IdnesSource (stav/popis jen v detailu -> detail jen pro nové, jako Sreality)."""
        return {
            "region_slugs": self.idnes_region_slugs,
            "max_listings": self.max_listings,
            "detail_limit": self.detail_limit,
            "known_source_ids": known_source_ids,
            "rate_limit_s": self.rate_limit_s,
            "user_agent": self.user_agent,
        }


def load_config(env_file: str = ".env") -> Config:
    """Načte .env a sestaví Config. Bez tokenu/chat_id vynutí dry_run (a varuje)."""
    load_dotenv(env_file)

    cfg = Config(
        telegram_token=_get("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_get("TELEGRAM_CHAT_ID"),
        dry_run=_get_bool("DRY_RUN", False),
        heartbeat=_get_bool("HEARTBEAT", True),
        startup_notice=_get_bool("STARTUP_NOTICE", True),
        interval_hours=float(_get("INTERVAL_HOURS", "0")),
        score_threshold=_get_int("SCORE_THRESHOLD", 70),
        active_sources=_get_list_str("ACTIVE_SOURCES", ["sreality"]),
        region_ids=_get_list_int("REGION_IDS", [8]),
        bezrealitky_region_ids=_get_list_int("BEZREALITKY_REGION_IDS", [495]),
        idnes_region_slugs=_get_list_str("IDNES_REGION_SLUGS", ["olomoucky-kraj"]),
        category_main_cb=_get_list_int("CATEGORY_MAIN_CB", [1]),
        renovation_cost_per_m2=_get_int("RENOVATION_COST_PER_M2", 20_000),
        sale_factor=float(_get("SALE_FACTOR", "0.92")),
        comps_radius_km=float(_get("COMPS_RADIUS_KM", "15")),
        category_type_cb=_get_int("CATEGORY_TYPE_CB", 1),
        page_size=_get_int("PAGE_SIZE", 100),
        max_listings=_get_int_opt("MAX_LISTINGS"),
        detail_limit=_get_int_opt("DETAIL_LIMIT"),
        rate_limit_s=float(_get("RATE_LIMIT_S", "1.6")),
        user_agent=_get("USER_AGENT", "realitni-scraper/0.1 (osobni nekomercni projekt)"),
        db_path=_get("DB_PATH", "realitky.db"),
    )

    if not cfg.dry_run and not (cfg.telegram_token and cfg.telegram_chat_id):
        log.warning("Chybí TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID → vynucuji dry_run "
                    "(zprávy se jen zalogují). Doplň .env pro reálné odesílání.")
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
    cfg = load_config()
    print("Načtený config:")
    for k, v in cfg.__dict__.items():
        shown = "***" if "token" in k and v else v
        print(f"  {k:18}= {shown!r}")
    print("\n✅ config.py OK")
