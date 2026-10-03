import os
import json
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
# DAILY BREAKOUT SETUP BOT
# ============================================================

BASE_URL = "https://api.gateio.ws/api/v4"

TIMEFRAME = "1d"

# Price must remain within this percentage beyond
# the previous daily high/low.
PROXIMITY_PERCENT = 0.02

# Number of workers for candle requests
MAX_WORKERS = 6

# We only need previous + current daily candle
CANDLE_LIMIT = 3

# Retry settings
MAX_RETRIES = 4
RETRY_DELAY = 1.5

# History file
HISTORY_FILE = "signals.json"

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_CHAT_ID_2 = os.getenv("TELEGRAM_CHAT_ID_2")

HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Daily-Breakout-Setup-Bot/1.0"
}


# ============================================================
# PRINT BANNER
# ============================================================

print("=" * 60)
print("DAILY BREAKOUT SETUP BOT")
print("=" * 60)
print("Timeframe: DAILY")
print("LONG: Current Daily candle breaks Previous Daily HIGH")
print("SHORT: Current Daily candle breaks Previous Daily LOW")
print("Break type: WICK BREAK IS ENOUGH")
print("Current price must remain beyond broken level")
print(f"Proximity: {PROXIMITY_PERCENT * 100:.0f}%")
print("No OBV • No indicators • LTF retest checked manually")
print("=" * 60)


# ============================================================
# SESSION
# ============================================================

session = requests.Session()
session.headers.update(HEADERS)


# ============================================================
# GENERIC GET WITH RETRIES
# ============================================================

def api_get(endpoint, params=None):
    for attempt in range(1, MAX_RETRIES + 1):

        try:
            response = session.get(
                BASE_URL + endpoint,
                params=params,
                timeout=15
            )

            # Rate limit
            if response.status_code == 429:
                wait_time = RETRY_DELAY * attempt
                time.sleep(wait_time)
                continue

            response.raise_for_status()

            return response.json()

        except Exception as e:

            if attempt == MAX_RETRIES:
                raise

            time.sleep(RETRY_DELAY * attempt)

    return None


# ============================================================
# LOAD HISTORY
# ============================================================

def load_history():

    if not os.path.exists(HISTORY_FILE):
        return {}

    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict):
            return data

        return {}

    except Exception:
        return {}


# ============================================================
# SAVE HISTORY
# ============================================================

def save_history(history):

    temp_file = HISTORY_FILE + ".tmp"

    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(
            history,
            f,
            indent=2,
            ensure_ascii=False
        )

    os.replace(temp_file, HISTORY_FILE)


# ============================================================
# GET USDT FUTURES CONTRACTS
# ============================================================

def get_contracts():

    data = api_get("/futures/usdt/contracts")

    contracts = []

    for item in data:

        if not isinstance(item, dict):
            continue

        name = item.get("name")

        if not name:
            continue

        # Only USDT perpetual contracts
        if not name.endswith("_USDT"):
            continue

        # Skip inactive contracts
        in_delisting = item.get("in_delisting", False)

        if in_delisting:
            continue

        contracts.append(name)

    return contracts


# ============================================================
# GET ALL LIVE FUTURES TICKERS
# ============================================================

def get_all_live_prices():

    prices = {}

    try:

        data = api_get("/futures/usdt/tickers")

        if not isinstance(data, list):
            return prices

        for ticker in data:

            if not isinstance(ticker, dict):
                continue

            contract = ticker.get("contract")

            if not contract:
                continue

            last_price = ticker.get("last")

            if last_price is None:
                continue

            try:
                prices[contract] = float(last_price)
            except (TypeError, ValueError):
                continue

    except Exception as e:

        print(f"Ticker error: {e}")

    return prices


# ============================================================
# GET DAILY CANDLES
# ============================================================

def get_daily_candles(contract):

    params = {
        "contract": contract,
        "interval": "1d",
        "limit": CANDLE_LIMIT
    }

    data = api_get(
        "/futures/usdt/candlesticks",
        params=params
    )

    if not isinstance(data, list):
        raise ValueError("Invalid candle response")

    if len(data) < 2:
        raise ValueError("Not enough candles")

    normalized = []

    for candle in data:

        # ----------------------------------------------------
        # CURRENT GATE FUTURES FORMAT
        # {
        #   "t": 1539852480,
        #   "v": "...",
        #   "c": "...",
        #   "h": "...",
        #   "l": "...",
        #   "o": "...",
        #   "sum": "..."
        # }
        # ----------------------------------------------------

        if isinstance(candle, dict):

            timestamp = candle.get("t")
            high = candle.get("h")
            low = candle.get("l")
            close = candle.get("c")

            if timestamp is None:
                raise ValueError("Candle timestamp missing")

            if high is None or low is None:
                raise ValueError("Candle high/low missing")

            normalized.append({
                "timestamp": int(float(timestamp)),
                "high": float(high),
                "low": float(low),
                "close": float(close) if close is not None else None
            })

        # ----------------------------------------------------
        # BACKWARD COMPATIBILITY
        # ----------------------------------------------------

        elif isinstance(candle, (list, tuple)):

            if len(candle) < 5:
                continue

            normalized.append({
                "timestamp": int(float(candle[0])),
                "close": float(candle[2]),
                "high": float(candle[3]),
                "low": float(candle[4])
            })

        else:
            continue

    if len(normalized) < 2:
        raise ValueError("Could not normalize candles")

    normalized.sort(
        key=lambda x: x["timestamp"]
    )

    return normalized


# ============================================================
# CHECK ONE CONTRACT
# ============================================================

def check_signal(contract, live_prices):

    try:

        candles = get_daily_candles(contract)

        # Latest candle = current/forming daily candle
        current = candles[-1]

        # Previous candle = previous daily candle
        previous = candles[-2]

        previous_high = previous["high"]
        previous_low = previous["low"]

        current_high = current["high"]
        current_low = current["low"]

        live_price = live_prices.get(contract)

        if live_price is None:
            return None

        # ====================================================
        # LONG
        #
        # Current daily candle must have broken previous HIGH
        # AND live price must still be ABOVE previous HIGH.
        #
        # Maximum distance = 2%
        # ====================================================

        if (
            current_high > previous_high
            and
            previous_high < live_price <=
            previous_high * (1 + PROXIMITY_PERCENT)
        ):

            distance_percent = (
                (live_price - previous_high)
                / previous_high
            ) * 100

            return {
                "contract": contract,
                "direction": "LONG",
                "price": live_price,
                "level": previous_high,
                "distance_percent": distance_percent,
                "candle_timestamp": current["timestamp"]
            }

        # ====================================================
        # SHORT
        #
        # Current daily candle must have broken previous LOW
        # AND live price must still be BELOW previous LOW.
        #
        # Maximum distance = 2%
        # ====================================================

        if (
            current_low < previous_low
            and
            previous_low * (1 - PROXIMITY_PERCENT)
            <= live_price < previous_low
        ):

            distance_percent = (
                (previous_low - live_price)
                / previous_low
            ) * 100

            return {
                "contract": contract,
                "direction": "SHORT",
                "price": live_price,
                "level": previous_low,
                "distance_percent": distance_percent,
                "candle_timestamp": current["timestamp"]
            }

        return None

    except Exception as e:

        return {
            "error": str(e),
            "contract": contract
        }


# ============================================================
# SIGNAL KEY
# ============================================================

def signal_key(signal):

    return (
        f"{signal['contract']}_"
        f"{signal['direction']}_"
        f"{signal['candle_timestamp']}"
    )


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message, chat_id):

    if not TELEGRAM_BOT_TOKEN:
        print("Telegram bot token missing.")
        return False

    if not chat_id:
        return False

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": chat_id,
        "text": message
    }

    try:

        response = requests.post(
            url,
            json=payload,
            timeout=15
        )

        if response.status_code == 200:
            return True

        print(
            f"Telegram error {response.status_code}: "
            f"{response.text[:300]}"
        )

        return False

    except Exception as e:

        print(f"Telegram exception: {e}")

        return False


# ============================================================
# SEND TELEGRAM IN CHUNKS
# ============================================================

def send_telegram_chunks(message, chat_id):

    if not chat_id:
        return False

    max_length = 3500

    parts = []

    while len(message) > max_length:

        split_at = message.rfind(
            "\n",
            0,
            max_length
        )

        if split_at <= 0:
            split_at = max_length

        parts.append(
            message[:split_at]
        )

        message = message[split_at:].lstrip()

    if message:
        parts.append(message)

    success = True

    for part in parts:

        if not send_telegram(part, chat_id):
            success = False

        time.sleep(0.5)

    return success


# ============================================================
# MAIN
# ============================================================

def main():

    start_time = time.time()

    history = load_history()

    print(
        f"Previously recorded signals: "
        f"{len(history)}"
    )

    # --------------------------------------------------------
    # GET CONTRACTS
    # --------------------------------------------------------

    try:

        contracts = get_contracts()

    except Exception as e:

        print(f"Contract error: {e}")
        return

    print(
        f"USDT contracts found: "
        f"{len(contracts)}"
    )

    if not contracts:
        print("No contracts found.")
        return

    # --------------------------------------------------------
    # GET ALL LIVE PRICES ONCE
    # --------------------------------------------------------

    print("Fetching live futures prices...")

    live_prices = get_all_live_prices()

    print(
        f"Live prices received: "
        f"{len(live_prices)}"
    )

    if not live_prices:

        print("No live prices received.")
        return

    # --------------------------------------------------------
    # SCAN
    # --------------------------------------------------------

    print("Scanning...")

    setups = []
    errors = []

    completed = 0

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        futures = {
            executor.submit(
                check_signal,
                contract,
                live_prices
            ): contract
            for contract in contracts
        }

        for future in as_completed(futures):

            contract = futures[future]

            try:

                result = future.result()

                if result is None:
                    pass

                elif "error" in result:

                    errors.append(result)

                else:

                    setups.append(result)

            except Exception as e:

                errors.append({
                    "contract": contract,
                    "error": str(e)
                })

            completed += 1

            if (
                completed % 100 == 0
                or completed == len(contracts)
            ):

                print(
                    f"Progress: "
                    f"{completed}/{len(contracts)}"
                )

    # --------------------------------------------------------
    # ERROR SUMMARY
    # --------------------------------------------------------

    if errors:

        print(
            f"Candle/API errors: "
            f"{len(errors)}"
        )

        # Print only first 10 errors
        # so GitHub Actions log stays clean.

        for error in errors[:10]:

            print(
                f"Error {error['contract']}: "
                f"{error['error']}"
            )

        if len(errors) > 10:

            print(
                f"... and "
                f"{len(errors) - 10} more errors"
            )

    # --------------------------------------------------------
    # SORT SETUPS
    # Largest distance first
    # --------------------------------------------------------

    setups.sort(
        key=lambda x: x["distance_percent"],
        reverse=True
    )

    long_count = sum(
        1
        for x in setups
        if x["direction"] == "LONG"
    )

    short_count = sum(
        1
        for x in setups
        if x["direction"] == "SHORT"
    )

    print()
    print(
        f"Daily wick flip setups found: "
        f"{len(setups)}"
    )

    print(
        f"LONG: {long_count}"
    )

    print(
        f"SHORT: {short_count}"
    )

    # --------------------------------------------------------
    # FIND NEW SIGNALS
    # --------------------------------------------------------

    new_signals = []

    for signal in setups:

        key = signal_key(signal)

        if key not in history:

            history[key] = {
                "contract": signal["contract"],
                "direction": signal["direction"],
                "candle_timestamp": signal[
                    "candle_timestamp"
                ],
                "telegram_sent": False
            }

            new_signals.append(signal)

    print(
        f"New signals: "
        f"{len(new_signals)}"
    )

    # --------------------------------------------------------
    # TELEGRAM MESSAGE
    # --------------------------------------------------------

    unsent_signals = []

    for signal in new_signals:

        key = signal_key(signal)

        if not history[key].get(
            "telegram_sent",
            False
        ):

            unsent_signals.append(signal)

    if unsent_signals:

        blocks = []

        for signal in unsent_signals:

            symbol = signal["contract"].replace(
                "_USDT",
                ""
            )

            distance = signal[
                "distance_percent"
            ]

            if signal["direction"] == "LONG":

                block = (
                    f"🟢 {symbol} "
                    f"+{distance:.2f}%"
                )

            else:

                block = (
                    f"🔴 {symbol} "
                    f"-{distance:.2f}%"
                )

            blocks.append(block)

        message = (
            "🚨 DAILY WICK FLIP SETUPS\n"
            "Current price within 2% "
            "of previous daily wick.\n\n"
            +
            "\n".join(blocks)
            +
            "\n\n"
            "LTF RETEST: 15M / 1H / 4H"
        )

        print()
        print("Sending Telegram alerts...")

        # ----------------------------------------------------
        # CHAT 1
        # ----------------------------------------------------

        sent_1 = send_telegram_chunks(
            message,
            TELEGRAM_CHAT_ID
        )

        # ----------------------------------------------------
        # CHAT 2
        # ----------------------------------------------------

        sent_2 = True

        if TELEGRAM_CHAT_ID_2:

            sent_2 = send_telegram_chunks(
                message,
                TELEGRAM_CHAT_ID_2
            )

        # ----------------------------------------------------
        # Mark sent only if at least the configured
        # Telegram destination succeeded.
        # ----------------------------------------------------

        if sent_1 or sent_2:

            for signal in unsent_signals:

                key = signal_key(signal)

                history[key][
                    "telegram_sent"
                ] = True

            print(
                f"Telegram messages sent: "
                f"{len(unsent_signals)} signals"
            )

        else:

            print(
                "Telegram sending failed."
            )

    else:

        print(
            "No new signals to send."
        )

    # --------------------------------------------------------
    # SAVE HISTORY
    # --------------------------------------------------------

    save_history(history)

    # --------------------------------------------------------
    # SUMMARY
    # --------------------------------------------------------

    runtime = time.time() - start_time

    print()
    print("=" * 60)
    print("SCAN COMPLETE")
    print(
        f"Runtime: {runtime:.1f} seconds"
    )
    print(
        f"History records: {len(history)}"
    )
    print("=" * 60)


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()
