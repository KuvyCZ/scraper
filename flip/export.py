"""flip/export.py — aktuální data přes Telegram: napiš botovi /data.

Příkazy (zapojují se do flip/bot.py, odpovídají jen v TELEGRAM_CHAT_ID):
  /data    pošle 3 soubory: tabulku nabídek (CSV pro Excel), týdenní ceny obcí
           (town_prices.csv, pokud už existuje) a celou databázi flip.db
  /status  kolik nabídek je v DB, kdy který web naposledy běžel, verze programu
  /help    seznam příkazů

Navíc `announce_update`: po nasazení nové verze pošle „✅ Scraper is now running version …“.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from flip.config import FlipConfig
from flip.model import price_steps
from flip.notifier import Notifier, e
from flip.report import CSV_NAME as TOWN_PRICES_CSV
from flip.sources import DISPLAY_NAMES
from flip.store import Store

log = logging.getLogger("flip.export")

UPLOAD_LIMIT = 50 * 1024 * 1024    # Bot API limit pro sendDocument

# Sloupce tabulky nabídek v pořadí, v jakém dávají smysl v Excelu.
CSV_COLUMNS = [
    "town", "kind", "subtype", "status", "price", "first_price", "price_change_pct", "price_cuts",
    "area_m2", "land_m2", "price_per_m2", "vs_town_pct", "flip_profit", "score", "days_online",
    "online_before_watching", "first_seen", "last_seen", "gone_on", "also_on", "url", "source",
    "locality", "condition",
]
ONLINE_RECENT_DAYS = 3      # Bazoš "zmizení" nepozná: neviděný déle než tohle = "not seen lately"

HELP = {
    "data": "send the listings table (Excel), town prices and the full database",
    "status": "how many listings are stored and when each site last ran",
    "latest": "the newest listings re-scored against today's prices (e.g. /latest 15)",
    "drops": "the biggest price cuts in your towns (e.g. /drops 14 for the last 14 days)",
    "stale": "listings online a long time, often with cuts: good targets for a lower offer (e.g. /stale 30)",
    "help": "list the commands",
}


def data_dir(cfg: FlipConfig) -> str:
    return os.path.dirname(os.path.abspath(cfg.db_path))


def listing_status(row: Mapping[str, Any], now: datetime) -> str:
    """"online" | "gone" (zmizel z webu, nejspíš prodaný) | "not seen lately" (Bazoš apod.)."""
    if row["gone_at"]:
        return "gone"
    try:
        recent = now - datetime.fromisoformat(row["last_seen"]) <= timedelta(days=ONLINE_RECENT_DAYS)
    except (TypeError, ValueError):
        recent = False
    return "online" if recent else "not seen lately"


def days_online(first_seen: str, row: Mapping[str, Any], now: datetime) -> int | None:
    """Od prvního spatření do zmizení (nebo do dneška, když je pořád online)."""
    status = listing_status(row, now)
    end_iso = row["gone_at"] if status == "gone" else row["last_seen"] if status != "online" else None
    try:
        end = datetime.fromisoformat(end_iso) if end_iso else now
        return max(0, (end - datetime.fromisoformat(first_seen)).days)
    except (TypeError, ValueError):
        return None


def _cell(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:g}".replace(".", ",")
    return v


def listings_csv(conn: sqlite3.Connection, now: datetime | None = None) -> tuple[bytes, int]:
    """Nabídky ve sledovaných obcích (bez duplicit z jiných webů) jako CSV pro český Excel,
    i s historií: první cena, kolikrát zlevnila, kolik dní je online, jestli už zmizela.

    Středník jako oddělovač, desetinná čárka a UTF-8 s BOM, aby Excel ukázal háčky
    a čísla bral jako čísla. Vrací (obsah, počet řádků).
    """
    now = now or datetime.now(timezone.utc)
    rows = conn.execute(
        """SELECT * FROM listings WHERE town IS NOT NULL AND dup_of IS NULL ORDER BY last_seen DESC"""
    ).fetchall()
    members: dict[str, list[sqlite3.Row]] = {}
    for m in conn.execute("SELECT * FROM listings WHERE dup_of IS NOT NULL"):
        members.setdefault(m["dup_of"], []).append(m)
    history: dict[tuple[str, str], list[tuple[int | None, str]]] = {}
    for h in conn.execute("SELECT source, external_id, price, seen_at FROM price_history ORDER BY seen_at, rowid"):
        history.setdefault((h["source"], h["external_id"]), []).append((h["price"], h["seen_at"]))

    out = io.StringIO()
    writer = csv.writer(out, delimiter=";")
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        others = members.get(f'{row["source"]}:{row["external_id"]}', [])
        cluster = [row, *others]
        earliest = min(cluster, key=lambda m: m["first_seen"])
        steps = price_steps(history.get((row["source"], row["external_id"]), []))
        first_price = steps[0][0] if steps else row["price"]
        change = round((row["price"] / first_price - 1) * 100, 1) if row["price"] and first_price else None
        cuts = sum(1 for (a, _), (b, _) in zip(steps, steps[1:]) if b < a)
        ppm2 = row["price"] / row["area_m2"] if row["price"] and row["area_m2"] else None
        breakdown = json.loads(row["last_breakdown"] or "{}")
        median = (breakdown.get("market") or {}).get("median_ppm2")
        margin = (breakdown.get("estimate") or {}).get("margin")
        writer.writerow([_cell(v) for v in (
            row["town"], "flat" if row["kind"] == "byt" else "house", row["subtype"],
            listing_status(row, now), row["price"], first_price, change, cuts,
            row["area_m2"], row["land_m2"], round(ppm2) if ppm2 else None,
            round((ppm2 / median - 1) * 100, 1) if ppm2 and median else None, margin, row["last_score"],
            days_online(earliest["first_seen"], row, now), "yes" if earliest["seen_at_start"] else "",
            earliest["first_seen"][:10], row["last_seen"][:10], (row["gone_at"] or "")[:10],
            ", ".join(sorted({DISPLAY_NAMES.get(m["source"], m["source"]) for m in others})),
            row["url"], DISPLAY_NAMES.get(row["source"], row["source"]), row["locality"],
            row["building_condition"],
        )])
    return out.getvalue().encode("utf-8-sig"), len(rows)


def snapshot_db(conn: sqlite3.Connection) -> bytes:
    """Konzistentní kopie celé DB (sqlite backup API), i když do ní scraper zrovna píše."""
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "flip.db")
        dst = sqlite3.connect(target)
        try:
            conn.backup(dst)
        finally:
            dst.close()
        with open(target, "rb") as fh:
            return fh.read()


def _send(notifier: Notifier, filename: str, content: bytes, caption: str) -> None:
    if len(content) > UPLOAD_LIMIT:
        notifier.send(f"⚠️ {e(filename)} is {len(content) // 1_000_000} MB, over Telegram's 50 MB limit. "
                      "Download it in File Station from docker/flip-scraper/data instead.")
        return
    if not notifier.send_document(filename, content, caption):
        notifier.send(f"❌ Couldn't send {e(filename)}. Details are in the container log.")


def cmd_data(message: dict[str, Any], store: Store, cfg: FlipConfig, notifier: Notifier) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    content, n = listings_csv(store.conn, store.now())
    _send(notifier, f"listings_{stamp}.csv", content,
          f"{n} listings in your towns with their price history (opens in Excel)")
    town_csv = os.path.join(data_dir(cfg), TOWN_PRICES_CSV)
    if os.path.exists(town_csv):
        with open(town_csv, "rb") as fh:
            _send(notifier, TOWN_PRICES_CSV, fh.read(), "Average price per m² by town, one row per week")
    _send(notifier, f"flip_{stamp}.db", snapshot_db(store.conn),
          "Full database. Attach this file when you ask Claude to look at the data.")


def cmd_status(message: dict[str, Any], store: Store, cfg: FlipConfig, notifier: Notifier) -> None:
    total = store.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    in_area = store.conn.execute(
        "SELECT COUNT(*) FROM listings WHERE town IS NOT NULL AND dup_of IS NULL").fetchone()[0]
    lines = [f"📊 <b>{in_area}</b> listings in your towns ({total} stored in total)"]
    since = (store.now() - timedelta(days=7)).isoformat(timespec="seconds")
    gone = store.conn.execute(
        "SELECT COUNT(*) FROM listings WHERE town IS NOT NULL AND dup_of IS NULL AND gone_at >= ?", (since,)
    ).fetchone()[0]
    cuts = store.conn.execute(
        """SELECT COUNT(DISTINCT COALESCE(l.dup_of, l.source || ':' || l.external_id)) FROM price_history p
           JOIN listings l USING (source, external_id)
           WHERE l.town IS NOT NULL AND p.seen_at >= ? AND p.price < (
               SELECT q.price FROM price_history q WHERE q.source = p.source AND q.external_id = p.external_id
                 AND q.price IS NOT NULL AND (q.seen_at < p.seen_at OR (q.seen_at = p.seen_at AND q.rowid < p.rowid))
               ORDER BY q.seen_at DESC, q.rowid DESC LIMIT 1)""", (since,)
    ).fetchone()[0]
    lines.append(f"Last 7 days: {cuts} price cuts, {gone} gone (most likely sold)")
    for h in store.conn.execute("SELECT * FROM source_health ORDER BY source"):
        last = (h["last_run_at"] or "never")[:16].replace("T", " ")
        fails = h["consecutive_failures"]
        name = e(DISPLAY_NAMES.get(h["source"], h["source"]))
        lines.append(f"{'⚠️' if fails else '✅'} {name}: last run {last} UTC"
                     + (f", failed {fails}× in a row" if fails else ""))
    if version := os.environ.get("APP_VERSION"):
        lines.append(f"🏷 version {e(version)}")
    notifier.send("\n".join(lines))


def cmd_help(message: dict[str, Any], store: Store, cfg: FlipConfig, notifier: Notifier) -> None:
    notifier.send("Commands:\n" + "\n".join(f"/{name} — {text}" for name, text in HELP.items()))


def register(bot: Any) -> None:
    bot.register_command("data", cmd_data)
    bot.register_command("status", cmd_status)
    bot.register_command("help", cmd_help)
    bot.register_command("start", cmd_help)    # Telegram posílá /start při prvním otevření bota


def announce_update(cfg: FlipConfig, notifier: Notifier) -> None:
    """Po nové verzi (jiné APP_VERSION než minule) pošle „✅ Scraper is now running version …“.

    Verze se pamatuje v data/.version vedle DB, takže obyčejný restart nic neposílá.
    """
    version = os.environ.get("APP_VERSION", "")
    if not version:
        return
    marker = os.path.join(data_dir(cfg), ".version")
    try:
        with open(marker, encoding="utf-8") as fh:
            previous = fh.read().strip()
    except OSError:
        previous = ""
    if previous == version:
        return
    title = os.environ.get("APP_TITLE", "")
    notifier.send(f"✅ Scraper is now running version {e(version)}" + (f"\n{e(title)}" if title else "")
                  + "\nSend /help to see what you can ask me.")
    try:
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(version)
    except OSError as exc:
        log.warning("nelze zapsat %s: %s", marker, exc)
