"""flip/notifier.py — Telegram zprávy (Bot API, parse_mode HTML).

Nikdy nevyhazuje výjimku ven: chyba se zaloguje a vrátí False (zpráva se pak
zkusí znovu příští běh, protože se neoznačí jako odeslaná). dry_run jen loguje.
"""

from __future__ import annotations

import html
import json
import logging
import time
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Mapping

import httpx

from flip.market import scope_label
from flip.model import Listing
from flip.scoring import ScoreResult
from flip.sources import DISPLAY_NAMES
from flip.text import RISK_LABELS, TAGGED_HOUSE_SUBTYPES
from flip.towns import normalize

log = logging.getLogger("flip.notifier")

API_URL = "https://api.telegram.org/bot{token}/sendMessage"
METHOD_URL = "https://api.telegram.org/bot{token}/{method}"
SIMILAR_PREFIX = "sim:"           # callback_data tlačítka "🔎 Similar nearby": sim:{source}:{external_id}
FAV_PREFIX = "fav:"               # callback_data tlačítka "⭐ Save": fav:{source}:{external_id}
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


def short_date(iso_ts: str | None) -> str:
    """'2026-10-12T08:00:00+00:00' -> '12 Oct'."""
    if not iso_ts:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_ts)
    except ValueError:
        return iso_ts[:10]
    return f"{dt.day} {dt.strftime('%b')}"


def price_story(steps: list[tuple[int, str]], *, max_steps: int = 5) -> str | None:
    """[(3790000, t0), (3550000, t1), (3450000, t2)] -> '3.79M → 3.55M → 3.45M in 18 days (2 cuts, −9 %)'.
    None = cena se nikdy neměnila."""
    if len(steps) < 2:
        return None
    prices = [p for p, _ in steps]
    shown = [money_short(p) for p in prices[-max_steps:]]
    if len(prices) > max_steps:
        shown.insert(0, "…")
    cuts = sum(1 for a, b in zip(prices, prices[1:]) if b < a)
    rises = len(prices) - 1 - cuts
    try:
        days = (datetime.fromisoformat(steps[-1][1]) - datetime.fromisoformat(steps[0][1])).days
    except ValueError:
        days = None
    total = (prices[-1] / prices[0] - 1) * 100
    bits = []
    if cuts:
        bits.append(f"{cuts} cut{'s' if cuts > 1 else ''}")
    if rises:
        bits.append(f"{rises} rise{'s' if rises > 1 else ''}")
    bits.append(f"{total:+.0f} %".replace("-", "−"))
    when = f" in {days} day{'s' if days != 1 else ''}" if days else ""
    return f"{' → '.join(shown)}{when} ({', '.join(bits)})"


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
            land = " with similar land" if res.market.land_matched else ""
            price += f" ({res.vs_median_pct:+.0f} % vs median, {res.market.comps_n} similar{land} in {where})"
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
        sale = res.market.sale
        haggle = (f"sale price assumes asking prices fall {sale.drop_pct:.0f} % before selling, as {sale.n} gone "
                  f"listings did{' in this okres' if sale.scope == 'okres' else ''}, plus 5 % haggling"
                  if sale else "sale price assumes the usual haggling off asking")
        lines.append(f"   <i>from {res.market.arv_n} renovated listings in {where}; {haggle}</i>")
    elif res.needs_work:
        lines.append("💰 Flip estimate: not enough renovated listings nearby yet to estimate resale price")

    extra = []
    if story := price_story(res.price_steps):
        icon = "📉" if res.price_steps[-1][0] < res.price_steps[0][0] else "💲"
        extra.append(f"{icon} {story}")
    elif res.price_drop_pct and res.price_drop_pct > 0:
        extra.append(f"📉 {res.price_drop_pct:.0f} % cheaper than when first seen")
    days = res.days_on_market or 0
    if res.listed_before_start:
        since = f"online {days}+ days" if days >= 1 else "already online"
        extra.append(f"⏳ {since} (was up before watching started)")
    elif days >= 30:
        extra.append(f"⏳ {days} days on market")
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


def code_tag(code: int | None) -> str:
    return f" · #{code}" if code else ""


def header_title(lst: Listing, res: ScoreResult) -> str:
    score = f"{res.score}/100" if res.score is not None else "?/100"
    return f"<b>{score}</b> · {e(describe_kind(lst))} · {e(lst.town)}{code_tag(lst.code)}"


def format_new(lst: Listing, res: ScoreResult) -> str:
    icon = "🔥" if res.hot else "🏠"
    return "\n".join([f"{icon} {header_title(lst, res)}", *_body(lst, res)])


def format_drop(lst: Listing, res: ScoreResult, old_price: int, *, other_site: bool = False) -> str:
    """`other_site` = stará cena je z jiného webu: stejná nemovitost je tady levnější."""
    icon = "🔥📉" if res.hot else "📉"
    diff = (lst.price or 0) - old_price
    pct = diff / old_price * 100 if old_price else 0
    site = e(DISPLAY_NAMES.get(lst.source, lst.source))
    if other_site:
        head = (f"{icon} <b>Cheaper on {site} {czk_short(diff)} ({pct:.0f} %)</b>: {czk(old_price)} on the site"
                f" you got it from → {czk(lst.price)} here")
    else:
        head = (f"{icon} <b>Price drop {czk_short(diff)} ({pct:.0f} %)</b>: {czk(old_price)} → {czk(lst.price)}"
                f" on {site}")
    return "\n".join([head, header_title(lst, res), *_body(lst, res)])


def format_gone(row: Mapping[str, Any], steps: list[tuple[int, str]], days: int | None,
                listed_before_start: bool, score: int | None, hot_score: int, *,
                code: int | None = None) -> str:
    """🏁 inzerát, o kterém přišla zpráva, zmizel z webu (nejspíš prodaný). Oblíbené: format_favorite_sold."""
    kind = describe_kind(SimpleNamespace(kind=row["kind"], subtype=row["subtype"]))
    age = "" if days is None else f" after {days}{'+' if listed_before_start else ''} day{'s' if days != 1 else ''}"
    lines = [f"🏁 <b>Gone{age}</b> · {e(kind)} · {e(row['town'])}{code_tag(code)}"]
    price = f"Last price {czk(row['price'])}" if row["price"] else "Last price on request"
    if story := price_story(steps):
        price += f" ({story})"
    lines.append(price)
    if score is not None:
        lines.append(f"You got it as {score}/100{' 🔥' if score >= hot_score else ''}")
    lines.append("Most likely sold or withdrawn. — " + link(row["url"], DISPLAY_NAMES.get(row["source"], row["source"])))
    return "\n".join(lines)


def _short_date(value: str | None) -> str | None:
    try:
        d = datetime.fromisoformat(value) if value else None
    except ValueError:
        return None
    return f"{d.day} {d.strftime('%b')}" if d else None


def format_favorite_sold(row: Mapping[str, Any], steps: list[tuple[int, str]], *, first_seen: str | None,
                         days: int | None, listed_before_start: bool,
                         typical: tuple[float, int, str | None] | None = None,
                         code: int | None = None, note: str | None = None) -> str:
    """⭐🏁 oblíbená nemovitost zmizela z webu -> nejspíš prodaná: která, za kolik a jak rychle."""
    kind = describe_kind(SimpleNamespace(kind=row["kind"], subtype=row["subtype"]))
    what = [e(kind)]
    if row["area_m2"]:
        what.append(f"{row['area_m2']:g} m²")
    what.append(e(row["town"] or "?"))
    lines = ["⭐🏁 <b>Your favourite is gone, probably sold</b>", " · ".join(what) + code_tag(code)]

    span = " → ".join(d for d in (_short_date(first_seen), _short_date(row["gone_at"])) if d)
    span = f" (online {span})" if span else ""
    if days is None:
        speed = "⏱ Not sure how long it was online"
    elif listed_before_start:
        speed = (f"⏱ Online at least {days} day{'s' if days != 1 else ''}{span}; it was already listed "
                 "when the scraper started watching, so the real time is longer")
    else:
        speed = f"⏱ Sold in {days} day{'s' if days != 1 else ''}{span}"
        if typical:
            usual, n, town = typical
            where = f"in {e(town)}" if town else "in your towns"
            things = "flats" if row["kind"] == "byt" else "houses"
            if days < usual * 0.8:
                verdict = "faster than usual"
            elif days > usual * 1.2:
                verdict = "slower than usual"
            else:
                verdict = "about usual"
            speed += f", {verdict}: {things} {where} typically go in {usual:.0f} days ({n} sold)"
    lines.append(speed)

    price = f"💰 Last price {czk(row['price'])}" if row["price"] else "💰 Last price on request"
    if story := price_story(steps):
        price += f" ({story})"
    lines.append(price)
    if note:
        lines.append(f"📝 Your note: <i>{e(note)}</i>")
    lines.append("🔗 " + link(row["url"], DISPLAY_NAMES.get(row["source"], row["source"]))
                 + " <i>(the page may already be gone)</i>")
    return "\n".join(lines)


def format_favorite_change(lst: Listing, old_price: int, steps: list[tuple[int, str]]) -> str:
    """⭐ oblíbená nemovitost změnila cenu (i o méně než PRICE_DROP_ALERT_PCT)."""
    pct = ((lst.price or 0) / old_price - 1) * 100 if old_price else 0
    lines = [f"⭐ <b>Favourite{code_tag(lst.code)}: {czk(old_price)} → {czk(lst.price)} ({pct:+.0f} %)</b>".replace("-", "−"),
             f"{e(describe_kind(lst))} · {e(lst.town)}"]
    if story := price_story(steps):
        lines.append(f"📉 {story}" if steps[-1][0] < steps[0][0] else f"💲 {story}")
    lines.append("🔗 " + link(lst.url, DISPLAY_NAMES.get(lst.source, lst.source)))
    return "\n".join(lines)


def format_summary_line(lst: Listing, res: ScoreResult) -> str:
    icon = "🔥" if res.hot else "•"
    bits = [f"{res.score if res.score is not None else '?'}", e(describe_kind(lst)), e(lst.town),
            czk(lst.price) if lst.price else "price on request"]
    if lst.area_m2:
        bits.append(f"{lst.area_m2:g} m²")
    if res.estimate:
        bits.append(f"flip {czk_short(res.estimate.margin)}")
    return f"{icon} {' · '.join(bits)}{code_tag(lst.code)} — {link(lst.url, 'open')}"


class Notifier:
    def __init__(self, token: str, chat_id: str, *, dry_run: bool = False, timeout: float = 15.0) -> None:
        self.token = token
        self.chat_id = chat_id
        self.dry_run = dry_run
        self.timeout = timeout

    def send(self, text: str, *, similar_key: tuple[str, str] | None = None) -> bool:
        """`similar_key` = (source, external_id): pod zprávu přidá tlačítka 🔎 Similar nearby a ⭐ Save."""
        if len(text) > MAX_LEN:
            text = text[: MAX_LEN - 1] + "…"
        if self.dry_run:
            log.info("[dry_run] Telegram:\n%s", text)
            return True
        data = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
        if similar_key:
            ref = f"{similar_key[0]}:{similar_key[1]}"
            data["reply_markup"] = json.dumps({"inline_keyboard": [[   # limit Telegramu 64 B na callback
                {"text": "🔎 Similar nearby", "callback_data": f"{SIMILAR_PREFIX}{ref}"[:64]},
                {"text": "⭐ Save", "callback_data": f"{FAV_PREFIX}{ref}"[:64]},
            ]]})
        for attempt in range(2):
            try:
                resp = httpx.post(API_URL.format(token=self.token), data=data, timeout=self.timeout)
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

    def call(self, method: str, http_timeout: float | None = None, **params: object) -> dict | None:
        """Obecné volání Bot API (getUpdates, answerCallbackQuery). None = chyba."""
        try:
            resp = httpx.post(METHOD_URL.format(token=self.token, method=method), data=params,
                              timeout=http_timeout or self.timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("Telegram %s selhal: %s", method, exc)
            return None

    def send_document(self, filename: str, content: bytes, caption: str = "") -> bool:
        """Pošle soubor do chatu (sendDocument, max 50 MB). False = chyba (zalogovaná)."""
        if self.dry_run:
            log.info("[dry_run] Telegram soubor %s (%d B): %s", filename, len(content), caption)
            return True
        try:
            resp = httpx.post(METHOD_URL.format(token=self.token, method="sendDocument"),
                              data={"chat_id": self.chat_id, "caption": caption},
                              files={"document": (filename, content)}, timeout=120)
            resp.raise_for_status()
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("Telegram sendDocument %s selhal: %s", filename, exc)
            return False

    def send_failure_warning(self, source: str, failures: int, error: str) -> bool:
        return self.send(
            f"⚠️ <b>{e(DISPLAY_NAMES.get(source, source))} failed {failures}× in a row.</b>\n"
            f"Last error: {e(error[:500])}\n"
            "Other sources keep running. If it keeps failing, the site may have changed — "
            "run <code>python main.py check</code>."
        )
