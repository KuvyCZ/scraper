"""flip/pipeline.py — jeden běh jednoho zdroje: stáhnout → uložit → deduplikovat → skórovat → poslat.

Pravidla notifikací:
  * NOVÁ nemovitost ve sledované obci a v rozpočtu -> zpráva vždy (🏠), silná 🔥.
  * Stejná nemovitost z jiného portálu -> nic (už odešla), LEDAŽE je tam levnější
    o >= PRICE_DROP_ALERT_PCT -> 📉.
  * Zlevnění už oznámené nemovitosti o >= PRICE_DROP_ALERT_PCT -> 📉.
  * PRVNÍ běh zdroje (prázdná DB) -> místo desítek zpráv jedno shrnutí s top N;
    ostatní se tiše uloží a ozvou se až při zlevnění.
  * Zdroj, který nic nerozparsuje (0 inzerátů), se počítá jako selhání — nejspíš
    změna webu; po N selháních v kuse přijde jedno varování.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from flip.config import FlipConfig
from flip.dedup import is_duplicate
from flip.market import MarketData, analyze
from flip.model import FetchResult, Listing
from flip.notifier import Notifier, format_drop, format_new, format_summary_line
from flip.scoring import ScoreResult, score_listing
from flip.sources import DISPLAY_NAMES, SOURCES
from flip.store import Store, cluster_key

log = logging.getLogger("flip.pipeline")

HOT_DETAIL_ON_BASELINE = 5


@dataclass
class RunStats:
    source: str
    fetched: int = 0
    in_area: int = 0
    eligible: int = 0
    new_sent: int = 0
    drops_sent: int = 0
    duplicates: int = 0
    baseline: bool = False
    error: str | None = None
    scored: list[tuple[Listing, ScoreResult]] = field(default_factory=list)


def eligible(lst: Listing, cfg: FlipConfig) -> bool:
    """Má se o tomhle inzerátu vůbec posílat zpráva?"""
    return (
        lst.in_area
        and cfg.wants_kind(lst.kind)
        and cfg.area_ok(lst.area_m2)
        and (lst.price is None or lst.price <= cfg.price_max)
    )


def _link_duplicate(store: Store, lst: Listing) -> None:
    """Nový inzerát ve sledované obci: je to nemovitost, kterou už máme z jiného portálu?"""
    row = store.get(*lst.key)
    if row is None or row["dup_of"]:
        return
    for cand in store.dedup_candidates(lst):
        if is_duplicate(lst, cand):
            canonical = cluster_key(cand["source"], cand["external_id"])
            store.set_dup_of(lst.source, lst.external_id, canonical)
            log.info("%s #%s = stejná nemovitost jako %s", lst.source, lst.external_id, canonical)
            return


def _score(store: Store, lst: Listing, markets: dict[str, MarketData], cfg: FlipConfig) -> tuple[str, ScoreResult]:
    key = store.cluster_of(*lst.key)
    members = store.cluster_members(key)
    cluster = {(m["source"], m["external_id"]) for m in members}
    lst.duplicate_sources = [(m["source"], m["url"]) for m in members if (m["source"], m["external_id"]) != lst.key]
    first_seen = min((m["first_seen"] for m in members), default=None)
    view = analyze(lst, markets[lst.kind], min_comps=cfg.min_comps, cluster=cluster)
    res = score_listing(
        lst, view, cfg, price_history=store.price_history(*lst.key), first_seen=first_seen, now=store.now(),
    )
    store.save_score(lst.source, lst.external_id, res.score, res.breakdown())
    return key, res


def process(result: FetchResult, source: str, store: Store, notifier: Notifier, cfg: FlipConfig) -> RunStats:
    stats = RunStats(source=source, fetched=len(result.listings))
    stats.baseline = not store.baseline_done(source)

    # 1) uložit všechno (i mimo oblast = data o trhu), u nových v oblasti hledat duplicity
    for lst in result.listings:
        prev, _ = store.upsert(lst)
        if prev is not None:
            store.fill_from_stored(lst)
        elif lst.in_area:
            _link_duplicate(store, lst)

    # 2) trh až po uložení dávky (nové inzeráty už jsou ve srovnání)
    markets = {
        kind: MarketData(store.market_rows(kind=kind, window_days=cfg.market_window_days, in_area_only=False))
        for kind in ("byt", "dum")
    }

    # 3) skóre + rozhodnutí o zprávě
    baseline_items: list[tuple[Listing, ScoreResult]] = []
    for lst in result.listings:
        if lst.in_area:
            stats.in_area += 1
        if not eligible(lst, cfg):
            continue
        stats.eligible += 1
        try:
            key, res = _score(store, lst, markets, cfg)
        except Exception as exc:  # noqa: BLE001 – vadný inzerát nesmí shodit běh
            log.error("%s #%s: skórování selhalo: %s", source, lst.external_id, exc)
            continue
        stats.scored.append((lst, res))

        done = store.notified(key)
        if done is None:
            if key != cluster_key(*lst.key):
                # duplikát nemovitosti, která ještě nešla ven (např. kanonická je nad rozpočtem)
                stats.duplicates += 1
            if stats.baseline:
                baseline_items.append((lst, res))
            elif notifier.send(format_new(lst, res)):
                store.mark_notified(key, lst.price, res.score)
                stats.new_sent += 1
            continue

        last = done["last_notified_price"]
        if lst.price and last and lst.price <= last * (1 - cfg.price_drop_alert_pct / 100):
            if notifier.send(format_drop(lst, res, last)):
                store.mark_notified(key, lst.price, res.score)
                stats.drops_sent += 1
        elif key != cluster_key(*lst.key):
            stats.duplicates += 1

    # 4) první běh: jedno shrnutí místo záplavy
    if stats.baseline:
        if _send_baseline(source, baseline_items, store, notifier, cfg):
            store.mark_baseline_done(source)

    log.info(
        "%s: staženo=%d | v obcích=%d | v rozpočtu=%d | nové=%d | zlevnění=%d | duplicity=%d%s",
        source, stats.fetched, stats.in_area, stats.eligible, stats.new_sent, stats.drops_sent,
        stats.duplicates, " | PRVNÍ BĚH" if stats.baseline else "",
    )
    return stats


def _send_baseline(
    source: str, items: list[tuple[Listing, ScoreResult]], store: Store, notifier: Notifier, cfg: FlipConfig
) -> bool:
    name = DISPLAY_NAMES.get(source, source)
    flats = sum(1 for lst, _ in items if lst.kind == "byt")
    houses = len(items) - flats
    ranked = sorted(items, key=lambda x: (x[1].hot, x[1].score or 0), reverse=True)[: cfg.baseline_top_n]
    lines = [f"👀 <b>Now watching {name}</b>"]
    if items:
        lines.append(f"Found {flats} flats and {houses} houses in your towns under {cfg.price_max / 1_000_000:g}M Kč.")
        lines.append(f"\n<b>Top {len(ranked)} right now:</b>")
        lines += [format_summary_line(lst, res) for lst, res in ranked]
        lines.append("\nFrom now on you'll get every new listing, plus a 📉 message when a price drops.")
    else:
        lines.append("Nothing new here — everything it lists in your towns was already sent from another site.")
    if not notifier.send("\n".join(lines)):
        return False   # nic se neoznačí -> příští běh to zkusí znovu
    for lst, res in items:
        store.mark_notified(store.cluster_of(*lst.key), lst.price, res.score)
    # 🔥 kusy i s plným detailem, ať nezapadnou v shrnutí
    for lst, res in [x for x in ranked if x[1].hot][:HOT_DETAIL_ON_BASELINE]:
        notifier.send(format_new(lst, res))
    return True


def run_source(source: str, cfg: FlipConfig, notifier: Notifier, store: Store) -> RunStats:
    """Jeden běh jednoho zdroje včetně evidence selhání. Nikdy nevyhazuje výjimku."""
    module = SOURCES[source]
    try:
        result = module.fetch_listings(cfg, store.known_ids(source))
        if result.raw_count == 0:
            raise RuntimeError("source returned 0 listings — site down or layout changed")
    except Exception as exc:  # noqa: BLE001 – pád jednoho zdroje nesmí shodit ostatní
        failures = store.record_failure(source, str(exc))
        log.error("%s: běh selhal (%d× v kuse): %s", source, failures, exc)
        if store.should_warn(source, cfg.source_failure_alert_threshold):
            if notifier.send_failure_warning(source, failures, str(exc)):
                store.mark_warned(source)
        return RunStats(source=source, error=str(exc))

    store.record_success(source)
    return process(result, source, store, notifier, cfg)
