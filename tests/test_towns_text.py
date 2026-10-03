import unittest

from flip.text import (
    condition_from_text, house_subtype_from_text, is_motivated, parse_area, parse_land, parse_layout,
    parse_price, risks,
)
from flip.towns import match_town


class TownTests(unittest.TestCase):
    def test_exact_towns(self):
        self.assertEqual(match_town("Šumperk"), "Šumperk")
        self.assertEqual(match_town("Nový Malín"), "Nový Malín")
        self.assertEqual(match_town("Vikyrovice"), "Vikýřovice")   # bez diakritiky
        self.assertEqual(match_town("RAPOTÍN"), "Rapotín")

    def test_town_parts(self):
        self.assertEqual(match_town("Králec"), "Dolní Studénky")
        self.assertEqual(match_town("Mladoňov"), "Nový Malín")
        self.assertEqual(match_town("Šumperk - Horní Temenice"), "Šumperk")
        self.assertEqual(match_town("Temenická, Šumperk"), "Šumperk")

    def test_address_formats(self):
        self.assertEqual(match_town("Školní 12, Rapotín"), "Rapotín")
        self.assertEqual(match_town("Sudkov, 788 21"), "Sudkov")
        self.assertEqual(match_town("Šumperk 787 01"), "Šumperk")
        self.assertEqual(match_town(None, "Dolní Studénky"), "Dolní Studénky")

    def test_not_watched(self):
        self.assertIsNone(match_town("Bludov, okres Šumperk"))     # okres není obec
        self.assertIsNone(match_town("Horní Studénky"))           # podobné jméno
        self.assertIsNone(match_town("Zábřeh"))
        self.assertIsNone(match_town("Šumperská, Bludov"))         # ulice pojmenovaná po městě
        self.assertIsNone(match_town(""))
        self.assertIsNone(match_town(None))


class TextTests(unittest.TestCase):
    def test_condition(self):
        self.assertEqual(condition_from_text("Byt vhodný k rekonstrukci, umakartové jádro"), "needs_reno")
        self.assertEqual(condition_from_text("Dům v původním stavu"), "original")
        self.assertEqual(condition_from_text("Byt po kompletní rekonstrukci"), "renovated")
        self.assertIsNone(condition_from_text("Prodej bytu 2+1 v klidné části"))
        # koupelna k rekonstrukci přebije "po částečné rekonstrukci"
        self.assertEqual(condition_from_text("Po částečné rekonstrukci, koupelna k rekonstrukci"), "needs_reno")

    def test_risks(self):
        self.assertIn("share", risks("Prodej spoluvlastnického podílu 1/2 na domě"))
        self.assertIn("share", risks("Nabízíme spoluvlastnický podíl o velikosti 1/2"))
        self.assertIn("auction", risks("Dobrovolná dražba rodinného domu"))
        self.assertIn("coop", risks("Družstevní byt 2+1"))
        self.assertEqual(risks("Pěkný byt v osobním vlastnictví"), [])

    def test_motivated(self):
        self.assertTrue(is_motivated("Prodej z důvodu dědictví, rychlé jednání"))
        self.assertFalse(is_motivated("Krásný byt"))

    def test_house_subtype(self):
        self.assertEqual(house_subtype_from_text("Prodej chaty u lesa"), "Chata")
        self.assertEqual(house_subtype_from_text("Dům v chatrném stavu"), "Rodinný")   # "chatrný" není chata
        self.assertEqual(house_subtype_from_text("Prodej chata Sudkov"), "Chata")
        self.assertEqual(house_subtype_from_text("Chalupa na samotě"), "Chalupa")
        self.assertEqual(house_subtype_from_text("Zemědělská usedlost se stodolou"), "Zemědělská usedlost")
        self.assertEqual(house_subtype_from_text("Prodej RD 5+1"), "Rodinný")
        self.assertEqual(house_subtype_from_text("Privilegovaná lokalita"), "Rodinný")  # ne "vila"

    def test_parsers(self):
        self.assertEqual(parse_layout("Prodej bytu 3 + kk"), "3+kk")
        self.assertEqual(parse_area("Dům 140 m², pozemek 900 m²"), 140.0)
        self.assertEqual(parse_area("pozemek 900 m2, dům 120 m2"), 120.0)
        self.assertEqual(parse_land("Dům 140 m², pozemek o výměře 1 250 m²"), 1250.0)
        self.assertEqual(parse_price("1 290 000 Kč"), 1_290_000)
        self.assertIsNone(parse_price("Dohodou"))
        self.assertIsNone(parse_price("1 Kč"))


if __name__ == "__main__":
    unittest.main()
