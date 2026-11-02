"""Historie: zmizelé inzeráty, příběh ceny, /drops, /stale, rychlost trhu v týdenní tabulce."""

import csv
import io
import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from flip import export, insights, pipeline, report
from flip.model import FetchResult
from flip.notifier import price_story
from flip.sources import realingo, sreality
from flip.store import Store

from tests.helpers import config, make
from tests.test_pipeline import CapturingNotifier

T0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


class HistoryBase(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.cfg = config(min_comps=3)
        self.notifier = CapturingNotifier()
        self.now = T0
        self.store.set_now(self.now)

    def at(self, **delta):
        self.now = T0 + timedelta(**delta)
        self.store.set_now(self.now)

    def run_src(self, listings, *, source="sreality", full_scan=True):
        result = FetchResult(list(listings), len(listings) or 1, full_scan=full_scan)
        fake = SimpleNamespace(fetch_listings=lambda cfg, known: result)
        with mock.patch.dict(pipeline.SOURCES, {source: fake}):
            return pipeline.run_source(source, self.cfg, self.notifier, self.store)


class GoneTests(HistoryBase):
    def test_missing_for_two_days_after_full_scan_is_gone(self):
        self.run_src([make(eid="1"), make(eid="2")])
        self.at(days=1)
        self.run_src([make(eid="1")])
        self.assertIsNone(self.store.get("sreality", "2")["gone_at"])     # 1 den = může být výpadek
        self.at(days=3)
        stats = self.run_src([make(eid="1")])
        self.assertEqual(stats.gone, 1)
        self.assertEqual(self.store.get("sreality", "2")["gone_at"], T0.isoformat())   # = naposledy viděn

    def test_partial_scan_never_marks_gone(self):
        self.run_src([make(eid="1"), make(eid="2")])
        self.at(days=5)
        self.run_src([make(eid="1")], full_scan=False)
        self.assertIsNone(self.store.get("sreality", "2")["gone_at"])

    def test_bazos_is_never_marked_gone(self):
        self.run_src([make("bazos", "1"), make("bazos", "2")], source="bazos")
        self.at(days=5)
        self.run_src([make("bazos", "1")], source="bazos")
        self.assertIsNone(self.store.get("bazos", "2")["gone_at"])

    def test_coming_back_clears_gone(self):
        self.run_src([make(eid="1"), make(eid="2"), make(eid="3")])
        self.at(days=3)
        self.run_src([make(eid="1"), make(eid="3")])
        self.assertIsNotNone(self.store.get("sreality", "2")["gone_at"])
        self.at(days=4)
        self.run_src([make(eid="1"), make(eid="2"), make(eid="3")])
        self.assertIsNone(self.store.get("sreality", "2")["gone_at"])

    def test_mass_disappearance_is_treated_as_site_error(self):
        self.run_src([make(eid=str(i)) for i in range(20)])
        self.at(days=3)
        self.run_src([make(eid="0")])
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM listings WHERE gone_at IS NOT NULL").fetchone()[0], 0)

    def test_first_run_listings_are_marked_as_already_online(self):
        self.run_src([make(eid="old")])
        self.at(days=1)
        self.run_src([make(eid="old"), make(eid="new")])
        self.assertEqual(self.store.get("sreality", "old")["seen_at_start"], 1)
        self.assertEqual(self.store.get("sreality", "new")["seen_at_start"], 0)


class MigrationTests(unittest.TestCase):
    def test_old_database_gets_gone_and_start_flags_quietly(self):
        import os
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE listings (source TEXT NOT NULL, external_id TEXT NOT NULL, url TEXT, kind TEXT NOT NULL,
                subtype TEXT, price INTEGER, area_m2 REAL, land_m2 REAL, town TEXT, locality TEXT, description TEXT,
                building_condition TEXT, energy_rating TEXT, gps_lat REAL, gps_lon REAL,
                parse_confidence TEXT NOT NULL DEFAULT 'high', dup_of TEXT, last_score INTEGER, last_breakdown TEXT,
                first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, PRIMARY KEY (source, external_id));
            PRAGMA user_version = 3;
        """)
        rows = [
            ("sreality", "a", "Šumperk", "2026-10-04T23:00:00+00:00", "2026-11-02T18:00:00+00:00"),   # od začátku
            ("sreality", "b", "Šumperk", "2026-10-20T10:00:00+00:00", "2026-10-25T10:00:00+00:00"),   # zmizel
            ("sreality", "c", "Olomouc", "2026-11-02T18:00:00+00:00", "2026-11-02T18:00:00+00:00"),   # nový okres
            ("sreality", "d", "Olomouc", "2026-11-02T18:01:00+00:00", "2026-11-02T18:01:00+00:00"),
            ("bazos", "e", "Šumperk", "2026-10-20T10:00:00+00:00", "2026-10-21T10:00:00+00:00"),
        ]
        conn.executemany("INSERT INTO listings (source, external_id, kind, town, first_seen, last_seen) "
                         "VALUES (?, ?, 'byt', ?, ?, ?)", rows)
        conn.commit()
        conn.close()
        with Store(path) as store:
            got = {r["external_id"]: (r["gone_at"] is not None, r["seen_at_start"])
                   for r in store.conn.execute("SELECT * FROM listings")}
        self.assertEqual(got, {"a": (False, 1), "b": (True, 0), "c": (False, 1), "d": (False, 1), "e": (False, 1)})          # první inzerát Bazoše = začátek


class PriceStoryTests(HistoryBase):
    def test_story_in_new_listing_message(self):
        self.run_src([make(eid="0")])                                   # první běh
        self.run_src([make(eid="0"), make(eid="1", price=5_600_000)])   # nad rozpočtem -> nic
        self.at(days=9)
        self.run_src([make(eid="0"), make(eid="1", price=5_200_000)])
        self.notifier.sent.clear()
        self.at(days=18)
        self.run_src([make(eid="0"), make(eid="1", price=4_900_000)])
        self.assertIn("5.6M → 5.2M → 4.9M in 18 days (2 cuts, −12 %)", self.notifier.sent[0])

    def test_story_formats(self):
        self.assertIsNone(price_story([(1_000_000, T0.isoformat())]))
        steps = [(p, (T0 + timedelta(days=i)).isoformat()) for i, p in enumerate([9, 8, 7, 6, 5, 4])]
        steps = [(p * 1_000_000, t) for p, t in steps]
        self.assertTrue(price_story(steps).startswith("… → 8M → 7M"))

    def test_already_online_shows_at_least(self):
        self.run_src([make(eid="0", price=4_000_000)])
        self.at(days=40)
        self.run_src([make(eid="0", price=3_500_000)])                  # zlevnění -> zpráva 📉
        self.assertIn("online 40+ days (was up before watching started)", self.notifier.sent[-1])


class InsightTests(HistoryBase):
    def setUp(self):
        super().setUp()
        self.run_src([make(eid="1", price=3_000_000), make(eid="2", price=2_000_000, town="Zábřeh"),
                  make(eid="3", price=7_500_000)])
        self.at(days=30)
        self.run_src([make(eid="1", price=2_700_000), make(eid="2", price=2_000_000, town="Zábřeh"),
                  make(eid="3", price=6_500_000)])

    def test_drops_lists_cuts_biggest_first_including_over_budget(self):
        drops = insights.recent_drops(self.store, self.cfg, 7)
        self.assertEqual([it.row["external_id"] for it, _, _ in drops], ["3", "1"])
        text = insights.drops_text(self.store, self.cfg, 7)
        self.assertIn("−13 %", text)
        self.assertIn("💸 over budget", text)
        self.at(days=40)
        self.assertEqual(insights.recent_drops(self.store, self.cfg, 7), [])

    def test_stale_needs_enough_days_and_sorts_by_cut(self):
        self.assertIn("Nothing in your towns", insights.stale_text(self.store, self.cfg, 60))
        items = insights.stale_listings(self.store, self.cfg, 21)
        self.assertEqual([it.row["external_id"] for it, _ in items], ["1", "2"])   # 6.5M je >20 % nad rozpočtem
        self.assertIn("<b>30+ days</b>, 1 cut (−10 %)", insights.stale_text(self.store, self.cfg, 21))

    def test_gone_listings_are_not_stale(self):
        self.at(days=33)
        self.run_src([make(eid="1", price=2_700_000), make(eid="3", price=6_500_000)])
        self.assertNotIn("2", [it.row["external_id"] for it, _ in insights.stale_listings(self.store, self.cfg, 21)])


class ExportHistoryTests(HistoryBase):
    def test_csv_has_price_history_and_status(self):
        self.run_src([make(eid="1", price=3_000_000), make(eid="2"), make(eid="3")])
        self.at(days=10)
        self.run_src([make(eid="1", price=2_850_000), make(eid="3")])
        self.at(days=12)
        self.run_src([make(eid="1", price=2_700_000), make(eid="3")])
        content, _ = export.listings_csv(self.store.conn, self.store.now())
        rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig")), delimiter=";"))
        by_id = {r[rows[0].index("url")].rsplit("/", 1)[1]: dict(zip(rows[0], r)) for r in rows[1:]}
        one, two = by_id["1"], by_id["2"]
        self.assertEqual((one["first_price"], one["price"], one["price_change_pct"], one["price_cuts"]),
                         ("3000000", "2700000", "-10", "2"))
        self.assertEqual((one["status"], one["days_online"], one["online_before_watching"]), ("online", "12", "yes"))
        self.assertEqual((two["status"], two["gone_on"], two["days_online"]), ("gone", "2026-11-02", "0"))


class MarketSpeedTests(HistoryBase):
    def test_counts_new_gone_and_cuts_since_last_report(self):
        self.run_src([make(eid=str(i), price=2_000_000 + i) for i in range(6)])       # začátek sledování
        self.at(days=1)
        for i in range(3):                                                          # nové, zmizí za 5 dní
            self.run_src([make(eid=str(j), price=2_000_000 + j) for j in range(6)] + [make(eid=f"n{i}")])
        self.at(days=6)
        self.run_src([make(eid=str(j), price=1_900_000 if j == 0 else 2_000_000 + j) for j in range(6)])
        self.at(days=9)
        self.run_src([make(eid=str(j), price=1_900_000 if j == 0 else 2_000_000 + j) for j in range(6)])
        since = T0 + timedelta(hours=12)
        speeds = report.market_speed(self.store, since)
        s = speeds[0]
        self.assertEqual((s.town, s.new, s.gone, s.cuts), ("Šumperk", 3, 3, 1))
        self.assertEqual(s.typical_days, 0)        # new n0..n2 viděné jen první den
        text = report.format_speed(speeds, since)
        self.assertIn("3 gone", text)

    def test_trend_compares_with_about_three_months_ago(self):
        dates = [(T0 - timedelta(days=d)).isoformat() for d in (120, 91, 63, 28, 7)]
        self.assertEqual(report.pick_reference(dates, T0), dates[1])
        self.assertEqual(report.pick_reference(dates[3:], T0), dates[3])
        self.assertEqual(report.pick_reference(dates[4:], T0), dates[4])
        self.assertIsNone(report.pick_reference([], T0))


class FullScanTests(unittest.TestCase):
    def test_sreality_full_scan_only_when_it_reaches_the_end(self):
        flats = [{"hash_id": i, "price_czk": 1, "category_main_cb": {"value": 1},
                  "category_sub_cb": {"name": "2+1"}, "locality": {"city": "Šumperk"}} for i in range(5)]

        def fake(total):
            def get(self, url, params=None):
                if url == sreality.SEARCH_URL:
                    return {"results": flats if params["offset"] == 0 else [], "pagination": {"total": total}}
                return {"result": {}}
            return get

        for total, expected in ((5, True), (50, False)):
            with mock.patch.object(sreality._Client, "get", fake(total)):
                res = sreality.fetch_listings(config(fetch_details=False, include_houses=False), set())
            self.assertEqual(res.full_scan, expected, total)

    def test_realingo_page_cap_is_not_a_full_scan(self):
        item = {"id": 1, "purpose": "SELL", "property": "FLAT", "category": "FLAT21", "url": "/p/1",
                "price": {"total": 1}, "area": {"main": 54}, "location": {"address": "Rapotín"}}
        html = '<script id="__NEXT_DATA__">' + json.dumps(
            {"props": {"pageProps": {"store": {"offer": {"list": {"data": [item]}}}}}}) + "</script>"
        empty = '<script id="__NEXT_DATA__">' + json.dumps(
            {"props": {"pageProps": {"store": {"offer": {"list": {"data": []}}}}}}) + "</script>"
        cfg = config(fetch_details=False, realingo_districts=["Šumperk"], realingo_max_pages=2)
        with mock.patch.object(realingo, "polite_get", return_value=mock.Mock(text=html)):
            self.assertFalse(realingo.fetch_listings(cfg, set()).full_scan)            # strop 2 stránek
        pages = iter([html, empty])
        with mock.patch.object(realingo, "polite_get", side_effect=lambda *a, **k: mock.Mock(text=next(pages))):
            self.assertTrue(realingo.fetch_listings(cfg, set()).full_scan)


if __name__ == "__main__":
    unittest.main()
