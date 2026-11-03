"""⭐🏁 oblíbená nemovitost zmizela: zpráva (která, cena, jak rychle) + přímá kontrola stránky."""

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import httpx

from flip import favcheck, pipeline
from flip.model import FetchResult
from flip.store import Store

from tests.helpers import config, make
from tests.test_pipeline import CapturingNotifier

T0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
OTHERS = [make(eid=f"o{i}", price=2_500_000 + i) for i in range(6)]


class FavBase(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.cfg = config(min_comps=3)
        self.notifier = CapturingNotifier()
        self.at(days=0)

    def at(self, **delta):
        self.store.set_now(T0 + timedelta(**delta))

    def run_src(self, listings, *, source="sreality", full_scan=True):
        result = FetchResult(list(listings), len(listings) or 1, full_scan=full_scan)
        fake = SimpleNamespace(fetch_listings=lambda cfg, known, *refresh: result)
        with mock.patch.dict(pipeline.SOURCES, {source: fake}):
            return pipeline.run_source(source, self.cfg, self.notifier, self.store)


class FavouriteSoldMessage(FavBase):
    def test_says_which_listing_price_and_how_fast(self):
        self.run_src(OTHERS)                                    # první běh: start sledování
        self.at(days=1)
        self.run_src([make(eid="fav", price=2_400_000, area=54), *OTHERS])
        self.store.add_favorite("sreality:fav", "corner flat")
        self.at(days=4)
        self.run_src([make(eid="fav", price=2_300_000, area=54), *OTHERS])
        self.at(days=7)
        self.run_src(OTHERS)                                    # 3 dny chybí -> zmizel
        msg = self.notifier.sent[-1]
        self.assertIn("⭐🏁 <b>Your favourite is gone, probably sold</b>", msg)
        self.assertIn("Flat 2+1 · 54 m² · Šumperk · #", msg)
        self.assertIn("⏱ Sold in 3 days (online 3 Nov → 6 Nov)", msg)
        self.assertIn("Last price 2 300 000 Kč", msg)
        self.assertIn("corner flat", msg)
        self.assertIn('href="https://example.test/sreality/fav"', msg)
        self.assertEqual(sum("probably sold" in m for m in self.notifier.sent), 1)

    def test_compares_with_how_fast_others_sold(self):
        self.run_src(OTHERS)
        quick = [make(eid=f"q{i}", price=2_100_000 + i) for i in range(3)]
        self.at(days=1)
        self.run_src([*quick, *OTHERS])
        self.at(days=10)
        self.run_src([*quick, *OTHERS])                         # naposledy viděny po 9 dnech
        self.at(days=13)
        self.run_src([make(eid="fav"), *OTHERS])                # 3 jiné byty v Šumperku zmizely
        self.store.add_favorite("sreality:fav")
        self.at(days=15)
        self.run_src([make(eid="fav"), *OTHERS])
        self.at(days=18)
        self.run_src(OTHERS)
        self.assertIn("Sold in 2 days", self.notifier.sent[-1])
        self.assertIn("faster than usual: flats in Šumperk typically go in 9 days (3 sold)", self.notifier.sent[-1])

    def test_not_favourite_keeps_short_gone_message(self):
        self.run_src(OTHERS)
        self.at(days=1)
        self.run_src([make(eid="a"), *OTHERS])
        self.store.mark_notified("sreality:a", 2_000_000, 50)
        self.at(days=4)
        self.run_src(OTHERS)
        self.assertTrue(self.notifier.sent[-1].startswith("🏁 <b>Gone after"))


class FavouritePageCheck(FavBase):
    def setUp(self):
        super().setUp()
        self.run_src([make("bazos", "b1", price=1_900_000)], source="bazos", full_scan=False)
        self.store.add_favorite("bazos:b1")
        self.notifier.sent.clear()

    def test_missing_page_means_sold(self):
        self.at(hours=12)
        self.assertEqual(favcheck.check_favorites(self.store, self.notifier, self.cfg, check=lambda *a: True), 0)
        self.at(days=3)                                          # Bazoš ho 3 dny neukázal
        calls = []
        sent = favcheck.check_favorites(self.store, self.notifier, self.cfg,
                                        check=lambda *a: calls.append(a) or True)
        self.assertEqual((sent, len(calls)), (1, 1))
        self.assertIn("probably sold", self.notifier.sent[-1])
        self.assertIsNotNone(self.store.get("bazos", "b1")["gone_at"])
        self.at(days=4)                                          # už ohlášená -> znovu ne
        self.assertEqual(favcheck.check_favorites(self.store, self.notifier, self.cfg, check=lambda *a: True), 0)

    def test_page_still_there_or_unknown_sends_nothing_and_waits(self):
        self.at(days=3)
        for answer in (False, None):
            self.store.conn.execute("UPDATE favorites SET checked_at = NULL")
            self.assertEqual(favcheck.check_favorites(self.store, self.notifier, self.cfg,
                                                      check=lambda *a: answer), 0)
        self.assertEqual(self.notifier.sent, [])
        self.assertIsNone(self.store.get("bazos", "b1")["gone_at"])
        calls = []
        self.at(days=3, hours=6)                                  # do 12 h se stránka znovu neotvírá
        favcheck.check_favorites(self.store, self.notifier, self.cfg, check=lambda *a: calls.append(a))
        self.assertEqual(calls, [])

    def test_page_responses(self):
        def answer(status, body="", location=None):
            headers = {"location": location} if location else {}
            transport = httpx.MockTransport(lambda req: httpx.Response(status, text=body, headers=headers))
            client = httpx.Client(transport=transport)
            robots = SimpleNamespace(can_fetch=lambda url: True)
            return favcheck.page_gone("bazos", "123", "https://reality.bazos.cz/inzerat/123/x.php",
                                      client=client, robots=robots)
        self.assertIs(answer(404), True)
        self.assertIs(answer(410), True)
        self.assertIs(answer(302, location="https://reality.bazos.cz/prodam/byt/"), True)
        self.assertIs(answer(301, location="https://reality.bazos.cz/inzerat/123/y.php"), False)
        self.assertIs(answer(200), False)
        self.assertIsNone(answer(503))

    def test_realingo_page_without_detail_is_gone(self):
        def answer(detail):
            import json
            data = {"props": {"pageProps": {"store": {"offer": {"details": {"9": {"offer": {"detail": detail}}}}}}}}
            body = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'
            client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=body)))
            return favcheck.page_gone("realingo", "9", "https://www.realingo.cz/x/9", client=client,
                                      robots=SimpleNamespace(can_fetch=lambda url: True))
        self.assertIs(answer(None), True)
        self.assertIs(answer({"description": "pěkný byt"}), False)

    def test_robots_disallow_means_unknown(self):
        client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(404)))
        self.assertIsNone(favcheck.page_gone("bazos", "1", "https://x.test/1", client=client,
                                             robots=SimpleNamespace(can_fetch=lambda url: False)))


if __name__ == "__main__":
    unittest.main()
