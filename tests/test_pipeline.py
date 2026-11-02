import unittest
from types import SimpleNamespace
from unittest import mock

from flip import pipeline
from flip.model import FetchResult
from flip.notifier import Notifier
from flip.store import Store

from tests.helpers import config, make


class CapturingNotifier(Notifier):
    def __init__(self):
        super().__init__("", "", dry_run=True)
        self.sent: list[str] = []
        self.fail = False
        self.keys: list[tuple[str, str] | None] = []

    def send(self, text: str, *, similar_key=None) -> bool:
        if self.fail:
            return False
        self.sent.append(text)
        self.keys.append(similar_key)
        return True


def fake_source(listings, raw_count=None):
    return SimpleNamespace(fetch_listings=lambda cfg, known: FetchResult(
        list(listings), len(listings) if raw_count is None else raw_count))


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.cfg = config(min_comps=3)
        self.notifier = CapturingNotifier()

    def run_source(self, source, listings, raw_count=None):
        with mock.patch.dict(pipeline.SOURCES, {source: fake_source(listings, raw_count)}):
            return pipeline.run_source(source, self.cfg, self.notifier, self.store)

    def test_first_run_sends_one_summary_then_each_new_listing(self):
        first = [make(eid=str(i), price=2_000_000 + i * 50_000) for i in range(8)]
        stats = self.run_source("sreality", first)
        self.assertTrue(stats.baseline)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("Now watching Sreality", self.notifier.sent[0])

        self.notifier.sent.clear()
        stats = self.run_source("sreality", first + [make(eid="new", town="Rapotín", price=1_500_000)])
        self.assertFalse(stats.baseline)
        self.assertEqual(stats.new_sent, 1)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("Rapotín", self.notifier.sent[0])

    def test_first_run_waits_for_complete_list(self):
        partial = FetchResult([make(eid="1")], 1, complete=False)
        with mock.patch.dict(pipeline.SOURCES, {"sreality": SimpleNamespace(fetch_listings=lambda c, k: partial)}):
            pipeline.run_source("sreality", self.cfg, self.notifier, self.store)
        self.assertEqual(self.notifier.sent, [])
        self.assertFalse(self.store.baseline_done("sreality"))
        self.run_source("sreality", [make(eid="1"), make(eid="2")])
        self.assertEqual(len([m for m in self.notifier.sent if "Now watching" in m]), 1)
        self.assertTrue(self.store.baseline_done("sreality"))

    def test_over_budget_only_when_much_cheaper_per_m2_than_town(self):
        comps = [make(eid=f"c{i}", price=9_600_000, area=200) for i in range(5)]       # 48k/m² v Šumperku
        self.run_source("sreality", comps)                                              # baseline
        self.notifier.sent.clear()
        self.run_source("sreality", comps + [
            make(eid="bargain", price=6_000_000, area=200, subtype="5+1"),   # 30k/m² = −38 % (medián 48k)
            make(eid="pricey", price=9_600_000, area=200, subtype="5+1"),    # 48k/m² = jako obec
        ])
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("💸 Over your 5M budget, but 38 % below", self.notifier.sent[0])

    def test_out_of_area_and_over_budget_are_stored_but_silent(self):
        self.run_source("sreality", [make(eid="0")])          # baseline
        self.notifier.sent.clear()
        self.run_source("sreality", [
            make(eid="0"),
            make(eid="far", town=None),                        # Bludov apod.
            make(eid="rich", price=6_000_000),                 # nad rozpočtem
            make(eid="cabin", kind="dum", subtype="Rodinný", price=1_000_000, area=None),  # bez plochy projde
        ])
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("Family house", self.notifier.sent[0])
        self.assertIsNotNone(self.store.get("sreality", "far"))   # data o trhu zůstávají

    def test_cross_portal_duplicate_is_not_sent_twice_unless_cheaper(self):
        self.run_source("sreality", [make(eid="base")])
        self.run_source("bazos", [make(source="bazos", eid="b0", town="Sudkov")])   # baseline bazoše
        self.notifier.sent.clear()

        self.run_source("sreality", [make(eid="base"), make(eid="1", price=2_500_000, area=70)])
        self.assertEqual(len(self.notifier.sent), 1)

        # stejný byt na Bazoši za stejnou cenu -> nic
        self.run_source("bazos", [make(source="bazos", eid="b0", town="Sudkov"),
                                  make(source="bazos", eid="b1", price=2_500_000, area=70)])
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(self.store.cluster_of("bazos", "b1"), "sreality:1")

        # Bazoš ho zlevnil o 8 % -> 📉
        self.run_source("bazos", [make(source="bazos", eid="b0", town="Sudkov"),
                                  make(source="bazos", eid="b1", price=2_300_000, area=70)])
        self.assertEqual(len(self.notifier.sent), 2)
        self.assertIn("Price drop", self.notifier.sent[1])
        self.assertIn("Bazoš", self.notifier.sent[1])

    def test_price_drop_alert_threshold(self):
        self.run_source("sreality", [make(eid="0")])
        self.run_source("sreality", [make(eid="0"), make(eid="1", price=2_000_000)])
        self.notifier.sent.clear()
        self.run_source("sreality", [make(eid="0"), make(eid="1", price=1_980_000)])   # -1 % -> nic
        self.assertEqual(self.notifier.sent, [])
        self.run_source("sreality", [make(eid="0"), make(eid="1", price=1_900_000)])   # -5 % -> 📉
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("2 000 000 Kč → 1 900 000 Kč", self.notifier.sent[0])

    def test_dropping_under_budget_counts_as_new(self):
        self.run_source("sreality", [make(eid="0")])
        self.run_source("sreality", [make(eid="0"), make(eid="1", price=5_300_000)])
        self.notifier.sent.clear()
        self.run_source("sreality", [make(eid="0"), make(eid="1", price=4_900_000)])
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("cheaper than when first seen", self.notifier.sent[0])

    def test_failed_telegram_send_is_retried_next_run(self):
        self.run_source("sreality", [make(eid="0")])
        self.notifier.sent.clear()
        self.notifier.fail = True
        self.run_source("sreality", [make(eid="0"), make(eid="1")])
        self.notifier.fail = False
        self.run_source("sreality", [make(eid="0"), make(eid="1")])
        self.assertEqual(len(self.notifier.sent), 1)

    def test_failed_baseline_summary_is_retried(self):
        self.notifier.fail = True
        self.run_source("sreality", [make(eid="0")])
        self.assertFalse(self.store.baseline_done("sreality"))
        self.notifier.fail = False
        stats = self.run_source("sreality", [make(eid="0")])
        self.assertTrue(stats.baseline)
        self.assertIn("Flat 2+1", self.notifier.sent[0])

    def test_zero_parsed_listings_is_a_failure_and_warns_once(self):
        for _ in range(4):
            stats = self.run_source("bazos", [], raw_count=0)
            self.assertIsNotNone(stats.error)
        warnings = [m for m in self.notifier.sent if "failed" in m]
        self.assertEqual(len(warnings), 1)
        self.assertIn("3×", warnings[0])

    def test_crashing_source_is_contained(self):
        def boom(cfg, known):
            raise ConnectionError("site down")
        with mock.patch.dict(pipeline.SOURCES, {"realingo": SimpleNamespace(fetch_listings=boom)}):
            stats = pipeline.run_source("realingo", self.cfg, self.notifier, self.store)
        self.assertEqual(stats.error, "site down")

    def test_hot_listing_message_has_flip_estimate(self):
        comps = [make(eid=f"r{i}", price=3_600_000, area=60, condition="Po rekonstrukci") for i in range(3)]
        comps += [make(eid=f"m{i}", price=2_400_000, area=60, condition="Dobrý") for i in range(3)]
        self.run_source("sreality", comps)
        self.notifier.sent.clear()
        wreck = make(eid="w", price=1_400_000, area=60, condition="Před rekonstrukcí", energy_rating="G")
        self.run_source("sreality", comps + [wreck])
        msg = self.notifier.sent[-1]
        self.assertTrue(msg.startswith("🔥"), msg)
        self.assertIn("Flip estimate", msg)
        self.assertIn("Před rekonstrukcí", msg)


if __name__ == "__main__":
    unittest.main()
