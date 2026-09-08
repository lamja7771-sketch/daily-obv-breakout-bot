def get_daily_candles(contract):
    url = f"{GATE_URL}/futures/usdt/candlesticks"

    response = session.get(
        url,
        params={
            "contract": contract,
            "interval": TIMEFRAME,
            "limit": CANDLE_LIMIT
        },
        timeout=20
    )

    response.raise_for_status()

    candles = response.json()

    if not isinstance(candles, list):
        return []

    parsed = []

    for candle in candles:
        try:
            # Current Gate Futures API format:
            # {
            #     "t": timestamp,
            #     "v": volume,
            #     "c": close,
            #     "h": high,
            #     "l": low,
            #     "o": open,
            #     "sum": volume in quote currency
            # }

            timestamp = int(candle["t"])
            volume = float(candle["v"])
            close = float(candle["c"])

            parsed.append({
                "timestamp": timestamp,
                "volume": volume,
                "close": close
            })

        except (KeyError, TypeError, ValueError):
            continue

    parsed.sort(key=lambda x: x["timestamp"])

    return parsed
