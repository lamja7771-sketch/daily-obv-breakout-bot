import os
import json
import time
import requests
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
# DAILY WICK FLIP SETUP BOT
# ============================================================

GATE_BASE_URL = "https://api.gateio.ws/api/v4"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_CHAT_ID_2 = os.getenv("TELEGRAM_CHAT_ID_2")

HISTORY_FILE = "signals.json"

TIMEFRAME = "1d"

# Current price must remain beyond the previous daily wick
# but not more than 2% away from it.
PROXIMITY_PERCENT = 0.02

MAX_WORKERS = 8

CANDLE_LIMIT = 3

HEADERS = {
    "User-Agent": "Daily-Wick-Flip-Bot/1.0"
}


# ============================================================
# HISTORY
# ============================================================

def load_history():
    if not os.path.exists(HISTORY_FILE):
        return {}

    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_history(history):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)


# ============================================================
# GATE FUTURES CONTRACTS
# ============================================================

def get_contracts():
    url = f"{GATE_BASE_URL}/futures/usdt/contracts"

    try:
        response = requests.get(
            url,
            headers=HEADERS,
            timeout=15
        )
        response.raise_for_status()

        data = response.json()

        contracts = []

        for item in data:
            contract = item.get("name")

            if not contract:
                continue

            # Only USDT contracts
            if not contract.endswith("_USDT"):
                continue

            # Active contracts only
            if item.get("in_delisting") is True:
                continue

            contracts.append(contract)

        return contracts

    except Exception as e:
        print(f"Error fetching contracts: {e}")
        return []


# ============================================================
# DAILY CANDLES
# ============================================================

def get_daily_candles(contract):

    url = f"{GATE_BASE_URL}/futures/usdt/candlesticks"

    params = {
        "contract": contract,
        "interval": "1d",
        "limit": CANDLE_LIMIT
    }

    try:
        response = requests.get(
            url,
            params=params,
            headers=HEADERS,
            timeout=15
        )

        response.raise_for_status()

        data = response.json()

        if not data or len(data) < 2:
            return None

        # Gate returns candles in chronological order in normal API usage.
        # Sort by timestamp to be safe.
        data = sorted(data, key=lambda x: int(x[0]))

        return data

    except Exception as e:
        print(f"Candle error {contract}: {e}")
        return None


# ============================================================
# LIVE FUTURES PRICE
# ============================================================

def get_live_price(contract):

    url = f"{GATE_BASE_URL}/futures/usdt/tickers"

    params = {
        "contract": contract
    }

    try:
        response = requests.get(
            url,
            params=params,
            headers=HEADERS,
            timeout=15
        )

        response.raise_for_status()

        data = response.json()

        if not data:
            return None

        price = data[0].get("last")

        if price is None:
            return None

        return float(price)

    except Exception as e:
        print(f"Ticker error {contract}: {e}")
        return None


# ============================================================
# CHECK SIGNAL
# ============================================================

def check_signal(contract):

    candles = get_daily_candles(contract)

    if not candles or len(candles) < 2:
        return None

    # Latest candle = current/forming daily candle
    current = candles[-1]

    # Previous completed daily candle
    previous = candles[-2]

    try:
        current_timestamp = int(current[0])

        current_high = float(current[3])
        current_low = float(current[4])

        previous_high = float(previous[3])
        previous_low = float(previous[4])

    except Exception:
        return None

    # Make sure latest candle is actually the current daily candle.
    now = int(time.time())

    if current_timestamp + 86400 <= now:
        return None

    # --------------------------------------------------------
    # LIVE FUTURES PRICE
    # --------------------------------------------------------

    live_price = get_live_price(contract)

    if live_price is None:
        return None

    # ========================================================
    # LONG
    # ========================================================
    #
    # Current daily candle must break previous HIGH.
    #
    # AND
    #
    # Live price must STILL be above previous HIGH.
    #
    # AND
    #
    # Live price must be no more than 2% above previous HIGH.
    #
    # Example:
    #
    # Previous HIGH = 100
    #
    # Valid:
    # 100.01
    # 101
    # 101.99
    # 102
    #
    # Invalid:
    # 99.99
    # 102.01
    # ========================================================

    if (
        current_high > previous_high
        and
        previous_high < live_price <= previous_high * (1 + PROXIMITY_PERCENT)
    ):

        distance_percent = (
            (live_price - previous_high)
            / previous_high
            * 100
        )

        return {
            "contract": contract,
            "direction": "LONG",
            "current_price": live_price,
            "previous_level": previous_high,
            "distance_percent": distance_percent,
            "candle_timestamp": current_timestamp
        }

    # ========================================================
    # SHORT
    # ========================================================
    #
    # Current daily candle must break previous LOW.
    #
    # AND
    #
    # Live price must STILL be below previous LOW.
    #
    # AND
    #
    # Live price must be no more than 2% below previous LOW.
    #
    # Example:
    #
    # Previous LOW = 100
    #
    # Valid:
    # 99.99
    # 99
    # 98.01
    # 98
    #
    # Invalid:
    # 100.01
    # 97.99
    # ========================================================

    if (
        current_low < previous_low
        and
        previous_low * (1 - PROXIMITY_PERCENT) <= live_price < previous_low
    ):

        distance_percent = (
            (previous_low - live_price)
            / previous_low
            * 100
        )

        return {
            "contract": contract,
            "direction": "SHORT",
            "current_price": live_price,
            "previous_level": previous_low,
            "distance_percent": distance_percent,
            "candle_timestamp": current_timestamp
        }

    return None


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message, chat_id):

    if not TELEGRAM_BOT_TOKEN or not chat_id:
        print("Telegram configuration missing.")
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

        response.raise_for_status()

        result = response.json()

        return result.get("ok", False)

    except Exception as e:
        print(f"Telegram error: {e}")
        return False


# ============================================================
# MAIN
# ============================================================

def main():

    start_time = time.time()

    print("=" * 60)
    print("DAILY WICK FLIP SETUP BOT")
    print("=" * 60)

    print("Timeframe: DAILY")
    print("LONG: Current Daily candle breaks Previous Daily HIGH")
    print("SHORT: Current Daily candle breaks Previous Daily LOW")
    print("Break type: WICK BREAK IS ENOUGH")
    print("Current price must remain beyond broken level")
    print("Proximity: 2%")
    print("No OBV • No indicators • LTF retest checked manually")

    if not TELEGRAM_BOT_TOKEN:
        print("WARNING: TELEGRAM_BOT_TOKEN missing")

    if not TELEGRAM_CHAT_ID:
        print("WARNING: TELEGRAM_CHAT_ID missing")

    history = load_history()

    print(f"Previously recorded signals: {len(history)}")

    contracts = get_contracts()

    if not contracts:
        print("No contracts found.")
        return

    print(f"USDT contracts found: {len(contracts)}")
    print("Scanning...")

    signals = []

    completed = 0
    total = len(contracts)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:

        futures = {
            executor.submit(check_signal, contract): contract
            for contract in contracts
        }

        for future in as_completed(futures):

            contract = futures[future]

            try:
                signal = future.result()

                if signal:
                    signals.append(signal)

            except Exception as e:
                print(f"Scan error {contract}: {e}")

            completed += 1

            if completed % 100 == 0 or completed == total:
                print(
                    f"Progress: {completed}/{total}"
                )

    # ========================================================
    # SORT
    # ========================================================

    signals.sort(
        key=lambda x: x["distance_percent"]
    )

    print()
    print(f"Daily wick flip setups found: {len(signals)}")

    long_count = sum(
        1 for s in signals
        if s["direction"] == "LONG"
    )

    short_count = sum(
        1 for s in signals
        if s["direction"] == "SHORT"
    )

    print(f"LONG: {long_count}")
    print(f"SHORT: {short_count}")

    # ========================================================
    # FIND NEW SIGNALS
    # ========================================================

    new_signals = []

    for signal in signals:

        contract = signal["contract"]
        direction = signal["direction"]

        signal_key = (
            f"{contract}_"
            f"{direction}_"
            f"{signal['candle_timestamp']}"
        )

        old = history.get(signal_key)

        # New signal
        if old is None:
            signal["signal_key"] = signal_key
            new_signals.append(signal)

        # Retry signals that were recorded but not sent
        elif not old.get("telegram_sent", False):
            signal["signal_key"] = signal_key
            new_signals.append(signal)

    print(f"New signals: {len(new_signals)}")

    # ========================================================
    # TELEGRAM MESSAGE
    # ========================================================

    if new_signals:

        blocks = []

        for signal in new_signals:

            symbol = signal["contract"].replace(
                "_USDT",
                ""
            )

            if signal["direction"] == "LONG":

                block = (
                    f"🟢 {symbol} "
                    f"+{signal['distance_percent']:.2f}%"
                )

            else:

                block = (
                    f"🔴 {symbol} "
                    f"-{signal['distance_percent']:.2f}%"
                )

            blocks.append(block)

        header = (
            "🚨 DAILY WICK FLIP SETUPS\n"
            "Current price within 2% of previous daily wick.\n\n"
        )

        footer = (
            "\n\n"
            "LTF RETEST: 15M / 1H / 4H"
        )

        # Telegram has a message size limit.
        # Keep messages safely below it.
        messages = []
        current_message = header

        for block in blocks:

            candidate = current_message + block + "\n"

            if len(candidate) > 3500:

                current_message += footer
                messages.append(current_message)

                current_message = header + block + "\n"

            else:

                current_message = candidate

        if current_message != header:
            current_message += footer
            messages.append(current_message)

        telegram_success = True

        for message in messages:

            print()
            print("Sending Telegram message...")

            success = send_telegram(
                message,
                TELEGRAM_CHAT_ID
            )

            if not success:
                telegram_success = False

            # Optional second Telegram destination
            if TELEGRAM_CHAT_ID_2:

                success2 = send_telegram(
                    message,
                    TELEGRAM_CHAT_ID_2
                )

                if not success2:
                    telegram_success = False

        # ====================================================
        # SAVE HISTORY ONLY AFTER TELEGRAM ATTEMPT
        # ====================================================

        for signal in new_signals:

            key = signal["signal_key"]

            history[key] = {
                "contract": signal["contract"],
                "direction": signal["direction"],
                "current_price": signal["current_price"],
                "previous_level": signal["previous_level"],
                "distance_percent": signal["distance_percent"],
                "candle_timestamp": signal["candle_timestamp"],
                "telegram_sent": telegram_success,
                "created_at": datetime.now(
                    timezone.utc
                ).isoformat()
            }

        save_history(history)

        print(
            f"History updated: {len(history)}"
        )

    else:

        print("No new signals to send.")

    # ========================================================
    # DONE
    # ========================================================

    runtime = time.time() - start_time

    print()
    print("=" * 60)
    print(f"SCAN COMPLETE")
    print(f"Runtime: {runtime:.1f} seconds")
    print("=" * 60)


if __name__ == "__main__":
    main()
