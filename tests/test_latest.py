import unittest
from datetime import datetime, timedelta, timezone

from flip import latest
from flip.store import Store

from tests.helpers import config, make
from tests.test_pipeline import CapturingNotifier


class LatestTests(unittest.TestCase):
    def test_newest_first_rescored_against_town(self):
        store = Store(":memory:")
        t = datetime(2026, 11, 1, tzinfo=timezone.utc)
        for i in range(5):
            store.set_now(t + timedelta(hours=i))
            store.upsert(make(eid=f"c{i}", price=3_000_000, area=60))          # 50k/m²
        store.set_now(t + timedelta(days=1))
        store.upsert(make(eid="new", price=2_100_000, area=60))                  # 35k/m² = −30 %
        items = latest.latest_rescored(store, config(min_comps=3), n=2)
        self.assertEqual([lst.external_id for lst, _ in items], ["new", "c4"])
        text = latest.format_latest(items, config())
        self.assertIn("-30 % vs Šumperk", text)

    def test_telegram_command(self):
        store = Store(":memory:")
        store.upsert(make(eid="1"))
        notifier = CapturingNotifier()
        latest.cmd_latest({"text": "/latest 3"}, store, config(), notifier)
        self.assertIn("Latest 1 listings", notifier.sent[0])


class ExpensiveTests(unittest.TestCase):
    def test_lists_over_budget_cheapest_per_m2_first(self):
        store = Store(":memory:")
        for i in range(5):
            store.upsert(make(eid=f"c{i}", price=9_600_000, area=200, subtype="5+1"))     # 48k/m²
        store.upsert(make(eid="deal", price=6_000_000, area=200, subtype="5+1"))          # 30k/m²
        store.upsert(make(eid="cheap", price=2_000_000, area=60))                          # v rozpočtu
        items = latest.expensive_rescored(store, config(min_comps=3))
        self.assertEqual(items[0][0].external_id, "deal")
        self.assertNotIn("cheap", [lst.external_id for lst, _ in items])
        text = latest.format_expensive(items, config(), 10)
        self.assertIn("1 are at least 20 % cheaper", text)
        self.assertIn("✅ Flat 5+1 · Šumperk · 6 000 000 Kč", text)


if __name__ == "__main__":
    unittest.main()
