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
from flip.market import MarketData, scope_label
from flip.model import Listing
from flip.notifier import Notifier, czk, czk_short, describe_kind, e, link
from flip.pipeline import _score, over_budget, within_budget
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
        """SELECT * FROM listings WHERE town IS NOT NULL AND dup_of IS NULL
           ORDER BY first_seen DESC LIMIT ?""", (n,),
    ).fetchall()
    markets = {
        kind: MarketData(store.market_rows(kind=kind, window_days=cfg.market_window_days, in_area_only=False))
        for kind in ("byt", "dum")
    }
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
        lines.append(f"• {' · '.join(bits)} — {link(lst.url, DISPLAY_NAMES.get(lst.source, lst.source))}")
    lines.append("\n<i>vs = price per m² compared with similar listings there. Asking prices, not sale prices.</i>")
    return "\n".join(lines)


def cmd_latest(message: dict[str, Any], store: Store, cfg: FlipConfig, notifier: Notifier) -> None:
    parts = (message.get("text") or "").split()
    n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else DEFAULT_N
    notifier.send(format_latest(latest_rescored(store, cfg, min(n, 25)), cfg))


def register(bot: Any) -> None:
    bot.register_command("latest", cmd_latest)
