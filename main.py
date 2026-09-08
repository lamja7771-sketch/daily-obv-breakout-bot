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
SETTLE = "usdt"

TIMEFRAME = "1d"

# OBV must break the highest OBV of these previous
# completed Daily candles.
OBV_LOOKBACK = 20

# Enough candles to calculate OBV reliably.
CANDLE_LIMIT = 250

# Parallel scanning
MAX_WORKERS = 12

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Optional second destination
TELEGRAM_CHAT_ID_2 = os.getenv("TELEGRAM_CHAT_ID_2")

# Signal history
HISTORY_FILE = "signals.json"

HEADERS = {
    "User-Agent": "Daily-OBV-Breakout-Bot/1.0"
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
# GATE.IO CONTRACTS
# ============================================================

def get_contracts():
    url = f"{GATE_URL}/futures/contracts"

    response = session.get(
        url,
        params={"settle": SETTLE},
        timeout=20
    )

    response.raise_for_status()

    contracts = response.json()

    result = []

    for contract in contracts:
        name = contract.get("name", "")
        status = contract.get("status", "")

        # Only USDT perpetual-style contracts that are open.
        if not name.endswith("_USDT"):
            continue

        if status and status.lower() != "trading":
            continue

        result.append(name)

    return sorted(set(result))


# ============================================================
# DAILY CANDLES
# ============================================================

def get_daily_candles(contract):
    url = f"{GATE_URL}/futures/candlesticks"

    response = session.get(
        url,
        params={
            "settle": SETTLE,
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
            # Gate candle format:
            # [timestamp, volume, close, high, low, open, ...]
            timestamp = int(candle[0])
            volume = float(candle[1])
            close = float(candle[2])

            parsed.append({
                "timestamp": timestamp,
                "volume": volume,
                "close": close
            })

        except (IndexError, TypeError, ValueError):
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
# OBV CALCULATION
# ============================================================

def calculate_obv(candles):
    if not candles:
        return []

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

        candles = get_completed_daily_candles(candles)

        minimum_required = OBV_LOOKBACK + 1

        if len(candles) < minimum_required:
            return None

        obv = calculate_obv(candles)

        if len(obv) < minimum_required:
            return None

        # Latest completed Daily candle
        current_index = len(obv) - 1

        current_obv = obv[current_index]

        # Previous 20 completed Daily candles,
        # excluding the current breakout candle.
        previous_obv_values = obv[
            current_index - OBV_LOOKBACK:current_index
        ]

        if len(previous_obv_values) != OBV_LOOKBACK:
            return None

        previous_high = max(previous_obv_values)

        # ====================================================
        # THE ONLY SIGNAL CONDITION
        # ====================================================

        if current_obv <= previous_high:
            return None

        signal_candle = candles[current_index]

        signal_timestamp = signal_candle["timestamp"]

        signal_key = (
            f"{contract}|DAILY|OBV_BREAKOUT|{signal_timestamp}"
        )

        breakout_amount = current_obv - previous_high

        if previous_high != 0:
            breakout_percent = (
                breakout_amount / abs(previous_high)
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

def send_telegram(message, chat_id):
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN is missing.")
        return False

    if not chat_id:
        return False

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    try:
        response = session.post(
            url,
            data={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=20
        )

        if response.ok:
            return True

        print(
            f"Telegram error {response.status_code}: "
            f"{response.text}"
        )

    except Exception as e:
        print(f"Telegram send error: {e}")

    return False


def send_message_to_all(message):
    sent = False

    if TELEGRAM_CHAT_ID:
        if send_telegram(message, TELEGRAM_CHAT_ID):
            sent = True

    if TELEGRAM_CHAT_ID_2:
        if send_telegram(message, TELEGRAM_CHAT_ID_2):
            sent = True

    return sent


# ============================================================
# FORMAT SIGNAL
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
        f"OBV: <b>{signal['current_obv']:,.2f}</b>\n"
        f"Previous 20 OBV High: "
        f"<b>{signal['previous_high']:,.2f}</b>\n"
        f"Breakout Amount: "
        f"<b>{signal['breakout_amount']:,.2f}</b>\n"
        f"Breakout vs High: "
        f"<b>{signal['breakout_percent']:.2f}%</b>\n"
        "\n"
        f"📅 Candle: <b>{dt.strftime('%Y-%m-%d')}</b>\n"
        "\n"
        "⚠️ <i>OBV-only signal. "
        "No price/EMA/SMA/RSI/BOS condition.</i>"
    )


# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 60)
    print("DAILY OBV BREAKOUT BOT")
    print("=" * 60)

    print(f"Timeframe: DAILY")
    print(f"OBV Lookback: {OBV_LOOKBACK} completed candles")
    print(f"Max workers: {MAX_WORKERS}")
    print()

    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN is not configured.")
        return

    if not TELEGRAM_CHAT_ID:
        print("ERROR: TELEGRAM_CHAT_ID is not configured.")
        return

    history = load_history()

    # --------------------------------------------------------
    # GET CONTRACTS
    # --------------------------------------------------------

    try:
        contracts = get_contracts()
    except Exception as e:
        print(f"Failed to get contracts: {e}")
        return

    print(f"USDT contracts found: {len(contracts)}")
    print("Scanning...")
    print()

    signals = []

    # --------------------------------------------------------
    # PARALLEL SCAN
    # --------------------------------------------------------

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        futures = {
            executor.submit(check_signal, contract): contract
            for contract in contracts
        }

        for future in as_completed(futures):
            contract = futures[future]

            try:
                result = future.result()

                if result:
                    signals.append(result)

            except Exception as e:
                print(f"{contract}: worker error - {e}")

    print()
    print(f"OBV breakouts found: {len(signals)}")

    # --------------------------------------------------------
    # SORT STRONGEST BREAKOUT FIRST
    # --------------------------------------------------------

    signals.sort(
        key=lambda x: x["breakout_amount"],
        reverse=True
    )

    new_signals = []

    # --------------------------------------------------------
    # DUPLICATE PROTECTION
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # SEND NEW SIGNALS
    # --------------------------------------------------------

    if new_signals:

        print()
        print(
            f"NEW OBV BREAKOUTS: {len(new_signals)}"
        )

        for signal in new_signals:

            message = format_signal(signal)

            print(
                f"NEW SIGNAL: {signal['contract']} "
                f"{signal['breakout_percent']:.2f}%"
            )

            send_message_to_all(message)

            # Small delay between Telegram messages
            time.sleep(0.5)

    else:
        print()
        print("NO NEW OBV BREAKOUT")

        report = (
            "📊 <b>DAILY OBV SCAN</b>\n"
            "\n"
            "No new OBV breakout found.\n"
            "\n"
            f"🪙 Contracts scanned: "
            f"<b>{len(contracts)}</b>\n"
            f"📊 OBV lookback: "
            f"<b>{OBV_LOOKBACK} Daily candles</b>\n"
            "⏱ Timeframe: <b>DAILY</b>\n"
            "\n"
            "Condition:\n"
            "Latest completed Daily OBV must break "
            "above the highest OBV of the previous "
            f"{OBV_LOOKBACK} completed Daily candles."
        )

        send_message_to_all(report)

    # --------------------------------------------------------
    # SAVE HISTORY
    # --------------------------------------------------------

    save_history(history)

    print()
    print("=" * 60)
    print("SCAN COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
