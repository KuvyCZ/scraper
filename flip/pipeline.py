"""flip/pipeline.py — jeden běh jednoho zdroje: stáhnout → uložit → deduplikovat → skórovat → poslat.

Pravidla notifikací:
  * NOVÁ nemovitost ve sledované obci a v rozpočtu -> zpráva vždy (🏠), silná 🔥.
  * Stejná nemovitost z jiného portálu -> nic (už odešla), LEDAŽE je tam levnější
    o >= PRICE_DROP_ALERT_PCT -> 📉.
  * Zlevnění už oznámené nemovitosti o >= PRICE_DROP_ALERT_PCT -> 📉.
  * PRVNÍ běh zdroje (prázdná DB) -> místo desítek zpráv jedno shrnutí s top N;
    ostatní se tiše uloží a ozvou se až při zlevnění.
  * Zdroj, který prošel celou nabídku (Sreality, Realingo, Reality Šumperk), označí
    inzeráty, které v ní už 2 dny nejsou, jako zmizelé (gone_at) — nejspíš prodané.
  * Zdroj, který nic nerozparsuje (0 inzerátů), se počítá jako selhání — nejspíš
    změna webu; po N selháních v kuse přijde jedno varování.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from flip.config import FlipConfig
from flip.dedup import is_duplicate
from flip.favcheck import send_favorite_sold
from flip.market import MarketData, analyze
from flip.model import FetchResult, Listing
from flip.notifier import (Notifier, format_drop, format_favorite_change, format_gone, format_new,
                           format_summary_line)
from flip.sanity import clean
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
    gone: int = 0
    baseline: bool = False
    error: str | None = None
    scored: list[tuple[Listing, ScoreResult]] = field(default_factory=list)


def eligible(lst: Listing, cfg: FlipConfig) -> bool:
    """Má se o tomhle inzerátu vůbec posílat zpráva? (cenu řeší within_budget po skórování)"""
    return lst.in_area and cfg.wants_kind(lst.kind) and cfg.area_ok(lst.area_m2)


def over_budget(lst: Listing, cfg: FlipConfig) -> bool:
    return lst.price is not None and lst.price > cfg.price_max


def within_budget(lst: Listing, res: ScoreResult, cfg: FlipConfig) -> bool:
    """V rozpočtu, nebo dražší, ale výrazně levná za m² proti typické ceně v obci."""
    if not over_budget(lst, cfg):
        return True
    return (cfg.over_budget_discount_pct > 0 and res.vs_median_pct is not None
            and res.vs_median_pct <= -cfg.over_budget_discount_pct)


def _with_budget_note(text: str, lst: Listing, res: ScoreResult, cfg: FlipConfig) -> str:
    if not over_budget(lst, cfg) or res.vs_median_pct is None:
        return text
    head, _, rest = text.partition("\n")
    note = (f"💸 Over your {cfg.price_max / 1_000_000:g}M budget, but {abs(res.vs_median_pct):.0f} % "
            "below the typical price per m² here")
    return f"{head}\n{note}\n{rest}"


def load_markets(store: Store, cfg: FlipConfig) -> dict[str, MarketData]:
    """Srovnatelné inzeráty + změřená sleva při prodeji, pro byty i domy."""
    return {
        kind: MarketData(store.market_rows(kind=kind, window_days=cfg.market_window_days, in_area_only=False),
                         now=store.now(), sales=store.sale_rows(kind=kind))
        for kind in ("byt", "dum")
    }


def _report_gone(store: Store, notifier: Notifier, cfg: FlipConfig, rows: list) -> None:
    """🏁 krátká zpráva, když zmizí nemovitost, o které už přišla zpráva — jen jednou
    a jen když není k vidění ani na jiném webu. Oblíbená dostane podrobnější ⭐🏁."""
    for row in rows:
        key = store.cluster_of(row["source"], row["external_id"])
        sent = store.notified(key)
        favorite = store.is_favorite(key)
        if (sent is None and not favorite) or store.cluster_still_online(key, (row["source"], row["external_id"])):
            continue
        if favorite:
            send_favorite_sold(store, notifier, key, row)
            continue
        members = store.cluster_members(key)
        earliest = min(members, key=lambda m: m["first_seen"])
        try:
            days = (datetime.fromisoformat(row["gone_at"]) - datetime.fromisoformat(earliest["first_seen"])).days
        except (TypeError, ValueError):
            days = None
        notifier.send(format_gone(row, store.price_steps(row["source"], row["external_id"]), days,
                                  bool(earliest["seen_at_start"]), sent["score"] if sent else None, cfg.hot_score,
                                  code=store.code_of(key)))


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
    lst.code = store.code_of(key)
    members = store.cluster_members(key)
    cluster = {(m["source"], m["external_id"]) for m in members}
    lst.duplicate_sources = [(m["source"], m["url"]) for m in members if (m["source"], m["external_id"]) != lst.key]
    earliest = min(members, key=lambda m: m["first_seen"], default=None)
    first_seen = earliest["first_seen"] if earliest is not None else None
    view = analyze(lst, markets[lst.kind], min_comps=cfg.min_comps, cluster=cluster)
    res = score_listing(
        lst, view, cfg, price_history=store.price_history(*lst.key), first_seen=first_seen, now=store.now(),
        listed_before_start=bool(earliest is not None and earliest["seen_at_start"]),
    )
    store.save_score(lst.source, lst.external_id, res.score, res.breakdown())
    return key, res


def process(result: FetchResult, source: str, store: Store, notifier: Notifier, cfg: FlipConfig) -> RunStats:
    stats = RunStats(source=source, fetched=len(result.listings))
    stats.baseline = not store.baseline_done(source)
    listings = [lst for lst in map(clean, result.listings) if lst is not None]

    # 1) uložit všechno (i mimo oblast = data o trhu), u nových v oblasti hledat duplicity
    fav_changes: dict[str, tuple[Listing, int]] = {}      # oblíbené se změnou ceny -> zpráva
    for lst in listings:
        prev, changed = store.upsert(lst, seen_at_start=stats.baseline)
        if prev is not None:
            store.fill_from_stored(lst)
            if changed and prev["price"] and lst.price and not stats.baseline:
                key = store.cluster_of(*lst.key)
                if store.is_favorite(key):
                    fav_changes[key] = (lst, prev["price"])
        elif lst.in_area:
            _link_duplicate(store, lst)

    # co v úplném průchodu chybí, z webu zmizelo (u Bazoše se to poznat nedá)
    if result.full_scan:
        gone = store.mark_gone(source, [k for k in ("byt", "dum") if cfg.wants_kind(k)])
        stats.gone = len(gone)
        _report_gone(store, notifier, cfg, gone)

    # 2) trh až po uložení dávky (nové inzeráty už jsou ve srovnání)
    markets = load_markets(store, cfg)

    # 3) skóre + rozhodnutí o zprávě
    baseline_items: list[tuple[Listing, ScoreResult]] = []
    for lst in listings:
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
        if not within_budget(lst, res, cfg):
            continue
        stats.scored.append((lst, res))

        done = store.notified(key)
        if done is None:
            if key != cluster_key(*lst.key):
                # duplikát nemovitosti, která ještě nešla ven (např. kanonická je nad rozpočtem)
                stats.duplicates += 1
            if stats.baseline:
                baseline_items.append((lst, res))
            elif notifier.send(_with_budget_note(format_new(lst, res), lst, res, cfg), similar_key=lst.key):
                store.mark_notified(key, lst.price, res.score)
                stats.new_sent += 1
            continue

        last = done["last_notified_price"]
        if lst.price and last and lst.price <= last * (1 - cfg.price_drop_alert_pct / 100):
            # oznámená cena v historii TOHOTO inzerátu není -> pochází z jiného webu
            other_site = last not in {p for p, _ in store.price_history(*lst.key)}
            text = format_drop(lst, res, last, other_site=other_site)
            if notifier.send(_with_budget_note(text, lst, res, cfg), similar_key=lst.key):
                store.mark_notified(key, lst.price, res.score)
                stats.drops_sent += 1
                fav_changes.pop(key, None)        # už přišla 📉, ⭐ by to jen opakovala
        elif key != cluster_key(*lst.key):
            stats.duplicates += 1

    # oblíbené: každá změna ceny (i malá, i mimo rozpočet)
    for key, (lst, old) in fav_changes.items():
        lst.code = store.code_of(key)
        notifier.send(format_favorite_change(lst, old, store.price_steps(*lst.key)), similar_key=lst.key)

    # 4) první běh: jedno shrnutí místo záplavy
    if stats.baseline:
        if not result.complete:
            # část inzerátů ještě čeká na detail -> shrnutí až z úplného seznamu (jinak by
            # se odložené po dokončení prvního běhu poslaly každý zvlášť)
            log.info("%s: první běh ještě není kompletní, shrnutí pošlu, až bude", source)
        elif _send_baseline(source, baseline_items, store, notifier, cfg):
            store.mark_baseline_done(source)

    log.info(
        "%s: staženo=%d | v obcích=%d | v rozpočtu=%d | nové=%d | zlevnění=%d | duplicity=%d | zmizelo=%d%s",
        source, stats.fetched, stats.in_area, stats.eligible, stats.new_sent, stats.drops_sent,
        stats.duplicates, stats.gone, " | PRVNÍ BĚH" if stats.baseline else "",
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
        lines.append(f"Found {flats} flats and {houses} houses in your towns under {cfg.price_max / 1_000_000:g}M Kč"
                     " (or pricier but well below the town's price per m²).")
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
        notifier.send(_with_budget_note(format_new(lst, res), lst, res, cfg), similar_key=lst.key)
    return True


def run_source(source: str, cfg: FlipConfig, notifier: Notifier, store: Store) -> RunStats:
    """Jeden běh jednoho zdroje včetně evidence selhání. Nikdy nevyhazuje výjimku."""
    module = SOURCES[source]
    try:
        # po prvním běhu se u pár starších inzerátů znovu přečte detail (nový popis, "sleva")
        refresh = store.refresh_ids(source) if store.baseline_done(source) and cfg.fetch_details else set()
        if refresh:
            result = module.fetch_listings(cfg, store.known_ids(source), refresh)
        else:
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
