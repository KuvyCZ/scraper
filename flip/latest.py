"""flip/latest.py — nejnovější inzeráty znovu oskórované proti dnešnímu trhu.

`python main.py latest [N]` a Telegram /latest: vezme N naposledy nalezených
inzerátů ve sledovaných obcích (bez duplicit), spočítá skóre znovu z aktuálních
srovnatelných (obec -> okres -> kraj) a vypíše je s tím, o kolik jsou za m²
levnější/dražší než typická cena v obci. Skóre se v DB rovnou aktualizuje.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from flip.config import FlipConfig
from flip.market import scope_label
from flip.model import Listing
from flip.notifier import Notifier, code_tag, czk, czk_short, describe_kind, e, link
from flip.pipeline import _score, load_markets, over_budget, within_budget
from flip.sanity import clean
from flip.scoring import ScoreResult
from flip.sources import DISPLAY_NAMES
from flip.store import Store

DEFAULT_N = 10
_FIELDS = ("source", "external_id", "url", "kind", "subtype", "price", "area_m2", "land_m2", "town", "locality",
           "description", "building_condition", "energy_rating", "gps_lat", "gps_lon", "parse_confidence")


def _listing(row: sqlite3.Row) -> Listing:
    return Listing(**{f: row[f] for f in _FIELDS})


def latest_rescored(store: Store, cfg: FlipConfig, n: int = DEFAULT_N) -> list[tuple[Listing, ScoreResult]]:
    rows = store.conn.execute(
        """SELECT * FROM listings WHERE town IS NOT NULL AND dup_of IS NULL AND gone_at IS NULL
           ORDER BY first_seen DESC LIMIT ?""", (n,),
    ).fetchall()
    markets = load_markets(store, cfg)
    out = []
    for row in rows:
        lst = clean(_listing(row))        # starší řádky: chybějící plocha z popisu apod.
        if lst is None or not cfg.wants_kind(lst.kind):
            continue
        _, res = _score(store, lst, markets, cfg)
        out.append((lst, res))
    return out


def format_latest(items: list[tuple[Listing, ScoreResult]], cfg: FlipConfig) -> str:
    if not items:
        return "No listings in your towns yet."
    lines = [f"🆕 <b>Latest {len(items)} listings, re-scored against today's market</b>", ""]
    for lst, res in items:
        bits = [f"<b>{res.score if res.score is not None else '?'}</b>{'🔥' if res.hot else ''}",
                e(describe_kind(lst)), e(lst.town), czk(lst.price) if lst.price else "price on request"]
        if lst.area_m2:
            bits.append(f"{lst.area_m2:g} m²")
        if res.vs_median_pct is not None:
            bits.append(f"{res.vs_median_pct:+.0f} % vs {e(scope_label(res.market.scope, lst.town))}")
        if res.estimate:
            bits.append(f"flip {czk_short(res.estimate.margin)}")
        if over_budget(lst, cfg):
            bits.append("💸 over budget" + (" but cheap per m²" if within_budget(lst, res, cfg) else ""))
        lines.append(f"• {' · '.join(bits)}{code_tag(lst.code)} — "
                     f"{link(lst.url, DISPLAY_NAMES.get(lst.source, lst.source))}")
    lines.append("\n<i>vs = price per m² compared with similar listings there. Asking prices, not sale prices.</i>")
    return "\n".join(lines)


def cmd_latest(message: dict[str, Any], store: Store, cfg: FlipConfig, notifier: Notifier) -> None:
    parts = (message.get("text") or "").split()
    n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else DEFAULT_N
    notifier.send(format_latest(latest_rescored(store, cfg, min(n, 25)), cfg))


def expensive_rescored(store: Store, cfg: FlipConfig) -> list[tuple[Listing, ScoreResult]]:
    """Všechny aktivní inzeráty nad rozpočtem ve sledovaných obcích, znovu oskórované,
    seřazené od nejlevnějšího za m² proti obci. Bez srovnání (málo podobných) vynechané."""
    rows = store.conn.execute(
        """SELECT * FROM listings WHERE town IS NOT NULL AND dup_of IS NULL AND gone_at IS NULL
           AND price > ?""", (cfg.price_max,),
    ).fetchall()
    markets = load_markets(store, cfg)
    out = []
    for row in rows:
        lst = clean(_listing(row))
        if lst is None or not cfg.wants_kind(lst.kind):
            continue
        _, res = _score(store, lst, markets, cfg)
        if res.vs_median_pct is not None:
            out.append((lst, res))
    out.sort(key=lambda x: x[1].vs_median_pct)
    return out


def format_expensive(items: list[tuple[Listing, ScoreResult]], cfg: FlipConfig, n: int) -> str:
    budget = f"{cfg.price_max / 1_000_000:g}M"
    if not items:
        return f"No listings over {budget} with enough similar ones to compare yet."
    good = [x for x in items if within_budget(x[0], x[1], cfg)]
    lines = [f"💸 <b>Over {budget}: {len(items)} listings re-scored</b>",
             f"{len(good)} are at least {cfg.over_budget_discount_pct:g} % cheaper per m² than similar ones "
             "(those get sent as alerts). Cheapest per m² first:", ""]
    for lst, res in items[:n]:
        mark = "✅" if within_budget(lst, res, cfg) else "•"
        bits = [e(describe_kind(lst)), e(lst.town), czk(lst.price)]
        if lst.area_m2:
            bits.append(f"{lst.area_m2:g} m²")
        bits.append(f"{res.vs_median_pct:+.0f} % vs {e(scope_label(res.market.scope, lst.town))}")
        if res.estimate:
            bits.append(f"flip {czk_short(res.estimate.margin)}")
        lines.append(f"{mark} {' · '.join(bits)}{code_tag(lst.code)} — "
                     f"{link(lst.url, DISPLAY_NAMES.get(lst.source, lst.source))}")
    if len(items) > n:
        lines.append(f"…and {len(items) - n} more (e.g. /expensive {min(len(items), 25)})")
    lines.append("\n<i>Asking prices, not sale prices. Send a number to see one with similar listings.</i>")
    return "\n".join(lines)


def cmd_expensive(message: dict[str, Any], store: Store, cfg: FlipConfig, notifier: Notifier) -> None:
    parts = (message.get("text") or "").split()
    n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else DEFAULT_N
    notifier.send(format_expensive(expensive_rescored(store, cfg), cfg, max(1, min(n, 25))))


def register(bot: Any) -> None:
    bot.register_command("latest", cmd_latest)
    bot.register_command("expensive", cmd_expensive)
