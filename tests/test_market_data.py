"""Price timing, provenance, and settlement checks without network access."""

import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from bot.market_data import PriceState, authenticate, credentials, fresh, market_reference, parse_prices, price_point, receive_price_message

NOW = 1790196000000


def envelope(channel="price.crypto.twap", snapshot=False):
    payload = {"symbol": "btcusd", "timestamp": NOW, "value": 84365.24,
               "full_accuracy_value": "84365.23895987154", "window_seconds": 60}
    if snapshot:
        payload = {"symbol": "btcusd", "window_seconds": 60, "data": [payload]}
    return {"channel": channel, "snapshot": snapshot, "seq": 1, "payload": payload}


class PriceTests(unittest.TestCase):
    def test_subscription_ack_is_not_a_price(self):
        self.assertEqual(parse_prices({'op': 'subscribed', 'channel': 'price.crypto'}, 'polybolt'), [])

    def test_exact_price_and_source_labels(self):
        point = parse_prices(envelope(), "polybolt")[0]
        self.assertEqual(point["value"], "84365.23895987154")
        self.assertEqual(point["source"], "chainlink_twap_60s")
        self.assertEqual(parse_prices(envelope("price.crypto"), "polybolt")[0]["source"], "pyth_btc_usd")

    def test_wrong_symbol_and_window_rejected(self):
        event = envelope()
        event["payload"]["window_seconds"] = 300
        with self.assertRaises(ValueError): parse_prices(event, "polybolt")
        event = envelope()
        event["payload"]["symbol"] = "ethusd"
        with self.assertRaises(ValueError): parse_prices(event, "polybolt")

    def test_freshness_rejects_history_old_future_and_carried_prices(self):
        point = dict(parse_prices(envelope(), "polybolt")[0], received_ms=NOW)
        self.assertTrue(fresh(point, NOW + 100))
        self.assertFalse(fresh(point, NOW + 6000))
        self.assertFalse(fresh(dict(point, source_ms=NOW + 2000), NOW))
        self.assertFalse(fresh(dict(point, carried_forward=True), NOW))
        history = dict(parse_prices(envelope(snapshot=True), "polybolt")[0], received_ms=NOW)
        self.assertFalse(fresh(history, NOW))

    def test_reconnect_clears_live_state_but_keeps_audit_history(self):
        state = PriceState()
        state.reset(True)
        state.add(parse_prices(envelope(), "polybolt")[0], NOW)
        state.reset()
        self.assertEqual(state.snapshot(NOW // 1000, NOW)["prices"], {})
        self.assertEqual(len(state.history), 1)

    def test_external_feed_cannot_mark_reference_ready(self):
        state = PriceState()
        state.reset(True)
        point = parse_prices({"e": "aggTrade", "s": "BTCUSDT", "p": "84370", "T": NOW}, "binance")[0]
        state.add(point, NOW)
        state.references[NOW // 1000] = {"supported": True, "price_to_beat": "84365"}
        context = state.snapshot(NOW // 1000, NOW)
        self.assertFalse(context["reference_ready"])
        self.assertIsNone(context["twap_minus_start"])

    def test_round_reference_does_not_leak_into_next_round(self):
        state = PriceState()
        state.reset(True)
        state.add(parse_prices(envelope(), "polybolt")[0], NOW)
        state.references[NOW // 1000] = {"supported": True, "price_to_beat": "84365.23895987154"}
        self.assertTrue(state.snapshot(NOW // 1000, NOW)["reference_ready"])
        self.assertFalse(state.snapshot(NOW // 1000 + 300, NOW)["reference_ready"])

    def test_exact_boundary_supplies_delayed_opening_metadata(self):
        state = PriceState()
        state.reset(True)
        state.references[NOW // 1000] = {'supported': True, 'price_to_beat': None}
        state.add(parse_prices(envelope(), 'polybolt')[0], NOW + 1000)
        context = state.snapshot(NOW // 1000, NOW + 1100)
        self.assertTrue(context['reference_ready'])
        self.assertEqual(context['reference']['opening_source'], 'chainlink_boundary')
        self.assertFalse(state.snapshot(NOW // 1000, NOW + 900)['reference_ready'])
        state.references[NOW // 1000]['price_to_beat'] = '84000'
        context = state.snapshot(NOW // 1000, NOW + 1100)
        self.assertTrue(context['reference']['opening_mismatch'])
        self.assertFalse(context['reference_ready'])

    def test_alignment_requires_exact_endpoints(self):
        state = PriceState()
        ref = {"supported": True, "price_to_beat": "100", "final_price": "101", "winner": "Up"}
        for stamp, price in ((NOW, "100"), (NOW + 299999, "101")):
            state.add(price_point("chainlink_twap_60s", {"timestamp": stamp, "value": price}), stamp)
        self.assertFalse(state.alignment(NOW // 1000, ref)["endpoint_match"])
        state.add(price_point("chainlink_twap_60s", {"timestamp": NOW + 300000, "value": "101"}), NOW + 300000)
        self.assertTrue(state.alignment(NOW // 1000, ref)["endpoint_match"])
        ref["winner"] = "Down"
        self.assertFalse(state.alignment(NOW // 1000, ref)["endpoint_match"])

    def test_invalid_price_and_seconds_timestamp_rejected(self):
        for value, stamp in (("NaN", NOW), ("0", NOW), ("100", NOW // 1000)):
            with self.assertRaises(ValueError): price_point("test", {"value": value, "timestamp": stamp})

    def test_old_price_does_not_replace_new_price(self):
        state = PriceState()
        state.add(price_point("test", {"value": "100", "timestamp": NOW}), NOW)
        with self.assertRaises(ValueError):
            state.add(price_point("test", {"value": "99", "timestamp": NOW - 1}), NOW)
        self.assertEqual(state.latest["test"]["value"], "100")

    def test_missing_credentials_fail_without_values(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ValueError): credentials(Path('/tmp/no-polymarket-test-credentials'))

    def test_local_aliases_ignore_wallet_key_and_environment_wins(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            path.write_text('PK=not-used\napi_key="local-key"\napi_secret=local-secret\napi_passphrase=local-pass\n')
            with patch.dict('os.environ', {'POLYMARKET_API_KEY': 'environment-key'}, clear=True):
                self.assertEqual(credentials(path), {'apiKey': 'environment-key', 'secret': 'local-secret', 'passphrase': 'local-pass'})

    def test_authenticate_waits_before_subscribe_and_does_not_echo_secrets(self):
        connection = Mock()
        auth = {"apiKey": "test-key", "secret": "test-secret", "passphrase": "test-pass"}
        connection.recv.return_value = '{"op":"authed"}'
        authenticate(connection, auth)
        self.assertEqual(json.loads(connection.send.call_args_list[1].args[0])["op"], "subscribe")
        connection.reset_mock()
        connection.recv.return_value = '{"op":"error","message":"test-secret"}'
        with self.assertRaises(PermissionError) as caught: authenticate(connection, auth)
        self.assertNotIn("test-secret", str(caught.exception))
        self.assertEqual(connection.send.call_count, 1)

    def test_metadata_config_and_resolved_outcome(self):
        event = {"eventMetadata": {"priceToBeat": "100", "finalPrice": "100"}, "markets": [{
            "cryptoMarketConfig": {"asset": "btc", "duration": "5m", "twapEnabled": True, "twapLookbackSeconds": 60},
            "resolutionSource": "https://data.chain.link/streams/btc-usd-twap-60s-streams",
            "umaResolutionStatus": "resolved", "outcomes": '["Down","Up"]', "outcomePrices": '["0","1"]'}]}
        reference = market_reference(event)
        self.assertEqual(reference["winner"], "Up")
        self.assertTrue(reference["supported"])
        event["markets"][0]["cryptoMarketConfig"]["twapLookbackSeconds"] = 300
        self.assertFalse(market_reference(event)["supported"])

    def test_policy_close_is_not_retried_as_network_failure(self):
        connection = Mock()
        connection.recv_data.return_value = (8, (4008).to_bytes(2, 'big'))
        with self.assertRaises(PermissionError): receive_price_message(connection)
        connection.recv_data.return_value = (8, (1000).to_bytes(2, 'big'))
        with self.assertRaises(ConnectionError): receive_price_message(connection)


if __name__ == "__main__":
    unittest.main()
