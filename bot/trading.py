"""Shared market validation and fee calculations used by live prediction."""

MINIMUM_EDGE = 0.0
MINIMUM_SHARES = 5


def healthy_book(quote):
    if not quote or not quote.get("ready"):
        return False
    connection_age = quote.get("connection_age_ms")
    if connection_age is not None:
        return -1000 <= connection_age <= 25000
    age = quote.get("source_age_ms")
    return age is not None and -1000 <= age <= 2000


def valid_book(quote):
    if not healthy_book(quote):
        return False
    bid, ask = quote.get("bid"), quote.get("ask")
    return bid is not None and ask is not None and 0 < float(bid) <= float(ask) < 1


def market_fee_rate(market):
    details = market["event"]["markets"][0]
    if details.get("feesEnabled") is False:
        return 0.0
    fee = details.get("feeSchedule") or {}
    if (details.get("feesEnabled") is not True or fee.get("exponent") != 1
            or fee.get("takerOnly") is not True):
        raise ValueError("unsupported_fee_schedule")
    rate = float(fee["rate"])
    if not 0 <= rate <= 1:
        raise ValueError("unsupported_fee_schedule")
    return rate


def points_before(history, source, now_ms):
    points = {}
    for point in history:
        if (point["source"] == source and not point["carried_forward"]
                and point["received_ms"] <= now_ms
                and now_ms - 125000 <= point["source_ms"] <= now_ms):
            points[point["source_ms"]] = float(point["value"])
    return sorted(points.items())


def prior(points, target):
    found = next(((stamp, value) for stamp, value in reversed(points) if stamp <= target), None)
    if not found or target - found[0] > 5000:
        raise ValueError("insufficient_price_history")
    return found[1]
