"""flip/lookup.py — práce s konkrétní nabídkou podle jejího čísla (#1234) a oblíbené.

Každá nemovitost má krátké číslo (#1234 ve zprávách, /drops, /stale, /best, CSV).
Příkazy (zapojují se do flip/bot.py, odpovídají jen v TELEGRAM_CHAT_ID):
  /find 1234       nabídka znovu oskórovaná proti dnešnímu trhu + hotové (po rekonstrukci)
                   v okolí za plnou cenu + podobné v okolí. Stačí poslat i jen "1234".
  /fav 1234 [pozn] uložit do oblíbených (nebo tlačítko ⭐ Save pod zprávou);
                   oblíbená hlásí KAŽDOU změnu ceny a zmizení
  /unfav 1234      odebrat
  /favs            oblíbené s aktuální cenou a stavem
  /best [N]        nejlepších N nabídek, které jsou pořád online, oskórovaných dnes
"""

from __future__ import annotations

import sqlite3
import statistics
from typing import Any

from flip.bot import similar_line, similar_listings
from flip.config import FlipConfig
from flip.export import listing_status
from flip.latest import _listing
from flip.market import row_condition
from flip.model import Listing
from flip.notifier import (FAV_PREFIX, Notifier, code_tag, czk, czk_short, describe_kind, e, format_new, link,
                           money_short, price_story)
from flip.pipeline import _score, _with_budget_note, load_markets, over_budget, within_budget
from flip.sanity import clean
from flip.scoring import ScoreResult
from flip.sources import DISPLAY_NAMES
from flip.store import Store, cluster_key

RENOVATED_LIMIT = 5
SIMILAR_LIMIT = 5
BEST_DEFAULT = 10
BEST_MAX = 25


def _code_arg(message: dict[str, Any]) -> tuple[int | None, str]:
    """"/fav #1234 nice garden" -> (1234, "nice garden")."""
    parts = (message.get("text") or "").split(maxsplit=2)
    if len(parts) < 2 or not parts[1].lstrip("#").isdigit():
        return None, ""
    return int(parts[1].lstrip("#")), parts[2].strip() if len(parts) > 2 else ""


def _rescore(store: Store, cfg: FlipConfig, row: sqlite3.Row, markets=None) -> tuple[Listing, ScoreResult] | None:
    lst = clean(_listing(row))
    if lst is None:
        return None
    _, res = _score(store, lst, markets or load_markets(store, cfg), cfg)
    return lst, res


# ---------------------------------------------------------------------- #
# /find                                                                  #
# ---------------------------------------------------------------------- #

def find_text(store: Store, cfg: FlipConfig, code: int) -> str:
    row = store.by_code(code)
    if row is None:
        return f"No listing #{code}. The number is shown in every message, /drops, /stale and /best."
    scored = _rescore(store, cfg, row)
    status = listing_status(row, store.now())
    head = {"gone": "🏁 gone from the site (most likely sold)", "not seen lately": "❔ not seen on the site lately"}
    parts = []
    if scored:
        lst, res = scored
        parts.append(_with_budget_note(format_new(lst, res), lst, res, cfg))
    if status in head:
        parts.append(head[status])
    if store.is_favorite(cluster_key(row["source"], row["external_id"])):
        parts.append("⭐ in your favourites")

    # hotové po rekonstrukci v okolí = za kolik se to dá prodat po opravě
    renovated = lambda r: row_condition(r["building_condition"], r["description"]) == "renovated"  # noqa: E731
    where, reno = similar_listings(store, row, cfg.market_window_days, keep=renovated)
    if row["area_m2"]:
        if reno:
            typical = statistics.median(r["price"] / r["area_m2"] for r in reno)
            full = typical * row["area_m2"]
            lines = [f"\n🛠 <b>Renovated, similar size in {e(where)}</b>: typically {czk(typical)}/m²,"
                     f" so about {czk(full)} for {row['area_m2']:g} m² after renovation"]
            if row["price"]:
                lines.append(f"   This one asks {czk(row['price'])}: a gap of {czk_short(full - row['price'])}"
                             f" before renovation costs (~{money_short(cfg.reno_cost_m2(row['kind']) * row['area_m2'])})")
            lines += [similar_line(r, row["town"]) for r in reno[:RENOVATED_LIMIT]]
            parts.append("\n".join(lines))
        else:
            parts.append(f"\n🛠 No renovated listings of similar size in {e(where or row['town'])} yet.")
        where, similar = similar_listings(store, row, cfg.market_window_days)
        if similar:
            lines = [f"\n🔎 <b>Similar in {e(where)}</b> ({len(similar)})"]
            lines += [similar_line(r, row["town"]) for r in similar[:SIMILAR_LIMIT]]
            parts.append("\n".join(lines))
    parts.append(f"\n<i>/fav {code} to save it. Asking prices, not sale prices.</i>")
    return "\n".join(parts)


def cmd_find(message: dict[str, Any], store: Store, cfg: FlipConfig, notifier: Notifier) -> None:
    code, _ = _code_arg(message)
    if code is None:
        notifier.send("Send /find and the listing number, e.g. /find 1234 (or just 1234).")
        return
    row = store.by_code(code)
    key = (row["source"], row["external_id"]) if row is not None else None
    notifier.send(find_text(store, cfg, code), similar_key=key)


# ---------------------------------------------------------------------- #
# Oblíbené                                                               #
# ---------------------------------------------------------------------- #

def _favorite(store: Store, row: sqlite3.Row, note: str = "") -> str:
    key = store.cluster_of(row["source"], row["external_id"])
    new = store.add_favorite(key, note or None)
    code = store.code_of(key)
    return (f"⭐ Saved #{code}. You'll get a message on every price change and if it disappears. /favs lists them."
            if new else f"⭐ #{code} is already in your favourites" + (" (note updated)." if note else "."))


def cmd_fav(message: dict[str, Any], store: Store, cfg: FlipConfig, notifier: Notifier) -> None:
    code, note = _code_arg(message)
    row = store.by_code(code) if code is not None else None
    if row is None:
        notifier.send("Send /fav and the listing number, e.g. /fav 1234 good street. Or tap ⭐ Save under a listing.")
        return
    notifier.send(_favorite(store, row, note))


def cmd_unfav(message: dict[str, Any], store: Store, cfg: FlipConfig, notifier: Notifier) -> None:
    code, _ = _code_arg(message)
    row = store.by_code(code) if code is not None else None
    if row is None:
        notifier.send("Send /unfav and the listing number, e.g. /unfav 1234.")
        return
    removed = store.remove_favorite(store.cluster_of(row["source"], row["external_id"]))
    notifier.send(f"Removed #{code} from favourites." if removed else f"#{code} wasn't in your favourites.")


def fav_callback(query: dict, store: Store, cfg: FlipConfig, notifier: Notifier) -> None:
    source, _, external_id = (query.get("data") or "")[len(FAV_PREFIX):].partition(":")
    row = store.get(source, external_id)
    notifier.send(_favorite(store, row) if row is not None else "⭐ That listing is no longer in the database.")


def favs_text(store: Store, cfg: FlipConfig) -> str:
    favs = store.favorites()
    if not favs:
        return "No favourites yet. Tap ⭐ Save under a listing, or send /fav 1234."
    now = store.now()
    lines = [f"⭐ <b>Your favourites</b> ({len(favs)})", ""]
    for f in favs:
        source, _, external_id = f["cluster_key"].partition(":")
        row = store.get(source, external_id)
        if row is None:
            continue
        status = listing_status(row, now)
        bits = [f"#{row['code']}", e(describe_kind(_listing(row))), e(row["town"]),
                czk(row["price"]) if row["price"] else "price on request"]
        if row["area_m2"]:
            bits.append(f"{row['area_m2']:g} m²")
        if story := price_story(store.price_steps(source, external_id)):
            bits.append(story)
        if status != "online":
            bits.append("🏁 gone" if status == "gone" else "❔ not seen lately")
        note = f" — <i>{e(f['note'])}</i>" if f["note"] else ""
        lines.append(f"• {' · '.join(bits)} — {link(row['url'], DISPLAY_NAMES.get(source, source))}{note}")
    lines.append("\n<i>Send the number (e.g. 1234) for details, /unfav 1234 to remove.</i>")
    return "\n".join(lines)


def cmd_favs(message: dict[str, Any], store: Store, cfg: FlipConfig, notifier: Notifier) -> None:
    notifier.send(favs_text(store, cfg))


# ---------------------------------------------------------------------- #
# /best                                                                  #
# ---------------------------------------------------------------------- #

def best(store: Store, cfg: FlipConfig, n: int = BEST_DEFAULT) -> list[tuple[Listing, ScoreResult]]:
    """Nejlepší nabídky, které jsou pořád online, v rozpočtu (nebo výrazně levné za m²),
    oskórované proti dnešnímu trhu. 🔥 první, pak podle skóre."""
    now = store.now()
    rows = [r for r in store.conn.execute(
        "SELECT * FROM listings WHERE town IS NOT NULL AND dup_of IS NULL AND gone_at IS NULL")
        if listing_status(r, now) == "online" and cfg.wants_kind(r["kind"]) and cfg.area_ok(r["area_m2"])]
    markets = load_markets(store, cfg)
    out = []
    for row in rows:
        scored = _rescore(store, cfg, row, markets)
        if scored and scored[1].score is not None and within_budget(*scored, cfg):
            out.append(scored)
    out.sort(key=lambda x: (x[1].hot, x[1].score or 0), reverse=True)
    return out[:n]


def best_text(store: Store, cfg: FlipConfig, n: int = BEST_DEFAULT) -> str:
    items = best(store, cfg, n)
    if not items:
        return "Nothing scored in your towns yet."
    lines = [f"🏆 <b>Best {len(items)} listings still online</b>, re-scored against today's prices", ""]
    for lst, res in items:
        bits = [f"<b>{res.score}</b>{'🔥' if res.hot else ''}", e(describe_kind(lst)), e(lst.town),
                czk(lst.price) if lst.price else "price on request"]
        if lst.area_m2:
            bits.append(f"{lst.area_m2:g} m²")
        if res.vs_median_pct is not None:
            bits.append(f"{res.vs_median_pct:+.0f} % vs town".replace("-", "−"))
        if res.estimate:
            bits.append(f"flip {czk_short(res.estimate.margin)}")
        if res.price_steps and len(res.price_steps) > 1 and res.price_steps[-1][0] < res.price_steps[0][0]:
            bits.append("📉 cut")
        if over_budget(lst, cfg):
            bits.append("💸 over budget but cheap per m²")
        lines.append(f"• {' · '.join(bits)}{code_tag(lst.code)} — "
                     f"{link(lst.url, DISPLAY_NAMES.get(lst.source, lst.source))}")
    lines.append("\n<i>Send a number (e.g. 1234) to see it with renovated and similar listings nearby.</i>")
    return "\n".join(lines)


def cmd_best(message: dict[str, Any], store: Store, cfg: FlipConfig, notifier: Notifier) -> None:
    parts = (message.get("text") or "").split()
    n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else BEST_DEFAULT
    notifier.send(best_text(store, cfg, max(1, min(n, BEST_MAX))))


def register(bot: Any) -> None:
    bot.register_command("find", cmd_find)
    bot.register_command("fav", cmd_fav)
    bot.register_command("unfav", cmd_unfav)
    bot.register_command("favs", cmd_favs)
    bot.register_command("best", cmd_best)
    bot.register_callback(FAV_PREFIX, fav_callback)
