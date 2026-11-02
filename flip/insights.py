"""flip/insights.py — co říká historie: největší zlevnění a dlouho neprodané nabídky.

Telegram (zapojuje se do flip/bot.py) a `python main.py drops|stale`:
  /drops [dní]   největší zlevnění ve sledovaných obcích za posledních N dní (výchozí 7),
                 i ta, o kterých nepřišla zpráva (malý pokles, nad rozpočtem…)
  /stale [dní]   nabídky online aspoň N dní (výchozí 60), nejvíc zlevněné první —
                 prodávající, kteří nejspíš vezmou nižší nabídku

Vše jen z uložených dat (listings + price_history), nic se nestahuje.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

from flip.config import FlipConfig
from flip.export import days_online, listing_status
from flip.model import price_steps
from flip.notifier import Notifier, czk, describe_kind, e, link, money_short, short_date
from flip.sources import DISPLAY_NAMES
from flip.store import Store, iso

DROPS_DEFAULT_DAYS = 7
STALE_DEFAULT_DAYS = 60
LIMIT = 15
BUDGET_STRETCH = 1.2     # /stale ukáže i o 20 % dražší: po slevě a smlouvání se můžou vejít


@dataclass
class Item:
    row: sqlite3.Row
    steps: list[tuple[int, str]]
    first_seen: str            # nejstarší spatření v celém shluku (stejná nemovitost na víc webech)
    before_start: bool         # byla online už při spuštění sledování -> dny jsou "aspoň"

    @property
    def cuts(self) -> int:
        return sum(1 for (a, _), (b, _) in zip(self.steps, self.steps[1:]) if b < a)

    @property
    def total_change_pct(self) -> float | None:
        if len(self.steps) < 2:
            return None
        return (self.steps[-1][0] / self.steps[0][0] - 1) * 100


def _vs_town(row: sqlite3.Row) -> float | None:
    median = (json.loads(row["last_breakdown"] or "{}").get("market") or {}).get("median_ppm2")
    if not (median and row["price"] and row["area_m2"]):
        return None
    return (row["price"] / row["area_m2"] / median - 1) * 100


def _items(store: Store, cfg: FlipConfig) -> list[Item]:
    """Všechny inzeráty ve sledovaných obcích, jeden za nemovitost (shluk), s historií cen.
    Za shluk se bere člen s nejdelší historií cen (tam je nejvíc vidět)."""
    rows = store.conn.execute("SELECT * FROM listings WHERE town IS NOT NULL").fetchall()
    history: dict[tuple[str, str], list[tuple[int | None, str]]] = {}
    for h in store.conn.execute("SELECT source, external_id, price, seen_at FROM price_history ORDER BY seen_at, rowid"):
        history.setdefault((h["source"], h["external_id"]), []).append((h["price"], h["seen_at"]))
    clusters: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        if cfg.wants_kind(r["kind"]):
            clusters.setdefault(r["dup_of"] or f'{r["source"]}:{r["external_id"]}', []).append(r)
    out = []
    for members in clusters.values():
        best = max(members, key=lambda m: (len(price_steps(history.get((m["source"], m["external_id"]), []))),
                                           m["dup_of"] is None))
        earliest = min(members, key=lambda m: m["first_seen"])
        out.append(Item(best, price_steps(history.get((best["source"], best["external_id"]), [])),
                        earliest["first_seen"], bool(earliest["seen_at_start"])))
    return out


def _line(it: Item, cfg: FlipConfig, extra: str) -> str:
    r = it.row
    bits = [e(describe_kind(SimpleNamespace(kind=r["kind"], subtype=r["subtype"]))), e(r["town"]), czk(r["price"]) if r["price"] else "price on request"]
    if r["area_m2"]:
        bits.append(f"{r['area_m2']:g} m²")
    if (vs := _vs_town(r)) is not None:
        bits.append(f"{vs:+.0f} % vs town per m²".replace("-", "−"))
    if r["price"] and r["price"] > cfg.price_max:
        bits.append("💸 over budget")
    return f"• {extra} · {' · '.join(bits)} — {link(r['url'], DISPLAY_NAMES.get(r['source'], r['source']))}"


# ---------------------------------------------------------------------- #
# /drops                                                                 #
# ---------------------------------------------------------------------- #

def recent_drops(store: Store, cfg: FlipConfig, days: int = DROPS_DEFAULT_DAYS) -> list[tuple[Item, int, int]]:
    """[(inzerát, cena před prvním poklesem v okně, cena teď)], největší pokles v % první.
    Jen nabídky pořád online; jen poklesy (zdražení se nepočítá)."""
    since = iso(store.now() - timedelta(days=days))
    now = store.now()
    out = []
    for it in _items(store, cfg):
        if listing_status(it.row, now) != "online" or not it.row["price"]:
            continue
        before = None
        for (prev, _), (price, seen_at) in zip(it.steps, it.steps[1:]):
            if seen_at >= since and price < prev:
                before = prev
                break
        if before and it.row["price"] < before:
            out.append((it, before, it.row["price"]))
    out.sort(key=lambda x: x[2] / x[1])
    return out


def format_drops(drops: list[tuple[Item, int, int]], cfg: FlipConfig, days: int) -> str:
    if not drops:
        return f"No price cuts in your towns in the last {days} days."
    lines = [f"📉 <b>Biggest price cuts in the last {days} days</b> ({len(drops)} listings)", ""]
    for it, before, now_price in drops[:LIMIT]:
        pct = (now_price / before - 1) * 100
        extra = f"<b>{pct:.0f} %</b> ({money_short(before)} → {money_short(now_price)})".replace("-", "−")
        lines.append(_line(it, cfg, extra))
    if len(drops) > LIMIT:
        lines.append(f"…and {len(drops) - LIMIT} more")
    lines.append("\n<i>Includes cuts too small for a 📉 message and listings over your budget.</i>")
    return "\n".join(lines)


# ---------------------------------------------------------------------- #
# /stale                                                                 #
# ---------------------------------------------------------------------- #

def stale_listings(store: Store, cfg: FlipConfig, min_days: int = STALE_DEFAULT_DAYS) -> list[tuple[Item, int]]:
    """[(inzerát, dní online)] pro nabídky online aspoň min_days. Nejvíc zlevněné první,
    pak nejdéle online. Jen v rozpočtu nebo do 20 % nad ním."""
    now = store.now()
    out = []
    for it in _items(store, cfg):
        r = it.row
        if listing_status(r, now) != "online" or not r["price"] or r["price"] > cfg.price_max * BUDGET_STRETCH:
            continue
        days = days_online(it.first_seen, r, now)
        if days is not None and days >= min_days:
            out.append((it, days))
    out.sort(key=lambda x: (x[0].total_change_pct or 0, -x[1]))
    return out


def format_stale(items: list[tuple[Item, int]], cfg: FlipConfig, min_days: int, watching_since: str | None) -> str:
    if not items:
        msg = f"Nothing in your towns has been online for {min_days}+ days yet."
        if watching_since:
            msg += f" Watching started on {short_date(watching_since)}, so try a smaller number, e.g. /stale 21."
        return msg
    lines = [f"⏳ <b>Online for {min_days}+ days</b> ({len(items)} listings): sellers most likely to take a lower offer", ""]
    for it, days in items[:LIMIT]:
        age = f"{days}+ days" if it.before_start else f"{days} days"
        if it.cuts:
            change = f"{it.total_change_pct:.0f} %".replace("-", "−")
            extra = f"<b>{age}</b>, {it.cuts} cut{'s' if it.cuts > 1 else ''} ({change})"
        else:
            extra = f"<b>{age}</b>, no cuts yet"
        lines.append(_line(it, cfg, extra))
    if len(items) > LIMIT:
        lines.append(f"…and {len(items) - LIMIT} more")
    lines.append("\n<i>\"+\" = it was already online when watching started, so it's been up even longer.</i>")
    return "\n".join(lines)


def watching_since(store: Store) -> str | None:
    row = store.conn.execute("SELECT MIN(first_seen) FROM listings").fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------- #
# Telegram                                                               #
# ---------------------------------------------------------------------- #

def _days_arg(message: dict[str, Any], default: int) -> int:
    parts = (message.get("text") or "").split()
    return max(1, min(int(parts[1]), 365)) if len(parts) > 1 and parts[1].isdigit() else default


def drops_text(store: Store, cfg: FlipConfig, days: int = DROPS_DEFAULT_DAYS) -> str:
    return format_drops(recent_drops(store, cfg, days), cfg, days)


def stale_text(store: Store, cfg: FlipConfig, min_days: int = STALE_DEFAULT_DAYS) -> str:
    return format_stale(stale_listings(store, cfg, min_days), cfg, min_days, watching_since(store))


def cmd_drops(message: dict[str, Any], store: Store, cfg: FlipConfig, notifier: Notifier) -> None:
    notifier.send(drops_text(store, cfg, _days_arg(message, DROPS_DEFAULT_DAYS)))


def cmd_stale(message: dict[str, Any], store: Store, cfg: FlipConfig, notifier: Notifier) -> None:
    notifier.send(stale_text(store, cfg, _days_arg(message, STALE_DEFAULT_DAYS)))


def register(bot: Any) -> None:
    bot.register_command("drops", cmd_drops)
    bot.register_command("stale", cmd_stale)
