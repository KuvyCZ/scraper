"""flip/report.py — týdenní tabulka cen po obcích: Telegram + CSV s historií.

Každých TOWN_REPORT_DAYS dní (výchozí 7) se z inzerátů viděných za posledních
TOWN_REPORT_WINDOW_DAYS dní (výchozí 30) spočítá typická cena za m² v každé obci
(byty a domy zvlášť), uloží se do tabulky `town_stats` a připíše do
`town_prices.csv` vedle databáze (na NAS ve složce data — otevře se v Excelu).
Změna ceny se ukazuje proti snímku zhruba před 3 měsíci, když už existuje (jinak
před ~4 týdny, jinak minulý týden) — týden je při pár inzerátech v obci jen šum.

Třetí zpráva = rychlost trhu po obcích od minulé tabulky: kolik přibylo, kolik
zmizelo (nejspíš prodané), kolik zlevnilo a kolik dní typicky vydrží online.

⚠️ Jsou to NABÍDKOVÉ ceny z portálů; skutečné prodejní ceny se nezveřejňují.
"""

from __future__ import annotations

import csv
import html
import logging
import os
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from flip.config import FlipConfig
from flip.market import TownAverage, town_averages
from flip.model import price_steps
from flip.notifier import Notifier, short_date
from flip.store import GONE_TRACKED_SOURCES, Store, iso

log = logging.getLogger("flip.report")

CSV_NAME = "town_prices.csv"
# (cílové stáří snímku ve dnech, minimální stáří) — první úroveň, která má snímek, vyhrává
TREND_TIERS = ((90, 60), (28, 21))
SPEED_DAYS_WINDOW = 90       # "typicky online N dní" z inzerátů zmizelých za posledních 90 dní
MIN_GONE_FOR_DAYS = 3

CSV_HEADER = ["date", "town", "kind", "listings", "median_per_m2", "median_price",
              "renovated_per_m2", "to_renovate_per_m2"]


def report_due(store: Store, cfg: FlipConfig) -> bool:
    if cfg.town_report_days <= 0:
        return False
    last, _ = store.last_town_stats()
    if last is None:
        return True
    return store.now() - datetime.fromisoformat(last) >= timedelta(days=cfg.town_report_days)


def _k(value: float | None) -> str:
    """56 000 -> '56k'."""
    return f"{round(value / 1000)}k" if value else "–"


def _change(now: float, before: float | None) -> str:
    if not before:
        return ""
    pct = (now / before - 1) * 100
    if abs(pct) < 1:
        return " ="
    return f" {'▲' if pct > 0 else '▼'}{abs(pct):.0f}%"


def pick_reference(dates: list[str], now: datetime) -> str | None:
    """Se kterým starším snímkem srovnávat: ~3 měsíce, jinak ~4 týdny, jinak poslední."""
    if not dates:
        return None
    ages = {d: (now - datetime.fromisoformat(d)).days for d in dates}
    for target, min_age in TREND_TIERS:
        candidates = [d for d in dates if ages[d] >= min_age]
        if candidates:
            return min(candidates, key=lambda d: abs(ages[d] - target))
    return dates[-1]


def reference_label(taken_at: str | None, now: datetime) -> str:
    if taken_at is None:
        return "last week"
    days = (now - datetime.fromisoformat(taken_at)).days
    return f"{short_date(taken_at)} ({days} days ago)"


def format_report(averages: list[TownAverage], previous: dict[tuple[str, str], float], window_days: int,
                  compared_with: str = "last week") -> list[str]:
    """Jedna zpráva pro byty, jedna pro domy (kvůli limitu délky Telegramu)."""
    messages = []
    for kind, title in (("byt", "🏢 Flats"), ("dum", "🏡 Houses")):
        items = [a for a in averages if a.kind == kind]
        if not items:
            continue
        lines = [f"<b>{title} — typical asking price per m²</b>",
                 f"<i>listings seen in the last {window_days} days; change vs {compared_with}</i>", "<pre>"]
        lines.append(f"{'town':17}{'n':>4}{'per m²':>8}{'reno':>6}{'raw':>6}")
        for a in items:
            change = _change(a.median_ppm2, previous.get((a.town, a.kind)))
            lines.append(f"{html.escape(a.town[:16]):17}{a.n:>4}{_k(a.median_ppm2):>8}"
                         f"{_k(a.renovated_ppm2):>6}{_k(a.needs_reno_ppm2):>6}{change}")
        lines.append("</pre>")
        lines.append("<i>reno = already renovated, raw = needs renovation. A flip works where reno − raw is "
                     "well above the renovation cost (~25k/m² flats, ~30k/m² houses).</i>")
        messages.append("\n".join(lines))
    return messages


@dataclass
class TownSpeed:
    town: str
    kind: str
    new: int = 0
    gone: int = 0
    cuts: int = 0
    gone_days: list[int] = field(default_factory=list)   # dní online u zmizelých (posledních 90 dní)

    @property
    def typical_days(self) -> float | None:
        return statistics.median(self.gone_days) if len(self.gone_days) >= MIN_GONE_FOR_DAYS else None


def market_speed(store: Store, since: datetime) -> list[TownSpeed]:
    """Po obcích a typech od `since`: nové, zmizelé, zlevněné; + typická doba online."""
    now = store.now()
    since_iso, days_iso = iso(since), iso(now - timedelta(days=SPEED_DAYS_WINDOW))
    rows = store.conn.execute("SELECT * FROM listings WHERE town IS NOT NULL").fetchall()
    speeds: dict[tuple[str, str], TownSpeed] = {}

    def get(r) -> TownSpeed:
        return speeds.setdefault((r["town"], r["kind"]), TownSpeed(r["town"], r["kind"]))

    history: dict[tuple[str, str], list[tuple[int | None, str]]] = {}
    for h in store.conn.execute("SELECT source, external_id, price, seen_at FROM price_history ORDER BY seen_at, rowid"):
        history.setdefault((h["source"], h["external_id"]), []).append((h["price"], h["seen_at"]))
    cut_clusters: set[str] = set()
    for r in rows:
        steps = price_steps(history.get((r["source"], r["external_id"]), []))
        if any(seen >= since_iso and b < a for (a, _), (b, seen) in zip(steps, steps[1:])):
            cluster = r["dup_of"] or f'{r["source"]}:{r["external_id"]}'
            if cluster not in cut_clusters:
                cut_clusters.add(cluster)
                get(r).cuts += 1
        if r["dup_of"]:
            continue                              # nové / zmizelé počítat za nemovitost jednou
        if r["first_seen"] >= since_iso and not r["seen_at_start"]:
            get(r).new += 1
        if r["gone_at"] and r["source"] in GONE_TRACKED_SOURCES:
            if r["gone_at"] >= since_iso:
                get(r).gone += 1
            if r["gone_at"] >= days_iso and not r["seen_at_start"]:
                days = (datetime.fromisoformat(r["gone_at"]) - datetime.fromisoformat(r["first_seen"])).days
                get(r).gone_days.append(max(0, days))
    return sorted((s for s in speeds.values() if s.new or s.gone or s.cuts or s.typical_days),
                  key=lambda s: (s.kind, -(s.new + s.gone + s.cuts), s.town))


def format_speed(speeds: list[TownSpeed], since: datetime) -> str | None:
    if not speeds:
        return None
    new, gone, cuts = (sum(getattr(s, f) for s in speeds) for f in ("new", "gone", "cuts"))
    lines = [f"<b>⏱ Market speed since {short_date(iso(since))}</b>",
             f"{new} new · {gone} gone (most likely sold) · {cuts} price cuts", "<pre>"]
    lines.append(f"{'town':15}{'new':>4}{'gone':>5}{'cuts':>5}{'days':>5}")
    for kind, title in (("byt", "Flats"), ("dum", "Houses")):
        items = [s for s in speeds if s.kind == kind]
        if not items:
            continue
        lines.append(title)
        for s in items:
            days = f"{s.typical_days:.0f}" if s.typical_days is not None else "–"
            lines.append(f"{html.escape(s.town[:14]):15}{s.new:>4}{s.gone:>5}{s.cuts:>5}{days:>5}")
    lines.append("</pre>")
    lines.append("<i>gone = disappeared from Sreality, Realingo or Reality Šumperk for 2+ days. "
                 "days = typical days online before going (last 90 days, needs 3+). "
                 "/drops and /stale list the listings.</i>")
    return "\n".join(lines)


def score_check(store: Store, cfg: FlipConfig) -> str | None:
    """Byly 🔥 nabídky opravdu ty dobré? Jak rychle zmizely 🔥 vs ostatní, o kterých přišla
    zpráva (zmizelé za posledních 90 dní, aspoň 3 v každé skupině)."""
    since = iso(store.now() - timedelta(days=SPEED_DAYS_WINDOW))
    rows = store.conn.execute(
        """SELECT l.first_seen, l.gone_at, n.score FROM notified n
           JOIN listings l ON l.source || ':' || l.external_id = n.cluster_key
           WHERE l.gone_at >= ? AND l.seen_at_start = 0 AND n.score IS NOT NULL""", (since,)).fetchall()
    hot, rest = [], []
    for r in rows:
        days = (datetime.fromisoformat(r["gone_at"]) - datetime.fromisoformat(r["first_seen"])).days
        (hot if r["score"] >= cfg.hot_score else rest).append(max(0, days))
    if len(hot) < MIN_GONE_FOR_DAYS or len(rest) < MIN_GONE_FOR_DAYS:
        return None
    h, o = statistics.median(hot), statistics.median(rest)
    verdict = ("so the 🔥 ones really did go faster" if h < o else
               "so the score isn't picking the fast sellers yet; tell Claude, it may need tuning")
    return (f"<b>🎯 Score check</b> (listings you were sent that went offline, last 90 days)\n"
            f"🔥 {cfg.hot_score}+: gone after {h:.0f} days typically ({len(hot)} listings)\n"
            f"Others: {o:.0f} days ({len(rest)} listings), {verdict}.")


def write_csv(path: str, taken_at: str, averages: list[TownAverage]) -> None:
    """Připíše řádky na konec CSV (historie po týdnech). Středník + BOM kvůli českému Excelu."""
    new_file = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8-sig" if new_file else "utf-8") as fh:
        writer = csv.writer(fh, delimiter=";")
        if new_file:
            writer.writerow(CSV_HEADER)
        for a in averages:
            writer.writerow([taken_at[:10], a.town, "flat" if a.kind == "byt" else "house", a.n,
                             round(a.median_ppm2), round(a.median_price),
                             round(a.renovated_ppm2) if a.renovated_ppm2 else "",
                             round(a.needs_reno_ppm2) if a.needs_reno_ppm2 else ""])


def send_town_report(store: Store, cfg: FlipConfig, notifier: Notifier) -> bool:
    """Spočítá, uloží a pošle týdenní tabulku. False = nic k poslání / odeslání selhalo."""
    rows = []
    for kind in ("byt", "dum"):
        rows += store.market_rows(kind=kind, window_days=cfg.town_report_window_days, in_area_only=True)
    averages = town_averages(rows)
    if not averages:
        log.info("Týdenní tabulka: zatím málo inzerátů, nic neposílám")
        return False
    now = store.now()
    reference = pick_reference(store.town_stats_dates(), now)
    prev_rows = store.town_stats_at(reference) if reference else []
    previous = {(r["town"], r["kind"]): r["median_ppm2"] for r in prev_rows}
    messages = format_report(averages, previous, cfg.town_report_window_days, reference_label(reference, now))
    last, _ = store.last_town_stats()
    since = datetime.fromisoformat(last) if last else now - timedelta(days=max(cfg.town_report_days, 7))
    if speed := format_speed(market_speed(store, since), since):
        messages.append(speed)
    if check := score_check(store, cfg):
        messages.append(check)
    if not all(notifier.send(m) for m in messages):
        return False   # neuloží se -> příští smyčka to zkusí znovu
    taken_at = iso(store.now())
    store.save_town_stats(taken_at, [
        (a.town, a.kind, a.n, a.median_ppm2, a.median_price, a.renovated_ppm2, a.needs_reno_ppm2) for a in averages
    ])
    csv_path = os.path.join(os.path.dirname(os.path.abspath(cfg.db_path)), CSV_NAME)
    try:
        write_csv(csv_path, taken_at, averages)
    except OSError as exc:
        log.warning("Týdenní tabulka: CSV %s nejde zapsat: %s", csv_path, exc)
    log.info("Týdenní tabulka odeslána (%d obcí/typů), CSV: %s", len(averages), csv_path)
    return True
