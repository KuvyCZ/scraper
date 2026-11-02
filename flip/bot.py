"""flip/bot.py — odpovědi na tlačítko "🔎 Similar nearby" pod zprávou o inzerátu.

Tlačítko pošle Telegramu callback `sim:{source}:{external_id}`. Mezi běhy zdrojů
(místo prostého spánku) se long-pollingem `getUpdates` čeká na taková klepnutí
a bot odpoví zprávou se srovnatelnými inzeráty: stejná obec, stejný typ, podobná
plocha (±30 % bytu, ±40 % domu), viděné v posledních MARKET_WINDOW_DAYS dnech.
Když je jich v obci málo, vezme sledované obce téhož okresu.

Odpovídá jen v nastaveném TELEGRAM_CHAT_ID — cizí klepnutí ignoruje.

Další funkce bota se zapojují přes `Bot.register_command("data", fn)` (zpráva
"/data") a `Bot.register_callback("prefix:", fn)` — jen JEDEN getUpdates na token,
dva pollery by si updaty přebíraly (409 Conflict).
"""

from __future__ import annotations

import logging
import sqlite3
import statistics
import threading
import time
from datetime import timedelta
from typing import Any, Callable

from flip.config import FlipConfig
from flip.market import AREA_BAND, SANE_PPM2, row_condition
from flip.notifier import SIMILAR_PREFIX, Notifier, czk, e, link
from flip.sources import DISPLAY_NAMES
from flip.store import Store, iso
from flip.towns import TOWN_INFO, okres_of

log = logging.getLogger("flip.bot")

SIMILAR_LIMIT = 8
MIN_IN_TOWN = 3
POLL_TIMEOUT_S = 50               # Telegram drží spojení max ~50 s


def similar_listings(store: Store, row: sqlite3.Row, window_days: int) -> tuple[str | None, list[sqlite3.Row]]:
    """(kde se hledalo, srovnatelné inzeráty seřazené podle podobnosti plochy)."""
    if not row["area_m2"]:
        return None, []
    band = AREA_BAND.get(row["kind"], 0.30)
    lo, hi = row["area_m2"] * (1 - band), row["area_m2"] * (1 + band)
    since = iso(store.now() - timedelta(days=window_days))
    own_cluster = row["dup_of"] or f'{row["source"]}:{row["external_id"]}'
    candidates = [
        r for r in store.conn.execute(
            """SELECT * FROM listings
               WHERE kind = ? AND town IS NOT NULL AND last_seen >= ? AND dup_of IS NULL
                 AND price > 0 AND area_m2 BETWEEN ? AND ?""",
            (row["kind"], since, lo, hi),
        )
        if f'{r["source"]}:{r["external_id"]}' != own_cluster
        and SANE_PPM2[0] <= r["price"] / r["area_m2"] <= SANE_PPM2[1]
    ]
    in_town = [r for r in candidates if r["town"] == row["town"]]
    if len(in_town) >= MIN_IN_TOWN or not okres_of(row["town"]):
        where, picked = row["town"], in_town
    else:
        okres = okres_of(row["town"])
        where = f"okres {okres}"
        picked = [r for r in candidates if TOWN_INFO.get(r["town"]) and TOWN_INFO[r["town"]].okres == okres]
    picked.sort(key=lambda r: abs(r["area_m2"] - row["area_m2"]))
    return where, picked


def format_similar(row: sqlite3.Row, where: str | None, similar: list[sqlite3.Row]) -> str:
    kind = "flats" if row["kind"] == "byt" else "houses"
    size = f"{row['area_m2']:g}" if row["area_m2"] else "?"
    head = f"🔎 <b>Similar {kind} to {e(row['subtype'] or '')} {size} m² in {e(row['town'])}</b>"
    if not row["area_m2"]:
        return head + "\nThis listing has no size, so there's nothing to compare it with."
    if not similar:
        return head + f"\nNo similar listings in {e(where)} yet."
    ppm2 = [r["price"] / r["area_m2"] for r in similar]
    lines = [head, f"{len(similar)} found in {e(where)} · typical {czk(statistics.median(ppm2))}/m²"]
    if row["price"]:
        own = row["price"] / row["area_m2"]
        diff = (own / statistics.median(ppm2) - 1) * 100
        lines.append(f"This one: {czk(own)}/m² ({diff:+.0f} % vs typical)")
    lines.append("")
    for r in similar[:SIMILAR_LIMIT]:
        cond = row_condition(r["building_condition"], r["description"])
        tag = {"renovated": " · renovated", "needs_reno": " · to renovate"}.get(cond, "")
        town = f" · {e(r['town'])}" if r["town"] != row["town"] else ""
        lines.append(
            f"• {e(r['subtype'] or '')} {r['area_m2']:g} m² · {czk(r['price'])} · "
            f"{czk(r['price'] / r['area_m2'])}/m²{tag}{town} — "
            f"{link(r['url'], DISPLAY_NAMES.get(r['source'], r['source']))}"
        )
    if len(similar) > SIMILAR_LIMIT:
        lines.append(f"…and {len(similar) - SIMILAR_LIMIT} more")
    lines.append("<i>Asking prices, not sale prices.</i>")
    return "\n".join(lines)


Handler = Callable[[dict[str, Any], Store, FlipConfig, Notifier], None]


def handle_callback(query: dict, store: Store, cfg: FlipConfig, notifier: Notifier) -> None:
    """Výchozí obsluha klepnutí (jen 🔎 Similar nearby) — viz Bot.callbacks."""
    Bot(cfg, notifier).dispatch({"callback_query": query}, store)


def similar_callback(query: dict, store: Store, cfg: FlipConfig, notifier: Notifier) -> None:
    data = query.get("data") or ""
    source, _, external_id = data[len(SIMILAR_PREFIX):].partition(":")
    row = store.get(source, external_id)
    if row is None:
        notifier.send("🔎 That listing is no longer in the database.")
        return
    where, similar = similar_listings(store, row, cfg.market_window_days)
    notifier.send(format_similar(row, where, similar))


class Bot:
    """Odpovídá na příkazy a klepnutí: ve vlákně (start_background) nebo během wait()."""

    def __init__(self, cfg: FlipConfig, notifier: Notifier) -> None:
        self.cfg = cfg
        self.notifier = notifier
        self.offset: int | None = None
        self.callbacks: dict[str, Handler] = {SIMILAR_PREFIX: similar_callback}
        self.commands: dict[str, Handler] = {}

    def register_callback(self, prefix: str, fn: Handler) -> None:
        """fn(callback_query, store, cfg, notifier) pro callback_data začínající `prefix`."""
        self.callbacks[prefix] = fn

    def register_command(self, name: str, fn: Handler) -> None:
        """fn(message, store, cfg, notifier) pro zprávu "/name" z našeho chatu."""
        self.commands[name.lstrip("/")] = fn

    def _ours(self, chat: dict | None) -> bool:
        chat_id = str((chat or {}).get("id", ""))
        if chat_id != str(self.cfg.telegram_chat_id):
            log.warning("Zpráva/klepnutí z cizího chatu %s ignoruji", chat_id)
            return False
        return True

    def dispatch(self, update: dict[str, Any], store: Store) -> None:
        if query := update.get("callback_query"):
            self.notifier.call("answerCallbackQuery", callback_query_id=query.get("id", ""))
            if not self._ours((query.get("message") or {}).get("chat")):
                return
            data = query.get("data") or ""
            for prefix, fn in self.callbacks.items():
                if data.startswith(prefix):
                    fn(query, store, self.cfg, self.notifier)
                    return
        elif message := update.get("message"):
            text = (message.get("text") or "").strip()
            if not text.startswith("/") or not self._ours(message.get("chat")):
                return
            name = text[1:].split()[0].split("@")[0].lower() if len(text) > 1 else ""
            if fn := self.commands.get(name):
                fn(message, store, self.cfg, self.notifier)

    def start_background(self) -> threading.Thread | None:
        """Poslouchá příkazy ve vlastním vlákně, takže odpoví do pár sekund i během
        dlouhého běhu zdroje (první průchod Sreality trvá desítky minut)."""
        if self.notifier.dry_run:
            return None

        def loop() -> None:
            while True:
                try:
                    self.poll(POLL_TIMEOUT_S)
                except Exception:  # noqa: BLE001 – vlákno bota nesmí umřít
                    log.exception("Bot: chyba při čtení příkazů, zkusím znovu")
                    time.sleep(30)

        thread = threading.Thread(target=loop, name="telegram-bot", daemon=True)
        thread.start()
        return thread

    def wait(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        if self.notifier.dry_run:
            time.sleep(max(0.0, seconds))
            return
        while (left := deadline - time.monotonic()) > 1:
            self.poll(min(POLL_TIMEOUT_S, int(left)))

    def poll(self, timeout: int) -> None:
        params: dict[str, object] = {"timeout": timeout, "allowed_updates": '["callback_query", "message"]'}
        if self.offset is not None:
            params["offset"] = self.offset
        resp = self.notifier.call("getUpdates", http_timeout=timeout + 10, **params)
        if not resp or not resp.get("ok"):
            time.sleep(min(timeout, 30))     # Telegram nedostupný -> nezahlcuj ho
            return
        for update in resp.get("result") or []:
            self.offset = update["update_id"] + 1
            try:
                with Store(self.cfg.db_path) as store:
                    self.dispatch(update, store)
            except Exception:  # noqa: BLE001 – vadné klepnutí nesmí shodit sledování
                log.exception("Odpověď na klepnutí/příkaz selhala")
