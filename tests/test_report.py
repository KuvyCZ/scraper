import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from flip.report import CSV_NAME, report_due, send_town_report
from flip.store import Store

from tests.helpers import config, make
from tests.test_pipeline import CapturingNotifier


class WeeklyTownReportTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = config(db_path=os.path.join(self.dir, "flip.db"))
        self.store = Store(self.cfg.db_path)
        self.notifier = CapturingNotifier()
        self.t0 = datetime(2026, 11, 2, tzinfo=timezone.utc)
        self.store.set_now(self.t0)

    def add(self, price, n=3, town="Šumperk", prefix="a"):
        for i in range(n):
            self.store.upsert(make(eid=f"{prefix}{i}", town=town, price=price, area=60))

    def test_sends_saves_and_compares_with_last_week(self):
        self.add(2_400_000)                                   # 40k/m²
        self.assertTrue(report_due(self.store, self.cfg))
        self.assertTrue(send_town_report(self.store, self.cfg, self.notifier))
        self.assertIn("Šumperk", self.notifier.sent[0])
        self.assertIn("40k", self.notifier.sent[0])
        self.assertFalse(report_due(self.store, self.cfg))     # další až za týden

        self.store.set_now(self.t0 + timedelta(days=7))
        self.add(3_000_000, prefix="a")                       # ceny stejných inzerátů vzrostly na 50k/m²
        self.assertTrue(report_due(self.store, self.cfg))
        send_town_report(self.store, self.cfg, self.notifier)
        self.assertIn("▲25%", "\n".join(self.notifier.sent[-3:]))

        with open(os.path.join(self.dir, CSV_NAME), encoding="utf-8-sig") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(lines[0].split(";")[:3], ["date", "town", "kind"])
        self.assertEqual(len(lines), 3)                        # hlavička + 2 týdny

    def test_old_listings_left_out(self):
        self.add(2_400_000)
        self.store.set_now(self.t0 + timedelta(days=60))       # nikdo je 60 dní neviděl
        self.assertFalse(send_town_report(self.store, self.cfg, self.notifier))
        self.assertEqual(self.notifier.sent, [])

    def test_failed_send_is_retried(self):
        self.add(2_400_000)
        self.notifier.fail = True
        self.assertFalse(send_town_report(self.store, self.cfg, self.notifier))
        self.assertTrue(report_due(self.store, self.cfg))

    def test_can_be_switched_off(self):
        self.assertFalse(report_due(self.store, config(town_report_days=0)))


if __name__ == "__main__":
    unittest.main()
