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


if __name__ == "__main__":
    unittest.main()
