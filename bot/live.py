"""Thread-safe live state shared by the collector and API process."""

import threading
from decimal import Decimal


class LiveStore:
    def __init__(self):
        self.lock = threading.Lock()
        self.revision = 0
        self.market = None
        self.reference_revision = 0
        self.reference = {"slug": None, "opening_price": None, "opening_source": None,
                          "prices": {}}

    def update_market(self, *, slug, received_ms, remaining_seconds, quotes,
                      tokens=None, market_end_ms=None, accepting_orders=True,
                      closed=False):
        with self.lock:
            self.revision += 1
            self.market = {
                "revision": self.revision,
                "slug": slug,
                "received_ms": received_ms,
                "remaining_seconds": remaining_seconds,
                "quotes": quotes,
                "tokens": dict(tokens or {}),
                "market_end_ms": market_end_ms,
                "accepting_orders": accepting_orders,
                "closed": closed,
            }

    def market_snapshot(self):
        with self.lock:
            return dict(self.market) if self.market is not None else None

    def update_price(self, point, received_ms):
        with self.lock:
            self.reference_revision += 1
            self.reference["prices"][point["source"]] = {
                "value": point["value"], "source_ms": point["source_ms"],
                "received_ms": received_ms,
            }

    def clear_prices(self):
        with self.lock:
            if self.reference["prices"]:
                self.reference_revision += 1
                self.reference["prices"] = {}

    def update_reference_context(self, *, slug, opening_price, opening_source):
        with self.lock:
            changed = (self.reference["slug"] != slug
                       or self.reference["opening_price"] != opening_price
                       or self.reference["opening_source"] != opening_source)
            if changed:
                self.reference_revision += 1
            self.reference.update(slug=slug, opening_price=opening_price,
                                  opening_source=opening_source)

    def reference_snapshot(self):
        with self.lock:
            opening = self.reference["opening_price"]
            current = self.reference["prices"].get("chainlink_twap_60s", {}).get("value")
            margin = (str(Decimal(current) - Decimal(opening))
                      if current is not None and opening is not None else None)
            return {
                "revision": self.reference_revision,
                "slug": self.reference["slug"],
                "opening_price": opening,
                "opening_source": self.reference["opening_source"],
                "current_price": current,
                "current_source": "chainlink_twap_60s" if current is not None else None,
                "margin": margin,
                "prices": {name: dict(value)
                           for name, value in self.reference["prices"].items()},
            }
