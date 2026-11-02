"""flip/notifier.py — Telegram zprávy (Bot API, parse_mode HTML).

Nikdy nevyhazuje výjimku ven: chyba se zaloguje a vrátí False (zpráva se pak
zkusí znovu příští běh, protože se neoznačí jako odeslaná). dry_run jen loguje.
"""

from __future__ import annotations

import html
import logging
import time

import httpx

from flip.market import scope_label
from flip.model import Listing
from flip.scoring import ScoreResult
from flip.sources import DISPLAY_NAMES
from flip.text import RISK_LABELS, TAGGED_HOUSE_SUBTYPES
from flip.towns import normalize

log = logging.getLogger("flip.notifier")

API_URL = "https://api.telegram.org/bot{token}/sendMessage"
MAX_LEN = 4000  # Telegram limit 4096


def e(value: object) -> str:
    return html.escape(str(value)) if value is not None else "—"


def czk(value: float | None) -> str:
    """2450000 -> '2 450 000 Kč'."""
    if value is None:
        return "—"
    return f"{round(value):,} Kč".replace(",", " ")


def czk_short(value: float | None) -> str:
    """2450000 -> '2.45M', 480000 -> '480k'."""
    if value is None:
        return "—"
    sign = "−" if value < 0 else "+"
    v = abs(value)
    body = f"{v / 1_000_000:.2f}".rstrip("0").rstrip(".") + "M" if v >= 1_000_000 else f"{round(v / 1000)}k"
    return f"{sign}{body}"


def money_short(value: float | None) -> str:
    return czk_short(value).lstrip("+")


def link(url: str, text: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{e(text)}</a>'


def describe_kind(lst: Listing) -> str:
    if lst.kind == "byt":
        return f"Flat {lst.subtype}" if lst.subtype else "Flat"
    sub = {
        "Rodinný": "Family house", "Vila": "Villa", "Chalupa": "Chalupa (cottage)",
        "Zemědělská usedlost": "Farmhouse", "Vícegenerační dům": "Multi-generation house",
        "Památka/jiné": "Historic/other house",
    }.get(lst.subtype or "", lst.subtype or "House")
    return sub


def _body(lst: Listing, res: ScoreResult) -> list[str]:
    lines: list[str] = []

    size = []
    if lst.area_m2:
        size.append(f"{lst.area_m2:g} m²")
    if lst.land_m2:
        size.append(f"land {lst.land_m2:,.0f} m²".replace(",", " "))
    if size:
        lines.append(" · ".join(size))

    price = f"<b>{czk(lst.price)}</b>" if lst.price else "<b>price on request</b>"
    if res.ppm2:
        price += f" · {czk(res.ppm2)}/m²"
        if res.vs_median_pct is not None:
            where = scope_label(res.market.scope, lst.town)
            price += f" ({res.vs_median_pct:+.0f} % vs median, {res.market.comps_n} similar in {where})"
    lines.append(price)

    if res.condition_label:
        lines.append(f"🔨 {e(res.condition_label)}")
    else:
        lines.append("🔨 condition unknown — check photos")

    est = res.estimate
    if est:
        verdict = "✅" if est.margin > 0 else "❌"
        lines.append(
            f"💰 Flip estimate: sells ~{money_short(est.arv)} after reno · reno ~{money_short(est.reno_cost)}"
            f" · costs ~{money_short(est.holding_cost)} → {verdict} <b>{czk_short(est.margin)} ({est.margin_pct:+.0f} %)</b>"
        )
        where = scope_label(res.market.arv_scope, lst.town)
        lines.append(f"   <i>from {res.market.arv_n} renovated listings in {where}; asking prices, not sale prices</i>")
    elif res.needs_work:
        lines.append("💰 Flip estimate: not enough renovated listings nearby yet to estimate resale price")

    extra = []
    if res.price_drop_pct and res.price_drop_pct > 0:
        extra.append(f"📉 {res.price_drop_pct:.0f} % cheaper than when first seen")
    if res.days_on_market and res.days_on_market >= 30:
        extra.append(f"⏳ {res.days_on_market} days on market")
    if res.motivated_text:
        extra.append("🏃 seller sounds motivated")
    lines.extend(extra)

    for risk in res.risks:
        lines.append(f"⚠️ {e(RISK_LABELS[risk])}")
    if lst.subtype in TAGGED_HOUSE_SUBTYPES:
        lines.append("⚠️ listed as chalupa — check it's livable year-round")
    if lst.parse_confidence == "low":
        lines.append("❓ size/layout not clearly stated — check the listing")

    if lst.locality:
        # Bazoš píše u inzerátu často "Šumperk", i když PSČ patří jiné obci — ukaž, co platí
        if lst.town and normalize(lst.town) not in normalize(lst.locality):
            lines.append(f"📍 {e(lst.town)} (site says: {e(lst.locality)})")
        else:
            lines.append(f"📍 {e(lst.locality)}")
    src = link(lst.url, DISPLAY_NAMES.get(lst.source, lst.source))
    if lst.duplicate_sources:
        others = ", ".join(link(u, DISPLAY_NAMES.get(s, s)) for s, u in lst.duplicate_sources)
        src += f" · also on {others}"
    lines.append(f"🔗 {src}")
    return lines


def header_title(lst: Listing, res: ScoreResult) -> str:
    score = f"{res.score}/100" if res.score is not None else "?/100"
    return f"<b>{score}</b> · {e(describe_kind(lst))} · {e(lst.town)}"


def format_new(lst: Listing, res: ScoreResult) -> str:
    icon = "🔥" if res.hot else "🏠"
    return "\n".join([f"{icon} {header_title(lst, res)}", *_body(lst, res)])


def format_drop(lst: Listing, res: ScoreResult, old_price: int) -> str:
    icon = "🔥📉" if res.hot else "📉"
    diff = (lst.price or 0) - old_price
    pct = diff / old_price * 100 if old_price else 0
    head = (f"{icon} <b>Price drop {czk_short(diff)} ({pct:.0f} %)</b>: {czk(old_price)} → {czk(lst.price)}"
            f" on {e(DISPLAY_NAMES.get(lst.source, lst.source))}")
    return "\n".join([head, header_title(lst, res), *_body(lst, res)])


def format_summary_line(lst: Listing, res: ScoreResult) -> str:
    icon = "🔥" if res.hot else "•"
    bits = [f"{res.score if res.score is not None else '?'}", e(describe_kind(lst)), e(lst.town),
            czk(lst.price) if lst.price else "price on request"]
    if lst.area_m2:
        bits.append(f"{lst.area_m2:g} m²")
    if res.estimate:
        bits.append(f"flip {czk_short(res.estimate.margin)}")
    return f"{icon} {' · '.join(bits)} — {link(lst.url, 'open')}"


class Notifier:
    def __init__(self, token: str, chat_id: str, *, dry_run: bool = False, timeout: float = 15.0) -> None:
        self.token = token
        self.chat_id = chat_id
        self.dry_run = dry_run
        self.timeout = timeout

    def send(self, text: str) -> bool:
        if len(text) > MAX_LEN:
            text = text[: MAX_LEN - 1] + "…"
        if self.dry_run:
            log.info("[dry_run] Telegram:\n%s", text)
            return True
        for attempt in range(2):
            try:
                resp = httpx.post(
                    API_URL.format(token=self.token),
                    data={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                          "disable_web_page_preview": "true"},
                    timeout=self.timeout,
                )
                if resp.status_code == 429 and attempt == 0:   # flood limit -> počkej, co Telegram řekne
                    retry_after = (resp.json().get("parameters") or {}).get("retry_after", 5)
                    time.sleep(min(int(retry_after), 60))
                    continue
                resp.raise_for_status()
                time.sleep(1.0)   # max ~1 zpráva/s do jednoho chatu
                return True
            except Exception as exc:  # noqa: BLE001 – Telegram nesmí shodit běh
                log.error("Telegram selhal: %s", exc)
                return False
        return False

    def send_failure_warning(self, source: str, failures: int, error: str) -> bool:
        return self.send(
            f"⚠️ <b>{e(DISPLAY_NAMES.get(source, source))} failed {failures}× in a row.</b>\n"
            f"Last error: {e(error[:500])}\n"
            "Other sources keep running. If it keeps failing, the site may have changed — "
            "run <code>python main.py check</code>."
        )
