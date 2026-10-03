"""investment_main.py — orchestrace investičního režimu (Šumperk), 4 nezávislé zdroje.

Samostatný proces vedle main.py — jiná DB (investment.db), jiný Telegram chat
(TELEGRAM_INVESTMENT_CHAT_ID), jiné (a navzájem NEZÁVISLÉ) intervaly per zdroj.
main.py / notifier.py / database.py se tímto souborem nijak nemění a neimportují.

Zdroje: Sreality (JSON API), Realingo, Bazoš, reality-sumperk.cz (HTML) —
každý jako samostatný modul v investment/sources/*_source.py s jednotným
rozhraním `fetch_listings(config, known_ids) -> list[Listing]`. Pád JEDNOHO
zdroje (síť, redesign webu) se odchytí a zaloguje, běh pokračuje ostatními
zdroji dál. Po `source_failure_alert_threshold` selháních V KUSE přijde JEDNO
Telegram varování (ne za každý běh) — reset počítadla při dalším úspěchu.

Plánování: KAŽDÝ zdroj má vlastní interval (config.poll_interval_minutes).
Smyčka spí do nejbližšího dalšího "due" zdroje, ne jeden společný interval.

Tok pro jeden zdroj v jednom běhu:
  1. fetch_listings() — zdroj sám aplikuje tvrdé strukturální filtry (lokalita/
     velikost/dispozice/cena) a pro NOVÉ kandidáty dotáhne popis (detail).
  2. cross-source dedup proti aktuálně známému stavu OSTATNÍCH zdrojů v DB
     (investment/dedup.py:is_duplicate_pair) — nezávislé plánování = nesrovnává
     se jen dávka z tohoto běhu, ale živý stav napříč zdroji. Kandidát se
     PŘESKOČÍ, když (a) existuje levnější duplikát odjinud, NEBO (b) libovolný
     nalezený duplikát UŽ byl dřív alertnutý — samotné "jsem nejlevnější" nestačí,
     protože duplikát mohl být objeven a alertnutý dřívějším (nezávislým) během
     jiného zdroje předtím, než tenhle zdroj vůbec stihl doběhnout.
  3. Jinak: ulož snímek (rolling průměr), dopočti výnos + skóre (VŽDY se loguje,
     i pod prahem), nad prahem + ještě neodesláno -> Telegram (dedup přes
     investment_alerts).
"""

from __future__ import annotations

import logging
import os
import sys
import time

from investment.candidate import build_candidate, detect_renovation_flag
from investment.config import InvestmentConfig, load_investment_config
from investment.dedup import is_duplicate_pair
from investment.notifier import InvestmentNotifier
from investment.sources import bazos_source, realingo_source, sreality_source, sumperk_source
from investment.sources.base import Listing
from investment.store import InvestmentListingSnapshot, InvestmentStore

log = logging.getLogger("investment.main")

SOURCE_MODULES = {
    "sreality": sreality_source,
    "realingo": realingo_source,
    "bazos": bazos_source,
    "reality_sumperk": sumperk_source,
}


def _setup_logging() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    log_file = os.environ.get("INVESTMENT_LOG_FILE")
    if log_file:
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-7s  %(name)s  %(message)s", "%Y-%m-%d %H:%M:%S"))
        logging.getLogger().addHandler(handler)


def _row_to_lightweight_listing(row) -> Listing:
    """Minimální Listing z uloženého řádku — jen pro účely dedup porovnání
    (cena/velikost/dispozice/lokalita), ne pro plné zpracování."""
    return Listing(
        source=row["source"], external_id=row["external_id"], url=row["url"],
        price=row["price"], size_m2=row["size_m2"], layout=row["layout"], location=row["location"],
    )


def _resolve_renovation_flag(listing: Listing, store: InvestmentStore, config: InvestmentConfig) -> bool:
    """Popis se tahá jen pro NOVÉ kandidáty (viz zdrojové moduly) — u známých bez
    čerstvého popisu se recykluje dřív uložený renovation_flag, ať ho prázdný
    refetch neprodpíše na False (stejná COALESCE filozofie jako database.py)."""
    if listing.description is not None:
        return detect_renovation_flag(listing.description, config.condition_keywords)
    stored = store.get_listing(listing.source, listing.external_id)
    return bool(stored["renovation_flag"]) if stored is not None else False


def process_candidate(
    listing: Listing, *, store: InvestmentStore, notifier: InvestmentNotifier, config: InvestmentConfig
) -> tuple[int | None, bool]:
    """Zpracuje JEDEN kandidáta, co už prošel strukturálními filtry (uvnitř
    zdrojového modulu). Vrací (skóre, odesláno)."""
    renovation_flag = _resolve_renovation_flag(listing, store, config)

    store.upsert_listing(InvestmentListingSnapshot(
        source=listing.source, external_id=listing.external_id, url=listing.url,
        price=listing.price, size_m2=listing.size_m2, layout=listing.layout, location=listing.location,
        building_condition=listing.building_condition, renovation_flag=renovation_flag,
        price_per_m2=listing.price_per_m2, parse_confidence=listing.parse_confidence,
    ))

    # cross-source dedup proti ŽIVÉMU stavu ostatních zdrojů (ne jen dávce z tohoto běhu).
    # Nezávislé plánování zdrojů znamená, že duplikát stejného bytu se může objevit
    # AŽ V POZDĚJŠÍM běhu jiného zdroje, kdy už byl dřív alertnutý — "nejlevnější
    # verze" podle ceny NESTAČÍ (viz test: Sreality alertla byt v Maletíně, o běh
    # později ho reality-sumperk.cz našla za stejnou cenu a bez týhle kontroly by
    # ho alertla ZNOVU). Proto: pokud NĚKTERÝ shodný duplikát už byl alertnutý
    # (z libovolného zdroje), tenhle kandidát se NEALERTUJE, i kdyby byl "nejlevnější".
    others = store.find_cross_source_matches(exclude_source=listing.source, layout=listing.layout, location=listing.location)
    cluster = [r for r in others if is_duplicate_pair(listing, _row_to_lightweight_listing(r), config)]
    if cluster:
        listing.duplicate_sources = [(r["source"], r["url"]) for r in cluster]
        already_alerted_elsewhere = any(store.already_alerted(r["source"], r["external_id"]) for r in cluster)
        cheapest_other = min((r["price"] for r in cluster if r["price"] is not None), default=None)
        is_cheapest = listing.price is not None and (cheapest_other is None or listing.price <= cheapest_other)
        if already_alerted_elsewhere or not is_cheapest:
            reason = "už byl alertnutý odjinud" if already_alerted_elsewhere else "existuje levnější duplikát odjinud"
            log.info(
                "Kandidát #%s (%s) je cross-source duplikát (%s) — přeskakuji notifikaci",
                listing.external_id, listing.source, reason,
            )
            return None, False

    avg_ppm2, sample_n = store.rolling_avg_price_per_m2(location=listing.location, layout=listing.layout, n=config.avg_window_n)
    if sample_n < config.avg_min_sample:
        avg_ppm2 = None  # cold-start — málo srovnatelných dat, nehádáme průměr

    candidate = build_candidate(listing, config=config, avg_price_per_m2=avg_ppm2, renovation_flag=renovation_flag)

    store.save_score(
        source=candidate.source, external_id=candidate.external_id, score=candidate.score,
        price_per_m2=candidate.price_per_m2, avg_price_per_m2=candidate.avg_price_per_m2,
        gross_yield_pct=candidate.gross_yield_pct, estimated_rent=candidate.estimated_rent,
        estimated_mortgage=candidate.estimated_mortgage, net_monthly_cashflow=candidate.net_monthly_cashflow,
        renovation_flag=candidate.renovation_flag, duplicate_sources=candidate.duplicate_sources,
    )

    sent = False
    if (
        candidate.score is not None
        and candidate.score >= config.score_threshold
        and not store.already_alerted(candidate.source, candidate.external_id)
    ):
        sent = notifier.notify_candidate(candidate)
        log.info("KANDIDÁT #%s (%s) skóre=%s odesláno=%s", candidate.external_id, candidate.source, candidate.score, sent)
        if sent:
            store.record_alert(candidate.source, candidate.external_id, candidate.score)

    return candidate.score, sent


def run_source(source_name: str, config: InvestmentConfig, notifier: InvestmentNotifier) -> None:
    """Jeden běh JEDNOHO zdroje — vlastní try/except, ať pád nezastaví ostatní
    zdroje ani smyčku. Otevírá si vlastní DB spojení (krátce, per-běh)."""
    module = SOURCE_MODULES[source_name]
    log.info("=== %s: běh startuje ===", source_name)

    with InvestmentStore(config.db_path) as store:
        try:
            known = store.known_source_ids(source_name)
            listings = module.fetch_listings(config, known)
        except Exception as exc:  # noqa: BLE001 – pád JEDNOHO zdroje nesmí shodit ostatní
            failures = store.record_source_failure(source_name, str(exc))
            log.error("%s: běh selhal (%d× v kuse): %s", source_name, failures, exc)
            if store.should_send_failure_warning(source_name, config.source_failure_alert_threshold):
                notifier.notify_source_failure(source_name, failures, str(exc))
                store.mark_failure_warning_sent(source_name)
            return

        store.record_source_success(source_name)

        evaluated = sent_count = 0
        for listing in listings:
            try:
                score, sent = process_candidate(listing, store=store, notifier=notifier, config=config)
                if score is not None:
                    evaluated += 1
                sent_count += int(sent)
            except Exception as exc:  # noqa: BLE001 – vadný kandidát přeskoč, zdroj pokračuje
                log.error("%s: zpracování kandidáta #%s selhalo, přeskakuji: %s", source_name, listing.external_id, exc)

    log.info("=== %s: běh hotov | vyhodnoceno=%d | odesláno=%d ===", source_name, evaluated, sent_count)


def main() -> None:
    _setup_logging()
    config = load_investment_config()

    if not config.enabled:
        log.info("INVESTMENT_ENABLED=false — investiční režim vypnut, končím bez zásahu do hlavního monitoringu.")
        return

    active = [name for name in SOURCE_MODULES if name in config.active_sources]
    if not active:
        log.warning("INVESTMENT_ACTIVE_SOURCES neobsahuje žádný známý zdroj — končím.")
        return

    notifier = InvestmentNotifier(config.telegram_token, config.telegram_chat_id, dry_run=config.dry_run)
    log.info("Investiční režim startuje | zdroje=%s | dry_run=%s", active, config.dry_run)

    intervals_s = {name: config.poll_interval_minutes(name) * 60 for name in active}
    next_due = {name: 0.0 for name in active}  # 0 = spustit hned při prvním běhu

    while True:
        now = time.monotonic()
        due = [name for name in active if next_due[name] <= now]
        if due:
            for name in due:
                try:
                    run_source(name, config, notifier)
                except Exception as exc:  # noqa: BLE001 – smyčka přežije i neočekávaný pád jednoho běhu
                    log.exception("%s: neočekávaný pád mimo run_source, pokračuji: %s", name, exc)
                next_due[name] = float("inf") if intervals_s[name] <= 0 else time.monotonic() + intervals_s[name]
            if all(v == float("inf") for v in next_due.values()):
                log.info("Všechny zdroje mají INTERVAL=0 (jednorázový běh) — hotovo, končím.")
                return
        else:
            sleep_for = max(1.0, min(next_due.values()) - time.monotonic())
            log.info("Spím %.0f s do dalšího due zdroje…", sleep_for)
            time.sleep(sleep_for)


if __name__ == "__main__":
    main()
