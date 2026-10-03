"""
database.py — SQLite úložiště (bez externího DB serveru).

Tabulky:
  listings       – aktuální stav inzerátu (1 řádek na (source, source_id)).
  price_history  – každá změna ceny (i první cena) s časem pozorování.
  scores         – skóre u KAŽDÉHO inzerátu při každém běhu (i pod prahem), vč. rozpadu.

Klíčové principy:
  * Deduplikace GLOBÁLNĚ přes PRIMARY KEY (source, source_id).
  * seen_first / seen_last (UTC) = naše první a poslední spatření. Drží "dobu inzerce"
    i přes výpadky scraperu a i když Sreality recykluje hash_id / resetuje `since`.
  * Detailová pole (building_condition, energy_rating, description, listed_date) se při
    UPDATE nepřepisují NULLem (COALESCE) — u známých inzerátů detail znovu netaháme,
    takže si uložené hodnoty udržíme.
  * Časy v UTC (NAS běží v UTC) jako ISO 8601 string.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from sources.base import Listing

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    source             TEXT NOT NULL,
    source_id          TEXT NOT NULL,
    url                TEXT,
    price              INTEGER,
    area_m2            REAL,
    land_area_m2       REAL,
    property_type      TEXT NOT NULL DEFAULT 'byt',
    disposition        TEXT,
    building_condition TEXT,
    energy_rating      TEXT,
    description        TEXT,
    locality_text      TEXT,
    district           TEXT,
    region             TEXT,
    gps_lat            REAL,
    gps_lon            REAL,
    listed_date        TEXT,
    seen_first         TEXT NOT NULL,
    seen_last          TEXT NOT NULL,
    PRIMARY KEY (source, source_id)
);

CREATE TABLE IF NOT EXISTS price_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    price       INTEGER,
    observed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scores (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source     TEXT NOT NULL,
    source_id  TEXT NOT NULL,
    score      INTEGER,
    breakdown  TEXT,
    scored_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS ix_price_history_listing ON price_history (source, source_id);
CREATE INDEX IF NOT EXISTS ix_scores_listing        ON scores (source, source_id);
CREATE INDEX IF NOT EXISTS ix_listings_group        ON listings (district, disposition);
"""

# Index nad sloupci z migrace -> až PO ní (u staré DB by CREATE INDEX v SCHEMA spadl).
_POST_MIGRATION_SQL = [
    "CREATE INDEX IF NOT EXISTS ix_listings_comps ON listings (property_type, district, area_m2)",
]

# Sloupce přidané po prvním nasazení -> u starších DB je doplní ALTER TABLE (viz _migrate).
# Existující řádky jsou všechno byty, takže default 'byt' je pro ně správně.
_MIGRATIONS: list[tuple[str, str]] = [
    ("land_area_m2", "ALTER TABLE listings ADD COLUMN land_area_m2 REAL"),
    ("property_type", "ALTER TABLE listings ADD COLUMN property_type TEXT NOT NULL DEFAULT 'byt'"),
]


def _utc_now() -> str:
    """Aktuální čas v UTC jako ISO string (NAS běží v UTC)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class UpsertResult:
    """Výsledek upsertu jednoho inzerátu — co se s ním stalo."""
    is_new: bool
    price_changed: bool
    old_price: int | None
    new_price: int | None


class Database:
    """Tenká SQLite vrstva. Použití: `with Database("realitky.db") as db: ...`."""

    def __init__(self, path: str = "realitky.db") -> None:
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Doplní sloupce, které v už existující DB chybí (data zůstanou zachovaná)."""
        existing = {row["name"] for row in self.conn.execute("PRAGMA table_info(listings)")}
        for column, ddl in _MIGRATIONS:
            if column not in existing:
                self.conn.execute(ddl)
        for sql in _POST_MIGRATION_SQL:
            self.conn.execute(sql)

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ #
    # Zápis inzerátů                                                     #
    # ------------------------------------------------------------------ #

    def upsert_listing(self, listing: Listing) -> UpsertResult:
        """
        Vloží nový nebo aktualizuje existující inzerát. Zaznamená cenu do price_history
        (u nového vždy, u existujícího při změně). Vrací UpsertResult.
        """
        now = _utc_now()
        row = self.conn.execute(
            "SELECT price FROM listings WHERE source = ? AND source_id = ?",
            (listing.source, listing.source_id),
        ).fetchone()

        if row is None:
            self._insert_listing(listing, now)
            if listing.price is not None:
                self._record_price(listing.source, listing.source_id, listing.price, now)
            self.conn.commit()
            return UpsertResult(is_new=True, price_changed=False, old_price=None, new_price=listing.price)

        old_price = row["price"]
        self._update_listing(listing, now)
        price_changed = listing.price is not None and listing.price != old_price
        if price_changed:
            self._record_price(listing.source, listing.source_id, listing.price, now)
        self.conn.commit()
        return UpsertResult(
            is_new=False, price_changed=price_changed, old_price=old_price, new_price=listing.price
        )

    def _insert_listing(self, lst: Listing, now: str) -> None:
        self.conn.execute(
            """
            INSERT INTO listings (
                source, source_id, url, price, area_m2, land_area_m2, property_type, disposition,
                building_condition, energy_rating, description,
                locality_text, district, region, gps_lat, gps_lon,
                listed_date, seen_first, seen_last
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                lst.source, lst.source_id, lst.url, lst.price, lst.area_m2,
                lst.land_area_m2, lst.property_type, lst.disposition,
                lst.building_condition, lst.energy_rating, lst.description,
                lst.locality_text, lst.district, lst.region, lst.gps_lat, lst.gps_lon,
                lst.listed_date.isoformat() if lst.listed_date else None, now, now,
            ),
        )

    def _update_listing(self, lst: Listing, now: str) -> None:
        # Detailová pole přes COALESCE(?, sloupec) — NULL (známý inzerát bez detailu) nepřepíše.
        self.conn.execute(
            """
            UPDATE listings SET
                url = ?, price = ?, area_m2 = ?,
                land_area_m2 = COALESCE(?, land_area_m2),  -- pozemek chodí jen z detailu
                property_type = ?, disposition = ?,
                building_condition = COALESCE(?, building_condition),
                energy_rating      = COALESCE(?, energy_rating),
                description        = COALESCE(?, description),
                locality_text = ?,
                district = COALESCE(?, district),  -- iDnes dává okres jen z detailu; re-run bez detailu ho nesmí smazat
                region = ?, gps_lat = ?, gps_lon = ?,
                listed_date   = COALESCE(?, listed_date),
                seen_last = ?
            WHERE source = ? AND source_id = ?
            """,
            (
                lst.url, lst.price, lst.area_m2, lst.land_area_m2, lst.property_type, lst.disposition,
                lst.building_condition, lst.energy_rating, lst.description,
                lst.locality_text, lst.district, lst.region, lst.gps_lat, lst.gps_lon,
                lst.listed_date.isoformat() if lst.listed_date else None, now,
                lst.source, lst.source_id,
            ),
        )

    def _record_price(self, source: str, source_id: str, price: int, when: str) -> None:
        self.conn.execute(
            "INSERT INTO price_history (source, source_id, price, observed_at) VALUES (?,?,?,?)",
            (source, source_id, price, when),
        )

    # ------------------------------------------------------------------ #
    # Skóre                                                              #
    # ------------------------------------------------------------------ #

    def save_score(self, listing: Listing, score: int | None, breakdown: dict[str, Any]) -> None:
        """Uloží skóre (i None / pod prahem) + rozpad signálů jako JSON."""
        self.conn.execute(
            "INSERT INTO scores (source, source_id, score, breakdown, scored_at) VALUES (?,?,?,?,?)",
            (listing.source, listing.source_id, score, json.dumps(breakdown, ensure_ascii=False), _utc_now()),
        )
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # Meta (klíč-hodnota, např. datum posledního heartbeatu)             #
    # ------------------------------------------------------------------ #

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # Čtení pro scraper a scoring                                        #
    # ------------------------------------------------------------------ #

    def count_listings(self) -> int:
        """Počet inzerátů v DB (pro startup notice — 0 prozradí nenamountovanou předplněnou DB)."""
        return self.conn.execute("SELECT COUNT(*) AS c FROM listings").fetchone()["c"]

    def known_source_ids(self, source: str) -> set[str]:
        """ID inzerátů, které už máme — scraper jim nemusí znovu tahat detail."""
        rows = self.conn.execute(
            "SELECT source_id FROM listings WHERE source = ?", (source,)
        ).fetchall()
        return {r["source_id"] for r in rows}

    def get_listing_row(self, source: str, source_id: str) -> sqlite3.Row | None:
        """Celý uložený řádek (vč. detailových polí) — pro doplnění známého inzerátu před scoringem."""
        return self.conn.execute(
            "SELECT * FROM listings WHERE source = ? AND source_id = ?", (source, source_id)
        ).fetchone()

    @staticmethod
    def row_to_listing(row: sqlite3.Row) -> Listing:
        """Z DB řádku sestaví kompletní Listing (i s detailovými poli uloženými dřív)."""
        return Listing(
            source=row["source"], source_id=row["source_id"], url=row["url"],
            price=row["price"], area_m2=row["area_m2"],
            land_area_m2=row["land_area_m2"], property_type=row["property_type"] or "byt",
            disposition=row["disposition"],
            building_condition=row["building_condition"], energy_rating=row["energy_rating"],
            description=row["description"], locality_text=row["locality_text"],
            district=row["district"], region=row["region"],
            gps_lat=row["gps_lat"], gps_lon=row["gps_lon"],
            listed_date=date.fromisoformat(row["listed_date"]) if row["listed_date"] else None,
        )

    def price_history(self, source: str, source_id: str) -> list[tuple[int, str]]:
        """Historie cen (price, observed_at UTC) vzestupně — pro signál poklesu ceny."""
        rows = self.conn.execute(
            "SELECT price, observed_at FROM price_history WHERE source = ? AND source_id = ? ORDER BY observed_at",
            (source, source_id),
        ).fetchall()
        return [(r["price"], r["observed_at"]) for r in rows]

    def price_per_m2_values(
        self,
        *,
        region: str | None = None,
        district: str | None = None,
        disposition: str | None = None,
        property_type: str | None = None,
    ) -> list[float]:
        """
        Vzorek cen za m² z aktuálních inzerátů (jen kde známe cenu i plochu).
        Filtry jsou volitelné — scoring si určí granularitu (okres+disp → fallback kraj).

        `property_type` je pro korektnost ZÁSADNÍ: bez něj by se do mediánu bytů
        míchaly domy (a naopak) — jejich Kč/m² je řádově jinde.
        """
        clauses = ["price IS NOT NULL", "price > 0", "area_m2 IS NOT NULL", "area_m2 > 0"]
        params: list[Any] = []
        if property_type is not None:
            clauses.append("property_type = ?")
            params.append(property_type)
        if region is not None:
            clauses.append("region = ?")
            params.append(region)
        if district is not None:
            clauses.append("district = ?")
            params.append(district)
        if disposition is not None:
            clauses.append("disposition = ?")
            params.append(disposition)
        sql = f"SELECT price * 1.0 / area_m2 AS ppm2 FROM listings WHERE {' AND '.join(clauses)}"
        return [r["ppm2"] for r in self.conn.execute(sql, params).fetchall()]


if __name__ == "__main__":
    # Smoke test nad dočasnou DB v paměti — ověří new/update, price_history, score, known_ids.
    import sys
    from datetime import date

    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    db = Database(":memory:")

    lst = Listing(
        source="sreality", source_id="999", url="https://x/999",
        price=3_000_000, area_m2=60.0, disposition="2+kk",
        building_condition="Před rekonstrukcí", energy_rating="G",
        description="dědictví, nutný rychlý prodej",
        locality_text="Olomouc, Olomouc", district="Olomouc", region="Olomoucký kraj",
        gps_lat=49.59, gps_lon=17.25, listed_date=date(2026, 5, 1),
    )

    r1 = db.upsert_listing(lst)
    print("1. vložení :", r1)
    assert r1.is_new and not r1.price_changed

    # druhý běh, BEZ detailu (známý inzerát) + nižší cena
    lst2 = Listing(
        source="sreality", source_id="999", url="https://x/999",
        price=2_800_000, area_m2=60.0, disposition="2+kk",
        locality_text="Olomouc, Olomouc", district="Olomouc", region="Olomoucký kraj",
    )
    r2 = db.upsert_listing(lst2)
    print("2. update  :", r2)
    assert (not r2.is_new) and r2.price_changed and r2.old_price == 3_000_000

    # detailová pole se NESMÍ přepsat NULLem
    row = db.get_listing_row("sreality", "999")
    print("   building_condition po update (čekám zachované):", row["building_condition"])
    assert row["building_condition"] == "Před rekonstrukcí"
    assert row["price"] == 2_800_000  # cena se naopak aktualizovala

    print("   price_history:", db.price_history("sreality", "999"))
    assert len(db.price_history("sreality", "999")) == 2

    print("   known_source_ids:", db.known_source_ids("sreality"))
    db.save_score(lst, 78, {"stav": 30, "cena_m2": 18, "urgent": 20})

    # vzorek cen/m²
    print("   ppm2 (Olomouc, 2+kk):", db.price_per_m2_values(district="Olomouc", disposition="2+kk"))

    print("\n✅ database.py OK — new/update, COALESCE detailů, price_history, scores i known_ids fungují.")
    db.close()
