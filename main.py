import os
import json
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
# DAILY LONG BREAKOUT SETUP BOT
# ============================================================

BASE_URL = "https://api.gateio.ws/api/v4"

TIMEFRAME = "1d"

# Maximum distance above previous daily high
PROXIMITY_PERCENT = 0.02

MAX_WORKERS = 6
CANDLE_LIMIT = 3

MAX_RETRIES = 4
RETRY_DELAY = 1.5

HISTORY_FILE = "signals.json"

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_CHAT_ID_2 = os.getenv("TELEGRAM_CHAT_ID_2")

HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Daily-Long-Breakout-Bot/1.0"
}


# ============================================================
# BANNER
# ============================================================

print("=" * 60)
print("DAILY LONG BREAKOUT SETUP BOT")
print("=" * 60)
print("Timeframe: DAILY")
print("LONG: Current Daily candle breaks Previous Daily HIGH")
print("Break type: WICK BREAK IS ENOUGH")
print("Current price must remain ABOVE broken level")
print("Proximity: 2%")
print("No SHORT • No OBV • No indicators")
print("LTF retest checked manually")
print("=" * 60)


# ============================================================
# SESSION
# ============================================================

session = requests.Session()
session.headers.update(HEADERS)


# ============================================================
# API GET WITH RETRIES
# ============================================================

def api_get(endpoint, params=None):

    for attempt in range(1, MAX_RETRIES + 1):

        try:

            response = session.get(
                BASE_URL + endpoint,
                params=params,
                timeout=15
            )

            if response.status_code == 429:

                wait_time = RETRY_DELAY * attempt

                time.sleep(wait_time)

                continue

            response.raise_for_status()

            return response.json()

        except Exception:

            if attempt == MAX_RETRIES:
                raise

            time.sleep(
                RETRY_DELAY * attempt
            )

    return None


# ============================================================
# LOAD HISTORY
# ============================================================

def load_history():

    if not os.path.exists(HISTORY_FILE):
        return {}

    try:

        with open(
            HISTORY_FILE,
            "r",
            encoding="utf-8"
        ) as f:

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

    with open(
        temp_file,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            history,
            f,
            indent=2,
            ensure_ascii=False
        )

    os.replace(
        temp_file,
        HISTORY_FILE
    )


# ============================================================
# GET USDT FUTURES CONTRACTS
# ============================================================

def get_contracts():

    data = api_get(
        "/futures/usdt/contracts"
    )

    contracts = []

    for item in data:

        if not isinstance(item, dict):
            continue

        name = item.get("name")

        if not name:
            continue

        if not name.endswith("_USDT"):
            continue

        if item.get("in_delisting", False):
            continue

        contracts.append(name)

    return contracts


# ============================================================
# GET ALL LIVE FUTURES PRICES
# ============================================================

def get_all_live_prices():

    prices = {}

    try:

        data = api_get(
            "/futures/usdt/tickers"
        )

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

                prices[contract] = float(
                    last_price
                )

            except (
                TypeError,
                ValueError
            ):

                continue

    except Exception as e:

        print(
            f"Ticker error: {e}"
        )

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
        raise ValueError(
            "Invalid candle response"
        )

    if len(data) < 2:
        raise ValueError(
            "Not enough candles"
        )

    candles = []

    for candle in data:

        # ----------------------------------------------------
        # CURRENT GATE FORMAT
        # ----------------------------------------------------

        if isinstance(candle, dict):

            timestamp = candle.get("t")
            high = candle.get("h")
            low = candle.get("l")
            close = candle.get("c")

            if timestamp is None:
                raise ValueError(
                    "Timestamp missing"
                )

            if high is None:
                raise ValueError(
                    "High missing"
                )

            if low is None:
                raise ValueError(
                    "Low missing"
                )

            candles.append({
                "timestamp": int(
                    float(timestamp)
                ),
                "high": float(high),
                "low": float(low),
                "close": (
                    float(close)
                    if close is not None
                    else None
                )
            })

        # ----------------------------------------------------
        # BACKWARD COMPATIBILITY
        # ----------------------------------------------------

        elif isinstance(
            candle,
            (list, tuple)
        ):

            if len(candle) < 5:
                continue

            candles.append({
                "timestamp": int(
                    float(candle[0])
                ),
                "close": float(candle[2]),
                "high": float(candle[3]),
                "low": float(candle[4])
            })

    if len(candles) < 2:

        raise ValueError(
            "Could not normalize candles"
        )

    candles.sort(
        key=lambda x: x["timestamp"]
    )

    return candles


# ============================================================
# CHECK LONG SIGNAL
# ============================================================

def check_signal(
    contract,
    live_prices
):

    try:

        candles = get_daily_candles(
            contract
        )

        # Current/forming daily candle
        current = candles[-1]

        # Previous completed daily candle
        previous = candles[-2]

        previous_high = previous["high"]

        current_high = current["high"]

        live_price = live_prices.get(
            contract
        )

        if live_price is None:
            return None

        # ====================================================
        # LONG ONLY
        #
        # Current daily candle must break previous HIGH
        #
        # Live price must STILL be above previous HIGH
        #
        # Maximum distance = 2%
        # ====================================================

        if not (
            current_high > previous_high
        ):
            return None

        if not (
            live_price > previous_high
        ):
            return None

        if not (
            live_price <=
            previous_high *
            (1 + PROXIMITY_PERCENT)
        ):
            return None

        # Distance above previous high
        distance_percent = (
            (
                live_price -
                previous_high
            )
            / previous_high
        ) * 100

        return {
            "contract": contract,
            "direction": "LONG",
            "price": live_price,
            "level": previous_high,
            "distance_percent": distance_percent,
            "candle_timestamp": current[
                "timestamp"
            ]
        }

    except Exception as e:

        return {
            "error": str(e),
            "contract": contract
        }


# ============================================================
# CATEGORY
# ============================================================

def get_category(distance):

    if distance < 0.50:
        return "0–0.50%"

    elif distance < 1.00:
        return "0.50–1.00%"

    elif distance < 1.50:
        return "1.00–1.50%"

    else:
        return "1.50–2.00%"


# ============================================================
# SIGNAL KEY
# ============================================================

def signal_key(signal):

    return (
        f"{signal['contract']}_"
        f"LONG_"
        f"{signal['candle_timestamp']}"
    )


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(
    message,
    chat_id
):

    if not TELEGRAM_BOT_TOKEN:
        print(
            "Telegram bot token missing."
        )
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
            f"Telegram error "
            f"{response.status_code}: "
            f"{response.text[:300]}"
        )

        return False

    except Exception as e:

        print(
            f"Telegram exception: {e}"
        )

        return False


# ============================================================
# TELEGRAM CHUNKS
# ============================================================

def send_telegram_chunks(
    message,
    chat_id
):

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

        message = message[
            split_at:
        ].lstrip()

    if message:
        parts.append(message)

    success = True

    for part in parts:

        if not send_telegram(
            part,
            chat_id
        ):
            success = False

        time.sleep(0.5)

    return success


# ============================================================
# BUILD TELEGRAM MESSAGE
# ============================================================

def build_message(
    signals
):

    categories = {
        "0–0.50%": [],
        "0.50–1.00%": [],
        "1.00–1.50%": [],
        "1.50–2.00%": []
    }

    # --------------------------------------------------------
    # Put signals into categories
    # --------------------------------------------------------

    for signal in signals:

        distance = signal[
            "distance_percent"
        ]

        category = get_category(
            distance
        )

        categories[
            category
        ].append(signal)

    # --------------------------------------------------------
    # Sort each category
    # Closest to previous high first
    # --------------------------------------------------------

    for category in categories:

        categories[
            category
        ].sort(
            key=lambda x:
            x["distance_percent"]
        )

    lines = []

    lines.append(
        "🚨 DAILY LONG BREAKOUTS"
    )

    lines.append(
        "Current price is above "
        "previous daily HIGH."
    )

    lines.append("")

    # --------------------------------------------------------
    # Categories
    # --------------------------------------------------------

    for category in categories:

        signals_in_category = (
            categories[category]
        )

        if not signals_in_category:
            continue

        lines.append(
            f"📊 {category}"
        )

        for signal in signals_in_category:

            symbol = signal[
                "contract"
            ].replace(
                "_USDT",
                ""
            )

            distance = signal[
                "distance_percent"
            ]

            lines.append(
                f"🟢 {symbol} "
                f"+{distance:.2f}%"
            )

        lines.append("")

    lines.append(
        "LTF RETEST: 15M / 1H / 4H"
    )

    return "\n".join(lines)


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
    # CONTRACTS
    # --------------------------------------------------------

    try:

        contracts = get_contracts()

    except Exception as e:

        print(
            f"Contract error: {e}"
        )

        return

    print(
        f"USDT contracts found: "
        f"{len(contracts)}"
    )

    if not contracts:

        print(
            "No contracts found."
        )

        return

    # --------------------------------------------------------
    # LIVE PRICES
    # --------------------------------------------------------

    print(
        "Fetching live futures prices..."
    )

    live_prices = (
        get_all_live_prices()
    )

    print(
        f"Live prices received: "
        f"{len(live_prices)}"
    )

    if not live_prices:

        print(
            "No live prices received."
        )

        return

    # --------------------------------------------------------
    # SCAN
    # --------------------------------------------------------

    print(
        "Scanning..."
    )

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

        for future in as_completed(
            futures
        ):

            contract = futures[
                future
            ]

            try:

                result = (
                    future.result()
                )

                if result is None:

                    pass

                elif "error" in result:

                    errors.append(
                        result
                    )

                else:

                    setups.append(
                        result
                    )

            except Exception as e:

                errors.append({
                    "contract": contract,
                    "error": str(e)
                })

            completed += 1

            if (
                completed % 100 == 0
                or
                completed == len(
                    contracts
                )
            ):

                print(
                    f"Progress: "
                    f"{completed}/"
                    f"{len(contracts)}"
                )

    # --------------------------------------------------------
    # ERROR SUMMARY
    # --------------------------------------------------------

    if errors:

        print(
            f"Candle/API errors: "
            f"{len(errors)}"
        )

        for error in errors[:10]:

            print(
                f"Error "
                f"{error['contract']}: "
                f"{error['error']}"
            )

        if len(errors) > 10:

            print(
                f"... and "
                f"{len(errors) - 10} "
                f"more errors"
            )

    # --------------------------------------------------------
    # SORT
    # --------------------------------------------------------

    setups.sort(
        key=lambda x:
        x["distance_percent"]
    )

    print()
    print(
        f"Daily LONG setups found: "
        f"{len(setups)}"
    )

    # --------------------------------------------------------
    # CATEGORY COUNTS
    # --------------------------------------------------------

    category_counts = {
        "0–0.50%": 0,
        "0.50–1.00%": 0,
        "1.00–1.50%": 0,
        "1.50–2.00%": 0
    }

    for signal in setups:

        category = get_category(
            signal["distance_percent"]
        )

        category_counts[
            category
        ] += 1

    print(
        f"0–0.50%: "
        f"{category_counts['0–0.50%']}"
    )

    print(
        f"0.50–1.00%: "
        f"{category_counts['0.50–1.00%']}"
    )

    print(
        f"1.00–1.50%: "
        f"{category_counts['1.00–1.50%']}"
    )

    print(
        f"1.50–2.00%: "
        f"{category_counts['1.50–2.00%']}"
    )

    # --------------------------------------------------------
    # NEW SIGNALS
    # --------------------------------------------------------

    new_signals = []

    for signal in setups:

        key = signal_key(
            signal
        )

        if key not in history:

            history[key] = {
                "contract": signal[
                    "contract"
                ],
                "direction": "LONG",
                "candle_timestamp":
                    signal[
                        "candle_timestamp"
                    ],
                "telegram_sent": False
            }

            new_signals.append(
                signal
            )

    print(
        f"New signals: "
        f"{len(new_signals)}"
    )

    # --------------------------------------------------------
    # UNSENT
    # --------------------------------------------------------

    unsent_signals = []

    for signal in new_signals:

        key = signal_key(
            signal
        )

        if not history[key].get(
            "telegram_sent",
            False
        ):

            unsent_signals.append(
                signal
            )

    # --------------------------------------------------------
    # TELEGRAM
    # --------------------------------------------------------

    if unsent_signals:

        message = build_message(
            unsent_signals
        )

        print()
        print(
            "Sending Telegram alerts..."
        )

        sent_1 = send_telegram_chunks(
            message,
            TELEGRAM_CHAT_ID
        )

        sent_2 = True

        if TELEGRAM_CHAT_ID_2:

            sent_2 = (
                send_telegram_chunks(
                    message,
                    TELEGRAM_CHAT_ID_2
                )
            )

        if sent_1 or sent_2:

            for signal in unsent_signals:

                key = signal_key(
                    signal
                )

                history[key][
                    "telegram_sent"
                ] = True

            print(
                "Telegram alerts sent."
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

    save_history(
        history
    )

    # --------------------------------------------------------
    # COMPLETE
    # --------------------------------------------------------

    runtime = (
        time.time() -
        start_time
    )

    print()
    print("=" * 60)
    print("SCAN COMPLETE")
    print(
        f"Runtime: {runtime:.1f} seconds"
    )
    print(
        f"History records: "
        f"{len(history)}"
    )
    print("=" * 60)


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()
