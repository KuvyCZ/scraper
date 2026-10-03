"""
notifier.py — Telegram notifikace přes PROSTÉ HTTP volání Bot API (httpx).

Žádný python-telegram-bot (async, těžký na RAM). Jen httpx.post na
api.telegram.org/bot{token}/sendMessage.

parse_mode = HTML (NE MarkdownV2 — formát má '-', '.', '(' a escapování MarkdownV2
by byl očistec). Dynamický text se escapuje přes html.escape.

Posílá:
  * notify_listing() — jen inzeráty NAD prahem (rozhodnutí o prahu je v main.py).
  * heartbeat()      — jednou denně "✅ Běžím, X inzerátů, Y nad prahem" (kdy zavolat
    řeší main.py; tichý výpadek scraperu jinak zjistíš za měsíc).

Notifier NIKDY nevyhazuje výjimku ven — chyba odeslání se zaloguje a vrátí False,
ať kvůli Telegramu nespadne celý běh. dry_run=True jen vypíše zprávu (vývoj/test).
"""

from __future__ import annotations

import html
import logging
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from scoring import ScoreResult
    from sources.base import Listing

log = logging.getLogger("notifier")

API_URL = "https://api.telegram.org/bot{token}/sendMessage"

# Emoji pro jednotlivé signály skóre (pořadí výpisu).
_SIGNAL_EMOJI: dict[str, str] = {
    "stav": "🔨",
    "cena_m2": "💸",
    "urgent": "⚠️",
    "pokles": "📉",
    "doba": "🕐",
}
_SIGNAL_ORDER = ["stav", "cena_m2", "urgent", "pokles", "doba"]


def _fmt_czk(value: int | float | None) -> str:
    """1890000 -> '1 890 000' (mezera jako oddělovač tisíců, CZ styl)."""
    if value is None:
        return "—"
    return f"{int(round(value)):,}".replace(",", " ")


class TelegramNotifier:
    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        timeout: float = 15.0,
        dry_run: bool = False,
    ) -> None:
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout
        self.dry_run = dry_run

    # ------------------------------------------------------------------ #
    # Nízkoúrovňové odeslání                                             #
    # ------------------------------------------------------------------ #

    def _send(self, text: str) -> bool:
        if self.dry_run:
            log.info("[dry_run] zpráva by se odeslala:\n%s", text)
            return True
        try:
            resp = httpx.post(
                API_URL.format(token=self.token),
                data={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": "true",
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return True
        except Exception as exc:  # noqa: BLE001 – Telegram nesmí shodit běh
            log.error("Odeslání na Telegram selhalo: %s", exc)
            return False

    # ------------------------------------------------------------------ #
    # Formátování a odeslání inzerátu                                    #
    # ------------------------------------------------------------------ #

    def _comps_lines(self, comps: Any) -> list[str]:
        """Blok 'srovnatelné v okolí' + ekonomika rekonstrukce. Formulace drží nejistotu:
        jsou to NABÍDKOVÉ ceny a hrubý odhad nákladů, ne ocenění."""
        if comps is None or not comps.has_data:
            return []
        lines = ["", f"📐 <b>Srovnatelné v okolí</b> ({html.escape(comps.scope)}, {comps.count} ks)"]
        spread = ""
        if comps.p25_ppm2 and comps.p75_ppm2:
            spread = f" · rozptyl {_fmt_czk(comps.p25_ppm2)}–{_fmt_czk(comps.p75_ppm2)}"
        lines.append(f"   nabídky: {_fmt_czk(comps.median_price)} Kč "
                     f"({_fmt_czk(comps.median_ppm2)} Kč/m²{spread})")
        if comps.vs_median_pct is not None:
            lines.append(f"   tato: {_fmt_czk(comps.subject_ppm2)} Kč/m² "
                         f"({comps.vs_median_pct:+.0f} % vs medián)")
        if comps.median_land_m2:
            lines.append(f"   medián pozemku srovnatelných: {_fmt_czk(comps.median_land_m2)} m²")
        if comps.arv_value:
            lines.append(f"   po rekonstrukci ~{_fmt_czk(comps.arv_value)} Kč "
                         f"(z {comps.arv_count} hotových)")
        if comps.margin is not None:
            verdict = "✅" if comps.margin > 0 else "❌"
            lines.append(f"   {verdict} odhad: −{_fmt_czk(comps.reno_cost)} Kč reko → "
                         f"marže <b>{_fmt_czk(comps.margin)} Kč</b> ({comps.margin_pct:+.0f} %)")
        if comps.warning:
            lines.append(f"   ⚠️ {html.escape(comps.warning)}")
        return lines

    def format_listing(self, listing: "Listing", result: "ScoreResult", comps: Any = None) -> str:
        e = lambda s: html.escape(str(s)) if s is not None else "—"  # noqa: E731

        area = f"{listing.area_m2:g} m²" if listing.area_m2 else "? m²"
        header = f"🏠 <b>{e(listing.disposition)}, {area}, {e(listing.locality_text)}</b>"

        ppm2 = listing.price_per_m2
        price_line = (
            f"💰 {_fmt_czk(listing.price)} Kč" + (f" ({_fmt_czk(ppm2)} Kč/m²)" if ppm2 else "")
            if listing.price else "💰 Cena na vyžádání (info u RK)"
        )

        lines = [header, price_line, f"📊 Skóre: <b>{result.score}/100</b>"]
        for sig in _SIGNAL_ORDER:
            reason = result.reasons.get(sig)
            if reason:
                lines.append(f"   {_SIGNAL_EMOJI[sig]} {e(reason)}")
        lines.extend(self._comps_lines(comps))
        lines.append(f"🌐 Zdroj: {e(listing.source.capitalize())}")
        lines.append(f'🔗 <a href="{html.escape(listing.url, quote=True)}">Zobrazit inzerát</a>')
        return "\n".join(lines)

    def notify_listing(self, listing: "Listing", result: "ScoreResult", comps: Any = None) -> bool:
        return self._send(self.format_listing(listing, result, comps))

    # ------------------------------------------------------------------ #
    # Heartbeat                                                          #
    # ------------------------------------------------------------------ #

    def heartbeat(self, total: int, above_threshold: int, *, threshold: int | None = None) -> bool:
        thr = f" (práh {threshold})" if threshold is not None else ""
        return self._send(
            f"✅ Běžím. Zpracováno {total} inzerátů, {above_threshold} nad prahem{thr}."
        )

    # ------------------------------------------------------------------ #
    # Startup notice (po Buildu/restartu kontejneru)                      #
    # ------------------------------------------------------------------ #

    def startup(
        self,
        sources: list[str],
        *,
        threshold: int | None = None,
        interval_hours: float | None = None,
        db_count: int | None = None,
    ) -> bool:
        """Po startu procesu: 'nasazeno a běží' + diagnostika (zdroje, práh, počet v DB)."""
        lines = ["✅ <b>Scraper nasazen a běží.</b>", f"Zdroje: {html.escape(', '.join(sources))}"]
        meta = []
        if threshold is not None:
            meta.append(f"práh {threshold}")
        meta.append(f"interval {interval_hours:g} h" if interval_hours and interval_hours > 0 else "jednorázový běh")
        lines.append(" · ".join(meta))
        if db_count is not None:
            lines.append(f"📦 V DB {db_count} inzerátů")  # 0 = nenamountovaná předplněná DB
        return self._send("\n".join(lines))


if __name__ == "__main__":
    import sys
    from datetime import date

    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # Lazy import až tady (modul nemá tvrdou závislost na scoringu kvůli typům).
    from scoring import score_listing
    from sources.base import Listing

    listing = Listing(
        source="sreality", source_id="42", url="https://www.sreality.cz/detail/prodej/byt/2+kk/sumperk-temenice/42",
        price=1_890_000, area_m2=54.0, disposition="2+kk",
        building_condition="Před rekonstrukcí", energy_rating="G",
        description="Byt v původním stavu, z dědictví, nutný rychlý prodej.",
        locality_text="Šumperk, Šumperk", district="Šumperk", region="Olomoucký kraj",
        listed_date=date(2026, 5, 1),
    )
    result = score_listing(
        listing,
        district_samples=[55000.0] * 10, region_samples=[55000.0] * 10,
        price_history=[(2_050_000, "2026-05-20T08:00:00+00:00"), (1_890_000, "2026-06-03T08:00:00+00:00")],
        days_on_market=41,
    )

    notifier = TelegramNotifier(token="TEST", chat_id="0", dry_run=True)
    print("=== náhled Telegram zprávy (HTML) ===\n")
    print(notifier.format_listing(listing, result))
    print("\n=== heartbeat ===")
    notifier.heartbeat(total=735, above_threshold=3, threshold=70)
    print("\n✅ notifier.py OK — formát zprávy i heartbeat sestaveny (dry_run).")
