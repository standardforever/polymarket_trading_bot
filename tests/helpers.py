NOW = 1790198280000


def fixture():
    history = []
    for second in range(126):
        for source in ("pyth_btc_usd", "chainlink_twap_60s"):
            history.append({"source": source, "source_ms": NOW - (125-second)*1000,
                            "received_ms": NOW - (125-second)*1000,
                            "value": str(84000 + second/10), "snapshot": False,
                            "carried_forward": False})
    context = {"reference_ready": True,
               "reference": {"price_to_beat": "84000"},
               "prices": {source: {"fresh": True} for source in
                          ("pyth_btc_usd", "chainlink_twap_60s")}}
    quotes = {side: {"ready": True, "bid": bid, "ask": ask, "ask_size": "20",
                     "source_age_ms": 100, "connection_age_ms": 0}
              for side, bid, ask in (("Up", ".79", ".8"), ("Down", ".19", ".2"))}
    market = {"slug": "btc-updown-5m-1790198100", "start": 1790198100,
              "end": 1790198400, "tokens": {"up": "Up", "down": "Down"},
              "event": {"markets": [{"feesEnabled": True,
                         "feeSchedule": {"rate": .07, "exponent": 1, "takerOnly": True}}]}}
    return market, context, history, quotes


class MemoryRecorder:
    def __init__(self):
        self.records = []
        self.status = None

    def write(self, kind, **fields):
        self.records.append({"kind": kind, **fields})

    def write_status(self, filename, **fields):
        self.status = {"filename": filename, **fields}
