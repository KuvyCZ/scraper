"""Druhá vlna: čísla nabídek, /find, oblíbené, /best, 🏁 zmizelé, vážené srovnání, sleva při prodeji,
pozemky domů, znovu-čtení detailů, "levnější na jiném webu", kontrola skóre."""

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from flip import lookup, pipeline, report
from flip.bot import Bot
from flip.dedup import is_duplicate, streets
from flip.market import MarketData, analyze, weighted_median
from flip.model import FetchResult
from flip.sources import sreality
from flip.store import Store

from tests.helpers import config, make
from tests.test_pipeline import CapturingNotifier

T0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


class Base(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.cfg = config(min_comps=3, telegram_chat_id="42")
        self.notifier = CapturingNotifier()
        self.at(days=0)

    def at(self, **delta):
        self.store.set_now(T0 + timedelta(**delta))

    def run_src(self, listings, *, source="sreality", full_scan=True):
        result = FetchResult(list(listings), len(listings) or 1, full_scan=full_scan)
        fake = SimpleNamespace(fetch_listings=lambda cfg, known, *refresh: result)
        with mock.patch.dict(pipeline.SOURCES, {source: fake}):
            return pipeline.run_source(source, self.cfg, self.notifier, self.store)

    def code(self, eid, source="sreality"):
        return self.store.get(source, eid)["code"]


class CodeTests(Base):
    def test_every_listing_gets_a_unique_code_shown_in_messages(self):
        self.run_src([make(eid="a")])
        self.run_src([make(eid="a"), make(eid="b", price=1_500_000)])
        self.assertEqual((self.code("a"), self.code("b")), (1, 2))
        self.assertIn("#2", self.notifier.sent[-1])

    def test_duplicate_code_leads_to_the_canonical_listing(self):
        self.store.upsert(make(eid="a"))
        self.store.upsert(make("bazos", "x"))
        self.store.set_dup_of("bazos", "x", "sreality:a")
        self.assertEqual(self.store.by_code(self.code("x", "bazos"))["external_id"], "a")


class FindTests(Base):
    def test_find_shows_renovated_nearby_and_similar(self):
        self.store.upsert(make(eid="me", price=2_000_000, area=60, condition="Před rekonstrukcí"))
        for i, price in enumerate((3_600_000, 3_900_000, 3_300_000)):
            self.store.upsert(make(eid=f"r{i}", price=price, area=60, condition="Po rekonstrukci"))
        self.store.upsert(make(eid="raw", price=2_200_000, area=62, condition="Před rekonstrukcí"))
        text = lookup.find_text(self.store, self.cfg, self.code("me"))
        self.assertIn("Renovated, similar size in Šumperk", text)
        self.assertIn("typically 60 000 Kč/m², so about 3 600 000 Kč for 60 m²", text)
        self.assertIn("a gap of +1.6M", text)
        self.assertIn("Similar in Šumperk", text)
        self.assertIn("No listing #999", lookup.find_text(self.store, self.cfg, 999))

    def test_plain_number_message_works_like_find(self):
        self.store.upsert(make(eid="me"))
        bot = Bot(self.cfg, self.notifier)
        lookup.register(bot)
        bot.dispatch({"message": {"chat": {"id": 42}, "text": "#1"}}, self.store)
        self.assertIn("Flat 2+1", self.notifier.sent[-1])


class FavoriteTests(Base):
    def setUp(self):
        super().setUp()
        self.bot = Bot(self.cfg, self.notifier)
        lookup.register(self.bot)

    def say(self, text):
        self.bot.dispatch({"message": {"chat": {"id": 42}, "text": text}}, self.store)
        return self.notifier.sent[-1]

    def test_add_list_remove(self):
        self.run_src([make(eid="a", price=2_000_000)])
        self.assertIn("Saved #1", self.say("/fav 1 corner flat"))
        self.assertIn("already", self.say("/fav 1"))
        favs = self.say("/favs")
        self.assertIn("#1", favs)
        self.assertIn("corner flat", favs)
        self.assertIn("Removed #1", self.say("/unfav 1"))
        self.assertIn("No favourites", self.say("/favs"))

    def test_save_button(self):
        self.store.upsert(make(eid="a"))
        self.bot.dispatch({"callback_query": {"id": "q", "data": "fav:sreality:a",
                                              "message": {"chat": {"id": 42}}}}, self.store)
        self.assertTrue(self.store.is_favorite("sreality:a"))

    def test_every_price_change_and_disappearance_is_reported(self):
        others = [make(eid=f"o{i}") for i in range(5)]
        self.run_src([make(eid="a", price=7_000_000), *others])         # nad rozpočtem -> zpráva nepřišla
        self.store.add_favorite("sreality:a")
        self.notifier.sent.clear()
        self.at(days=1)
        self.run_src([make(eid="a", price=6_900_000), *others])          # −1,4 %: malé, mimo rozpočet
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("⭐ <b>Favourite · #1: 7 000 000 Kč → 6 900 000 Kč (−1 %)", self.notifier.sent[0])
        self.at(days=4)
        self.run_src(others)
        self.assertIn("Your favourite is gone, probably sold", self.notifier.sent[-1])
        self.assertIn("Online at least 1 day", self.notifier.sent[-1])   # byl tam už při startu


class GoneNoteTests(Base):
    def test_sent_listing_that_disappears_gets_one_note(self):
        others = [make(eid=f"o{i}") for i in range(5)]
        self.run_src(others)                                              # první běh
        self.run_src([*others, make(eid="a", price=2_100_000)])            # 🏠 zpráva
        self.at(days=9)
        self.run_src([*others, make(eid="a", price=1_990_000)])
        self.at(days=12)
        self.run_src(others)
        self.assertIn("🏁 <b>Gone after 9 days</b>", self.notifier.sent[-1])
        self.assertIn("2.1M → 1.99M", self.notifier.sent[-1])
        n = len(self.notifier.sent)
        self.at(days=13)
        self.run_src(others)
        self.assertEqual(len(self.notifier.sent), n)                      # jen jednou

    def test_no_note_while_the_same_flat_is_still_on_another_site(self):
        others = [make(eid=f"o{i}") for i in range(5)]
        self.run_src(others)
        self.run_src([*others, make(eid="a", price=2_345_000)])
        self.run_src([make("bazos", "x", price=2_345_000)], source="bazos")   # stejný byt na Bazoši
        self.assertEqual(self.store.get("bazos", "x")["dup_of"], "sreality:a")
        self.at(days=1)
        self.run_src([make("bazos", "x", price=2_345_000)], source="bazos")
        self.at(days=3)
        self.run_src([make("bazos", "x", price=2_345_000)], source="bazos")
        self.run_src(others)
        self.assertFalse(any("Gone" in m for m in self.notifier.sent))


class BestTests(Base):
    def test_best_sorts_by_score_and_skips_gone(self):
        flats = [make(eid=f"c{i}", price=3_000_000 + i * 10_000, condition="Po rekonstrukci") for i in range(5)]
        cheap = make(eid="deal", price=1_500_000, condition="Před rekonstrukcí")
        self.run_src([*flats, cheap, make(eid="gone", price=1_400_000, condition="Před rekonstrukcí")])
        self.at(days=3)
        self.run_src([*flats, cheap])
        items = lookup.best(self.store, self.cfg, 3)
        self.assertEqual(items[0][0].external_id, "deal")
        self.assertNotIn("gone", [lst.external_id for lst, _ in items])
        self.assertIn("#", lookup.best_text(self.store, self.cfg, 3))


class MarketHistoryTests(unittest.TestCase):
    def rows(self, items):
        return [dict(source="s", external_id=str(i), kind="byt", price=p, area_m2=60, land_m2=None,
                     building_condition="Po rekonstrukci", description=None, town="Šumperk",
                     first_seen=fs, gone_at=g, seen_at_start=0) for i, (p, fs, g) in enumerate(items)]

    def test_weighted_median(self):
        self.assertEqual(weighted_median([(1, 1), (2, 1), (3, 1)]), 2)
        self.assertEqual(weighted_median([(1, 0.5), (2, 0.5), (3, 1.5)]), 3)

    def test_quick_sales_count_more_than_stale_listings(self):
        old, recent = (T0 - timedelta(days=200)).isoformat(), (T0 - timedelta(days=20)).isoformat()
        rows = [_Row(r) for r in self.rows([
            (3_000_000, old, None), (3_000_000, old, None),               # 50k/m², visí 200 dní
            (2_400_000, recent, T0.isoformat()), (2_400_000, recent, T0.isoformat()),   # 40k, prodané rychle
            (2_700_000, recent, None),
        ])]
        view = analyze(make(eid="x", area=60), MarketData(rows, now=T0), min_comps=3)
        self.assertEqual(view.arv_ppm2, 40_000)

    def test_sale_discount_needs_enough_gone_listings(self):
        sales = [_Row(dict(town="Šumperk", price=900_000, first_price=1_000_000))] * 30
        market = MarketData([], sales=sales)
        self.assertAlmostEqual(market.sale_ratio("Zábřeh").ratio, 0.9)
        self.assertEqual(market.sale_ratio("Šumperk").scope, "okres")
        self.assertAlmostEqual(market.sale_ratio("Šumperk").sale_factor, 0.855)
        self.assertIsNone(MarketData([], sales=sales[:5]).sale_ratio("Šumperk"))

    def test_houses_prefer_similar_land(self):
        rows = [_Row(dict(source="s", external_id=str(i), kind="dum", price=p, area_m2=100, land_m2=land,
                          building_condition=None, description=None, town="Šumperk", first_seen=None,
                          gone_at=None, seen_at_start=0))
                for i, (p, land) in enumerate([(3_000_000, 500)] * 3 + [(6_000_000, 3000)] * 4)]
        view = analyze(make(eid="x", kind="dum", area=100, land_m2=450), MarketData(rows, now=T0), min_comps=3)
        self.assertEqual((view.median_ppm2, view.land_matched), (30_000, True))
        view = analyze(make(eid="y", kind="dum", area=100), MarketData(rows, now=T0), min_comps=3)
        self.assertEqual((view.median_ppm2, view.land_matched), (60_000, False))


class _Row(dict):
    """sqlite3.Row stand-in: r["x"] and r.keys()."""


class CheaperElsewhereTests(Base):
    def test_streets(self):
        self.assertEqual(streets("Šumperk, Šumperk, Zábřežská"), {"zabrezska"})
        self.assertEqual(streets("Zábřežská 12/3, Šumperk, 787 01"), {"zabrezska"})
        self.assertEqual(streets("Šumperk, 787 01"), set())

    def test_same_street_allows_bigger_price_gap(self):
        a = make(eid="a", price=3_000_000, locality="Šumperk, Šumperk, Zábřežská")
        other = dict(source="realingo", kind="byt", town="Šumperk", price=3_300_000, area_m2=60, subtype="2+1",
                     locality="Zábřežská 5, Šumperk")
        self.assertTrue(is_duplicate(a, other))
        self.assertFalse(is_duplicate(a, {**other, "locality": "Revoluční 5, Šumperk"}))
        self.assertFalse(is_duplicate(a, {**other, "price": 3_600_000}))

    def test_cheaper_copy_on_another_site_is_announced(self):
        others = [make(eid=f"o{i}") for i in range(5)]
        self.run_src(others)
        self.run_src([*others, make(eid="a", price=3_300_000, locality="Šumperk, Šumperk, Zábřežská")])
        self.run_src([], source="realingo")                           # první běh Realinga (shrnutí)
        self.run_src([make("realingo", "r", price=3_000_000, locality="Zábřežská 5, Šumperk")], source="realingo")
        self.assertIn("Cheaper on Realingo", self.notifier.sent[-1])


class RefreshTests(Base):
    def test_old_details_in_watched_towns_are_reread(self):
        lst = make(eid="a", description="pěkný byt")
        lst.detail_fetched = True
        self.store.upsert(lst)
        self.store.upsert(make(eid="out", town=None))
        self.assertEqual(self.store.refresh_ids("sreality"), set())
        self.at(days=15)
        self.assertEqual(self.store.refresh_ids("sreality"), {"a"})

    def test_sreality_refresh_uses_spare_detail_budget_and_is_never_deferred(self):
        flats = [{"hash_id": i, "price_czk": 1, "category_main_cb": {"value": 1},
                  "category_sub_cb": {"name": "2+1"}, "locality": {"city": "Šumperk"}} for i in range(6)]
        details = []

        def fake_get(self, url, params=None):
            if url == sreality.SEARCH_URL:
                return {"results": flats if params["offset"] == 0 else [], "pagination": {"total": 6}}
            details.append(url)
            return {"result": {"advert_description": "sleva"}}

        with mock.patch.object(sreality, "DETAIL_LIMIT_PER_RUN", 3), \
                mock.patch.object(sreality._Client, "get", fake_get):
            res = sreality.fetch_listings(config(include_houses=False), {"0", "1", "2", "3"}, {"0", "1"})
        self.assertEqual(len(details), 3)                             # 2 nové + 1 znovu (strop 3)
        self.assertEqual(len(res.listings), 6)                        # nic známého se neodložilo
        self.assertEqual(sum(x.detail_fetched for x in res.listings), 3)


class ScoreCheckTests(Base):
    def test_compares_how_fast_hot_and_other_sent_listings_went(self):
        for i in range(6):
            key = f"sreality:{i}"
            self.store.upsert(make(eid=str(i)))
            self.store.mark_notified(key, 2_000_000, 80 if i < 3 else 30)
            gone = (T0 + timedelta(days=5 if i < 3 else 40)).isoformat()
            self.store.conn.execute("UPDATE listings SET gone_at = ? WHERE external_id = ?", (gone, str(i)))
        self.at(days=45)
        text = report.score_check(self.store, self.cfg)
        self.assertIn("gone after 5 days typically (3 listings)", text)
        self.assertIn("really did go faster", text)


if __name__ == "__main__":
    unittest.main()
