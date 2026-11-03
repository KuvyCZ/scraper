"""main.py — hledač flipů: byty a domy ve větších obcích Olomouckého kraje → Telegram.

Použití:
  python main.py            běží pořád, každý zdroj ve vlastním intervalu (výchozí, Docker)
  python main.py once       jeden průchod všemi zdroji a konec (cron / Task Scheduler)
  python main.py check      ověří každý zdroj proti živému webu: kolik najde, v jakých obcích;
                            nic neukládá a nic neposílá (použij po nasazení nebo když zdroj hlásí chyby)
  python main.py top        vypíše nejlepší aktuální nabídky z DB
  python main.py latest [N] nejnovějších N inzerátů znovu oskórovaných proti dnešnímu trhu
  python main.py drops [D]  největší zlevnění za posledních D dní (výchozí 7)
  python main.py stale [D]  nabídky online aspoň D dní (výchozí 60), nejvíc zlevněné první
  python main.py best [N]   nejlepších N nabídek, které jsou pořád online (oskórované dnes)
  python main.py find 1234  nabídka #1234 + hotové a podobné v okolí
  python main.py towns      typická cena za m² v každé sledované obci (z uložených inzerátů)
  python main.py report     pošle tabulku cen po obcích na Telegram hned (jinak chodí sama 1× týdně)
  python main.py test-telegram   pošle zkušební zprávu (ověření tokenu a chat_id)

Konfigurace: .env (viz .env.example). Bez TELEGRAM_BOT_TOKEN/CHAT_ID se jen loguje.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from collections import Counter

from flip import export, insights, latest, lookup
from flip.bot import Bot
from flip.config import FlipConfig, load_config
from flip.market import town_averages
from flip.notifier import Notifier, czk, czk_short
from flip.favcheck import check_favorites
from flip.pipeline import run_source
from flip.report import report_due, send_town_report
from flip.sources import DISPLAY_NAMES, SOURCES
from flip.store import Store
from flip.towns import watched_towns

log = logging.getLogger("flip.main")


def setup_logging() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    fmt = "%(asctime)s  %(levelname)-7s  %(name)s  %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt, datefmt="%Y-%m-%d %H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if log_file := os.environ.get("LOG_FILE"):
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setFormatter(logging.Formatter(fmt, "%Y-%m-%d %H:%M:%S"))
        logging.getLogger().addHandler(handler)


def run_forever(cfg: FlipConfig, notifier: Notifier) -> None:
    sources = cfg.active_sources
    intervals = {s: cfg.interval_minutes(s) * 60 for s in sources}
    next_due = {s: 0.0 for s in sources}
    bot = Bot(cfg, notifier)
    export.register(bot)            # /data, /status, /help v Telegramu
    latest.register(bot)            # /latest
    insights.register(bot)          # /drops, /stale
    lookup.register(bot)            # /find, /fav, /unfav, /favs, /best, tlačítko ⭐ Save
    export.announce_update(cfg, notifier)
    bot.start_background()          # odpovídá do pár sekund, i během dlouhého běhu zdroje
    log.info("Startuji | zdroje=%s | obce=%s | rozpočet=%s | dry_run=%s",
             sources, watched_towns(), czk(cfg.price_max), cfg.dry_run)
    while True:
        now = time.monotonic()
        due = [s for s in sources if next_due[s] <= now]
        for source in due:
            try:
                with Store(cfg.db_path) as store:
                    run_source(source, cfg, notifier, store)
            except Exception:  # noqa: BLE001 – smyčka musí přežít cokoliv
                log.exception("%s: neočekávaná chyba, pokračuji", source)
            next_due[source] = float("inf") if intervals[source] <= 0 else time.monotonic() + intervals[source]
        if due:
            try:
                with Store(cfg.db_path) as store:
                    if report_due(store, cfg):
                        send_town_report(store, cfg, notifier)
            except Exception:  # noqa: BLE001 – tabulka nesmí shodit sledování
                log.exception("týdenní tabulka selhala, pokračuji")
            try:
                with Store(cfg.db_path) as store:
                    check_favorites(store, notifier, cfg)
            except Exception:  # noqa: BLE001 – kontrola oblíbených nesmí shodit sledování
                log.exception("kontrola oblíbených selhala, pokračuji")
        if all(v == float("inf") for v in next_due.values()):
            return
        if not due:
            time.sleep(max(1.0, min(next_due.values()) - time.monotonic()))


def run_once(cfg: FlipConfig, notifier: Notifier) -> int:
    failed = 0
    with Store(cfg.db_path) as store:
        for source in cfg.active_sources:
            stats = run_source(source, cfg, notifier, store)
            failed += stats.error is not None
        try:
            check_favorites(store, notifier, cfg)
        except Exception:  # noqa: BLE001
            log.exception("kontrola oblíbených selhala")
    return 1 if failed == len(cfg.active_sources) else 0


def check(cfg: FlipConfig) -> int:
    """Živé ověření zdrojů do dočasné paměťové DB — nic neukládá, nic neposílá."""
    cfg.fetch_details = False   # jen seznamy: pár minut místo ~20, na ověření webu to stačí
    print(f"Checking sources against live sites ({len(watched_towns())} towns in Olomoucký kraj) …")
    print("Quick mode: listing pages only, no detail pages. Takes a few minutes in total.\n")
    all_ok = True
    for source in cfg.active_sources:
        print(f"== {DISPLAY_NAMES[source]} ==")
        started = time.monotonic()
        try:
            result = SOURCES[source].fetch_listings(cfg, set())
        except Exception as exc:  # noqa: BLE001
            print(f"   ❌ FAILED: {exc}\n")
            all_ok = False
            continue
        took = time.monotonic() - started
        in_area = [x for x in result.listings if x.in_area]
        ok = result.raw_count > 0
        all_ok &= ok
        print(f"   {'✅' if ok else '❌'} parsed {result.raw_count} listings from the site in {took:.0f}s; "
              f"{len(in_area)} in your towns")
        by = Counter((x.town, x.kind) for x in in_area)
        for (town, kind), n in sorted(by.items()):
            print(f"      {town:20} {'flats ' if kind == 'byt' else 'houses'} {n}")
        for kind in ("byt", "dum"):
            if cfg.wants_kind(kind) and not any(x.kind == kind for x in result.listings):
                print(f"   ⚠️  no {'flats' if kind == 'byt' else 'houses'} at all — category path may be wrong")
        missing = sum(1 for x in in_area if x.price is None or x.area_m2 is None)
        if missing:
            print(f"   ℹ️  {missing} listings without price or size (shown anyway, marked)")
        for x in in_area[:3]:
            print(f"      e.g. {x.kind} {x.subtype or ''} {x.area_m2 or '?'} m² {czk(x.price)} {x.town} — {x.url}")
        print()
    print("All sources OK ✅" if all_ok else "Some sources need attention ❌ (see above)")
    return 0 if all_ok else 1


def _budget_ok(row, cfg: FlipConfig) -> bool:
    """Jako pipeline.within_budget, ale z uloženého řádku."""
    if not (row["price"] and row["price"] > cfg.price_max):
        return True
    median = ((json.loads(row["last_breakdown"] or "{}").get("market") or {}).get("median_ppm2"))
    if not (median and row["area_m2"] and cfg.over_budget_discount_pct > 0):
        return False
    return row["price"] / row["area_m2"] <= median * (1 - cfg.over_budget_discount_pct / 100)


def top(cfg: FlipConfig, limit: int = 20) -> int:
    with Store(cfg.db_path) as store:
        rows = store.conn.execute(
            """SELECT * FROM listings WHERE town IS NOT NULL AND dup_of IS NULL AND last_score IS NOT NULL
               ORDER BY last_seen DESC, last_score DESC"""
        ).fetchall()
    if not rows:
        print("No scored listings yet — run `python main.py once` first.")
        return 0
    latest = max(r["last_seen"] for r in rows)[:10]
    current = [r for r in rows if r["last_seen"][:10] >= latest      # viděné v posledním dni běhu
               and _budget_ok(r, cfg)]
    current.sort(key=lambda r: r["last_score"], reverse=True)
    for r in current[:limit]:
        bd = json.loads(r["last_breakdown"] or "{}")
        est = bd.get("estimate") or {}
        flip = f" · flip {czk_short(est['margin'])}" if est else ""
        hot = "🔥" if bd.get("hot") else "  "
        print(f"{hot} {r['last_score']:>3} · {r['kind']} {r['subtype'] or ''} · {r['town']} · "
              f"{czk(r['price'])} · {r['area_m2'] or '?'} m²{flip}\n      {r['url']}")
    return 0


def towns(cfg: FlipConfig) -> int:
    """Průměr (medián) Kč/m² pro každou obec — s čím se inzeráty srovnávají."""
    with Store(cfg.db_path) as store:
        rows = store.market_rows(kind="byt", window_days=cfg.market_window_days, in_area_only=True)
        rows += store.market_rows(kind="dum", window_days=cfg.market_window_days, in_area_only=True)
    averages = town_averages(rows)
    if not averages:
        print("Not enough listings yet — let it run a few hours first.")
        return 0
    for kind, title in (("byt", "FLATS"), ("dum", "HOUSES")):
        items = [a for a in averages if a.kind == kind]
        if not items:
            continue
        print(f"\n{title} — typical asking price per m² (last {cfg.market_window_days} days)")
        print(f"   {'town':20} {'listings':>8} {'per m²':>12} {'renovated':>12} {'to renovate':>12}")
        for a in items:
            reno = czk(a.renovated_ppm2) if a.renovated_ppm2 else "–"
            raw = czk(a.needs_reno_ppm2) if a.needs_reno_ppm2 else "–"
            print(f"   {a.town:20} {a.n:>8} {czk(a.median_ppm2):>12} {reno:>12} {raw:>12}")
    print("\nTowns with fewer than 3 listings are left out. These are asking prices, not sale prices.")
    return 0


def main(argv: list[str]) -> int:
    setup_logging()
    cfg = load_config()
    command = argv[1] if len(argv) > 1 else "run"
    if command == "check":
        return check(cfg)
    if command == "top":
        return top(cfg)
    if command == "latest":
        n = int(argv[2]) if len(argv) > 2 and argv[2].isdigit() else latest.DEFAULT_N
        with Store(cfg.db_path) as store:
            text = latest.format_latest(latest.latest_rescored(store, cfg, n), cfg)
        print(re.sub(r"<[^>]+>", "", text))
        return 0
    if command in ("drops", "stale"):
        default = insights.DROPS_DEFAULT_DAYS if command == "drops" else insights.STALE_DEFAULT_DAYS
        days = int(argv[2]) if len(argv) > 2 and argv[2].isdigit() else default
        with Store(cfg.db_path) as store:
            text = (insights.drops_text if command == "drops" else insights.stale_text)(store, cfg, days)
        print(re.sub(r"<[^>]+>", "", text))
        return 0
    if command in ("best", "find"):
        arg = int(argv[2].lstrip("#")) if len(argv) > 2 and argv[2].lstrip("#").isdigit() else None
        if command == "find" and arg is None:
            print("usage: python main.py find 1234")
            return 2
        with Store(cfg.db_path) as store:
            text = (lookup.best_text(store, cfg, arg or lookup.BEST_DEFAULT) if command == "best"
                    else lookup.find_text(store, cfg, arg))
        print(re.sub(r"<[^>]+>", "", text))
        return 0
    if command == "towns":
        return towns(cfg)
    notifier = Notifier(cfg.telegram_token, cfg.telegram_chat_id, dry_run=cfg.dry_run)
    if command == "test-telegram":
        ok = notifier.send("✅ Flip scraper can reach this chat.")
        print("sent ✅" if ok else "failed ❌ (check TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
        return 0 if ok else 1
    if command == "report":
        with Store(cfg.db_path) as store:
            ok = send_town_report(store, cfg, notifier)
        print("sent ✅" if ok else "nothing sent (not enough listings yet, or Telegram failed)")
        return 0 if ok else 1
    if command == "once":
        return run_once(cfg, notifier)
    if command == "run":
        run_forever(cfg, notifier)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
