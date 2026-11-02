"""flip/store.py — SQLite úložiště (jeden soubor, DB_PATH).

Tabulky:
  listings       poslední známý stav KAŽDÉHO inzerátu (i mimo sledované obce a nad
                 rozpočtem — slouží jako data o trhu pro mediány a ARV). `dup_of`
                 ukazuje na kanonický záznam, když jde o stejnou nemovitost z jiného
                 portálu (mediány pak duplicity nepočítají dvakrát).
                 `gone_at` = kdy inzerát z webu zmizel (nejspíš prodaný nebo stažený);
                 počítá se jen u zdrojů, které procházejí celou nabídku (FetchResult.full_scan).
                 `seen_at_start` = 1, když byl inzerát na webu už při spuštění sledování
                 (první běh zdroje / nově přidané obce) -> "online aspoň N dní", ne "nový".
                 `detail_fetched_at` = kdy se naposledy četl detail (popis, stav); po
                 DETAIL_REFRESH_DAYS se u sledovaných obcí přečte znovu (prodejce připíše "sleva").
                 `code` = krátké číslo inzerátu pro Telegram ("/find 1234", "/fav 1234"); zpráva
                 ukazuje číslo kanonického inzerátu shluku.
  favorites      oblíbené nemovitosti (klíč shluku) — zpráva při KAŽDÉ změně ceny a při zmizení.
  price_history  každá ZMĚNA ceny (ne každý běh) -> signál "zlevnilo".
  notified       co už šlo na Telegram, klíčované KANONICKÝM inzerátem shluku ->
                 stejná nemovitost z jiného portálu nepřijde podruhé; drží poslední
                 oznámenou cenu kvůli upozornění na zlevnění.
  source_health  selhání zdroje v kuse (jedno varování, ne každý běh) + jestli už
                 proběhl první (baseline) běh.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from flip.model import Listing, price_steps
from flip.sanity import FLAT_AREA_RANGE, HOUSE_AREA_MAX, MIN_LAND_M2, MIN_SALE_PRICE, RENTAL_PRICE_MAX
from flip.text import looks_like_rental
from flip.towns import _PSC_RE, DEFAULT_PSC, match_town, match_town_by_psc, normalize_psc, okres_of, psc_fits_town

# Zdroje, které při každém běhu projdou celou nabídku -> jen u nich má "zmizel z webu" smysl.
GONE_TRACKED_SOURCES = ("sreality", "realingo", "reality_sumperk")
GONE_AFTER_HOURS = 48            # chybí aspoň tak dlouho -> zmizel (ne jen výpadek jednoho běhu)
GONE_MAX_SHARE = 0.30            # víc než 30 % nabídky naráz = chyba webu, ne vlna prodejů
DETAIL_REFRESH_DAYS = 14         # detail inzerátu ve sledované obci znovu po 2 týdnech
DETAIL_REFRESH_PER_RUN = 30      # max znovu-čtených detailů na běh zdroje (běh zůstane krátký)

log = logging.getLogger("flip.store")

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
    gone_at             TEXT,
    seen_at_start       INTEGER NOT NULL DEFAULT 0,
    detail_fetched_at   TEXT,
    code                INTEGER,
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

CREATE TABLE IF NOT EXISTS favorites (
    cluster_key TEXT PRIMARY KEY,
    added_at    TEXT NOT NULL,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS town_stats (
    taken_at        TEXT NOT NULL,
    town            TEXT NOT NULL,
    kind            TEXT NOT NULL,
    n               INTEGER NOT NULL,
    median_ppm2     REAL,
    median_price    REAL,
    renovated_ppm2  REAL,
    needs_reno_ppm2 REAL,
    PRIMARY KEY (taken_at, town, kind)
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
        self.conn = sqlite3.connect(path, timeout=30)   # bot čte z vlastního vlákna souběžně se scraperem
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
        if version < 2:
            self._clean_stored_values()
            self.conn.execute("PRAGMA user_version = 2")
        if version < 3:
            self._rematch_towns()
            # nové obce = stovky "nových" inzerátů naráz -> místo záplavy zpráv znovu jedno shrnutí
            self.conn.execute("UPDATE source_health SET baseline_done = 0")
            self.conn.execute("PRAGMA user_version = 3")
        if version < 4:
            self._add_history_columns()
            self.conn.execute("PRAGMA user_version = 4")
        if version < 5:
            cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(listings)")}
            if "detail_fetched_at" not in cols:
                self.conn.execute("ALTER TABLE listings ADD COLUMN detail_fetched_at TEXT")
            # detail se dosud četl jen při prvním spatření
            self.conn.execute("UPDATE listings SET detail_fetched_at = first_seen "
                              "WHERE detail_fetched_at IS NULL AND description IS NOT NULL")
            self.conn.execute("PRAGMA user_version = 5")
        if version < 6:
            cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(listings)")}
            if "code" not in cols:
                self.conn.execute("ALTER TABLE listings ADD COLUMN code INTEGER")
            rows = self.conn.execute(
                "SELECT source, external_id FROM listings WHERE code IS NULL ORDER BY first_seen, source, external_id"
            ).fetchall()
            start = self.conn.execute("SELECT COALESCE(MAX(code), 0) FROM listings").fetchone()[0]
            self.conn.executemany("UPDATE listings SET code = ? WHERE source = ? AND external_id = ?",
                                  [(start + i + 1, r["source"], r["external_id"]) for i, r in enumerate(rows)])
            self.conn.execute("PRAGMA user_version = 6")
        self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_listings_code ON listings (code)")

    def _add_history_columns(self) -> None:
        """gone_at + seen_at_start pro starší DB. Doplní se potichu (žádné zprávy):
        co už teď chybí, dostane gone_at = last_seen; co bylo na webu už při spuštění
        sledování (první den zdroje, první den nové obce/okresu), dostane seen_at_start."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(listings)")}
        if "gone_at" not in cols:
            self.conn.execute("ALTER TABLE listings ADD COLUMN gone_at TEXT")
        if "seen_at_start" not in cols:
            self.conn.execute("ALTER TABLE listings ADD COLUMN seen_at_start INTEGER NOT NULL DEFAULT 0")

        for source in GONE_TRACKED_SOURCES:
            row = self.conn.execute("SELECT MAX(last_seen) AS m FROM listings WHERE source = ?", (source,)).fetchone()
            if row and row["m"]:
                cutoff = iso(datetime.fromisoformat(row["m"]) - timedelta(hours=GONE_AFTER_HOURS))
                self.conn.execute(
                    "UPDATE listings SET gone_at = last_seen WHERE source = ? AND gone_at IS NULL AND last_seen < ?",
                    (source, cutoff))

        rows = self.conn.execute("SELECT source, external_id, town, first_seen FROM listings").fetchall()
        groups: dict[tuple[str, str | None], list[datetime]] = {}    # (zdroj, okres) -> první spatření
        for r in rows:
            seen = datetime.fromisoformat(r["first_seen"])
            groups.setdefault((r["source"], None), []).append(seen)
            if okres := okres_of(r["town"]):
                groups.setdefault((r["source"], okres), []).append(seen)
        # začátek sledování okresu = kdy přišla první velká dávka (10. percentil), ne první
        # zatoulaný inzerát (starý kód občas našel 1 inzerát z Olomouce i v okrese Šumperk)
        starts = {g: sorted(v)[len(v) // 10] for g, v in groups.items()}
        source_start = {src: min(v) for (src, o), v in groups.items() if o is None}
        day = timedelta(days=1)
        for r in rows:
            seen = datetime.fromisoformat(r["first_seen"])
            okres = okres_of(r["town"])
            if seen < source_start[r["source"]] + day:
                at_start = True
            elif okres is not None:
                at_start = seen < starts[(r["source"], okres)] + day
            else:
                # mimo sledované obce okres neznáme: první den kteréhokoli okresu
                at_start = any(abs(seen - s) < day for (src, o), s in starts.items()
                               if src == r["source"] and o is not None)
            if at_start:
                self.conn.execute("UPDATE listings SET seen_at_start = 1 WHERE source = ? AND external_id = ?",
                                  (r["source"], r["external_id"]))

    def _rematch_towns(self) -> None:
        """Seznam sledovaných obcí se změnil (6 obcí -> větší obce kraje): obec
        uložených řádků znovu z lokality. Bazoš jen s PSČ (bez něj se nedá ověřit)."""
        rows = self.conn.execute("SELECT source, external_id, locality FROM listings").fetchall()
        for r in rows:
            loc = r["locality"]
            if r["source"] == "bazos":
                psc = normalize_psc(loc)
                name = _PSC_RE.sub(" ", loc or "").strip()
                town = match_town_by_psc(psc, DEFAULT_PSC) if psc else None
                if psc and town is None:
                    named = match_town(name)
                    town = named if psc_fits_town(psc, named) else None
            else:
                town = match_town(loc)
            self.conn.execute("UPDATE listings SET town = ? WHERE source = ? AND external_id = ?",
                              (town, r["source"], r["external_id"]))

    def _clean_stored_values(self) -> None:
        """Stejná pravidla jako flip/sanity.py pro řádky uložené před jejich zavedením
        (jinak by špatné plochy a "ceny" pronájmů kazily mediány celý rok)."""
        rows = self.conn.execute(
            "SELECT source, external_id, description FROM listings WHERE price IS NOT NULL AND price < ?",
            (RENTAL_PRICE_MAX,),
        ).fetchall()
        for r in rows:
            if looks_like_rental(r["description"]):     # pronájem není prodej -> pryč úplně
                for table in ("listings", "price_history"):
                    self.conn.execute(f"DELETE FROM {table} WHERE source = ? AND external_id = ?",
                                      (r["source"], r["external_id"]))
        self.conn.executescript(f"""
            UPDATE listings SET price = NULL WHERE price < {MIN_SALE_PRICE};
            UPDATE listings SET land_m2 = NULL WHERE land_m2 < {MIN_LAND_M2};
            UPDATE listings SET area_m2 = NULL
                WHERE kind = 'dum' AND land_m2 IS NOT NULL AND abs(area_m2 - land_m2) < 1;
            UPDATE listings SET land_m2 = COALESCE(land_m2, area_m2), area_m2 = NULL
                WHERE kind = 'dum' AND area_m2 > {HOUSE_AREA_MAX};
            UPDATE listings SET area_m2 = NULL
                WHERE kind = 'byt' AND (area_m2 < {FLAT_AREA_RANGE[0]} OR area_m2 > {FLAT_AREA_RANGE[1]});
        """)

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

    def refresh_ids(self, source: str, days: int = DETAIL_REFRESH_DAYS,
                    limit: int = DETAIL_REFRESH_PER_RUN) -> set[str]:
        """Inzeráty ve sledovaných obcích, pořád online, s detailem starším než N dní
        (nejstarší první) — jejich popis a stav se přečtou znovu."""
        cutoff = iso(self.now() - timedelta(days=days))
        rows = self.conn.execute(
            """SELECT external_id FROM listings
               WHERE source = ? AND town IS NOT NULL AND gone_at IS NULL
                 AND (detail_fetched_at IS NULL OR detail_fetched_at < ?)
               ORDER BY COALESCE(detail_fetched_at, first_seen) LIMIT ?""", (source, cutoff, limit)).fetchall()
        return {r["external_id"] for r in rows}

    def upsert(self, lst: Listing, *, seen_at_start: bool = False) -> tuple[sqlite3.Row | None, bool]:
        """Uloží inzerát. Vrací (předchozí řádek nebo None, změnila se cena?).

        Pole, která se tahají jen z detailu (popis, stav, štítek, pozemek, GPS), se
        při refetchi bez detailu NEPŘEPÍŠOU na NULL (COALESCE). Inzerát, který byl
        "zmizelý" a znovu se objevil, je zase online (gone_at = NULL).
        `seen_at_start` = ukládá se při prvním (baseline) běhu zdroje.
        """
        now = iso(self.now())
        prev = self.get(lst.source, lst.external_id)
        if prev is None:
            self.conn.execute(
                """INSERT INTO listings (source, external_id, url, kind, subtype, price, area_m2, land_m2,
                       town, locality, description, building_condition, energy_rating, gps_lat, gps_lon,
                       parse_confidence, first_seen, last_seen, seen_at_start, detail_fetched_at, code)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                           (SELECT COALESCE(MAX(code), 0) + 1 FROM listings))""",
                (lst.source, lst.external_id, lst.url, lst.kind, lst.subtype, lst.price, lst.area_m2,
                 lst.land_m2, lst.town, lst.locality, lst.description, lst.building_condition,
                 lst.energy_rating, lst.gps_lat, lst.gps_lon, lst.parse_confidence, now, now, int(seen_at_start),
                 now if lst.detail_fetched else None),
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
                   parse_confidence = ?, last_seen = ?, gone_at = NULL,
                   detail_fetched_at = COALESCE(?, detail_fetched_at)
               WHERE source = ? AND external_id = ?""",
            (lst.url, lst.kind, lst.subtype, lst.price, lst.area_m2, lst.land_m2, lst.town, lst.locality,
             lst.description, lst.building_condition, lst.energy_rating, lst.gps_lat, lst.gps_lon,
             lst.parse_confidence, now, now if lst.detail_fetched else None, lst.source, lst.external_id),
        )
        price_changed = prev["price"] != lst.price
        if price_changed:
            self._record_price(lst, now)
        self.conn.commit()
        return prev, price_changed

    def mark_gone(self, source: str, kinds: Iterable[str]) -> list[sqlite3.Row]:
        """Po ÚPLNÉM průchodu zdroje: co v něm chybí aspoň GONE_AFTER_HOURS, z webu zmizelo.
        Když by to bylo víc než GONE_MAX_SHARE nabídky naráz, je to spíš chyba webu ->
        nic se neoznačí. Vrací nově zmizelé řádky."""
        kinds = list(kinds)
        if source not in GONE_TRACKED_SOURCES or not kinds:
            return []
        marks = ",".join("?" * len(kinds))
        cutoff = iso(self.now() - timedelta(hours=GONE_AFTER_HOURS))
        online = self.conn.execute(
            f"SELECT COUNT(*) FROM listings WHERE source = ? AND gone_at IS NULL AND kind IN ({marks})",
            (source, *kinds)).fetchone()[0]
        where = f"source = ? AND gone_at IS NULL AND kind IN ({marks}) AND last_seen < ?"
        params = (source, *kinds, cutoff)
        rows = self.conn.execute(f"SELECT * FROM listings WHERE {where}", params).fetchall()
        n = len(rows)
        if n == 0:
            return []
        if n > 5 and n > GONE_MAX_SHARE * online:
            log.warning("%s: %d z %d inzerátů naráz chybí — spíš chyba webu, neoznačuji jako zmizelé",
                        source, n, online)
            return []
        self.conn.execute(f"UPDATE listings SET gone_at = last_seen WHERE {where}", params)
        self.conn.commit()
        return [self.get(r["source"], r["external_id"]) for r in rows]

    def cluster_still_online(self, key: str, except_key: tuple[str, str]) -> bool:
        """Je stejná nemovitost pořád k vidění na jiném webu (viděná za posledních 48 h)?"""
        cutoff = iso(self.now() - timedelta(hours=GONE_AFTER_HOURS))
        return any(m["gone_at"] is None and m["last_seen"] >= cutoff
                   for m in self.cluster_members(key) if (m["source"], m["external_id"]) != except_key)

    def price_steps(self, source: str, external_id: str) -> list[tuple[int, str]]:
        """Ceny v čase bez "cena na dotaz" a bez opakování: [(cena, kdy), …]."""
        return price_steps(self.price_history(source, external_id))

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

    def code_of(self, key: str) -> int | None:
        """Krátké číslo nemovitosti = číslo kanonického inzerátu shluku."""
        source, _, external_id = key.partition(":")
        row = self.get(source, external_id)
        return row["code"] if row is not None else None

    def by_code(self, code: int) -> sqlite3.Row | None:
        """Inzerát podle čísla; u duplicity rovnou kanonický inzerát shluku."""
        row = self.conn.execute("SELECT * FROM listings WHERE code = ?", (code,)).fetchone()
        if row is not None and row["dup_of"]:
            source, _, external_id = row["dup_of"].partition(":")
            return self.get(source, external_id) or row
        return row

    # ------------------------------------------------------------------ #
    # Oblíbené                                                           #
    # ------------------------------------------------------------------ #

    def add_favorite(self, key: str, note: str | None = None) -> bool:
        """False = už tam byla (poznámka se jen aktualizuje, když je zadaná)."""
        exists = self.is_favorite(key)
        self.conn.execute(
            """INSERT INTO favorites (cluster_key, added_at, note) VALUES (?,?,?)
               ON CONFLICT(cluster_key) DO UPDATE SET note = COALESCE(excluded.note, favorites.note)""",
            (key, iso(self.now()), note))
        self.conn.commit()
        return not exists

    def remove_favorite(self, key: str) -> bool:
        n = self.conn.execute("DELETE FROM favorites WHERE cluster_key = ?", (key,)).rowcount
        self.conn.commit()
        return n > 0

    def is_favorite(self, key: str) -> bool:
        return self.conn.execute("SELECT 1 FROM favorites WHERE cluster_key = ?", (key,)).fetchone() is not None

    def favorites(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM favorites ORDER BY added_at").fetchall()

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
        sql = """SELECT source, external_id, kind, price, area_m2, land_m2, building_condition, description, town,
                        first_seen, gone_at, seen_at_start
                 FROM listings
                 WHERE kind = ? AND last_seen >= ? AND dup_of IS NULL
                   AND price IS NOT NULL AND price > 0 AND area_m2 IS NOT NULL AND area_m2 > 0"""
        if in_area_only:
            sql += " AND town IS NOT NULL"
        return self.conn.execute(sql, (kind, since)).fetchall()

    def sale_rows(self, *, kind: str, days: int = 365) -> list[sqlite3.Row]:
        """Zmizelé inzeráty (nejspíš prodané) za posledních N dní s první a poslední cenou —
        z nich se měří, o kolik nabídková cena typicky klesne, než se nemovitost prodá.
        Jen ty, které jsme viděli od začátku (u starších neznáme původní cenu)."""
        since = iso(self.now() - timedelta(days=days))
        marks = ",".join("?" * len(GONE_TRACKED_SOURCES))
        return self.conn.execute(
            f"""SELECT l.source, l.external_id, l.town, l.price,
                       (SELECT p.price FROM price_history p WHERE p.source = l.source
                          AND p.external_id = l.external_id AND p.price IS NOT NULL
                        ORDER BY p.seen_at, p.rowid LIMIT 1) AS first_price
                FROM listings l
                WHERE l.kind = ? AND l.gone_at >= ? AND l.dup_of IS NULL AND l.price IS NOT NULL
                  AND l.seen_at_start = 0 AND l.source IN ({marks})""",
            (kind, since, *GONE_TRACKED_SOURCES),
        ).fetchall()

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
    # Týdenní ceny po obcích                                             #
    # ------------------------------------------------------------------ #

    def last_town_stats(self) -> tuple[str | None, list[sqlite3.Row]]:
        """Poslední uložený snímek (čas, řádky) — pro srovnání s minulým týdnem."""
        row = self.conn.execute("SELECT MAX(taken_at) AS t FROM town_stats").fetchone()
        if not row or not row["t"]:
            return None, []
        return row["t"], self.conn.execute("SELECT * FROM town_stats WHERE taken_at = ?", (row["t"],)).fetchall()

    def town_stats_dates(self) -> list[str]:
        """Časy všech uložených týdenních snímků, nejstarší první."""
        return [r["taken_at"] for r in self.conn.execute(
            "SELECT DISTINCT taken_at FROM town_stats ORDER BY taken_at")]

    def town_stats_at(self, taken_at: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM town_stats WHERE taken_at = ?", (taken_at,)).fetchall()

    def save_town_stats(self, taken_at: str, rows: list[tuple[Any, ...]]) -> None:
        """rows: (town, kind, n, median_ppm2, median_price, renovated_ppm2, needs_reno_ppm2)."""
        self.conn.executemany(
            """INSERT OR REPLACE INTO town_stats (taken_at, town, kind, n, median_ppm2, median_price,
                   renovated_ppm2, needs_reno_ppm2) VALUES (?,?,?,?,?,?,?,?)""",
            [(taken_at, *r) for r in rows],
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
