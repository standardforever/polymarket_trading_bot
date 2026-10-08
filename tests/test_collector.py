"""Offline checks for the failure-prone parts of the live collector."""

import unittest
import json
import threading
import tempfile
from pathlib import Path
from unittest.mock import patch

from bot.collector import Book, Recorder, apply_event, round_start, run, stream_round, validate_market


class CollectorTests(unittest.TestCase):
    def test_human_readable_status_is_replaced_atomically(self):
        with tempfile.TemporaryDirectory() as folder:
            recorder = Recorder(Path(folder))
            recorder.write_status("live-status.json", status="WAITING", remaining_seconds=150)
            recorder.write_status("live-status.json", status="SKIP", reason="test")
            status = json.loads((Path(folder) / "live-status.json").read_text())
            recorder.file.close()
        self.assertEqual(status["status"], "SKIP")
        self.assertEqual(status["reason"], "test")
        self.assertNotIn("remaining_seconds", status)

    def test_compact_round_history_deduplicates_decision_and_resolution(self):
        with tempfile.TemporaryDirectory() as folder:
            recorder = Recorder(Path(folder))
            self.assertTrue(recorder.append_round_history("decision", "round-1", direction="Up"))
            self.assertFalse(recorder.append_round_history("decision", "round-1", direction="Down"))
            self.assertTrue(recorder.append_round_history("resolution", "round-1", actual_result="Up"))
            recorder.file.close()
            rows = [json.loads(line) for line in
                    (Path(folder) / "round-history.jsonl").read_text().splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["direction"], "Up")
        self.assertEqual(rows[1]["actual_result"], "Up")

    def test_raw_round_is_deleted_after_compact_resolution(self):
        with tempfile.TemporaryDirectory() as folder:
            recorder = Recorder(Path(folder))
            recorder.round_history = True
            slug = "btc-updown-5m-1790193600"
            recorder.write("market_event", slug=slug, event={"event_type": "book"})
            self.assertTrue(list((Path(folder) / "active-rounds").glob("*.jsonl")))
            recorder.append_round_history("decision", slug, start=1790193600,
                                          direction="Up", components={})
            recorder.append_round_history("entry", slug, delay_seconds=5,
                                          decision_ms=1, outcomes={"Up": {"price": .8}})
            recorder.append_round_history("resolution", slug, start=1790193600,
                                          actual_result="Up")
            recorder.close()
            rows = [json.loads(line) for line in
                    (Path(folder) / "round-history.jsonl").read_text().splitlines()]
            session = next(Path(folder).glob("session-*.jsonl")).read_text()
        self.assertEqual([row["record_type"] for row in rows],
                         ["decision", "entry", "resolution"])
        self.assertFalse(list((Path(folder) / "active-rounds").glob("*.jsonl")))
        self.assertNotIn("market_event", session)

    def test_round_boundary(self):
        self.assertEqual(round_start(1790193899.9), 1790193600)
        self.assertEqual(round_start(1790193900), 1790193900)

    def test_outcomes_are_mapped_by_label(self):
        event = {"markets": [{"slug": "btc-updown-5m-1790193600",
                  "outcomes": '["Down", "Up"]', 'clobTokenIds': '["d", "u"]',
                  "eventStartTime": "2026-09-23T20:00:00Z",
                  "endDate": "2026-09-23T20:05:00Z", "acceptingOrders": True}]}
        self.assertEqual(validate_market(event, 1790193600)["tokens"], {"d": "Down", "u": "Up"})
        event["markets"][0]["closed"] = True
        with self.assertRaises(ValueError):
            validate_market(event, 1790193600)

    def test_unsorted_snapshot_and_deleted_best_ask(self):
        books = {"u": Book()}
        apply_event(books, {"event_type": "book", "asset_id": "u", "timestamp": "1000",
                    "bids": [{"price": "0.70", "size": "4"}, {"price": "0.79", "size": "3"}],
                    "asks": [{"price": "0.95", "size": "9"}, {"price": "0.80", "size": "5"}]})
        self.assertEqual(books["u"].quote(1100)["ask"], "0.80")
        apply_event(books, {"event_type": "price_change", "timestamp": "1200", "price_changes": [
                    {"asset_id": "u", "side": "SELL", "price": "0.80", "size": "0", "best_bid": "0.79", "best_ask": "0.95"},
                    {"asset_id": "u", "side": "BUY", "price": "0.79", "size": "2", "best_bid": "0.79", "best_ask": "0.95"}]})
        quote = books["u"].quote(1500)
        self.assertEqual(quote["ask"], "0.95")
        self.assertEqual(quote["bid_size"], "2")  # Updates replace size, not add to it.
        self.assertIsNone(quote["ask_size"])  # Newly revealed level has unknown current size.
        self.assertEqual(quote["source_age_ms"], 300)

    def test_empty_book_is_not_zero_price(self):
        book = Book()
        book.snapshot({"bids": [], "asks": [], "timestamp": "1000"})
        self.assertIsNone(book.quote(1200)["ask"])
        self.assertTrue(book.ready)

    def test_updates_need_snapshot_and_ignore_old_timestamps(self):
        book = Book()
        change = {"side": "BUY", "price": "0.8", "size": "5"}
        book.change(change, "1000")
        self.assertFalse(book.ready)
        self.assertIsNone(book.bid)
        book.snapshot({"bids": [], "asks": [], "timestamp": "2000"})
        self.assertFalse(book.change(change, "1999"))
        self.assertEqual(book.source_ms, 2000)

    def test_server_best_price_wins_over_old_snapshot(self):
        book = Book()
        book.snapshot({"bids": [], "asks": [{"price": "0.48", "size": "2"}], "timestamp": "1000"})
        book.change({"side": "SELL", "price": "0.49", "size": "235",
                     "best_bid": "0.47", "best_ask": "0.49"}, "1200")
        self.assertEqual(book.quote(1300)["ask"], "0.49")
        self.assertEqual(book.quote(1300)["ask_size"], "235")
        book.change({"side": "SELL", "price": "0.49", "size": "0",
                     "best_bid": "0", "best_ask": "1"}, "1400")
        self.assertIsNone(book.quote(1500)["ask"])
        self.assertIsNone(book.quote(1500)["bid"])

    def test_closed_socket_is_closed_and_raises_for_retry(self):
        from unittest.mock import Mock
        connection, recorder = Mock(), Mock()
        connection.recv.return_value = ""
        market = {"slug": "test", "start": 9700, "end": 10000, "tokens": {"u": "Up", "d": "Down"}}
        price_state = Mock()
        price_state.snapshot.return_value = {
            "prices": {}, "reference": {"price_to_beat": None}, "reference_ready": False}
        price_state.history = []
        price_state.lock = threading.Lock()
        with patch("bot.collector.websocket.create_connection", return_value=connection), \
             patch("bot.collector.time.time", return_value=100), \
             patch("bot.collector.time.monotonic", side_effect=[100, 100, 99, 100, 100]):
            with self.assertRaises(ConnectionError):
                stream_round(market, recorder, 1000,
                             {"connections": 0, "reference_ready": 0,
                              "btc_observations": 0, "paired_quotes": 0},
                             price_state, Mock(), stop=Mock(is_set=Mock(return_value=False)))
        connection.close.assert_called_once()



if __name__ == "__main__":
    unittest.main()
