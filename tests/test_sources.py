"""Parsování zdrojů nad uloženými ukázkami HTML/JSON ve tvaru, který selektory očekávají.

Testuje NAŠI logiku (obec, typ, plocha, cena, vyřazení chat). Jestli web pořád
vypadá takhle, ověřuje `python main.py check` proti živému webu.
"""

import json
import unittest

from flip.http import RobotsChecker
from flip.sources import bazos, realingo, reality_sumperk, sreality


class SrealityTests(unittest.TestCase):
    def estate(self, **kw):
        base = {
            "hash_id": 123, "price_czk": 2_400_000, "price_czk_m2": 40_000,
            "category_main_cb": {"value": 1}, "category_sub_cb": {"name": "2+1"},
            "locality": {"city": "Šumperk", "citypart": "Horní Temenice", "street": "Temenická",
                         "city_seo_name": "sumperk", "citypart_seo_name": "horni-temenice",
                         "gps_lat": 49.97, "gps_lon": 16.97},
        }
        base.update(kw)
        return base

    def test_flat(self):
        lst = sreality.normalize_search(self.estate())
        self.assertEqual((lst.kind, lst.subtype, lst.town, lst.area_m2), ("byt", "2+1", "Šumperk", 60.0))
        self.assertEqual(lst.url, "https://www.sreality.cz/detail/prodej/byt/2+1/sumperk-horni-temenice/123")

    def test_house_and_exclusions(self):
        house = sreality.normalize_search(self.estate(
            category_main_cb={"value": 2}, category_sub_cb={"name": "Rodinný"},
            locality={"city": "Nový Malín", "city_seo_name": "novy-malin"}))
        self.assertEqual((house.kind, house.town), ("dum", "Nový Malín"))
        self.assertIn("/prodej/dum/rodinny/novy-malin/", house.url)
        cabin = self.estate(category_main_cb={"value": 2}, category_sub_cb={"name": "Chata"})
        self.assertIsNone(sreality.normalize_search(cabin))
        plot = self.estate(category_main_cb={"value": 3})
        self.assertIsNone(sreality.normalize_search(plot))

    def test_outside_area_kept_as_market_data(self):
        lst = sreality.normalize_search(self.estate(locality={"city": "Bludov", "district": "Šumperk"}))
        self.assertIsNone(lst.town)

    def test_detail(self):
        lst = sreality.normalize_search(self.estate(category_main_cb={"value": 2}, category_sub_cb={"name": "Rodinný"}))
        sreality.enrich_detail(lst, {"result": {
            "advert_description": "K rekonstrukci", "building_condition": {"name": "Před rekonstrukcí"},
            "energy_efficiency_rating_cb": {"value": 7}, "usable_area": 140, "estate_area": 900}})
        self.assertEqual((lst.building_condition, lst.energy_rating, lst.area_m2, lst.land_m2),
                         ("Před rekonstrukcí", "G", 140.0, 900.0))


class RealingoTests(unittest.TestCase):
    def setUp(self):
        self.sel = realingo.load_selectors()

    def item(self, **kw):
        base = {"id": 55, "purpose": "SELL", "property": "FLAT", "category": "FLAT21", "url": "/prodej/55",
                "price": {"total": 1_990_000}, "area": {"main": 54},
                "location": {"address": "Školní, Rapotín"}}
        base.update(kw)
        return base

    def test_next_data_roundtrip(self):
        page = {"props": {"pageProps": {"store": {"offer": {"list": {"data": [self.item()]}}}}}}
        html = f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(page)}</script></html>'
        items = realingo.list_items(realingo.parse_next_data(html))
        self.assertEqual(len(items), 1)

    def test_flat(self):
        lst = realingo.normalize_item(self.item(), self.sel)
        self.assertEqual((lst.kind, lst.subtype, lst.town, lst.price), ("byt", "2+1", "Rapotín", 1_990_000))
        self.assertEqual(lst.url, "https://realingo.cz/prodej/55")

    def test_house_types(self):
        house = realingo.normalize_item(self.item(property="HOUSE", category="HOUSE_FAMILY",
                                                  area={"main": 120, "plot": 800}), self.sel)
        self.assertEqual((house.kind, house.subtype, house.land_m2), ("dum", "Rodinný", 800.0))
        cottage = realingo.normalize_item(self.item(property="HOUSE", category="HOUSE_COTTAGE"), self.sel)
        self.assertEqual(cottage.subtype, "Chalupa")
        self.assertIsNone(realingo.normalize_item(self.item(property="HOUSE", category="HOUSE_CABIN"), self.sel))

    def test_rent_and_other_types_skipped(self):
        self.assertIsNone(realingo.normalize_item(self.item(purpose="RENT"), self.sel))
        self.assertIsNone(realingo.normalize_item(self.item(property="LAND"), self.sel))


BAZOS_BLOCK = """<div class="inzeraty inzeratyflex">
<h2 class=nadpis><a href="/inzerat/{id}/prodej.php">{title}</a></h2>
<div class=popis>{desc}</div>
<div class="inzeratycena"><b><span translate="no"> {price}</span></b></div>
<div class="inzeratylok">{town}<br>{psc}</div>
</div>"""


class BazosTests(unittest.TestCase):
    def setUp(self):
        self.sel = bazos.load_selectors()

    def page(self, *blocks):
        return "<html>" + "".join(BAZOS_BLOCK.format(**{"psc": "787 01", **b}) for b in blocks) + "</html>"

    def test_flat_and_house(self):
        html = self.page(
            {"id": 1, "title": "Prodej bytu 2+1", "desc": "Byt 56 m², k rekonstrukci", "price": "1 650 000 Kč",
             "town": "Šumperk"},
            {"id": 2, "title": "Prodej RD v Sudkově", "desc": "Dům 130 m2, pozemek 1 100 m2", "price": "2 900 000 Kč",
             "town": "Sudkov", "psc": "788 21"},
        )
        items = bazos.parse_page(html, self.sel)
        self.assertEqual(len(items), 2)
        flat = bazos.to_listing(items[0], "byt", self.sel["base_url"])
        self.assertEqual((flat.town, flat.subtype, flat.area_m2, flat.price, flat.parse_confidence),
                         ("Šumperk", "2+1", 56.0, 1_650_000, "high"))
        house = bazos.to_listing(items[1], "dum", self.sel["base_url"])
        self.assertEqual((house.town, house.area_m2, house.land_m2), ("Sudkov", 130.0, 1100.0))

    def test_low_confidence_and_cabin(self):
        items = bazos.parse_page(self.page(
            {"id": 3, "title": "Prodám byt", "desc": "Volejte", "price": "Dohodou", "town": "Rapotín",
             "psc": "788 14"},
            {"id": 4, "title": "Prodej chaty", "desc": "Chata 40 m2", "price": "500 000 Kč", "town": "Sudkov"},
        ), self.sel)
        flat = bazos.to_listing(items[0], "byt", self.sel["base_url"])
        self.assertEqual((flat.price, flat.parse_confidence), (None, "low"))
        self.assertIsNone(bazos.to_listing(items[1], "dum", self.sel["base_url"]))

    def test_town_by_psc_not_name(self):
        items = bazos.parse_page(self.page(
            # Bazoš píše "Šumperk", ale PSČ je Vikýřovic -> Vikýřovice
            {"id": 5, "title": "Byt 2+1", "desc": "56 m2", "price": "1 Kč", "town": "Šumperk", "psc": "788 13"},
            # "Šumperk" s PSČ mimo sledované obce (Zábřeh) -> mimo oblast
            {"id": 6, "title": "Byt 2+1", "desc": "56 m2", "price": "1 Kč", "town": "Šumperk", "psc": "789 01"},
            # neznámý název, ale sledované PSČ -> projde
            {"id": 7, "title": "Byt 2+1", "desc": "56 m2", "price": "1 Kč", "town": "Hrabišín", "psc": "78803"},
        ), self.sel)
        self.assertEqual([i["psc"] for i in items], ["788 13", "789 01", "78803"])
        towns = [bazos.to_listing(i, "byt", self.sel["base_url"]).town for i in items]
        self.assertEqual(towns, ["Vikýřovice", None, "Nový Malín"])
        self.assertEqual(bazos.to_listing(items[0], "byt", self.sel["base_url"]).locality, "Šumperk 788 13")

    def test_custom_psc_map_and_name_fallback(self):
        items = bazos.parse_page(self.page(
            {"id": 8, "title": "Byt", "desc": "", "price": "1 Kč", "town": "Šumperk", "psc": "789 01"},
        ), self.sel)
        lst = bazos.to_listing(items[0], "byt", self.sel["base_url"], {"78901": "Šumperk"})
        self.assertEqual(lst.town, "Šumperk")
        no_psc = dict(items[0], psc="")                            # PSČ nenalezeno -> podle názvu
        self.assertEqual(bazos.to_listing(no_psc, "byt", self.sel["base_url"], {}).town, "Šumperk")


SUMPERK_BLOCK = """<div class="estate">
<h3><a href="/detail/prodej-{slug}-{id}.html" title="x">{title}</a></h3>
<p class="fs-14 no-margin-bottom adress">{address}</p>
<p class="hidden-xxs-down">{desc}</p>
<div>Cena: <strong><span class="big text-blue">{price}</span></strong></div>
</div>"""


class RealitySumperkTests(unittest.TestCase):
    def setUp(self):
        self.sel = reality_sumperk.load_selectors()

    def test_parse(self):
        html = "".join([
            SUMPERK_BLOCK.format(slug="bytu", id=11, title="Prodej bytu 3+1, 72 m²", address="Žerotínova, Šumperk",
                                 desc="Byt v původním stavu…", price="2 190 000 Kč"),
            SUMPERK_BLOCK.format(slug="domu", id=12, title="Prodej rodinného domu 160 m², pozemek 700 m²",
                                 address="Králec", desc="", price="Info v RK"),
        ])
        items = reality_sumperk.parse_page(html, self.sel)
        flat = reality_sumperk.to_listing(items[0], "byt", "3+1", self.sel["base_url"])
        self.assertEqual((flat.town, flat.subtype, flat.area_m2, flat.price, flat.external_id),
                         ("Šumperk", "3+1", 72.0, 2_190_000, "11"))
        house = reality_sumperk.to_listing(items[1], "dum", None, self.sel["base_url"])
        self.assertEqual((house.town, house.area_m2, house.land_m2, house.price),
                         ("Dolní Studénky", 160.0, 700.0, None))


class RobotsTests(unittest.TestCase):
    def test_wildcards(self):
        rules = RobotsChecker._parse("User-agent: *\nDisallow: /*hledat=\nDisallow: /ad-mail.php\n")
        checker = RobotsChecker()
        checker._rules["reality.bazos.cz"] = rules
        checker._loaded.add("reality.bazos.cz")
        self.assertTrue(checker.can_fetch("https://reality.bazos.cz/prodam/dum/40/"))
        self.assertFalse(checker.can_fetch("https://reality.bazos.cz/prodam/byt/?hledat=byt"))
        self.assertFalse(checker.can_fetch("https://reality.bazos.cz/ad-mail.php"))


if __name__ == "__main__":
    unittest.main()


class SrealityFetchTests(unittest.TestCase):
    """fetch_listings nad podvrženým API: stránkování, detail jen pro nové, oblast první."""

    def test_fetch_flow(self):
        from unittest import mock
        from tests.helpers import config

        def estate(i, city, main=1):
            return {"hash_id": i, "price_czk": 2_000_000, "price_czk_m2": 40_000,
                    "category_main_cb": {"value": main}, "category_sub_cb": {"name": "2+1" if main == 1 else "Rodinný"},
                    "locality": {"city": city}}

        flats = [estate(i, "Šumperk" if i % 2 else "Bludov") for i in range(150)]
        houses = [estate(1000, "Sudkov", main=2)]
        details = []

        def fake_get(self, url, params=None):
            if url == sreality.SEARCH_URL:
                pool = flats if params["category_main_cb"] == 1 else houses
                page = pool[params["offset"]:params["offset"] + params["limit"]]
                return {"results": page, "pagination": {"total": len(pool)}}
            details.append(url)
            return {"result": {"advert_description": "k rekonstrukci", "usable_area": 55}}

        with mock.patch.object(sreality._Client, "get", fake_get):
            res = sreality.fetch_listings(config(), known_ids={"1", "3"})
        self.assertEqual(res.raw_count, 151)
        self.assertEqual(len(res.listings), 151)
        self.assertEqual(len(details), 149)                         # 2 známé bez detailu
        self.assertEqual(sum(x.in_area for x in res.listings), 76)
        sudkov = next(x for x in res.listings if x.external_id == "1000")
        self.assertEqual((sudkov.kind, sudkov.town, sudkov.area_m2), ("dum", "Sudkov", 55.0))

    def test_detail_cap_defers_rest(self):
        from unittest import mock
        from tests.helpers import config

        flats = [{"hash_id": i, "price_czk": 1, "category_main_cb": {"value": 1},
                  "category_sub_cb": {"name": "2+1"}, "locality": {"city": "Šumperk" if i < 5 else "Bludov"}}
                 for i in range(20)]

        def fake_get(self, url, params=None):
            if url == sreality.SEARCH_URL:
                return {"results": flats if params["offset"] == 0 else [], "pagination": {"total": 20}}
            return {"result": {}}

        with mock.patch.object(sreality, "DETAIL_LIMIT_PER_RUN", 8), \
                mock.patch.object(sreality._Client, "get", fake_get):
            res = sreality.fetch_listings(config(include_houses=False), known_ids=set())
        self.assertEqual(len(res.listings), 8)                     # zbytek až příště (s detailem)
        self.assertEqual(sum(x.in_area for x in res.listings), 5)  # sledované obce dostaly detail první


class QuickCheckTests(unittest.TestCase):
    def test_no_details_when_disabled(self):
        from unittest import mock
        from tests.helpers import config

        calls = []

        def fake_get(self, url, params=None):
            calls.append(url)
            if url == sreality.SEARCH_URL:
                return {"results": [{"hash_id": 1, "price_czk": 1, "category_main_cb": {"value": 1},
                                     "category_sub_cb": {"name": "2+1"}, "locality": {"city": "Šumperk"}}],
                        "pagination": {"total": 1}}
            return {"result": {}}

        with mock.patch.object(sreality._Client, "get", fake_get):
            res = sreality.fetch_listings(config(fetch_details=False, include_houses=False), known_ids=set())
        self.assertEqual(len(res.listings), 1)
        self.assertEqual(calls, [sreality.SEARCH_URL])
