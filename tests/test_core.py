import unittest
from datetime import datetime, timedelta, timezone

from flip.dedup import is_duplicate
from flip.market import MarketData, MarketView, analyze
from flip.scoring import score_listing
from flip.store import Store, cluster_key

from tests.helpers import config, make


def _store_with(*listings) -> Store:
    store = Store(":memory:")
    for lst in listings:
        store.upsert(lst)
    return store


class MarketTests(unittest.TestCase):
    def test_area_first_then_district_fallback(self):
        area_comps = [make(eid=f"a{i}", price=2_400_000, area=60) for i in range(3)]       # 40k/m² v obcích
        district = [make(eid=f"d{i}", town=None, price=1_800_000, area=60) for i in range(5)]  # 30k/m² okres
        store = _store_with(*area_comps, *district)
        market = MarketData(store.market_rows(kind="byt", window_days=365, in_area_only=False))
        subject = make(eid="x", area=62)
        view = analyze(subject, market, min_comps=3)
        self.assertEqual(view.scope, "area")
        self.assertAlmostEqual(view.median_ppm2, 40_000)
        view4 = analyze(subject, market, min_comps=4)     # v obcích jen 3 -> okres
        self.assertEqual(view4.scope, "district")

    def test_arv_only_from_renovated_and_ignores_own_cluster(self):
        done = [make(eid=f"r{i}", price=3_000_000, area=60, condition="Po rekonstrukci") for i in range(3)]
        raw = [make(eid=f"p{i}", price=1_500_000, area=60, condition="Před rekonstrukcí") for i in range(3)]
        store = _store_with(*done, *raw)
        market = MarketData(store.market_rows(kind="byt", window_days=365, in_area_only=False))
        view = analyze(make(eid="r0", area=60), market, min_comps=3, cluster={("sreality", "r0")})
        self.assertIsNone(view.arv_ppm2)                   # jen 2 hotové zbydou -> nehádá se
        view = analyze(make(eid="new", area=60), market, min_comps=3)
        self.assertAlmostEqual(view.arv_ppm2, 50_000)

    def test_area_band_and_kind_separation(self):
        houses = [make(eid=f"h{i}", kind="dum", subtype="Rodinný", price=3_000_000, area=150) for i in range(5)]
        flats_small = [make(eid=f"f{i}", price=2_000_000, area=30) for i in range(5)]
        store = _store_with(*houses, *flats_small)
        flats = MarketData(store.market_rows(kind="byt", window_days=365, in_area_only=False))
        self.assertIsNone(analyze(make(eid="x", area=70), flats, min_comps=3).median_ppm2)  # 30 m² nejsou srovnatelné


class ScoringTests(unittest.TestCase):
    def test_cheap_wreck_with_good_margin_is_hot(self):
        lst = make(price=1_500_000, area=60, condition="Před rekonstrukcí", energy_rating="G")
        view = MarketView(median_ppm2=40_000, comps_n=8, scope="area", arv_ppm2=60_000, arv_n=6, arv_scope="area")
        res = score_listing(lst, view, config())
        # ARV 3.6M*0.92=3.312M; reno 1.5M; holding 0.15M -> marže ~0.66M (~21 %)
        self.assertTrue(res.needs_work)
        self.assertIsNotNone(res.estimate)
        self.assertGreater(res.estimate.margin, 0)
        self.assertTrue(res.hot)
        self.assertGreaterEqual(res.score, 65)

    def test_renovated_flat_scores_low_and_no_flip_estimate(self):
        lst = make(price=3_300_000, area=60, condition="Po rekonstrukci")
        view = MarketView(median_ppm2=50_000, comps_n=8, scope="area", arv_ppm2=60_000, arv_n=6, arv_scope="area")
        res = score_listing(lst, view, config())
        self.assertIsNone(res.estimate)
        self.assertFalse(res.hot)
        self.assertLess(res.score, 40)

    def test_serious_risk_halves_and_never_hot(self):
        lst = make(price=900_000, area=60, condition="Před rekonstrukcí",
                   description="Prodej spoluvlastnického podílu 1/2")
        view = MarketView(median_ppm2=40_000, comps_n=8, scope="area", arv_ppm2=60_000, arv_n=6, arv_scope="area")
        res = score_listing(lst, view, config())
        self.assertIn("share", res.risks)
        self.assertFalse(res.hot)
        self.assertLessEqual(res.score, 50)

    def test_no_price_signals_never_hot(self):
        lst = make(price=None, condition="Před rekonstrukcí")
        res = score_listing(lst, MarketView(), config())
        self.assertEqual(res.score, 100)        # jen stav… ale bez cenového signálu
        self.assertFalse(res.hot)

    def test_price_drop_and_time_on_market(self):
        now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        lst = make(price=1_800_000)
        res = score_listing(lst, MarketView(), config(), price_history=[(2_000_000, "x"), (1_800_000, "y")],
                            first_seen=(now - timedelta(days=60)).isoformat(), now=now)
        self.assertEqual(res.price_drop_pct, 10.0)
        self.assertEqual(res.days_on_market, 60)
        self.assertEqual(res.signals["motivace"], 1.0)

    def test_house_uses_house_reno_cost(self):
        lst = make(kind="dum", subtype="Rodinný", price=2_000_000, area=100, condition="Před rekonstrukcí")
        view = MarketView(median_ppm2=30_000, comps_n=8, scope="area", arv_ppm2=50_000, arv_n=6, arv_scope="area")
        res = score_listing(lst, view, config())
        self.assertEqual(res.estimate.reno_cost, 3_000_000)


class DedupTests(unittest.TestCase):
    def test_same_property_other_portal(self):
        store = _store_with(make(source="sreality", eid="1", price=2_000_000, area=60))
        row = store.get("sreality", "1")
        self.assertTrue(is_duplicate(make(source="bazos", eid="9", price=2_030_000, area=61), row))

    def test_not_duplicate(self):
        store = _store_with(make(source="sreality", eid="1", price=2_000_000, area=60))
        row = store.get("sreality", "1")
        self.assertFalse(is_duplicate(make(source="bazos", eid="9", price=2_100_000, area=60), row))   # cena
        self.assertFalse(is_duplicate(make(source="bazos", eid="9", area=65), row))                    # plocha
        self.assertFalse(is_duplicate(make(source="bazos", eid="9", subtype="3+1"), row))             # dispozice
        self.assertFalse(is_duplicate(make(source="bazos", eid="9", town="Rapotín"), row))            # obec
        self.assertFalse(is_duplicate(make(source="bazos", eid="9", area=None), row))                 # bez plochy
        self.assertFalse(is_duplicate(make(source="sreality", eid="2"), row))                         # stejný portál


class StoreTests(unittest.TestCase):
    def test_detail_fields_survive_refetch_without_detail(self):
        store = _store_with(make(eid="1", description="k rekonstrukci", condition="Před rekonstrukcí"))
        prev, changed = store.upsert(make(eid="1", description=None, condition=None))
        self.assertIsNotNone(prev)
        self.assertFalse(changed)
        row = store.get("sreality", "1")
        self.assertEqual(row["description"], "k rekonstrukci")
        self.assertEqual(row["building_condition"], "Před rekonstrukcí")

    def test_price_history_only_on_change(self):
        store = _store_with(make(eid="1", price=2_000_000))
        store.upsert(make(eid="1", price=2_000_000))
        store.upsert(make(eid="1", price=1_900_000))
        self.assertEqual([p for p, _ in store.price_history("sreality", "1")], [2_000_000, 1_900_000])

    def test_cluster(self):
        store = _store_with(make(source="sreality", eid="1"), make(source="bazos", eid="9"))
        store.set_dup_of("bazos", "9", cluster_key("sreality", "1"))
        self.assertEqual(store.cluster_of("bazos", "9"), "sreality:1")
        self.assertEqual(len(store.cluster_members("sreality:1")), 2)
        # duplicita se nepočítá do mediánů dvakrát
        self.assertEqual(len(store.market_rows(kind="byt", window_days=365, in_area_only=True)), 1)

    def test_old_bazos_towns_without_psc_are_dropped_once(self):
        import os, tempfile
        path = os.path.join(tempfile.mkdtemp(), "flip.db")
        with Store(path) as store:
            store.conn.execute("PRAGMA user_version = 0")      # databáze z doby před PSČ
            store.upsert(make(source="bazos", eid="old", town="Šumperk", locality="Šumperk"))
            store.upsert(make(source="bazos", eid="new", town="Šumperk", locality="Šumperk 787 01"))
            store.upsert(make(source="sreality", eid="1", town="Šumperk", locality="Šumperk"))
        with Store(path) as store:
            self.assertIsNone(store.get("bazos", "old")["town"])
            self.assertEqual(store.get("bazos", "new")["town"], "Šumperk")
            self.assertEqual(store.get("sreality", "1")["town"], "Šumperk")
            # znovu viděný inzerát dostane obec zpět a další start ji už nemaže
            store.upsert(make(source="bazos", eid="old", town="Šumperk", locality="Šumperk"))
        with Store(path) as store:
            self.assertEqual(store.get("bazos", "old")["town"], "Šumperk")

    def test_market_window(self):
        store = Store(":memory:")
        store.set_now(datetime(2025, 1, 1, tzinfo=timezone.utc))
        store.upsert(make(eid="old"))
        store.set_now(datetime(2026, 10, 1, tzinfo=timezone.utc))
        store.upsert(make(eid="new"))
        rows = store.market_rows(kind="byt", window_days=365, in_area_only=False)
        self.assertEqual([r["external_id"] for r in rows], ["new"])

    def test_failure_warning_once(self):
        store = Store(":memory:")
        for _ in range(2):
            store.record_failure("bazos", "x")
        self.assertFalse(store.should_warn("bazos", 3))
        store.record_failure("bazos", "x")
        self.assertTrue(store.should_warn("bazos", 3))
        store.mark_warned("bazos")
        store.record_failure("bazos", "x")
        self.assertFalse(store.should_warn("bazos", 3))
        store.record_success("bazos")
        self.assertEqual(store.record_failure("bazos", "x"), 1)


if __name__ == "__main__":
    unittest.main()
