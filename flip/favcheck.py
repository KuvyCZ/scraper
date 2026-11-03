"""flip/favcheck.py — ⭐🏁 zpráva, když oblíbená nemovitost zmizí (nejspíš prodaná).

Dvě cesty, jak se to pozná:
  1) Zdroj s úplným průchodem (Sreality, Reality Šumperk, Realingo když nenarazí na
     strop stránek) ji 2 dny nevidí -> pipeline._report_gone -> send_favorite_sold.
  2) U Bazoše (čte se jen nejnovějších pár stránek) a u Realinga, když narazí na strop,
     se zmizení z výpisu poznat nedá. Proto check_favorites() jednou za
     FAV_CHECK_HOURS přímo otevře stránku oblíbené, kterou žádný zdroj 2 dny neviděl:
     404/410, přesměrování pryč z inzerátu nebo Realingo bez detailu = zmizela.
     Cokoliv nejasného (chyba sítě, 5xx, robots.txt) = nic se neoznačí.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable

import httpx

from flip.config import FlipConfig
from flip.http import USER_AGENT, RobotsChecker
from flip.notifier import Notifier, format_favorite_sold
from flip.sources.realingo import parse_next_data
from flip.sources.sreality import DETAIL_URL as SREALITY_DETAIL_URL
from flip.store import GONE_AFTER_HOURS, Store, iso

log = logging.getLogger("flip.favcheck")

FAV_CHECK_HOURS = 12          # stránku jedné oblíbené otevřít nejvýš 2× denně
FAV_CHECK_PER_RUN = 10        # strop na jeden běh (běh zůstane krátký i při hodně oblíbených)

PageCheck = Callable[[str, str, str], "bool | None"]   # (source, external_id, url) -> zmizel? None = nevím


def send_favorite_sold(store: Store, notifier: Notifier, key: str, row) -> bool:
    """Pošle ⭐🏁 zprávu za oblíbenou `key`; `row` = člen shluku, který právě zmizel."""
    members = store.cluster_members(key)
    earliest = min(members, key=lambda m: m["first_seen"])
    try:
        days = max(0, (datetime.fromisoformat(row["gone_at"]) - datetime.fromisoformat(earliest["first_seen"])).days)
    except (TypeError, ValueError):
        days = None
    fav = store.conn.execute("SELECT note FROM favorites WHERE cluster_key = ?", (key,)).fetchone()
    text = format_favorite_sold(
        row, store.price_steps(row["source"], row["external_id"]),
        first_seen=earliest["first_seen"], days=days, listed_before_start=bool(earliest["seen_at_start"]),
        typical=store.typical_days_online(row["kind"], row["town"], exclude=key),
        code=store.code_of(key), note=fav["note"] if fav else None)
    return notifier.send(text)


def page_gone(source: str, external_id: str, url: str, *, client: httpx.Client | None = None,
              robots: RobotsChecker | None = None) -> bool | None:
    """Otevře stránku inzerátu: True = zmizel, False = pořád tam je, None = nejde poznat."""
    own = client is None
    client = client or httpx.Client(timeout=15.0, follow_redirects=False)
    try:
        if source == "sreality":
            target = SREALITY_DETAIL_URL.format(hash_id=external_id)
        else:
            if not url:
                return None
            robots = robots or RobotsChecker()
            if not robots.can_fetch(url):
                return None
            target = url
        resp = client.get(target, headers={"User-Agent": USER_AGENT})
        if resp.status_code in (404, 410):
            return True
        if resp.status_code in (301, 302, 303, 307, 308):
            # stažený inzerát často přesměruje na výpis/kategorii; jiná adresa téhož inzerátu ne
            return external_id not in resp.headers.get("location", "")
        if resp.status_code != 200:
            return None
        if source == "realingo":
            data = parse_next_data(resp.text)
            if not data:
                return None
            try:
                return data["props"]["pageProps"]["store"]["offer"]["details"][external_id]["offer"]["detail"] is None
            except (KeyError, TypeError):
                return None
        return False
    except httpx.HTTPError as exc:
        log.warning("Kontrola oblíbené %s:%s selhala: %s", source, external_id, exc)
        return None
    finally:
        if own:
            client.close()


def check_favorites(store: Store, notifier: Notifier, cfg: FlipConfig, *, check: PageCheck | None = None) -> int:
    """Oblíbené, které žádný zdroj 2 dny neviděl: otevřít jejich stránky. Vrací počet zpráv."""
    check = check or page_gone
    cutoff = iso(store.now() - timedelta(hours=GONE_AFTER_HOURS))
    sent = checked = 0
    for fav in store.favorites_to_check(FAV_CHECK_HOURS):
        if checked >= FAV_CHECK_PER_RUN:
            break
        key = fav["cluster_key"]
        members = store.cluster_members(key)
        online = [m for m in members if m["gone_at"] is None]
        if not online or any(m["last_seen"] >= cutoff for m in online):
            continue          # už ohlášená, nebo ji nějaký web pořád ukazuje
        checked += 1
        store.mark_favorite_checked(key)
        results = [check(m["source"], m["external_id"], m["url"]) for m in online]
        if not all(r is True for r in results):
            continue
        rows = [store.mark_listing_gone(m["source"], m["external_id"]) for m in online]
        last = max(rows, key=lambda r: r["last_seen"])
        if send_favorite_sold(store, notifier, key, last):
            sent += 1
    if checked:
        log.info("Oblíbené: zkontrolováno %d stránek, zmizelo %d", checked, sent)
    return sent
