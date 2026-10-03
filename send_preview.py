"""
send_preview.py — JEDNORÁZOVÝ náhled: pošli pár nejlepších inzerátů z DB na Telegram.

K čemu: vidět, jak reálná notifikace vypadá v appce, aniž bys čekal, až scraper
něco nového najde. Bere TOP-N inzerátů podle uloženého skóre, přepočítá skóre
PŘESNĚ jako ostrý běh (stejné vstupy jako main.score_and_notify → zprávy 1:1)
a odešle je. Nic nezapisuje, neřeší práh ani anti-spam — je to čistě náhled.

Použití (PowerShell):
    python send_preview.py            # pošle 5 nejlepších
    python send_preview.py 3          # pošle 3 nejlepší
    python send_preview.py above      # pošle VŠE nad prahem (SCORE_THRESHOLD z .env)
    python send_preview.py above realitky.db   # i s explicitní cestou k DB

Token a chat_id se berou z .env (stejně jako ostrý běh). Bez nich skript skončí
s chybou (nedělá tiché dry_run — chceš přece reálně vidět zprávu).
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timezone

from config import load_config
from database import Database
from notifier import TelegramNotifier
from scoring import score_listing


def _fix_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # Windows cp1250 by padal na diakritice/emoji
        except (AttributeError, ValueError):
            pass


def _days_on_market(seen_first_iso: str) -> int:
    try:
        seen = datetime.fromisoformat(seen_first_iso)
        return max(0, (datetime.now(timezone.utc) - seen).days)
    except (ValueError, TypeError):
        return 0


def _pick_db_path(config_path: str) -> str | None:
    """DB z .env, jinak realitky.db v rootu (na PC bývá naplněná tam)."""
    for candidate in (config_path, "realitky.db", "data/realitky.db"):
        if candidate and os.path.exists(candidate):
            return candidate
    return None


_LATEST_SCORE_JOIN = """
    FROM scores s
    JOIN (SELECT source, source_id, MAX(id) AS max_id
          FROM scores GROUP BY source, source_id) last
      ON s.id = last.max_id
"""


def _top_scored(db: Database, limit: int) -> list[tuple[str, str, int]]:
    """(source, source_id, score) nejlepších N podle POSLEDNÍHO skóre inzerátu."""
    rows = db.conn.execute(
        f"SELECT s.source, s.source_id, s.score {_LATEST_SCORE_JOIN}"
        " WHERE s.score IS NOT NULL ORDER BY s.score DESC, s.source_id LIMIT ?",
        (limit,),
    ).fetchall()
    return [(r["source"], r["source_id"], r["score"]) for r in rows]


def _above_threshold(db: Database, threshold: int) -> list[tuple[str, str, int]]:
    """Všechny inzeráty s POSLEDNÍM skóre >= práh (to, co by reálně šlo na Telegram)."""
    rows = db.conn.execute(
        f"SELECT s.source, s.source_id, s.score {_LATEST_SCORE_JOIN}"
        " WHERE s.score >= ? ORDER BY s.score DESC, s.source_id",
        (threshold,),
    ).fetchall()
    return [(r["source"], r["source_id"], r["score"]) for r in rows]


def main() -> int:
    _fix_console()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s  %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log = logging.getLogger("preview")

    count = 5
    above_mode = False
    db_arg: str | None = None
    args = sys.argv[1:]
    if args:
        first = args[0].lower()
        if first in ("above", "prah", "nadprah", "all", "vse"):
            above_mode = True   # pošli VŠE nad prahem (přesně to, co by chodilo notifikací)
        else:
            try:
                count = max(1, int(args[0]))
            except ValueError:
                print(f"První argument má být číslo (počet) nebo 'above' (vše nad prahem), "
                      f"dostal jsem {args[0]!r}.")
                return 2
        if len(args) > 1:
            db_arg = args[1]

    config = load_config()
    if not (config.telegram_token and config.telegram_chat_id):
        print("❌ Chybí TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID v .env — nemám kam poslat.")
        print("   Vyplň je v .env (stejné, co půjde na NAS) a spusť znovu.")
        return 1

    db_path = db_arg or _pick_db_path(config.db_path)
    if not db_path:
        print(f"❌ Nenašel jsem DB (zkoušel jsem {config.db_path!r}, realitky.db, data/realitky.db).")
        return 1

    vyber = f"VŠE nad prahem {config.score_threshold}" if above_mode else f"TOP {count} podle skóre"
    log.info("DB = %s | posílám %s | chat_id = %s", db_path, vyber, config.telegram_chat_id)

    notifier = TelegramNotifier(config.telegram_token, config.telegram_chat_id, dry_run=False)

    sent = 0
    with Database(db_path) as db:
        picks = _above_threshold(db, config.score_threshold) if above_mode else _top_scored(db, count)
        if not picks:
            msg = (f"Žádný inzerát nad prahem {config.score_threshold}."
                   if above_mode else "V DB nejsou žádná skóre — nejdřív spusť main.py.")
            print(f"❌ {msg}")
            return 1

        for i, (source, source_id, saved_score) in enumerate(picks, 1):
            row = db.get_listing_row(source, source_id)
            if row is None:
                log.warning("#%s není v listings (přeskakuji)", source_id)
                continue
            full = db.row_to_listing(row)

            # Přepočet PŘESNĚ jako main.score_and_notify → náhled odpovídá ostrému běhu.
            result = score_listing(
                full,
                district_samples=db.price_per_m2_values(district=full.district, disposition=full.disposition),
                region_samples=db.price_per_m2_values(region=full.region, disposition=full.disposition),
                price_history=db.price_history(full.source, full.source_id),
                days_on_market=_days_on_market(row["seen_first"]),
            )

            ok = notifier.notify_listing(full, result)
            sent += int(ok)
            log.info("%d/%d  skóre=%s  %s, %s  → odesláno=%s",
                     i, len(picks), result.score, full.disposition, full.locality_text, ok)
            time.sleep(0.5)  # drobná pauza, ať Telegram zprávy nesloučí/neomezí

    print(f"\n✅ Hotovo — odesláno {sent}/{len(picks)} zpráv na Telegram. Mrkni do appky.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
