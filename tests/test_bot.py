import json
import unittest
from unittest import mock

from flip import bot
from flip.notifier import Notifier
from flip.store import Store

from tests.helpers import config, make
from tests.test_pipeline import CapturingNotifier


def _store(*listings):
    store = Store(":memory:")
    for lst in listings:
        store.upsert(lst)
    return store


class SimilarListingsTests(unittest.TestCase):
    def test_same_town_similar_size_closest_first(self):
        store = _store(
            make(eid="me", price=1_500_000, area=60),
            make(eid="a", price=3_000_000, area=62), make(eid="b", price=2_800_000, area=70),
            make(eid="c", price=2_500_000, area=55), make(eid="big", price=5_000_000, area=120),
            make(eid="hou", kind="dum", subtype="Rodinný", price=3_000_000, area=60),
            make(eid="zab", town="Zábřeh", price=2_000_000, area=60),
        )
        where, similar = bot.similar_listings(store, store.get("sreality", "me"), 90)
        self.assertEqual(where, "Šumperk")
        self.assertEqual([r["external_id"] for r in similar], ["a", "c", "b"])
        text = bot.format_similar(store.get("sreality", "me"), where, similar)
        self.assertIn("typical 45 455 Kč/m²", text)          # medián 48 387 / 45 455 / 40 000
        self.assertIn("-45 % vs typical", text)
        self.assertIn("2+1 60 m²", text)

    def test_widens_to_okres_when_town_has_few(self):
        store = _store(make(eid="me", area=60), make(eid="z1", town="Zábřeh", area=60),
                       make(eid="z2", town="Mohelnice", area=60), make(eid="o", town="Olomouc", area=60))
        where, similar = bot.similar_listings(store, store.get("sreality", "me"), 90)
        self.assertEqual(where, "okres Šumperk")
        self.assertEqual({r["external_id"] for r in similar}, {"z1", "z2"})     # Olomouc je jiný okres


class CallbackTests(unittest.TestCase):
    def setUp(self):
        self.store = _store(make(eid="me", area=60), *[make(eid=f"s{i}", area=60) for i in range(3)])
        self.notifier = CapturingNotifier()
        self.notifier.call = mock.Mock(return_value={"ok": True})
        self.cfg = config(telegram_chat_id="42")

    def query(self, chat_id=42, data="sim:sreality:me"):
        return {"id": "q1", "data": data, "message": {"chat": {"id": chat_id}}}

    def test_tap_replies_with_similar(self):
        bot.handle_callback(self.query(), self.store, self.cfg, self.notifier)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("🔎", self.notifier.sent[0])
        self.notifier.call.assert_called_with("answerCallbackQuery", callback_query_id="q1")

    def test_other_chat_is_ignored(self):
        bot.handle_callback(self.query(chat_id=666), self.store, self.cfg, self.notifier)
        self.assertEqual(self.notifier.sent, [])

    def test_unknown_listing(self):
        bot.handle_callback(self.query(data="sim:bazos:nope"), self.store, self.cfg, self.notifier)
        self.assertIn("no longer", self.notifier.sent[0])


class CommandRegistryTests(unittest.TestCase):
    def test_registered_command_only_from_our_chat(self):
        notifier = CapturingNotifier()
        notifier.call = mock.Mock(return_value={"ok": True})
        b = bot.Bot(config(telegram_chat_id="42"), notifier)
        got = []
        b.register_command("data", lambda msg, store, cfg, n: got.append(msg["text"]))
        store = Store(":memory:")
        b.dispatch({"message": {"text": "/data@flipbot", "chat": {"id": 42}}}, store)
        b.dispatch({"message": {"text": "/data", "chat": {"id": 7}}}, store)
        b.dispatch({"message": {"text": "hello", "chat": {"id": 42}}}, store)
        self.assertEqual(got, ["/data@flipbot"])


class ButtonTests(unittest.TestCase):
    def test_send_adds_button(self):
        n = Notifier("t", "42")
        with mock.patch("flip.notifier.httpx.post") as post, mock.patch("flip.notifier.time.sleep"):
            post.return_value.status_code = 200
            n.send("hi", similar_key=("bazos", "225612706"))
        markup = json.loads(post.call_args.kwargs["data"]["reply_markup"])
        self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"], "sim:bazos:225612706")

    def test_pipeline_sends_button_with_new_listing(self):
        from tests.test_pipeline import PipelineTests
        t = PipelineTests()
        t.setUp()
        t.store.mark_baseline_done("sreality")
        t.run_source("sreality", [make(eid="1")])
        self.assertEqual(t.notifier.keys, [("sreality", "1")])


if __name__ == "__main__":
    unittest.main()
