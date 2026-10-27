"""flip/report.py — týdenní tabulka cen po obcích: Telegram + CSV s historií.

Každých TOWN_REPORT_DAYS dní (výchozí 7) se z inzerátů viděných za posledních
TOWN_REPORT_WINDOW_DAYS dní (výchozí 30) spočítá typická cena za m² v každé obci
(byty a domy zvlášť), uloží se do tabulky `town_stats` a připíše do
`town_prices.csv` vedle databáze (na NAS ve složce data — otevře se v Excelu).
Zpráva ukazuje i změnu proti minulému týdnu.

⚠️ Jsou to NABÍDKOVÉ ceny z portálů; skutečné prodejní ceny se nezveřejňují.
"""

from __future__ import annotations

import csv
import html
import logging
import os
from datetime import datetime, timedelta

from flip.config import FlipConfig
from flip.market import TownAverage, town_averages
from flip.notifier import Notifier
from flip.store import Store, iso

log = logging.getLogger("flip.report")

CSV_NAME = "town_prices.csv"
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


def format_report(averages: list[TownAverage], previous: dict[tuple[str, str], float], window_days: int) -> list[str]:
    """Jedna zpráva pro byty, jedna pro domy (kvůli limitu délky Telegramu)."""
    messages = []
    for kind, title in (("byt", "🏢 Flats"), ("dum", "🏡 Houses")):
        items = [a for a in averages if a.kind == kind]
        if not items:
            continue
        lines = [f"<b>{title} — typical asking price per m²</b>",
                 f"<i>listings seen in the last {window_days} days; change vs last week</i>", "<pre>"]
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
    _, prev_rows = store.last_town_stats()
    previous = {(r["town"], r["kind"]): r["median_ppm2"] for r in prev_rows}
    messages = format_report(averages, previous, cfg.town_report_window_days)
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
