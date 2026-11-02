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
import logging
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from typing import Any

from flip.config import FlipConfig
from flip.notifier import Notifier, e
from flip.report import CSV_NAME as TOWN_PRICES_CSV
from flip.sources import DISPLAY_NAMES
from flip.store import Store

log = logging.getLogger("flip.export")

UPLOAD_LIMIT = 50 * 1024 * 1024    # Bot API limit pro sendDocument

# Sloupce tabulky nabídek v pořadí, v jakém dávají smysl v Excelu.
CSV_COLUMNS = [
    "town", "kind", "subtype", "price", "area_m2", "land_m2", "last_score", "url",
    "source", "locality", "building_condition", "first_seen", "last_seen",
]

HELP = {
    "data": "send the listings table (Excel), town prices and the full database",
    "status": "how many listings are stored and when each site last ran",
    "latest": "the newest listings re-scored against today's prices (e.g. /latest 15)",
    "help": "list the commands",
}


def data_dir(cfg: FlipConfig) -> str:
    return os.path.dirname(os.path.abspath(cfg.db_path))


def listings_csv(conn: sqlite3.Connection) -> tuple[bytes, int]:
    """Nabídky ve sledovaných obcích (bez duplicit z jiných webů) jako CSV pro český Excel.

    Středník jako oddělovač, desetinná čárka a UTF-8 s BOM, aby Excel ukázal háčky
    a čísla bral jako čísla. Vrací (obsah, počet řádků).
    """
    rows = conn.execute(
        f"""SELECT {', '.join(CSV_COLUMNS)} FROM listings
            WHERE town IS NOT NULL AND dup_of IS NULL ORDER BY last_seen DESC"""
    ).fetchall()
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";")
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        writer.writerow(["" if v is None else f"{v:g}".replace(".", ",") if isinstance(v, float) else v
                         for v in row])
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
    content, n = listings_csv(store.conn)
    _send(notifier, f"listings_{stamp}.csv", content, f"{n} listings in your towns (opens in Excel)")
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
