import os
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests


# ============================================================
# SETTINGS
# ============================================================

GATE_URL = "https://api.gateio.ws/api/v4"

TIMEFRAME = "1d"

# Latest completed Daily OBV must break above
# the highest OBV of the previous 20 completed
# Daily candles.
OBV_LOOKBACK = 20

# Number of Daily candles requested from Gate.
CANDLE_LIMIT = 250

# Parallel scanning.
MAX_WORKERS = 12

# Telegram secrets are supplied by GitHub Actions.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Signal history.
HISTORY_FILE = "signals.json"

HEADERS = {
    "User-Agent": "Daily-OBV-Breakout-Bot/3.0"
}

session = requests.Session()
session.headers.update(HEADERS)


# ============================================================
# HISTORY
# ============================================================

def load_history():
    if not os.path.exists(HISTORY_FILE):
        return {}

    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict):
            return data

    except Exception as e:
        print(f"History load error: {e}")

    return {}


def save_history(history):
    temp_file = HISTORY_FILE + ".tmp"

    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)

    os.replace(temp_file, HISTORY_FILE)


# ============================================================
# GET GATE USDT FUTURES CONTRACTS
# ============================================================

def get_contracts():
    url = f"{GATE_URL}/futures/usdt/contracts"

    response = session.get(
        url,
        timeout=20
    )

    response.raise_for_status()

    contracts = response.json()

    if not isinstance(contracts, list):
        return []

    result = []

    for contract in contracts:
        name = contract.get("name", "")
        status = contract.get("status", "")

        # Only USDT contracts.
        if not name.endswith("_USDT"):
            continue

        # Only currently trading contracts.
        if status != "trading":
            continue

        result.append(name)

    return sorted(set(result))


# ============================================================
# GET DAILY CANDLES
# ============================================================

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
            # Current Gate Futures candle format:
            #
            # {
            #   "t": timestamp,
            #   "v": volume,
            #   "c": close,
            #   "h": high,
            #   "l": low,
            #   "o": open,
            #   "sum": ...
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


# ============================================================
# REMOVE CURRENTLY FORMING DAILY CANDLE
# ============================================================

def get_completed_daily_candles(candles):
    now = int(time.time())

    completed = []

    for candle in candles:
        candle_end = candle["timestamp"] + 86400

        if candle_end <= now:
            completed.append(candle)

    return completed


# ============================================================
# CALCULATE CLASSIC OBV
# ============================================================

def calculate_obv(candles):
    if not candles:
        return []

    # Starting OBV value.
    obv_values = [0.0]

    for i in range(1, len(candles)):
        previous_close = candles[i - 1]["close"]
        current_close = candles[i]["close"]
        current_volume = candles[i]["volume"]

        previous_obv = obv_values[-1]

        if current_close > previous_close:
            current_obv = previous_obv + current_volume

        elif current_close < previous_close:
            current_obv = previous_obv - current_volume

        else:
            current_obv = previous_obv

        obv_values.append(current_obv)

    return obv_values


# ============================================================
# CHECK ONE CONTRACT
# ============================================================

def check_signal(contract):
    try:
        candles = get_daily_candles(contract)

        if not candles:
            return None

        # Ignore the currently forming Daily candle.
        candles = get_completed_daily_candles(candles)

        # We need:
        # previous 20 candles + current candle
        minimum_required = OBV_LOOKBACK + 1

        if len(candles) < minimum_required:
            return None

        obv = calculate_obv(candles)

        if len(obv) < minimum_required:
            return None

        # ----------------------------------------------------
        # Latest completed Daily candle
        # ----------------------------------------------------

        current_index = len(obv) - 1

        current_obv = obv[current_index]

        # ----------------------------------------------------
        # Previous 20 completed Daily OBV values
        #
        # IMPORTANT:
        # The current candle is NOT included in this range.
        # ----------------------------------------------------

        previous_obv_values = obv[
            current_index - OBV_LOOKBACK:
            current_index
        ]

        if len(previous_obv_values) != OBV_LOOKBACK:
            return None

        previous_high = max(previous_obv_values)

        # ----------------------------------------------------
        # ONLY SIGNAL CONDITION
        #
        # Latest completed Daily OBV must be greater than
        # the highest OBV of the previous 20 Daily candles.
        # ----------------------------------------------------

        if current_obv <= previous_high:
            return None

        signal_candle = candles[current_index]

        signal_timestamp = signal_candle["timestamp"]

        # Unique signal = contract + Daily + breakout candle.
        signal_key = (
            f"{contract}|DAILY|OBV_BREAKOUT|"
            f"{signal_timestamp}"
        )

        breakout_amount = current_obv - previous_high

        if previous_high != 0:
            breakout_percent = (
                breakout_amount /
                abs(previous_high)
            ) * 100
        else:
            breakout_percent = 0.0

        return {
            "contract": contract,
            "timestamp": signal_timestamp,
            "signal_key": signal_key,
            "current_obv": current_obv,
            "previous_high": previous_high,
            "breakout_amount": breakout_amount,
            "breakout_percent": breakout_percent
        }

    except Exception as e:
        print(f"{contract}: ERROR - {e}")

        return None


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN is missing.")
        return False

    if not TELEGRAM_CHAT_ID:
        print("ERROR: TELEGRAM_CHAT_ID is missing.")
        return False

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    try:
        response = session.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=20
        )

        if response.ok:
            return True

        print(
            f"Telegram error "
            f"{response.status_code}: "
            f"{response.text}"
        )

    except Exception as e:
        print(f"Telegram send error: {e}")

    return False


# ============================================================
# FORMAT SIGNAL MESSAGE
# ============================================================

def format_signal(signal):
    dt = datetime.fromtimestamp(
        signal["timestamp"],
        tz=timezone.utc
    )

    return (
        "📊 <b>DAILY OBV BREAKOUT</b>\n"
        "\n"
        f"🪙 <b>{signal['contract']}</b>\n"
        "📈 Signal: <b>OBV BREAKOUT</b>\n"
        "⏱ Timeframe: <b>DAILY</b>\n"
        "\n"
        f"Current OBV: "
        f"<b>{signal['current_obv']:,.2f}</b>\n"
        f"Previous 20 OBV High: "
        f"<b>{signal['previous_high']:,.2f}</b>\n"
        f"Breakout Amount: "
        f"<b>{signal['breakout_amount']:,.2f}</b>\n"
        f"Breakout vs High: "
        f"<b>{signal['breakout_percent']:.2f}%</b>\n"
        "\n"
        f"📅 Breakout Candle: "
        f"<b>{dt.strftime('%Y-%m-%d')}</b>\n"
        "\n"
        "⚠️ <i>OBV-only signal.</i>\n"
        "<i>No price, EMA, SMA, RSI, BOS "
        "or divergence condition.</i>"
    )


# ============================================================
# SCAN REPORT
# ============================================================

def format_no_signal_report(contract_count):
    return (
        "📊 <b>DAILY OBV SCAN</b>\n"
        "\n"
        "No new OBV breakout found.\n"
        "\n"
        f"🪙 Contracts scanned: "
        f"<b>{contract_count}</b>\n"
        f"📊 OBV lookback: "
        f"<b>{OBV_LOOKBACK} Daily candles</b>\n"
        "⏱ Timeframe: <b>DAILY</b>\n"
        "\n"
        "<b>Signal condition:</b>\n"
        "Latest completed Daily OBV must break "
        "above the highest OBV of the previous "
        f"{OBV_LOOKBACK} completed Daily candles.\n"
        "\n"
        "No price condition is used."
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 60)
    print("DAILY OBV BREAKOUT BOT")
    print("=" * 60)

    print("Timeframe: DAILY")
    print(
        f"OBV Lookback: "
        f"{OBV_LOOKBACK} completed candles"
    )
    print(f"Max workers: {MAX_WORKERS}")
    print()

    # --------------------------------------------------------
    # CHECK TELEGRAM CONFIGURATION
    # --------------------------------------------------------

    if not TELEGRAM_BOT_TOKEN:
        print(
            "ERROR: TELEGRAM_BOT_TOKEN "
            "is not configured."
        )
        return

    if not TELEGRAM_CHAT_ID:
        print(
            "ERROR: TELEGRAM_CHAT_ID "
            "is not configured."
        )
        return

    print("Telegram configuration: OK")

    # --------------------------------------------------------
    # LOAD HISTORY
    # --------------------------------------------------------

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
        print(
            f"Failed to get Gate contracts: {e}"
        )
        return

    print(
        f"USDT contracts found: "
        f"{len(contracts)}"
    )

    if not contracts:
        print(
            "ERROR: No USDT futures contracts found."
        )
        return

    print("Scanning...")
    print()

    # --------------------------------------------------------
    # PARALLEL SCAN
    # --------------------------------------------------------

    signals = []

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        futures = {
            executor.submit(
                check_signal,
                contract
            ): contract
            for contract in contracts
        }

        for future in as_completed(futures):

            contract = futures[future]

            try:
                result = future.result()

                if result:
                    signals.append(result)

            except Exception as e:
                print(
                    f"{contract}: "
                    f"worker error - {e}"
                )

    # --------------------------------------------------------
    # RESULTS
    # --------------------------------------------------------

    print()
    print(
        f"OBV breakouts found: "
        f"{len(signals)}"
    )

    # Strongest absolute OBV breakouts first.
    signals.sort(
        key=lambda x: x["breakout_amount"],
        reverse=True
    )

    # --------------------------------------------------------
    # DUPLICATE PROTECTION
    # --------------------------------------------------------

    new_signals = []

    for signal in signals:

        key = signal["signal_key"]

        if key in history:
            continue

        new_signals.append(signal)

        history[key] = {
            "contract": signal["contract"],
            "timestamp": signal["timestamp"],
            "created_at": datetime.now(
                timezone.utc
            ).isoformat()
        }

    print(
        f"New signals: "
        f"{len(new_signals)}"
    )

    # --------------------------------------------------------
    # SEND NEW SIGNALS
    # --------------------------------------------------------

    if new_signals:

        print()
        print(
            "Sending new Telegram alerts..."
        )

        for signal in new_signals:

            message = format_signal(signal)

            print(
                f"NEW SIGNAL: "
                f"{signal['contract']} | "
                f"OBV breakout: "
                f"{signal['breakout_percent']:.2f}%"
            )

            success = send_telegram(message)

            if success:
                print(
                    f"Telegram sent: "
                    f"{signal['contract']}"
                )
            else:
                print(
                    f"Telegram FAILED: "
                    f"{signal['contract']}"
                )

            time.sleep(0.5)

    # --------------------------------------------------------
    # NO NEW SIGNALS
    # --------------------------------------------------------

    else:

        print()
        print("NO NEW OBV BREAKOUT")

        report = format_no_signal_report(
            len(contracts)
        )

        success = send_telegram(report)

        if success:
            print("Scan report sent to Telegram.")
        else:
            print("Scan report failed to send.")

    # --------------------------------------------------------
    # SAVE HISTORY
    # --------------------------------------------------------

    save_history(history)

    print()
    print(
        f"Signal history saved: "
        f"{len(history)} records"
    )

    print()
    print("=" * 60)
    print("SCAN COMPLETE")
    print("=" * 60)


# ============================================================
# START BOT
# ============================================================

if __name__ == "__main__":
    main()
