import os
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests


# ============================================================
# DAILY WICK FLIP SETUP BOT
# ============================================================
#
# LOGIC:
#
# Previous completed Daily candle = reference candle
#
# LONG:
#   Current Daily candle must wick above Previous Daily HIGH
#   AND current live price must remain within 1% ABOVE that HIGH
#
# SHORT:
#   Current Daily candle must wick below Previous Daily LOW
#   AND current live price must remain within 1% BELOW that LOW
#
# Example:
#
# Previous High = 100
# Current price = 100.50
# -> LONG +0.50%       YES
#
# Previous High = 100
# Current price = 103
# -> LONG              NO
#
# Previous Low = 100
# Current price = 99.50
# -> SHORT -0.50%      YES
#
# Previous Low = 100
# Current price = 97
# -> SHORT             NO
#
# Wick break is enough.
# No OBV.
# No SMA.
# No EMA.
# No RSI.
# No BOS.
# LTF retest is checked manually.
#
# TELEGRAM FORMAT:
#
# 🟢 BTC +0.46%
# 🔴 ETH -0.61%
#
# ============================================================


GATE_URL = "https://api.gateio.ws/api/v4"

TIMEFRAME = "1d"

# Only a small amount of history is required.
CANDLE_LIMIT = 10

MAX_WORKERS = 12
REQUEST_DELAY = 0.03

# Maximum distance from previous Daily wick.
PROXIMITY_PERCENT = 0.01

HISTORY_FILE = "signals.json"


# ============================================================
# TELEGRAM CONFIGURATION
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    ""
).strip()

TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID",
    ""
).strip()

TELEGRAM_CHAT_ID_2 = os.getenv(
    "TELEGRAM_CHAT_ID_2",
    ""
).strip()


HEADERS = {
    "User-Agent": "Daily-Wick-Flip-Bot/2.0"
}


request_lock = threading.Lock()


# ============================================================
# GATE API REQUEST
# ============================================================

def gate_get(url, params=None, retries=5):

    for attempt in range(retries):

        try:

            with request_lock:
                time.sleep(REQUEST_DELAY)

            response = requests.get(
                url,
                params=params,
                headers=HEADERS,
                timeout=15
            )

            if response.status_code == 200:

                return response.json()

            if response.status_code == 429:

                wait_time = min(
                    8,
                    1.5 * (attempt + 1)
                )

                print(
                    f"429 received. "
                    f"Retrying in {wait_time:.1f}s..."
                )

                time.sleep(wait_time)

                continue

            print(
                f"Gate API error "
                f"{response.status_code}: "
                f"{response.text[:150]}"
            )

        except requests.RequestException as e:

            print(
                f"Request error: {e}"
            )

            if attempt < retries - 1:

                time.sleep(1.5)

    return None


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:

        print(
            "Telegram configuration missing."
        )

        return False

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    chat_ids = [
        TELEGRAM_CHAT_ID
    ]

    if TELEGRAM_CHAT_ID_2:

        chat_ids.append(
            TELEGRAM_CHAT_ID_2
        )

    success = True

    for chat_id in chat_ids:

        try:

            response = requests.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": message,
                    "disable_web_page_preview": True
                },
                timeout=15
            )

            if response.status_code == 200:

                print(
                    f"Telegram sent: {chat_id}"
                )

            else:

                print(
                    f"Telegram error "
                    f"{response.status_code}: "
                    f"{response.text[:200]}"
                )

                success = False

        except requests.RequestException as e:

            print(
                f"Telegram request error: {e}"
            )

            success = False

    return success


# ============================================================
# HISTORY
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

        if isinstance(data, list):

            return {
                str(item): True
                for item in data
            }

    except Exception as e:

        print(
            f"Could not load history: {e}"
        )

    return {}


def save_history(history):

    try:

        with open(
            HISTORY_FILE,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                history,
                f,
                indent=2,
                sort_keys=True
            )

        print(
            f"Signal history saved: "
            f"{len(history)} records"
        )

    except Exception as e:

        print(
            f"Could not save history: {e}"
        )


# ============================================================
# GET USDT FUTURES CONTRACTS
# ============================================================

def get_usdt_contracts():

    url = (
        f"{GATE_URL}/futures/usdt/contracts"
    )

    data = gate_get(url)

    if not data:

        return []

    contracts = []

    for item in data:

        name = item.get(
            "name",
            ""
        )

        if not name:

            continue

        if not name.endswith(
            "_USDT"
        ):

            continue

        if item.get(
            "in_delisting"
        ) is True:

            continue

        contracts.append(
            name
        )

    return sorted(
        set(contracts)
    )


# ============================================================
# DAILY CANDLES
# ============================================================

def get_daily_candles(contract):

    url = (
        f"{GATE_URL}/futures/usdt/"
        f"candlesticks"
    )

    params = {
        "contract": contract,
        "interval": TIMEFRAME,
        "limit": CANDLE_LIMIT
    }

    data = gate_get(
        url,
        params
    )

    if not data:

        return []

    candles = []

    for item in data:

        try:

            timestamp = int(
                item["t"]
            )

            open_price = float(
                item["o"]
            )

            high_price = float(
                item["h"]
            )

            low_price = float(
                item["l"]
            )

            close_price = float(
                item["c"]
            )

            volume = float(
                item.get(
                    "v",
                    0
                )
            )

            candles.append({
                "timestamp": timestamp,
                "open": open_price,
                "high": high_price,
                "low": low_price,
                "close": close_price,
                "volume": volume
            })

        except (
            KeyError,
            TypeError,
            ValueError
        ):

            continue

    candles.sort(
        key=lambda x: x["timestamp"]
    )

    return candles


# ============================================================
# GET LIVE FUTURES PRICE
# ============================================================

def get_live_price(contract):

    url = (
        f"{GATE_URL}/futures/usdt/tickers"
    )

    data = gate_get(
        url,
        params={
            "contract": contract
        }
    )

    if not data:

        return None

    try:

        return float(
            data[0]["last"]
        )

    except (
        KeyError,
        TypeError,
        ValueError,
        IndexError
    ):

        return None


# ============================================================
# CHECK ONE CONTRACT
# ============================================================

def check_signal(contract):

    candles = get_daily_candles(
        contract
    )

    if len(candles) < 2:

        return []

    now = int(
        time.time()
    )

    # Latest Daily candle = current/forming candle.
    current_candle = candles[-1]

    # Previous completed Daily candle.
    previous_candle = candles[-2]

    current_start = current_candle[
        "timestamp"
    ]

    current_end = (
        current_start + 86400
    )

    # Safety check:
    # latest candle must still be the current/forming candle.
    if current_end <= now:

        return []

    previous_high = previous_candle[
        "high"
    ]

    previous_low = previous_candle[
        "low"
    ]

    current_high = current_candle[
        "high"
    ]

    current_low = current_candle[
        "low"
    ]

    # --------------------------------------------------------
    # LIVE CURRENT PRICE
    # --------------------------------------------------------

    current_price = get_live_price(
        contract
    )

    if current_price is None:

        return []

    signals = []

    # --------------------------------------------------------
    # 1% PROXIMITY LEVELS
    # --------------------------------------------------------

    long_upper_limit = (
        previous_high
        * (1 + PROXIMITY_PERCENT)
    )

    short_lower_limit = (
        previous_low
        * (1 - PROXIMITY_PERCENT)
    )

    # ========================================================
    # LONG
    # ========================================================
    #
    # Current Daily wick must break Previous Daily HIGH.
    #
    # AND
    #
    # Current live price must still be:
    #
    # Previous High < Price <= Previous High + 1%
    #
    # ========================================================

    if (
        current_high > previous_high
        and
        previous_high < current_price <= long_upper_limit
    ):

        distance_percent = (
            (
                current_price
                - previous_high
            )
            / previous_high
        ) * 100

        signals.append({

            "key": (
                f"{contract}|DAILY|"
                f"LONG_BREAK|"
                f"{current_start}"
            ),

            "contract": contract,

            "timeframe": "DAILY",

            "direction": "LONG",

            "current_timestamp":
                current_start,

            "previous_high":
                previous_high,

            "previous_low":
                previous_low,

            "current_high":
                current_high,

            "current_low":
                current_low,

            "current_price":
                current_price,

            "distance_percent":
                distance_percent

        })

    # ========================================================
    # SHORT
    # ========================================================
    #
    # Current Daily wick must break Previous Daily LOW.
    #
    # AND
    #
    # Current live price must still be:
    #
    # Previous Low - 1% <= Price < Previous Low
    #
    # ========================================================

    if (
        current_low < previous_low
        and
        short_lower_limit <= current_price < previous_low
    ):

        distance_percent = (
            (
                previous_low
                - current_price
            )
            / previous_low
        ) * 100

        signals.append({

            "key": (
                f"{contract}|DAILY|"
                f"SHORT_BREAK|"
                f"{current_start}"
            ),

            "contract": contract,

            "timeframe": "DAILY",

            "direction": "SHORT",

            "current_timestamp":
                current_start,

            "previous_high":
                previous_high,

            "previous_low":
                previous_low,

            "current_high":
                current_high,

            "current_low":
                current_low,

            "current_price":
                current_price,

            "distance_percent":
                distance_percent

        })

    return signals


# ============================================================
# MAIN
# ============================================================

def main():

    start_time = time.time()

    print("=" * 60)

    print(
        "DAILY WICK FLIP SETUP BOT"
    )

    print("=" * 60)

    print(
        "Timeframe: DAILY"
    )

    print(
        "LONG: Current Daily wick breaks "
        "Previous Daily HIGH"
    )

    print(
        "SHORT: Current Daily wick breaks "
        "Previous Daily LOW"
    )

    print(
        "Proximity: Current price within 1% "
        "of Previous Daily wick"
    )

    print(
        "Break type: WICK BREAK IS ENOUGH"
    )

    print(
        "No OBV • No indicators • "
        "LTF retest checked manually"
    )

    telegram_ok = bool(
        TELEGRAM_BOT_TOKEN
        and TELEGRAM_CHAT_ID
    )

    print(
        "Telegram configuration: "
        +
        (
            "OK"
            if telegram_ok
            else "MISSING"
        )
    )

    history = load_history()

    print(
        f"Previously recorded signals: "
        f"{len(history)}"
    )

    contracts = get_usdt_contracts()

    print(
        f"USDT contracts found: "
        f"{len(contracts)}"
    )

    if not contracts:

        print(
            "No USDT contracts found."
        )

        return

    print(
        "Scanning..."
    )

    results = []

    completed = 0

    total = len(
        contracts
    )

    # ========================================================
    # PARALLEL SCAN
    # ========================================================

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        future_map = {

            executor.submit(
                check_signal,
                contract
            ): contract

            for contract in contracts
        }

        for future in as_completed(
            future_map
        ):

            contract = future_map[
                future
            ]

            try:

                signals = future.result()

                if signals:

                    results.extend(
                        signals
                    )

            except Exception as e:

                print(
                    f"Error scanning "
                    f"{contract}: {e}"
                )

            completed += 1

            if (
                completed % 100 == 0
                or completed == total
            ):

                print(
                    f"Progress: "
                    f"{completed}/{total}"
                )

    # ========================================================
    # NEW SIGNALS ONLY
    # ========================================================

    # Signals that:
    #
    # 1. Were never recorded before
    #
    # OR
    #
    # 2. Were recorded but Telegram was not confirmed.
    #
    # This preserves the previous retry behavior.
    # ========================================================

    new_signals = [

        signal

        for signal in results

        if (
            signal["key"] not in history

            or not isinstance(
                history.get(
                    signal["key"]
                ),
                dict
            )

            or not history[
                signal["key"]
            ].get(
                "telegram_sent",
                False
            )
        )
    ]

    long_count = sum(

        1

        for signal in new_signals

        if signal["direction"] == "LONG"
    )

    short_count = sum(

        1

        for signal in new_signals

        if signal["direction"] == "SHORT"
    )

    print(
        f"Daily wick flip setups found: "
        f"{len(results)}"
    )

    print(
        f"New LONG setups: "
        f"{long_count}"
    )

    print(
        f"New SHORT setups: "
        f"{short_count}"
    )

    # ========================================================
    # TELEGRAM
    # ========================================================

    delivered_signal_keys = set()

    if new_signals:

        print(
            "Sending Daily wick flip alerts..."
        )

        # Sort alphabetically by direction and coin.
        new_signals.sort(
            key=lambda x: (
                x["direction"],
                x["contract"]
            )
        )

        # ----------------------------------------------------
        # SHORT HEADER
        # ----------------------------------------------------

        header = (
            "🚨 DAILY WICK FLIP SETUPS\n"
            "Current price within 1% of "
            "previous daily wick.\n\n"
        )

        footer = (
            "\n\n"
            "LTF RETEST: 15M / 1H / 4H"
        )

        # Telegram limit is 4096 characters.
        # Keep a safe margin.
        max_message_length = 3500

        batches = []

        current_blocks = []

        current_keys = []

        # ----------------------------------------------------
        # BUILD ONE-LINE SIGNALS
        # ----------------------------------------------------

        for signal in new_signals:

            coin = signal[
                "contract"
            ].replace(
                "_USDT",
                ""
            )

            if signal[
                "direction"
            ] == "LONG":

                block = (
                    f"🟢 {coin} "
                    f"+{signal['distance_percent']:.2f}%"
                )

            else:

                block = (
                    f"🔴 {coin} "
                    f"-{signal['distance_percent']:.2f}%"
                )

            # Estimate message size.
            projected_length = (

                len(header)

                + len(
                    "Batch 999/999\n\n"
                )

                + sum(
                    len(item) + 1
                    for item in current_blocks
                )

                + len(block)

                + len(footer)
            )

            # Start a new batch if needed.
            if (
                current_blocks
                and
                projected_length
                > max_message_length
            ):

                batches.append(
                    (
                        current_blocks,
                        current_keys
                    )
                )

                current_blocks = []

                current_keys = []

            current_blocks.append(
                block
            )

            current_keys.append(
                signal["key"]
            )

        # Add final batch.
        if current_blocks:

            batches.append(
                (
                    current_blocks,
                    current_keys
                )
            )

        total_batches = len(
            batches
        )

        print(
            f"Sending "
            f"{len(new_signals)} setups "
            f"in "
            f"{total_batches} "
            f"Telegram messages."
        )

        # ----------------------------------------------------
        # SEND BATCHES
        # ----------------------------------------------------

        for index, (
            blocks,
            keys
        ) in enumerate(
            batches,
            start=1
        ):

            batch_header = (
                header
                +
                f"Batch {index}/"
                f"{total_batches}\n\n"
            )

            message = (
                batch_header
                +
                "\n".join(
                    blocks
                )
                +
                footer
            )

            if (
                len(message)
                > max_message_length
            ):

                print(
                    f"Batch {index} is too long "
                    f"({len(message)} chars); "
                    f"not sending."
                )

                continue

            if (
                telegram_ok
                and
                send_telegram(
                    message
                )
            ):

                delivered_signal_keys.update(
                    keys
                )

                print(
                    f"Batch {index}/"
                    f"{total_batches} sent "
                    f"({len(keys)} setups)."
                )

            else:

                print(
                    f"Batch {index}/"
                    f"{total_batches} failed; "
                    f"signals will not be "
                    f"marked as sent."
                )

    else:

        print(
            "NO NEW DAILY WICK FLIP SETUPS"
        )

        if telegram_ok:

            report = (
                "📊 DAILY WICK FLIP SETUPS\n\n"
                "No new Daily wick flip setup.\n\n"
                f"Current detected setups: "
                f"{len(results)}\n"
                f"Previously recorded: "
                f"{len(history)}"
            )

            send_telegram(
                report
            )

    # ========================================================
    # SAVE ONLY SIGNALS CONFIRMED SENT
    # ========================================================

    for signal in new_signals:

        if (
            signal["key"]
            not in delivered_signal_keys
        ):

            continue

        history[
            signal["key"]
        ] = {

            "contract":
                signal["contract"],

            "timeframe":
                signal["timeframe"],

            "direction":
                signal["direction"],

            "current_timestamp":
                signal["current_timestamp"],

            "previous_high":
                signal["previous_high"],

            "previous_low":
                signal["previous_low"],

            "current_high":
                signal["current_high"],

            "current_low":
                signal["current_low"],

            "current_price":
                signal["current_price"],

            "distance_percent":
                signal["distance_percent"],

            "telegram_sent":
                True,

            "created_at":
                int(
                    time.time()
                )
        }

    save_history(
        history
    )

    # ========================================================
    # RUNTIME
    # ========================================================

    elapsed = (
        time.time()
        - start_time
    )

    print(
        f"Total runtime: "
        f"{elapsed:.1f} seconds"
    )

    print("=" * 60)

    print(
        "SCAN COMPLETE"
    )

    print("=" * 60)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    main()
