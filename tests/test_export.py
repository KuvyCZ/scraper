import csv
import io
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from flip import export
from flip.bot import Bot
from flip.store import Store

from tests.helpers import config, make
from tests.test_pipeline import CapturingNotifier


class FileNotifier(CapturingNotifier):
    def __init__(self):
        super().__init__()
        self.files: list[tuple[str, bytes, str]] = []

    def send_document(self, filename, content, caption=""):
        self.files.append((filename, content, caption))
        return True


def chat_message(text, chat_id="42"):
    return {"message": {"chat": {"id": int(chat_id)}, "text": text}}


class TelegramExportTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = config(db_path=os.path.join(self.dir, "flip.db"), telegram_chat_id="42")
        self.store = Store(self.cfg.db_path)
        self.store.upsert(make(eid="1", town="Šumperk", price=2_000_000, area=61.5))
        self.store.upsert(make(eid="2", town=None, price=1_000_000))          # mimo sledované obce
        self.notifier = FileNotifier()
        self.bot = Bot(self.cfg, self.notifier)
        export.register(self.bot)

    def tearDown(self):
        self.store.close()

    def test_data_sends_excel_csv_and_db_copy(self):
        self.bot.dispatch(chat_message("/data"), self.store)
        names = [f[0] for f in self.notifier.files]
        self.assertTrue(names[0].startswith("listings_") and names[0].endswith(".csv"))
        self.assertTrue(names[-1].endswith(".db"))

        content = self.notifier.files[0][1]
        self.assertTrue(content.startswith(b"\xef\xbb\xbf"))                    # BOM pro Excel
        rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig")), delimiter=";"))
        self.assertEqual(rows[0][:5], ["town", "kind", "subtype", "price", "area_m2"])
        self.assertEqual(len(rows), 2)                                          # jen Šumperk
        self.assertEqual(rows[1][0], "Šumperk")
        self.assertEqual(rows[1][4], "61,5")                                    # desetinná čárka

        db_copy = os.path.join(self.dir, "copy.db")
        with open(db_copy, "wb") as fh:
            fh.write(self.notifier.files[-1][1])
        conn = sqlite3.connect(db_copy)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0], 2)
        conn.close()

    def test_data_includes_town_prices_when_present(self):
        with open(os.path.join(self.dir, "town_prices.csv"), "w", encoding="utf-8") as fh:
            fh.write("week;town\n")
        self.bot.dispatch(chat_message("/data"), self.store)
        self.assertIn("town_prices.csv", [f[0] for f in self.notifier.files])

    def test_other_chats_get_nothing(self):
        self.bot.dispatch(chat_message("/data", chat_id="999"), self.store)
        self.assertEqual(self.notifier.files, [])
        self.assertEqual(self.notifier.sent, [])

    def test_status_and_help(self):
        self.store.record_success("sreality")
        self.bot.dispatch(chat_message("/status"), self.store)
        self.assertIn("1</b> listings in your towns (2 stored", self.notifier.sent[-1])
        self.bot.dispatch(chat_message("/help@FlipBot"), self.store)
        self.assertIn("/data", self.notifier.sent[-1])

    def test_announces_each_new_version_once(self):
        with mock.patch.dict(os.environ, {"APP_VERSION": "v1", "APP_TITLE": "first"}):
            export.announce_update(self.cfg, self.notifier)                     # první start
        self.assertEqual(len(self.notifier.sent), 1)
        with mock.patch.dict(os.environ, {"APP_VERSION": "v1"}):
            export.announce_update(self.cfg, self.notifier)                     # restart
        self.assertEqual(len(self.notifier.sent), 1)
        with mock.patch.dict(os.environ, {"APP_VERSION": "v2", "APP_TITLE": "Fix <Bazoš>"}):
            export.announce_update(self.cfg, self.notifier)
        self.assertEqual(len(self.notifier.sent), 2)
        self.assertIn("v2", self.notifier.sent[1])
        self.assertIn("Fix &lt;Bazoš&gt;", self.notifier.sent[1])


class BackgroundBotTests(unittest.TestCase):
    def test_get_updates_reaches_telegram_without_crashing(self):
        # getUpdates posílá Telegramu parametr `timeout` a zároveň potřebuje vlastní HTTP timeout;
        # dřív se to v Notifier.call() srazilo (TypeError) a celý scraper spadl po prvním průchodu.
        cfg = config(db_path=os.path.join(tempfile.mkdtemp(), "flip.db"), telegram_chat_id="42", dry_run=False)
        notifier = FileNotifier()
        notifier.dry_run = False
        seen = {}

        def fake_post(url, data=None, timeout=None, **kw):
            seen.update(url=url, data=data, timeout=timeout)
            return mock.Mock(status_code=200, json=lambda: {"ok": True, "result": []},
                             raise_for_status=lambda: None)

        with mock.patch("flip.notifier.httpx.post", fake_post):
            Bot(cfg, notifier).poll(50)
        self.assertIn("getUpdates", seen["url"])
        self.assertEqual(seen["data"]["timeout"], 50)
        self.assertEqual(seen["timeout"], 60)

    def test_answers_from_its_own_thread(self):
        import threading
        cfg = config(db_path=os.path.join(tempfile.mkdtemp(), "flip.db"), telegram_chat_id="42", dry_run=False)
        notifier = FileNotifier()
        notifier.dry_run = False
        answered = threading.Event()
        updates = [{"ok": True, "result": [{"update_id": 1, **chat_message("/help")}]}]

        def fake_call(method, http_timeout=None, **params):
            if method == "getUpdates":
                if updates:
                    return updates.pop()
                answered.wait(5)
                return {"ok": True, "result": []}
            return {"ok": True}

        def send(text, **kw):
            notifier.sent.append(text)
            answered.set()
            return True

        notifier.call = fake_call
        notifier.send = send
        bot = Bot(cfg, notifier)
        export.register(bot)
        self.assertIsNotNone(bot.start_background())
        self.assertTrue(answered.wait(5))
        self.assertIn("/data", notifier.sent[0])


if __name__ == "__main__":
    unittest.main()
