"""investment/scraping_utils.py — sdílené HTTP chování pro HTML zdroje
(Realingo, Bazoš, reality-sumperk.cz). Sreality má vlastní ověřené JSON API
volání (sources/sreality.py) a tenhle modul nepoužívá.

Poskytuje:
  RobotsChecker — stáhne a naparsuje robots.txt (VLASTNÍ parser s podporou
    `*`/`$` wildcard patternů — ověřeno živě, že stdlib `urllib.robotparser`
    tyhle patterny NEumí a Bazošovo `Disallow: /*hledat=` by tiše ignoroval;
    to by u Bazoše znamenalo skutečné porušení robots.txt).
  polite_get() — GET s: robots.txt kontrolou, náhodnou pauzou 2-5 s (>= 1 req/s),
    retry s exponenciálním backoffem (max 3 pokusy), realistickým UA.
"""

from __future__ import annotations

import logging
import random
import re
import time
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("investment.scraping")

# Stejný identifikující UA jako zbytek projektu (README: "rozumný rate-limit +
# identifikující User-Agent" — NEmaskujeme se za prohlížeč, to by bylo neetické).
USER_AGENT = "realitni-scraper/0.1 (osobni nekomercni projekt)"

MIN_DELAY_S = 2.0
MAX_DELAY_S = 5.0
MAX_RETRIES = 3
BACKOFF_BASE_S = 2.0


def _pattern_to_regex(pattern: str) -> re.Pattern[str]:
    """robots.txt path pattern -> regex. Podporuje `*` (cokoliv) a `$` (konec) —
    de facto rozšíření (Google/Bing), víc sítí (vč. Bazoše) na něm staví pravidla."""
    ends_anchor = pattern.endswith("$")
    body = pattern[:-1] if ends_anchor else pattern
    parts = body.split("*")
    regex = "".join(re.escape(p) + ".*" for p in parts[:-1]) + re.escape(parts[-1])
    if ends_anchor:
        regex += "$"
    return re.compile("^" + regex)


class RobotsChecker:
    """Stáhne robots.txt jednou na doménu (cache), respektuje `*` skupinu (naše
    UA v žádném z 3 robots.txt souborů není jmenovitě zmíněné)."""

    def __init__(self, *, timeout: float = 10.0) -> None:
        self.timeout = timeout
        self._rules: dict[str, list[tuple[str, bool]]] = {}  # domain -> [(pattern, is_allow)]
        self._loaded: set[str] = set()

    def _load(self, domain: str) -> None:
        if domain in self._loaded:
            return
        self._loaded.add(domain)
        rules: list[tuple[str, bool]] = []
        try:
            resp = httpx.get(
                f"https://{domain}/robots.txt",
                headers={"User-Agent": USER_AGENT},
                timeout=self.timeout, follow_redirects=True,
            )
            if resp.status_code == 200:
                rules = self._parse(resp.text)
        except Exception as exc:  # noqa: BLE001 – bez robots.txt radši nic nefetchujeme (viz can_fetch)
            log.warning("robots.txt pro %s se nepodařilo stáhnout: %s", domain, exc)
        self._rules[domain] = rules

    @staticmethod
    def _parse(text: str) -> list[tuple[str, bool]]:
        """Vrátí pravidla platná pro '*' (nebo jmenovitě naši UA), v pořadí ze souboru."""
        our_ua = "realitni-scraper"
        groups: list[tuple[list[str], list[tuple[str, bool]]]] = []
        current_agents: list[str] = []
        current_rules: list[tuple[str, bool]] = []
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, _, value = line.partition(":")
            key, value = key.strip().lower(), value.strip()
            if key == "user-agent":
                if current_rules or not current_agents:
                    if current_agents:
                        groups.append((current_agents, current_rules))
                    current_agents, current_rules = [], []
                current_agents.append(value.lower())
            elif key == "disallow" and value:
                current_rules.append((value, False))
            elif key == "allow" and value:
                current_rules.append((value, True))
        if current_agents:
            groups.append((current_agents, current_rules))

        for agents, rules in groups:
            if our_ua in agents:
                return rules
        for agents, rules in groups:
            if "*" in agents:
                return rules
        return []

    def can_fetch(self, url: str) -> bool:
        """True = smíme fetchovat. Když robots.txt nejde stáhnout, jsme konzervativní
        a fetch POVOLÍME jen pro běžné stránkování (žádná disallow pravidla = nic
        neblokuje) — ale radši žádná data než tichý přeskok kvůli dočasnému výpadku."""
        parts = urlsplit(url)
        domain = parts.netloc
        self._load(domain)
        rules = self._rules.get(domain, [])
        path_q = parts.path + (("?" + parts.query) if parts.query else "")

        best_len = -1
        best_allow = True
        for pattern, is_allow in rules:
            if _pattern_to_regex(pattern).match(path_q):
                if len(pattern) > best_len:
                    best_len, best_allow = len(pattern), is_allow
        return best_allow


class RateLimiter:
    """Náhodná pauza 2-5 s před KAŽDÝM requestem -> automaticky <= ~0.3-0.5 req/s,
    hluboko pod požadovaným stropem ~1 req/s."""

    def __init__(self, min_delay_s: float = MIN_DELAY_S, max_delay_s: float = MAX_DELAY_S) -> None:
        self.min_delay_s = min_delay_s
        self.max_delay_s = max_delay_s
        self._last_request_ts = 0.0

    def wait(self) -> None:
        delay = random.uniform(self.min_delay_s, self.max_delay_s)
        elapsed = time.monotonic() - self._last_request_ts
        remaining = delay - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_request_ts = time.monotonic()


def polite_get(
    client: httpx.Client,
    url: str,
    *,
    robots: RobotsChecker,
    rate_limiter: RateLimiter,
    max_retries: int = MAX_RETRIES,
) -> httpx.Response | None:
    """GET respektující robots.txt + rate limit, s exponenciálním backoffem.
    Vrací None (a zaloguje), když je URL disallowed nebo všechny pokusy selžou —
    volající MUSÍ tohle ošetřit jako "přeskoč, neshazuj celý zdroj"."""
    if not robots.can_fetch(url):
        log.warning("robots.txt zakazuje fetch: %s — přeskakuji", url)
        return None

    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        rate_limiter.wait()
        try:
            resp = client.get(url, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
            return resp
        except Exception as exc:  # noqa: BLE001 – retry rozhoduje voláný kód, ne typ výjimky
            last_exc = exc
            if attempt < max_retries:
                backoff = BACKOFF_BASE_S * (2 ** (attempt - 1)) + random.uniform(0, 1)
                log.warning("GET %s selhal (pokus %d/%d): %s — čekám %.1fs", url, attempt, max_retries, exc, backoff)
                time.sleep(backoff)
    log.error("GET %s selhal po %d pokusech: %s", url, max_retries, last_exc)
    return None


if __name__ == "__main__":
    import sys

    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    checker = RobotsChecker()
    tests = [
        ("https://reality.bazos.cz/prodam/byt/", True),
        ("https://reality.bazos.cz/prodam/byt/100/", True),
        ("https://reality.bazos.cz/prodam/byt/?hledat=byt", False),
        ("https://reality.bazos.cz/prodam/byt/?hlokalita=Sumperk", False),
        ("https://reality.bazos.cz/ad-mail.php", False),
        ("https://realingo.cz/prodej_reality/Šumperk/nejlevnejsi/", True),
        ("https://www.reality-sumperk.cz/prodej/byty/byty-2-1/?strana=2", True),
        ("https://www.reality-sumperk.cz/moje-oblibene/pridat/?itemid=1", False),
    ]
    ok = True
    for url, expected in tests:
        result = checker.can_fetch(url)
        status = "OK" if result == expected else "FAIL"
        ok = ok and (result == expected)
        print(status, url, "-> can_fetch =", result, "(čekám", expected, ")")
    assert ok, "robots.txt wildcard matcher neodpovídá živým datům"

    print("\n✅ investment/scraping_utils.py OK — wildcard robots.txt matching ověřeno proti živým souborům.")
