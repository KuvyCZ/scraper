"""
main.py — orchestrace jednoho běhu (spouští cron každé 2–4 h).

Tok:
  pro každý aktivní zdroj:
    fetch() -> Listing[]            (Sreality: search + detail jen pro NEznámé)
  pro každý inzerát:
    upsert do DB                    (seen_first/last, price_history)
    doplň kompletní data z DB       (známé inzeráty mají detail uložený)
    spočítej skóre                  (loguje se u VŠECH, i pod prahem)
    nad prahem + (nový | zlevnil)  -> Telegram (ať bot nezahltí re-inzercí)
  heartbeat                         (jednou za běh: kolik zpracováno / nad prahem)

Robustnost: pád celého zdroje i jednoho inzerátu je odchycen a zalogován —
běh pokračuje dál (nedostupný portál ani vadný inzerát nesmí shodit zbytek).
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timezone

import comps
from config import Config, load_config
from database import Database
from notifier import TelegramNotifier
from scoring import score_listing
from sources.base import Listing, Source
from sources.bezrealitky import BezrealitkySource
from sources.idnes import IdnesSource
from sources.sreality import SrealitySource

log = logging.getLogger("main")


def _setup_logging() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # Windows cp1250 by padal na diakritice/emoji
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # httpx loguje každý request na INFO -> při stovkách detailů jen šum.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # Volitelný zápis do souboru (LOG_FILE) – hodí se na NAS i pro sledování běhu.
    log_file = os.environ.get("LOG_FILE")
    if log_file:
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-7s  %(name)s  %(message)s", "%Y-%m-%d %H:%M:%S"))
        logging.getLogger().addHandler(handler)


def _days_on_market(seen_first_iso: str) -> int:
    try:
        seen = datetime.fromisoformat(seen_first_iso)
        return max(0, (datetime.now(timezone.utc) - seen).days)
    except (ValueError, TypeError):
        return 0


def build_source(name: str, config: Config, db: Database) -> Source:
    """Sestaví zdroj. Předá known_source_ids -> detail se tahá jen pro nové inzeráty."""
    if name == "sreality":
        known = db.known_source_ids("sreality")
        log.info("Sreality: %d známých inzerátů v DB (jim se detail nepřetahuje)", len(known))
        return SrealitySource(**config.sreality_kwargs(known_source_ids=known))
    if name == "bezrealitky":
        return BezrealitkySource(**config.bezrealitky_kwargs())
    if name == "idnes":
        known = db.known_source_ids("idnes")
        log.info("iDnes: %d známých inzerátů v DB (jim se detail nepřetahuje)", len(known))
        return IdnesSource(**config.idnes_kwargs(known_source_ids=known))
    raise ValueError(f"Neznámý zdroj: {name!r}")


def score_and_notify(
    db: Database, notifier: TelegramNotifier, config: Config, fetched: Listing, upsert
) -> tuple[bool, bool]:
    """Oskóruje (proti hotovému snímku trhu) a případně notifikuje. Vrací (nad_prahem, odeslano)."""
    # kompletní data (u známých inzerátů doplní detail uložený dřív)
    row = db.get_listing_row(fetched.source, fetched.source_id)
    full = db.row_to_listing(row)

    score_res = score_listing(
        full,
        district_samples=db.price_per_m2_values(
            district=full.district, disposition=full.disposition, property_type=full.property_type),
        region_samples=db.price_per_m2_values(
            region=full.region, disposition=full.disposition, property_type=full.property_type),
        price_history=db.price_history(full.source, full.source_id),
        days_on_market=_days_on_market(row["seen_first"]),
    )
    # Srovnatelné v okolí + ekonomika rekonstrukce (jen u toho, co se bude posílat — je to dotaz navíc).
    is_above = score_res.score is not None and score_res.score >= config.score_threshold
    will_notify = is_above and (upsert.is_new or upsert.price_changed)
    comps_result = None
    if will_notify:
        try:
            comps_result = comps.analyze(
                db.conn, full,
                reno_cost_per_m2=config.renovation_cost_per_m2,
                sale_factor=config.sale_factor,
                needs_renovation=comps.needs_renovation(full),
            )
        except Exception as exc:  # noqa: BLE001 – comps jsou bonus, nesmí shodit notifikaci
            log.warning("Comps pro #%s selhaly: %s", full.source_id, exc)

    breakdown = score_res.breakdown()
    if comps_result:
        breakdown["comps"] = comps_result.as_dict()
    db.save_score(full, score_res.score, breakdown)  # log u VŠECH

    notified = False
    # Notifikuj jen nové nebo zlevněné nad prahem -> žádný spam každé 2-4 h.
    if will_notify:
        notified = notifier.notify_listing(full, score_res, comps_result)
        reason = "nový" if upsert.is_new else f"pokles ceny {upsert.old_price}->{upsert.new_price}"
        log.info("NAD PRAHEM #%s skóre=%s (%s) odeslano=%s",
                 full.source_id, score_res.score, reason, notified)
    return is_above, notified


def run(config: Config) -> tuple[int, int, int]:
    log.info("=== Běh startuje | zdroje=%s | regiony=%s | práh=%s | dry_run=%s ===",
             config.active_sources, config.region_ids, config.score_threshold, config.dry_run)
    total = above = sent = 0

    with Database(config.db_path) as db:
        notifier = TelegramNotifier(
            config.telegram_token, config.telegram_chat_id, dry_run=config.dry_run
        )
        for name in config.active_sources:
            try:
                source = build_source(name, config, db)
                listings = source.fetch()
            except Exception as exc:  # noqa: BLE001 – pád zdroje nezhasne ostatní
                log.error("Zdroj %s spadl celý, pokračuji dalším: %s", name, exc)
                continue

            # FÁZE 1: ulož vše -> v DB je kompletní snímek trhu (pro medián cena/m²).
            results: dict[str, object] = {}
            for fetched in listings:
                try:
                    results[fetched.source_id] = db.upsert_listing(fetched)
                except Exception as exc:  # noqa: BLE001 – vadný inzerát přeskoč
                    log.error("Uložení #%s selhalo, přeskakuji: %s", fetched.source_id, exc)

            # FÁZE 2: skóruj + notifikuj proti hotovému snímku (konzistentní pro všechny).
            for fetched in listings:
                upsert = results.get(fetched.source_id)
                if upsert is None:
                    continue
                try:
                    is_above, did_send = score_and_notify(db, notifier, config, fetched, upsert)
                    total += 1
                    above += int(is_above)
                    sent += int(did_send)
                except Exception as exc:  # noqa: BLE001 – vadné skórování přeskoč
                    log.error("Skórování #%s selhalo, přeskakuji: %s", fetched.source_id, exc)

        # Heartbeat max 1x za UTC den (i když smyčka běží každé 2-4 h).
        if config.heartbeat:
            today = datetime.now(timezone.utc).date().isoformat()
            if db.get_meta("last_heartbeat_date") != today:
                notifier.heartbeat(total, above, threshold=config.score_threshold)
                db.set_meta("last_heartbeat_date", today)

    log.info("=== Hotovo | zpracováno=%d | nad prahem=%d | odesláno=%d ===", total, above, sent)
    return total, above, sent


def _send_startup_notice(config: Config) -> None:
    """Po startu procesu (typicky po Buildu/restartu kontejneru) pošle 'nasazeno a běží'.
    Počet inzerátů v DB zároveň prozradí, jestli se předplněná DB správně namountovala."""
    notifier = TelegramNotifier(config.telegram_token, config.telegram_chat_id, dry_run=config.dry_run)
    db_count = None
    try:
        with Database(config.db_path) as db:
            db_count = db.count_listings()
    except Exception as exc:  # noqa: BLE001 – diagnostika nesmí shodit start
        log.warning("Startup notice: čtení počtu z DB selhalo: %s", exc)
    notifier.startup(config.active_sources, threshold=config.score_threshold,
                     interval_hours=config.interval_hours, db_count=db_count)


def main() -> None:
    _setup_logging()
    config = load_config()

    if config.startup_notice:
        _send_startup_notice(config)

    # Režim smyčky (INTERVAL_HOURS>0): kontejner běží pořád a scrapuje sám.
    # Vhodné pro Synology Container Manager (Projekt jako stálá služba) –
    # není potřeba Task Scheduler. Jednorázový běh (INTERVAL_HOURS=0) je pro cron.
    if config.interval_hours > 0:
        log.info("Režim smyčky: běh každých %s h", config.interval_hours)
        while True:
            try:
                run(config)
            except Exception as exc:  # noqa: BLE001 – smyčka přežije i pád celého běhu
                log.exception("Běh selhal, pokračuji další iterací: %s", exc)
            log.info("Spím %s h do dalšího běhu…", config.interval_hours)
            time.sleep(config.interval_hours * 3600)
    else:
        run(config)


if __name__ == "__main__":
    main()
