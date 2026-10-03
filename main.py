import os
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests


# ============================================================
# DAILY BREAKOUT SETUP BOT
# ============================================================
# Logic:
#   Previous completed Daily candle = reference candle
#   Current Daily candle breaks previous High -> LONG SETUP
#   Current Daily candle breaks previous Low  -> SHORT SETUP
#
# A wick break is enough.
# No OBV, SMA, EMA, RSI, BOS, or other indicators.
# The bot only gives the Daily setup. LTF retest is checked manually.
# ============================================================

GATE_URL = "https://api.gateio.ws/api/v4"

TIMEFRAME = "1d"

# Only a small amount of history is required.
CANDLE_LIMIT = 10

MAX_WORKERS = 12
REQUEST_DELAY = 0.03

HISTORY_FILE = "signals.json"

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN", ""
).strip()

TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID", ""
).strip()

TELEGRAM_CHAT_ID_2 = os.getenv(
    "TELEGRAM_CHAT_ID_2", ""
).strip()

HEADERS = {
    "User-Agent": "Daily-Breakout-Setup-Bot/1.0"
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

        contracts.append(name)

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
                item.get("v", 0)
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
# CHECK ONE CONTRACT
# ============================================================

def check_signal(contract):

    candles = get_daily_candles(
        contract
    )

    if len(candles) < 2:
        return []

    now = int(time.time())

    # Gate's latest Daily candle is normally the
    # currently forming candle. We deliberately KEEP it.
    current_candle = candles[-1]
    previous_candle = candles[-2]

    current_start = current_candle[
        "timestamp"
    ]

    current_end = (
        current_start + 86400
    )

    # Safety check: the latest candle must actually
    # be the current/forming Daily candle.
    if current_end <= now:
        return []

    signals = []

    previous_high = previous_candle["high"]
    previous_low = previous_candle["low"]

    current_high = current_candle["high"]
    current_low = current_candle["low"]

    # --------------------------------------------------------
    # LONG SETUP
    # Current candle WICK breaks previous candle HIGH.
    # --------------------------------------------------------

    if current_high > previous_high:

        signals.append({
            "key": (
                f"{contract}|DAILY|"
                f"LONG_BREAK|"
                f"{current_start}"
            ),
            "contract": contract,
            "timeframe": "DAILY",
            "direction": "LONG",
            "current_timestamp": current_start,
            "previous_high": previous_high,
            "previous_low": previous_low,
            "current_high": current_high,
            "current_low": current_low
        })

    # --------------------------------------------------------
    # SHORT SETUP
    # Current candle WICK breaks previous candle LOW.
    # --------------------------------------------------------

    if current_low < previous_low:

        signals.append({
            "key": (
                f"{contract}|DAILY|"
                f"SHORT_BREAK|"
                f"{current_start}"
            ),
            "contract": contract,
            "timeframe": "DAILY",
            "direction": "SHORT",
            "current_timestamp": current_start,
            "previous_high": previous_high,
            "previous_low": previous_low,
            "current_high": current_high,
            "current_low": current_low
        })

    return signals


# ============================================================
# MAIN
# ============================================================

def main():

    start_time = time.time()

    print("=" * 60)
    print(
        "DAILY BREAKOUT SETUP BOT"
    )
    print("=" * 60)

    print("Timeframe: DAILY")
    print(
        "LONG: Current Daily candle breaks "
        "Previous Daily HIGH"
    )
    print(
        "SHORT: Current Daily candle breaks "
        "Previous Daily LOW"
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
        + (
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

    print("Scanning...")

    results = []

    completed = 0
    total = len(contracts)

    # --------------------------------------------------------
    # PARALLEL SCAN
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # NEW SIGNALS ONLY
    # --------------------------------------------------------

    new_signals = [
        signal
        for signal in results
        if signal["key"] not in history
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
        f"Daily breakout setups found: "
        f"{len(results)}"
    )

    print(
        f"New LONG setups: {long_count}"
    )

    print(
        f"New SHORT setups: {short_count}"
    )

    # --------------------------------------------------------
    # TELEGRAM
    # --------------------------------------------------------

    if new_signals:

        print(
            "Sending ONE Daily breakout list..."
        )

        new_signals.sort(
            key=lambda x: (
                x["direction"],
                x["contract"]
            )
        )

        lines = [
            "🚨 DAILY BREAKOUT SETUPS",
            "",
            "Current Daily Candle broke Previous Daily Candle",
            "Wick break is enough.",
            ""
        ]

        for number, signal in enumerate(
            new_signals,
            start=1
        ):

            if signal["direction"] == "LONG":

                lines.append(
                    f"{number}. 🟢 "
                    f"{signal['contract']} — "
                    f"DAILY LONG SETUP"
                )

                lines.append(
                    f"   Previous High: "
                    f"{signal['previous_high']}"
                )

                lines.append(
                    f"   Current High: "
                    f"{signal['current_high']}"
                )

            else:

                lines.append(
                    f"{number}. 🔴 "
                    f"{signal['contract']} — "
                    f"DAILY SHORT SETUP"
                )

                lines.append(
                    f"   Previous Low: "
                    f"{signal['previous_low']}"
                )

                lines.append(
                    f"   Current Low: "
                    f"{signal['current_low']}"
                )

            lines.append("")

        lines.extend([
            f"Total: {len(new_signals)}",
            "",
            "Check LTF retest manually:",
            "15M / 1H / 4H",
            "",
            "PRICE ACTION ONLY"
        ])

        message = "\n".join(
            lines
        )

        if telegram_ok:

            send_telegram(
                message
            )

    else:

        print(
            "NO NEW DAILY BREAKOUT SETUPS"
        )

        if telegram_ok:

            report = (
                "📊 DAILY BREAKOUT SETUPS\n\n"
                "No new Daily breakout setup.\n\n"
                f"Current detected setups: "
                f"{len(results)}\n"
                f"Previously recorded: "
                f"{len(history)}"
            )

            send_telegram(
                report
            )

    # --------------------------------------------------------
    # SAVE SIGNALS
    # --------------------------------------------------------

    for signal in results:

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

            "created_at":
                int(time.time())
        }

    save_history(
        history
    )

    elapsed = (
        time.time()
        - start_time
    )

    print(
        f"Total runtime: "
        f"{elapsed:.1f} seconds"
    )

    print("=" * 60)
    print("SCAN COMPLETE")
    print("=" * 60)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()
