"""flip/store.py — SQLite úložiště (jeden soubor, DB_PATH).

Tabulky:
  listings       poslední známý stav KAŽDÉHO inzerátu (i mimo sledované obce a nad
                 rozpočtem — slouží jako data o trhu pro mediány a ARV). `dup_of`
                 ukazuje na kanonický záznam, když jde o stejnou nemovitost z jiného
                 portálu (mediány pak duplicity nepočítají dvakrát).
  price_history  každá ZMĚNA ceny (ne každý běh) -> signál "zlevnilo".
  notified       co už šlo na Telegram, klíčované KANONICKÝM inzerátem shluku ->
                 stejná nemovitost z jiného portálu nepřijde podruhé; drží poslední
                 oznámenou cenu kvůli upozornění na zlevnění.
  source_health  selhání zdroje v kuse (jedno varování, ne každý běh) + jestli už
                 proběhl první (baseline) běh.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from flip.model import Listing

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    source              TEXT NOT NULL,
    external_id         TEXT NOT NULL,
    url                 TEXT,
    kind                TEXT NOT NULL,
    subtype             TEXT,
    price               INTEGER,
    area_m2             REAL,
    land_m2             REAL,
    town                TEXT,
    locality            TEXT,
    description         TEXT,
    building_condition  TEXT,
    energy_rating       TEXT,
    gps_lat             REAL,
    gps_lon             REAL,
    parse_confidence    TEXT NOT NULL DEFAULT 'high',
    dup_of              TEXT,
    last_score          INTEGER,
    last_breakdown      TEXT,
    first_seen          TEXT NOT NULL,
    last_seen           TEXT NOT NULL,
    PRIMARY KEY (source, external_id)
);

CREATE TABLE IF NOT EXISTS price_history (
    source      TEXT NOT NULL,
    external_id TEXT NOT NULL,
    price       INTEGER,
    seen_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notified (
    cluster_key          TEXT PRIMARY KEY,
    last_notified_price  INTEGER,
    score                INTEGER,
    first_notified_at    TEXT NOT NULL,
    last_notified_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_health (
    source               TEXT PRIMARY KEY,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    warning_sent         INTEGER NOT NULL DEFAULT 0,
    baseline_done        INTEGER NOT NULL DEFAULT 0,
    last_run_at          TEXT,
    last_error           TEXT
);

CREATE INDEX IF NOT EXISTS ix_listings_market ON listings (kind, last_seen);
CREATE INDEX IF NOT EXISTS ix_listings_town ON listings (town, kind);
CREATE INDEX IF NOT EXISTS ix_price_history ON price_history (source, external_id, seen_at);
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def cluster_key(source: str, external_id: str) -> str:
    return f"{source}:{external_id}"


class Store:
    """Použití: `with Store("flip.db") as store: ...`. `now` jde přepsat v testech."""

    def __init__(self, path: str = "flip.db") -> None:
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()
        self._now: datetime | None = None

    def _migrate(self) -> None:
        """Jednorázové opravy starých dat (verze v PRAGMA user_version)."""
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            # Před PSČ filtrem Bazoš ukládal obec podle názvu, a tak dostaly "Šumperk"
            # i inzeráty ze Zábřehu, Mohelnice apod. Takové řádky (lokalita bez PSČ)
            # už nejdou ověřit -> vyřadit z oblasti. Když je Bazoš ukáže znovu, obec
            # se doplní podle PSČ.
            self.conn.execute(
                """UPDATE listings SET town = NULL
                   WHERE source = 'bazos' AND town IS NOT NULL
                     AND (locality IS NULL OR locality NOT GLOB '*[0-9][0-9][0-9]*')"""
            )
            self.conn.execute("PRAGMA user_version = 1")

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    def now(self) -> datetime:
        return self._now or utc_now()

    def set_now(self, dt: datetime | None) -> None:
        self._now = dt

    # ------------------------------------------------------------------ #
    # Inzeráty                                                           #
    # ------------------------------------------------------------------ #

    def get(self, source: str, external_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM listings WHERE source = ? AND external_id = ?", (source, external_id)
        ).fetchone()

    def known_ids(self, source: str) -> set[str]:
        """ID, která už máme — u nich se nestahuje detail znovu."""
        rows = self.conn.execute("SELECT external_id FROM listings WHERE source = ?", (source,)).fetchall()
        return {r["external_id"] for r in rows}

    def upsert(self, lst: Listing) -> tuple[sqlite3.Row | None, bool]:
        """Uloží inzerát. Vrací (předchozí řádek nebo None, změnila se cena?).

        Pole, která se tahají jen z detailu (popis, stav, štítek, pozemek, GPS), se
        při refetchi bez detailu NEPŘEPÍŠOU na NULL (COALESCE).
        """
        now = iso(self.now())
        prev = self.get(lst.source, lst.external_id)
        if prev is None:
            self.conn.execute(
                """INSERT INTO listings (source, external_id, url, kind, subtype, price, area_m2, land_m2,
                       town, locality, description, building_condition, energy_rating, gps_lat, gps_lon,
                       parse_confidence, first_seen, last_seen)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (lst.source, lst.external_id, lst.url, lst.kind, lst.subtype, lst.price, lst.area_m2,
                 lst.land_m2, lst.town, lst.locality, lst.description, lst.building_condition,
                 lst.energy_rating, lst.gps_lat, lst.gps_lon, lst.parse_confidence, now, now),
            )
            self._record_price(lst, now)
            self.conn.commit()
            return None, False

        self.conn.execute(
            """UPDATE listings SET url = ?, kind = ?, subtype = COALESCE(?, subtype), price = ?,
                   area_m2 = COALESCE(?, area_m2), land_m2 = COALESCE(?, land_m2),
                   town = ?, locality = COALESCE(?, locality),
                   description = COALESCE(?, description),
                   building_condition = COALESCE(?, building_condition),
                   energy_rating = COALESCE(?, energy_rating),
                   gps_lat = COALESCE(?, gps_lat), gps_lon = COALESCE(?, gps_lon),
                   parse_confidence = ?, last_seen = ?
               WHERE source = ? AND external_id = ?""",
            (lst.url, lst.kind, lst.subtype, lst.price, lst.area_m2, lst.land_m2, lst.town, lst.locality,
             lst.description, lst.building_condition, lst.energy_rating, lst.gps_lat, lst.gps_lon,
             lst.parse_confidence, now, lst.source, lst.external_id),
        )
        price_changed = prev["price"] != lst.price
        if price_changed:
            self._record_price(lst, now)
        self.conn.commit()
        return prev, price_changed

    def _record_price(self, lst: Listing, now: str) -> None:
        self.conn.execute(
            "INSERT INTO price_history (source, external_id, price, seen_at) VALUES (?,?,?,?)",
            (lst.source, lst.external_id, lst.price, now),
        )

    def fill_from_stored(self, lst: Listing) -> None:
        """Doplní do inzerátu detailová pole z DB (popis/stav…), když je tento běh nestáhl.
        Skóre pak u známého inzerátu nepadá jen proto, že se detail netahal znovu."""
        row = self.get(lst.source, lst.external_id)
        if row is None:
            return
        for attr in ("description", "building_condition", "energy_rating", "land_m2", "gps_lat", "gps_lon",
                     "area_m2", "subtype", "locality"):
            if getattr(lst, attr) is None and row[attr] is not None:
                setattr(lst, attr, row[attr])

    def price_history(self, source: str, external_id: str) -> list[tuple[int | None, str]]:
        rows = self.conn.execute(
            "SELECT price, seen_at FROM price_history WHERE source = ? AND external_id = ? ORDER BY seen_at, rowid",
            (source, external_id),
        ).fetchall()
        return [(r["price"], r["seen_at"]) for r in rows]

    def save_score(self, source: str, external_id: str, score: int | None, breakdown: dict[str, Any]) -> None:
        self.conn.execute(
            "UPDATE listings SET last_score = ?, last_breakdown = ? WHERE source = ? AND external_id = ?",
            (score, json.dumps(breakdown, ensure_ascii=False), source, external_id),
        )
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # Duplicity napříč portály                                           #
    # ------------------------------------------------------------------ #

    def dedup_candidates(self, lst: Listing) -> list[sqlite3.Row]:
        """Kanonické záznamy z JINÝCH portálů ve stejné obci a stejného typu."""
        if lst.town is None:
            return []
        return self.conn.execute(
            """SELECT * FROM listings
               WHERE source != ? AND town = ? AND kind = ? AND dup_of IS NULL""",
            (lst.source, lst.town, lst.kind),
        ).fetchall()

    def set_dup_of(self, source: str, external_id: str, canonical: str | None) -> None:
        self.conn.execute(
            "UPDATE listings SET dup_of = ? WHERE source = ? AND external_id = ?", (canonical, source, external_id)
        )
        self.conn.commit()

    def cluster_of(self, source: str, external_id: str) -> str:
        row = self.get(source, external_id)
        if row is not None and row["dup_of"]:
            return row["dup_of"]
        return cluster_key(source, external_id)

    def cluster_members(self, key: str) -> list[sqlite3.Row]:
        source, _, external_id = key.partition(":")
        return self.conn.execute(
            "SELECT * FROM listings WHERE (source = ? AND external_id = ?) OR dup_of = ?",
            (source, external_id, key),
        ).fetchall()

    # ------------------------------------------------------------------ #
    # Trh                                                                #
    # ------------------------------------------------------------------ #

    def market_rows(self, *, kind: str, window_days: int, in_area_only: bool) -> list[sqlite3.Row]:
        """Inzeráty pro mediány: daný typ, viděné v posledních N dnech, bez duplicit."""
        since = iso(self.now() - timedelta(days=window_days))
        sql = """SELECT source, external_id, price, area_m2, land_m2, building_condition, description, town
                 FROM listings
                 WHERE kind = ? AND last_seen >= ? AND dup_of IS NULL
                   AND price IS NOT NULL AND price > 0 AND area_m2 IS NOT NULL AND area_m2 > 0"""
        if in_area_only:
            sql += " AND town IS NOT NULL"
        return self.conn.execute(sql, (kind, since)).fetchall()

    # ------------------------------------------------------------------ #
    # Notifikace                                                         #
    # ------------------------------------------------------------------ #

    def notified(self, key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM notified WHERE cluster_key = ?", (key,)).fetchone()

    def mark_notified(self, key: str, price: int | None, score: int | None) -> None:
        now = iso(self.now())
        self.conn.execute(
            """INSERT INTO notified (cluster_key, last_notified_price, score, first_notified_at, last_notified_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(cluster_key) DO UPDATE SET
                   last_notified_price = excluded.last_notified_price, score = excluded.score,
                   last_notified_at = excluded.last_notified_at""",
            (key, price, score, now, now),
        )
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # Zdraví zdrojů                                                      #
    # ------------------------------------------------------------------ #

    def _ensure_health(self, source: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO source_health (source) VALUES (?)", (source,))

    def baseline_done(self, source: str) -> bool:
        row = self.conn.execute("SELECT baseline_done FROM source_health WHERE source = ?", (source,)).fetchone()
        return bool(row and row["baseline_done"])

    def mark_baseline_done(self, source: str) -> None:
        self._ensure_health(source)
        self.conn.execute("UPDATE source_health SET baseline_done = 1 WHERE source = ?", (source,))
        self.conn.commit()

    def record_success(self, source: str) -> None:
        self._ensure_health(source)
        self.conn.execute(
            """UPDATE source_health SET consecutive_failures = 0, warning_sent = 0, last_run_at = ?,
                   last_error = NULL WHERE source = ?""",
            (iso(self.now()), source),
        )
        self.conn.commit()

    def record_failure(self, source: str, error: str) -> int:
        self._ensure_health(source)
        self.conn.execute(
            """UPDATE source_health SET consecutive_failures = consecutive_failures + 1,
                   last_run_at = ?, last_error = ? WHERE source = ?""",
            (iso(self.now()), error, source),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT consecutive_failures FROM source_health WHERE source = ?", (source,)).fetchone()
        return int(row["consecutive_failures"])

    def should_warn(self, source: str, threshold: int) -> bool:
        row = self.conn.execute(
            "SELECT consecutive_failures, warning_sent FROM source_health WHERE source = ?", (source,)
        ).fetchone()
        return bool(row) and row["consecutive_failures"] >= threshold and not row["warning_sent"]

    def mark_warned(self, source: str) -> None:
        self.conn.execute("UPDATE source_health SET warning_sent = 1 WHERE source = ?", (source,))
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # Přehledy                                                           #
    # ------------------------------------------------------------------ #

    def counts_by_town(self, sources: Iterable[str] | None = None) -> dict[tuple[str, str], int]:
        sql = "SELECT town, kind, COUNT(*) AS n FROM listings WHERE town IS NOT NULL AND dup_of IS NULL"
        params: list[Any] = []
        if sources is not None:
            srcs = list(sources)
            sql += f" AND source IN ({','.join('?' * len(srcs))})"
            params += srcs
        sql += " GROUP BY town, kind"
        return {(r["town"], r["kind"]): r["n"] for r in self.conn.execute(sql, params).fetchall()}
