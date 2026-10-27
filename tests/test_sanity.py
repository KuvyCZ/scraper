"""Případy z reálné databáze z NAS (říjen 2026)."""

import os
import tempfile
import unittest
from unittest import mock

from flip.sanity import clean
from flip.sources import realingo
from flip.store import Store
from flip.text import parse_area

from tests.helpers import config, make


class SanityTests(unittest.TestCase):
    def test_rental_listed_as_sale_is_dropped(self):
        rent = make(price=11_000, description="Nabízíme k pronájmu byt o dispozici 2+1 a výměře 55 m²")
        self.assertIsNone(clean(rent))

    def test_tiny_price_without_rental_text_becomes_unknown(self):
        self.assertIsNone(clean(make(price=11_000, description="Prodej bytu 2+1")).price)
        self.assertEqual(clean(make(price=99_000, description="podíl 1/10")).price, 99_000)   # podíl je reálná cena

    def test_house_area_that_is_really_the_plot(self):
        same = clean(make(kind="dum", subtype="Rodinný", area=872.0, land_m2=872.0))
        self.assertEqual((same.area_m2, same.land_m2), (None, 872.0))
        title_only = clean(make(kind="dum", subtype="Rodinný", area=691.0))     # "RD 3+1 Lesnice 691 m2"
        self.assertEqual((title_only.area_m2, title_only.land_m2), (None, 691.0))
        normal = clean(make(kind="dum", subtype="Rodinný", area=140.0, land_m2=900.0))
        self.assertEqual((normal.area_m2, normal.land_m2), (140.0, 900.0))

    def test_bogus_plot_and_flat_area(self):
        self.assertIsNone(clean(make(kind="dum", subtype="Rodinný", land_m2=1.0)).land_m2)
        self.assertIsNone(clean(make(area=400.0)).area_m2)

    def test_missing_layout_and_area_taken_from_description(self):
        lst = clean(make(source="realingo", subtype=None, area=None,
                         description="Nabízíme byt 3+1 o podlahové ploše 73 m², sklep 4 m²"))
        self.assertEqual((lst.subtype, lst.area_m2), ("3+1", 73.0))


class AreaParsingTests(unittest.TestCase):
    def test_area_formats_seen_on_bazos(self):
        self.assertEqual(parse_area("Prodej byt 3+1, 76㎡"), 76.0)
        self.assertEqual(parse_area("2. nadzemní podlaží 35m čtverečních"), 35.0)

    def test_labelled_area_wins_and_garden_is_skipped(self):
        self.assertEqual(parse_area("pozemek 429 m² Dispozice: 2+1 Užitná plocha: 80 m²"), 80.0)
        self.assertIsNone(parse_area("zvýšené 2. patro, za domem společná zahrada 200m²"))
        self.assertEqual(parse_area("obývací pokoj 24,54 m², celkem 60 m2"), 60.0)


class RealingoResilienceTests(unittest.TestCase):
    def test_null_detail_does_not_crash(self):
        page = '<script id="__NEXT_DATA__">{"props":{"pageProps":{"store":{"offer":{"details":{"5":{"offer":{"detail":null}}}}}}}}</script>'
        with mock.patch.object(realingo, "polite_get", return_value=mock.Mock(text=page)):
            self.assertIsNone(realingo._fetch_description(None, "u", "5"))

    def test_one_bad_item_does_not_kill_the_source(self):
        good = {"id": 1, "purpose": "SELL", "property": "FLAT", "category": "FLAT21", "url": "/p/1",
                "price": {"total": 1_990_000}, "area": {"main": 54}, "location": {"address": "Rapotín"}}
        page = {"props": {"pageProps": {"store": {"offer": {"list": {"data": [None, {"id": 2, "purpose": "SELL",
                "property": "FLAT", "price": 5, "location": "x"}, good]}}}}}}
        import json
        html = f'<script id="__NEXT_DATA__">{json.dumps(page)}</script>'
        responses = iter([mock.Mock(text=html), None])
        with mock.patch.object(realingo, "polite_get", side_effect=lambda *a, **k: next(responses)):
            res = realingo.fetch_listings(config(fetch_details=False), set())
        self.assertEqual([x.external_id for x in res.listings], ["1"])


class StoredDataCleanupTests(unittest.TestCase):
    def test_old_rows_are_cleaned_once(self):
        path = os.path.join(tempfile.mkdtemp(), "flip.db")
        with Store(path) as store:
            store.conn.execute("PRAGMA user_version = 1")       # databáze z doby před kontrolami
            store.upsert(make(source="sreality", eid="rent", price=11_000, description="k pronájmu"))
            store.upsert(make(source="bazos", eid="plot", kind="dum", area=691.0, locality="Šumperk 787 01"))
            store.upsert(make(source="sreality", eid="ok", price=2_000_000, area=60.0))
        with Store(path) as store:
            rent = store.get("sreality", "rent")
            self.assertEqual((rent["town"], rent["price"]), (None, None))
            plot = store.get("bazos", "plot")
            self.assertEqual((plot["area_m2"], plot["land_m2"]), (None, 691.0))
            self.assertEqual(store.get("sreality", "ok")["area_m2"], 60.0)


if __name__ == "__main__":
    unittest.main()
