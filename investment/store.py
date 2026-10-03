"""investment/store.py — SQLite úložiště investičního režimu (vlastní DB soubor).

Zcela oddělené od database.py (hlavní monitoring). Čtyři tabulky:
  investment_listings – poslední snímek KAŽDÉHO scrapnutého kandidáta ze VŠECH
                        4 zdrojů (source, external_id), vč. first_seen -> zdroj
                        dat pro rolling průměr cena/m². Dedup (stejný byt na víc
                        portálech) se řeší až při skórování (investment/dedup.py),
                        NE tady — čím víc srovnatelných záznamů, tím lepší průměr.
  investment_scores   – skóre KAŽDÉHO kandidáta při každém běhu (i pod prahem),
                        ať jde zpětně vyhodnotit, jestli je práh rozumný.
  investment_alerts   – dedup ledger: co už bylo posláno na Telegram.
  investment_source_health – kolikrát v kuse daný zdroj selhal + jestli už bylo
                        posláno varování (ať přijde jen JEDNO, ne za každý běh).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS investment_listings (
    source              TEXT NOT NULL,
    external_id         TEXT NOT NULL,
    url                 TEXT,
    price               INTEGER,
    size_m2             REAL,
    layout              TEXT,
    location            TEXT,
    building_condition  TEXT,
    renovation_flag     INTEGER NOT NULL DEFAULT 0,
    price_per_m2        REAL,
    parse_confidence    TEXT NOT NULL DEFAULT 'high',
    first_seen          TEXT NOT NULL,
    last_seen           TEXT NOT NULL,
    PRIMARY KEY (source, external_id)
);

CREATE TABLE IF NOT EXISTS investment_scores (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    source                TEXT NOT NULL,
    external_id           TEXT NOT NULL,
    score                 INTEGER,
    price_per_m2          REAL,
    avg_price_per_m2      REAL,
    gross_yield_pct       REAL,
    estimated_rent        INTEGER,
    estimated_mortgage    INTEGER,
    net_monthly_cashflow  INTEGER,
    renovation_flag       INTEGER NOT NULL DEFAULT 0,
    duplicate_sources     TEXT,
    scored_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS investment_alerts (
    source      TEXT NOT NULL,
    external_id TEXT NOT NULL,
    score       INTEGER,
    alerted_at  TEXT NOT NULL,
    PRIMARY KEY (source, external_id)
);

CREATE TABLE IF NOT EXISTS investment_source_health (
    source               TEXT PRIMARY KEY,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    warning_sent         INTEGER NOT NULL DEFAULT 0,
    last_run_at          TEXT,
    last_error           TEXT
);

CREATE INDEX IF NOT EXISTS ix_investment_listings_group ON investment_listings (location, layout, first_seen);
CREATE INDEX IF NOT EXISTS ix_investment_scores_listing ON investment_scores (source, external_id);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class InvestmentListingSnapshot:
    source: str
    external_id: str
    url: str
    price: int | None
    size_m2: float | None
    layout: str | None
    location: str | None
    building_condition: str | None
    renovation_flag: bool
    price_per_m2: float | None
    parse_confidence: str = "high"


class InvestmentStore:
    """Použití: `with InvestmentStore("investment.db") as store: ...`."""

    def __init__(self, path: str = "investment.db") -> None:
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def __enter__(self) -> "InvestmentStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ #
    # Kandidáti (pro rolling průměr + historii)                          #
    # ------------------------------------------------------------------ #

    def upsert_listing(self, snap: InvestmentListingSnapshot) -> None:
        now = _utc_now()
        row = self.conn.execute(
            "SELECT first_seen FROM investment_listings WHERE source = ? AND external_id = ?",
            (snap.source, snap.external_id),
        ).fetchone()
        if row is None:
            self.conn.execute(
                """
                INSERT INTO investment_listings (
                    source, external_id, url, price, size_m2, layout, location,
                    building_condition, renovation_flag, price_per_m2, parse_confidence,
                    first_seen, last_seen
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    snap.source, snap.external_id, snap.url, snap.price, snap.size_m2, snap.layout,
                    snap.location, snap.building_condition, int(snap.renovation_flag),
                    snap.price_per_m2, snap.parse_confidence, now, now,
                ),
            )
        else:
            self.conn.execute(
                """
                UPDATE investment_listings SET
                    url = ?, price = ?, size_m2 = ?, layout = ?, location = ?,
                    building_condition = COALESCE(?, building_condition),
                    renovation_flag = ?, price_per_m2 = ?, parse_confidence = ?, last_seen = ?
                WHERE source = ? AND external_id = ?
                """,
                (
                    snap.url, snap.price, snap.size_m2, snap.layout, snap.location,
                    snap.building_condition, int(snap.renovation_flag), snap.price_per_m2,
                    snap.parse_confidence, now,
                    snap.source, snap.external_id,
                ),
            )
        self.conn.commit()

    def get_listing(self, source: str, external_id: str) -> sqlite3.Row | None:
        """Dřív uložený snímek — používá se hlavně pro reuse renovation_flag u
        známých inzerátů, když se popis tento běh znovu netahal (viz orchestrátor)."""
        return self.conn.execute(
            "SELECT * FROM investment_listings WHERE source = ? AND external_id = ?",
            (source, external_id),
        ).fetchone()

    def rolling_avg_price_per_m2(self, *, location: str | None, layout: str | None, n: int) -> tuple[float | None, int]:
        """Průměr cena/m² z posledních N srovnatelných inzerátů (lokalita+dispozice,
        napříč VŠEMI zdroji), seřazeno podle first_seen (nejnovější objevené, ne
        opakované re-scrapy stejného inzerátu). Vrací (průměr nebo None, vzorek)."""
        rows = self.conn.execute(
            """
            SELECT price_per_m2 FROM (
                SELECT price_per_m2 FROM investment_listings
                WHERE location = ? AND layout = ? AND price_per_m2 IS NOT NULL
                ORDER BY first_seen DESC LIMIT ?
            )
            """,
            (location, layout, n),
        ).fetchall()
        values = [r["price_per_m2"] for r in rows]
        if not values:
            return None, 0
        return sum(values) / len(values), len(values)

    def find_cross_source_matches(self, *, exclude_source: str, layout: str | None, location: str | None) -> list[sqlite3.Row]:
        """Ostatní zdroje se stejnou dispozicí+lokalitou — vstup pro cross-source
        dedup (investment/dedup.py). Nezávislé plánování zdrojů znamená, že se
        nesrovnává jen "dávka z tohoto běhu", ale i poslední známý stav OSTATNÍCH
        zdrojů z jejich vlastních (jindy proběhlých) běhů."""
        return self.conn.execute(
            "SELECT * FROM investment_listings WHERE source != ? AND layout = ? AND location = ?",
            (exclude_source, layout, location),
        ).fetchall()

    def known_source_ids(self, source: str) -> set[str]:
        """ID kandidátů, které už máme pro daný zdroj — ať se popis netahá znovu."""
        rows = self.conn.execute(
            "SELECT external_id FROM investment_listings WHERE source = ?", (source,)
        ).fetchall()
        return {r["external_id"] for r in rows}

    # ------------------------------------------------------------------ #
    # Skóre (log VŠECH kandidátů, i pod prahem — pro zpětné testování)    #
    # ------------------------------------------------------------------ #

    def save_score(
        self,
        *,
        source: str,
        external_id: str,
        score: int | None,
        price_per_m2: float | None,
        avg_price_per_m2: float | None,
        gross_yield_pct: float | None,
        estimated_rent: int | None,
        estimated_mortgage: int | None,
        net_monthly_cashflow: int | None,
        renovation_flag: bool,
        duplicate_sources: list[tuple[str, str]] | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO investment_scores (
                source, external_id, score, price_per_m2, avg_price_per_m2, gross_yield_pct,
                estimated_rent, estimated_mortgage, net_monthly_cashflow, renovation_flag,
                duplicate_sources, scored_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                source, external_id, score, price_per_m2, avg_price_per_m2, gross_yield_pct,
                estimated_rent, estimated_mortgage, net_monthly_cashflow, int(renovation_flag),
                json.dumps(duplicate_sources or [], ensure_ascii=False), _utc_now(),
            ),
        )
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # Alerty (dedup — nikdy neposlat stejné ID dvakrát)                   #
    # ------------------------------------------------------------------ #

    def already_alerted(self, source: str, external_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM investment_alerts WHERE source = ? AND external_id = ?", (source, external_id)
        ).fetchone()
        return row is not None

    def record_alert(self, source: str, external_id: str, score: int | None) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO investment_alerts (source, external_id, score, alerted_at) VALUES (?,?,?,?)",
            (source, external_id, score, _utc_now()),
        )
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # Zdraví zdrojů (3x v kuse selhání -> JEDNO varování, ne za každý běh) #
    # ------------------------------------------------------------------ #

    def record_source_success(self, source: str) -> None:
        self.conn.execute(
            """
            INSERT INTO investment_source_health (source, consecutive_failures, warning_sent, last_run_at, last_error)
            VALUES (?, 0, 0, ?, NULL)
            ON CONFLICT(source) DO UPDATE SET
                consecutive_failures = 0, warning_sent = 0, last_run_at = excluded.last_run_at, last_error = NULL
            """,
            (source, _utc_now()),
        )
        self.conn.commit()

    def record_source_failure(self, source: str, error: str) -> int:
        """Zvýší počítadlo selhání v kuse a vrátí novou hodnotu."""
        row = self.conn.execute(
            "SELECT consecutive_failures FROM investment_source_health WHERE source = ?", (source,)
        ).fetchone()
        failures = (row["consecutive_failures"] if row else 0) + 1
        self.conn.execute(
            """
            INSERT INTO investment_source_health (source, consecutive_failures, warning_sent, last_run_at, last_error)
            VALUES (?, ?, COALESCE((SELECT warning_sent FROM investment_source_health WHERE source = ?), 0), ?, ?)
            ON CONFLICT(source) DO UPDATE SET
                consecutive_failures = excluded.consecutive_failures,
                last_run_at = excluded.last_run_at, last_error = excluded.last_error
            """,
            (source, failures, source, _utc_now(), error),
        )
        self.conn.commit()
        return failures

    def should_send_failure_warning(self, source: str, threshold: int) -> bool:
        """True jen JEDNOU — hned po dosažení prahu (warning_sent se pak nastaví)."""
        row = self.conn.execute(
            "SELECT consecutive_failures, warning_sent FROM investment_source_health WHERE source = ?", (source,)
        ).fetchone()
        if row is None:
            return False
        return row["consecutive_failures"] >= threshold and not row["warning_sent"]

    def mark_failure_warning_sent(self, source: str) -> None:
        self.conn.execute(
            "UPDATE investment_source_health SET warning_sent = 1 WHERE source = ?", (source,)
        )
        self.conn.commit()


if __name__ == "__main__":
    import sys

    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    store = InvestmentStore(":memory:")

    for i in range(6):
        store.upsert_listing(InvestmentListingSnapshot(
            source="sreality", external_id=str(i), url=f"https://x/{i}",
            price=1_800_000 + i * 10_000, size_m2=58.0, layout="2+1",
            location="Šumperk", building_condition="Před rekonstrukcí",
            renovation_flag=True, price_per_m2=(1_800_000 + i * 10_000) / 58.0,
        ))

    avg, n = store.rolling_avg_price_per_m2(location="Šumperk", layout="2+1", n=30)
    print("rolling avg:", avg, "n=", n)
    assert n == 6 and avg is not None

    avg_none, n0 = store.rolling_avg_price_per_m2(location="Zábřeh", layout="2+1", n=30)
    print("rolling avg (žádný vzorek):", avg_none, "n=", n0)
    assert avg_none is None and n0 == 0

    assert not store.already_alerted("sreality", "0")
    store.record_alert("sreality", "0", 72)
    assert store.already_alerted("sreality", "0")
    store.record_alert("sreality", "0", 72)  # dedup — druhý zápis se ignoruje, nesmí spadnout

    store.save_score(
        source="sreality", external_id="0", score=72, price_per_m2=30862.0, avg_price_per_m2=34700.0,
        gross_yield_pct=7.8, estimated_rent=11600, estimated_mortgage=8259, net_monthly_cashflow=-259,
        renovation_flag=True, duplicate_sources=[("realingo", "https://realingo/x")],
    )

    # zdraví zdroje: 2 selhání pod prahem (3) -> žádné varování; 3. -> varování; reset po úspěchu
    assert not store.should_send_failure_warning("bazos", threshold=3)
    store.record_source_failure("bazos", "timeout")
    store.record_source_failure("bazos", "timeout")
    assert not store.should_send_failure_warning("bazos", threshold=3)
    n_fail = store.record_source_failure("bazos", "timeout")
    assert n_fail == 3
    assert store.should_send_failure_warning("bazos", threshold=3)
    store.mark_failure_warning_sent("bazos")
    assert not store.should_send_failure_warning("bazos", threshold=3)  # už poslané, nemá se opakovat
    store.record_source_failure("bazos", "timeout")  # 4. selhání v kuse
    assert not store.should_send_failure_warning("bazos", threshold=3)  # pořád jen jednou
    store.record_source_success("bazos")
    store.record_source_failure("bazos", "timeout")
    assert not store.should_send_failure_warning("bazos", threshold=3)  # počítadlo se resetlo úspěchem

    print("\n✅ investment/store.py OK — upsert, rolling avg, dedup, skóre-log i source health fungují.")
    store.close()
