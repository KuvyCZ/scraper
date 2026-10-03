"""investment/notifier.py — Telegram notifikace pro investiční kandidáty.

Záměrně NEZÁVISLÝ na notifier.py (hlavní monitoring) — jiný chat_id, jiný formát
zprávy. Stejná konvence: prosté HTTP na Bot API, parse_mode HTML, nikdy nevyhazuje
výjimku ven (chyba se zaloguje a vrátí False), dry_run jen vypíše zprávu.
"""

from __future__ import annotations

import html
import logging
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from investment.candidate import InvestmentCandidate

log = logging.getLogger("investment.notifier")

API_URL = "https://api.telegram.org/bot{token}/sendMessage"

SOURCE_DISPLAY_NAMES: dict[str, str] = {
    "sreality": "Sreality",
    "realingo": "Realingo",
    "bazos": "Bazoš",
    "reality_sumperk": "Reality Šumperk",
}


def _fmt_czk(value: int | float | None) -> str:
    """1790000 -> '1,790,000' (čárka jako oddělovač tisíců, dle šablony ze zadání)."""
    if value is None:
        return "—"
    return f"{int(round(value)):,}"


def _fmt_signed_czk(value: int | None) -> str:
    if value is None:
        return "—"
    sign = "+" if value >= 0 else ""
    return f"{sign}{_fmt_czk(value)}"


def _display_source(source: str) -> str:
    return SOURCE_DISPLAY_NAMES.get(source, source)


class InvestmentNotifier:
    def __init__(self, token: str, chat_id: str, *, timeout: float = 15.0, dry_run: bool = False) -> None:
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout
        self.dry_run = dry_run

    def _send(self, text: str) -> bool:
        if self.dry_run:
            log.info("[dry_run] investiční zpráva by se odeslala:\n%s", text)
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
            log.error("Odeslání investiční zprávy na Telegram selhalo: %s", exc)
            return False

    def format_candidate(self, c: "InvestmentCandidate") -> str:
        e = lambda s: html.escape(str(s)) if s is not None else "—"  # noqa: E731

        lines = [f"🏠 <b>INVESTMENT CANDIDATE — score {c.score}/100</b>"]
        area = f"{c.size_m2:g} m²" if c.size_m2 else "? m²"
        lines.append(f"{e(c.layout)}, {area}, {e(c.location)}")

        price_bits = [f"{_fmt_czk(c.price)} Kč"]
        if c.price_per_m2 is not None:
            ppm2_bit = f"{_fmt_czk(c.price_per_m2)} Kč/m²"
            if c.discount_pct is not None:
                ppm2_bit += f" — {round(c.discount_pct)}% below area avg"
            price_bits.append(f"({ppm2_bit})")
        lines.append(f"Price: {' '.join(price_bits)}")

        rent_bits = [f"Est. rent: {_fmt_czk(c.estimated_rent)} Kč/mo"]
        if c.gross_yield_pct is not None:
            rent_bits.append(f"Gross yield: {c.gross_yield_pct:.1f}%")
        lines.append(" | ".join(rent_bits))

        lines.append(
            f"Est. mortgage ({round(c.ltv * 100)}%, {c.interest_rate_pct:g}%, {c.mortgage_years}y): "
            f"{_fmt_czk(c.estimated_mortgage)} Kč"
        )
        lines.append(f"Est. net cashflow: {_fmt_signed_czk(c.net_monthly_cashflow)} Kč/mo")

        if c.renovation_flag:
            lines.append("⚠️ Renovation opportunity")
        if c.parse_confidence == "low":
            lines.append("❓ Low parse confidence (unstructured listing — verify size/layout manually)")

        lines.append(f"Source: {e(_display_source(c.source))}")
        lines.append(f'🔗 <a href="{html.escape(c.url, quote=True)}">{e(c.url)}</a>')

        if c.duplicate_sources:
            lines.append("Also listed on:")
            for dup_source, dup_url in c.duplicate_sources:
                lines.append(f'  • {e(_display_source(dup_source))}: <a href="{html.escape(dup_url, quote=True)}">{e(dup_url)}</a>')

        return "\n".join(lines)

    def notify_candidate(self, c: "InvestmentCandidate") -> bool:
        return self._send(self.format_candidate(c))

    def notify_source_failure(self, source: str, consecutive_failures: int, last_error: str) -> bool:
        """Posílá se JEDNOU po dosažení prahu (viz InvestmentStore.should_send_failure_warning) —
        ne po každém neúspěšném běhu, ať bot nezahltí opakováním."""
        text = (
            f"⚠️ <b>Zdroj '{html.escape(_display_source(source))}' selhal {consecutive_failures}× v kuse.</b>\n"
            f"Poslední chyba: {html.escape(last_error)}\n"
            f"Ostatní zdroje běží dál nezávisle — tohle je jen upozornění, ne zastavení."
        )
        return self._send(text)


if __name__ == "__main__":
    import sys

    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from investment.candidate import InvestmentCandidate

    candidate = InvestmentCandidate(
        source="sreality", external_id="42",
        url="https://www.sreality.cz/detail/prodej/byt/2+1/sumperk-sumperk/42",
        layout="2+1", size_m2=58.0, location="Šumperk",
        price=1_790_000, price_per_m2=30862, avg_price_per_m2=34700, discount_pct=11.0,
        estimated_rent=11600, gross_yield_pct=7.8,
        estimated_mortgage=8260, ltv=0.90, interest_rate_pct=4.6, mortgage_years=30,
        net_monthly_cashflow=40, renovation_flag=True, score=72,
        parse_confidence="low",
        duplicate_sources=[("bazos", "https://reality.bazos.cz/inzerat/999/x.php")],
    )

    notifier = InvestmentNotifier(token="TEST", chat_id="0", dry_run=True)
    print("=== náhled investiční Telegram zprávy ===\n")
    print(notifier.format_candidate(candidate))
    print("\n=== náhled varování o selhání zdroje ===\n")
    notifier.notify_source_failure("bazos", 3, "GET https://reality.bazos.cz/prodam/byt/ selhal po 3 pokusech: timeout")
    print("\n✅ investment/notifier.py OK")
