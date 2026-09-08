import os
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests


# ============================================================
# SETTINGS
# ============================================================

GATE_URL = "https://api.gateio.ws/api/v4"

TIMEFRAME = "1d"

# OBV swing confirmation
PIVOT_LEFT = 2
PIVOT_RIGHT = 2

CANDLE_LIMIT = 100

MAX_WORKERS = 4
REQUEST_DELAY = 0.08
MAX_RETRIES = 5

HISTORY_FILE = "signals.json"


# ============================================================
# TELEGRAM
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


# ============================================================
# HTTP
# ============================================================

HEADERS = {
    "User-Agent": "Daily-OBV-HH-HL-HH-Bot/1.0"
}

session = requests.Session()
session.headers.update(HEADERS)

request_lock = threading.Lock()
last_request_time = 0.0


# ============================================================
# GATE REQUEST
# ============================================================

def gate_get(url, params=None, timeout=20):

    global last_request_time

    for attempt in range(MAX_RETRIES):

        with request_lock:

            now = time.monotonic()
            elapsed = now - last_request_time

            if elapsed < REQUEST_DELAY:
                time.sleep(REQUEST_DELAY - elapsed)

            last_request_time = time.monotonic()

        try:

            response = session.get(
                url,
                params=params,
                timeout=timeout
            )

        except requests.RequestException as e:

            if attempt < MAX_RETRIES - 1:

                wait_time = 2 ** attempt

                print(f"Network error: {e}")
                print(f"Retrying in {wait_time}s...")

                time.sleep(wait_time)
                continue

            raise

        if response.ok:
            return response

        if response.status_code == 429:

            retry_after = response.headers.get("Retry-After")

            if retry_after:

                try:
                    wait_time = float(retry_after)
                except ValueError:
                    wait_time = 3.0

            else:
                wait_time = 2 ** attempt

            wait_time += 0.5

            print(
                f"Gate rate limit (429). "
                f"Retry {attempt + 1}/{MAX_RETRIES} "
                f"after {wait_time:.1f}s"
            )

            time.sleep(wait_time)
            continue

        response.raise_for_status()

    raise RuntimeError(
        "Gate API retries exhausted."
    )


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

    except Exception as e:

        print(f"History load error: {e}")

    return {}


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
            indent=2
        )

    os.replace(
        temp_file,
        HISTORY_FILE
    )


# ============================================================
# GET USDT FUTURES CONTRACTS
# ============================================================

def get_contracts():

    url = (
        f"{GATE_URL}"
        f"/futures/usdt/contracts"
    )

    response = gate_get(
        url,
        timeout=20
    )

    contracts = response.json()

    if not isinstance(contracts, list):
        return []

    result = []

    for contract in contracts:

        name = contract.get("name", "")
        status = contract.get("status", "")

        if not name.endswith("_USDT"):
            continue

        if status != "trading":
            continue

        result.append(name)

    return sorted(set(result))


# ============================================================
# GET DAILY CANDLES
# ============================================================

def get_daily_candles(contract):

    url = (
        f"{GATE_URL}"
        f"/futures/usdt/candlesticks"
    )

    response = gate_get(
        url,
        params={
            "contract": contract,
            "interval": TIMEFRAME,
            "limit": CANDLE_LIMIT
        },
        timeout=20
    )

    candles = response.json()

    if not isinstance(candles, list):
        return []

    parsed = []

    for candle in candles:

        try:

            parsed.append({
                "timestamp": int(candle["t"]),
                "volume": float(candle["v"]),
                "close": float(candle["c"])
            })

        except (
            KeyError,
            TypeError,
            ValueError
        ):

            continue

    parsed.sort(
        key=lambda x: x["timestamp"]
    )

    return parsed


# ============================================================
# REMOVE CURRENT FORMING DAILY CANDLE
# ============================================================

def get_completed_daily_candles(candles):

    now = int(time.time())

    completed = []

    for candle in candles:

        candle_end = (
            candle["timestamp"] + 86400
        )

        if candle_end <= now:
            completed.append(candle)

    return completed


# ============================================================
# CLASSIC OBV
# ============================================================

def calculate_obv(candles):

    if not candles:
        return []

    obv = [0.0]

    for i in range(1, len(candles)):

        previous_close = candles[i - 1]["close"]
        current_close = candles[i]["close"]
        volume = candles[i]["volume"]

        previous_obv = obv[-1]

        if current_close > previous_close:

            current_obv = (
                previous_obv + volume
            )

        elif current_close < previous_close:

            current_obv = (
                previous_obv - volume
            )

        else:

            current_obv = previous_obv

        obv.append(current_obv)

    return obv


# ============================================================
# FIND OBV SWING HIGHS
# ============================================================

def find_swing_highs(obv):

    highs = []

    start = PIVOT_LEFT
    end = len(obv) - PIVOT_RIGHT

    for i in range(start, end):

        current = obv[i]

        is_high = True

        # Left side
        for j in range(
            i - PIVOT_LEFT,
            i
        ):

            if current <= obv[j]:
                is_high = False
                break

        if not is_high:
            continue

        # Right side
        for j in range(
            i + 1,
            i + PIVOT_RIGHT + 1
        ):

            if current <= obv[j]:
                is_high = False
                break

        if is_high:
            highs.append(i)

    return highs


# ============================================================
# FIND OBV SWING LOWS
# ============================================================

def find_swing_lows(obv):

    lows = []

    start = PIVOT_LEFT
    end = len(obv) - PIVOT_RIGHT

    for i in range(start, end):

        current = obv[i]

        is_low = True

        # Left side
        for j in range(
            i - PIVOT_LEFT,
            i
        ):

            if current >= obv[j]:
                is_low = False
                break

        if not is_low:
            continue

        # Right side
        for j in range(
            i + 1,
            i + PIVOT_RIGHT + 1
        ):

            if current >= obv[j]:
                is_low = False
                break

        if is_low:
            lows.append(i)

    return lows


# ============================================================
# CHECK STRICT HH -> HL -> HH
# ============================================================

def check_signal(contract):

    try:

        candles = get_daily_candles(contract)

        if not candles:
            return None

        # Only completed Daily candles
        candles = get_completed_daily_candles(
            candles
        )

        minimum_required = (
            PIVOT_LEFT
            + PIVOT_RIGHT
            + 15
        )

        if len(candles) < minimum_required:
            return None

        # ----------------------------------------------------
        # OBV
        # ----------------------------------------------------

        obv = calculate_obv(candles)

        if not obv:
            return None

        # ----------------------------------------------------
        # SWING POINTS
        # ----------------------------------------------------

        swing_highs = find_swing_highs(obv)
        swing_lows = find_swing_lows(obv)

        if len(swing_highs) < 2:
            return None

        if len(swing_lows) < 2:
            return None

        # ----------------------------------------------------
        # LATEST TWO CONFIRMED SWING HIGHS
        # ----------------------------------------------------

        hh1_index = swing_highs[-2]
        hh2_index = swing_highs[-1]

        hh1_value = obv[hh1_index]
        hh2_value = obv[hh2_index]

        # HH2 must be HIGHER than HH1
        if hh2_value <= hh1_value:
            return None

        # ----------------------------------------------------
        # FIND THE SWING LOW BETWEEN HH1 AND HH2
        # ----------------------------------------------------

        lows_between = [
            i
            for i in swing_lows
            if hh1_index < i < hh2_index
        ]

        if not lows_between:
            return None

        # Most recent confirmed low between HH1 and HH2
        hl1_index = lows_between[-1]

        hl1_value = obv[hl1_index]

        # ----------------------------------------------------
        # FIND THE PREVIOUS SWING LOW
        # ----------------------------------------------------

        previous_lows = [
            i
            for i in swing_lows
            if i < hl1_index
        ]

        if not previous_lows:
            return None

        previous_low_index = previous_lows[-1]

        previous_low_value = obv[
            previous_low_index
        ]

        # ----------------------------------------------------
        # HL CONDITION
        # ----------------------------------------------------

        # HL1 must be HIGHER than previous swing low
        if hl1_value <= previous_low_value:
            return None

        # ----------------------------------------------------
        # ORDER CHECK
        # ----------------------------------------------------

        # Must be exactly in chronological structure:
        #
        # HH1 -> HL1 -> HH2
        #
        if not (
            hh1_index
            < hl1_index
            < hh2_index
        ):
            return None

        # ----------------------------------------------------
        # SIGNAL
        # ----------------------------------------------------

        signal_timestamp = candles[
            hh2_index
        ]["timestamp"]

        signal_key = (
            f"{contract}"
            f"|DAILY"
            f"|OBV_HH_HL_HH"
            f"|{signal_timestamp}"
        )

        return {
            "contract": contract,

            "timestamp": signal_timestamp,

            "signal_key": signal_key,

            "hh1_index": hh1_index,
            "hl1_index": hl1_index,
            "hh2_index": hh2_index,

            "hh1": hh1_value,
            "hl1": hl1_value,
            "hh2": hh2_value,

            "previous_low":
                previous_low_value
        }

    except Exception as e:

        if "429" in str(e):

            print(
                f"{contract}: RATE LIMITED"
            )

        else:

            print(
                f"{contract}: ERROR - {e}"
            )

        return None


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    if not TELEGRAM_BOT_TOKEN:

        print(
            "ERROR: TELEGRAM_BOT_TOKEN missing."
        )

        return False

    if not TELEGRAM_CHAT_ID:

        print(
            "ERROR: TELEGRAM_CHAT_ID missing."
        )

        return False

    url = (
        "https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
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

        print(
            f"Telegram send error: {e}"
        )

    return False


# ============================================================
# FORMAT SIGNAL
# ============================================================

def format_signal(signal):

    dt = datetime.fromtimestamp(
        signal["timestamp"],
        tz=timezone.utc
    )

    hh1_date = datetime.fromtimestamp(
        signal["timestamp"]
        - (
            signal["hh2_index"]
            - signal["hh1_index"]
        ) * 86400,
        tz=timezone.utc
    )

    hl1_date = datetime.fromtimestamp(
        signal["timestamp"]
        - (
            signal["hh2_index"]
            - signal["hl1_index"]
        ) * 86400,
        tz=timezone.utc
    )

    return (
        "📈 <b>DAILY OBV UPTREND</b>\n"
        "\n"
        f"🪙 <b>{signal['contract']}</b>\n"
        "⏱ Timeframe: <b>DAILY</b>\n"
        "\n"
        "📐 <b>OBV STRUCTURE</b>\n"
        "\n"
        f"🔺 HH1: "
        f"<b>{signal['hh1']:,.2f}</b>\n"
        f"📅 {hh1_date.strftime('%Y-%m-%d')}\n"
        "\n"
        f"🔻 HL1: "
        f"<b>{signal['hl1']:,.2f}</b>\n"
        f"📅 {hl1_date.strftime('%Y-%m-%d')}\n"
        "\n"
        f"🔺 HH2: "
        f"<b>{signal['hh2']:,.2f}</b>\n"
        f"📅 {dt.strftime('%Y-%m-%d')}\n"
        "\n"
        "✅ <b>HH → HL → HH</b>\n"
        "\n"
        "OBV Higher High + Higher Low "
        "uptrend structure confirmed.\n"
        "\n"
        "<i>No price, EMA, SMA, RSI, BOS "
        "or divergence condition.</i>"
    )


# ============================================================
# NO SIGNAL REPORT
# ============================================================

def format_no_signal_report(contract_count):

    return (
        "📊 <b>DAILY OBV SCAN</b>\n"
        "\n"
        "No new OBV HH → HL → HH signal found.\n"
        "\n"
        f"🪙 Contracts scanned: "
        f"<b>{contract_count}</b>\n"
        "⏱ Timeframe: <b>DAILY</b>\n"
        "\n"
        "<b>Required structure:</b>\n"
        "🔺 Higher High\n"
        "🔻 Higher Low\n"
        "🔺 Higher High\n"
        "\n"
        "No price condition is used."
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 60)
    print("DAILY OBV HH -> HL -> HH BOT")
    print("=" * 60)

    print("Timeframe: DAILY")
    print("Structure: OBV HIGHER HIGH -> HIGHER LOW -> HIGHER HIGH")
    print(f"Pivot left: {PIVOT_LEFT}")
    print(f"Pivot right: {PIVOT_RIGHT}")
    print(f"Candle limit: {CANDLE_LIMIT}")
    print(f"Max workers: {MAX_WORKERS}")
    print(f"Request delay: {REQUEST_DELAY}s")
    print()

    # --------------------------------------------------------
    # TELEGRAM
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
    # HISTORY
    # --------------------------------------------------------

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
            f"Failed to get Gate contracts: {e}"
        )

        return

    print(
        f"USDT contracts found: "
        f"{len(contracts)}"
    )

    if not contracts:

        print(
            "ERROR: No USDT futures "
            "contracts found."
        )

        return

    print()
    print("Scanning...")
    print()

    # --------------------------------------------------------
    # SCAN
    # --------------------------------------------------------

    signals = []
    completed_count = 0

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

                completed_count += 1

                if result:
                    signals.append(result)

            except Exception as e:

                completed_count += 1

                print(
                    f"{contract}: "
                    f"worker error - {e}"
                )

            if (
                completed_count % 100 == 0
                or completed_count == len(contracts)
            ):

                print(
                    f"Progress: "
                    f"{completed_count}/"
                    f"{len(contracts)}"
                )

    # --------------------------------------------------------
    # RESULTS
    # --------------------------------------------------------

    print()

    print(
        f"OBV HH -> HL -> HH found: "
        f"{len(signals)}"
    )

    # Sort by latest HH value
    signals.sort(
        key=lambda x: x["hh2"],
        reverse=True
    )

    # --------------------------------------------------------
    # DUPLICATE PROTECTION
    # --------------------------------------------------------

    new_signals = []

    for signal in signals:

        if signal["signal_key"] not in history:

            new_signals.append(signal)

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

            print(
                f"NEW SIGNAL: "
                f"{signal['contract']} | "
                f"HH -> HL -> HH"
            )

            message = format_signal(
                signal
            )

            success = send_telegram(
                message
            )

            if success:

                print(
                    f"Telegram sent: "
                    f"{signal['contract']}"
                )

                history[
                    signal["signal_key"]
                ] = {
                    "contract":
                        signal["contract"],

                    "timestamp":
                        signal["timestamp"],

                    "created_at":
                        datetime.now(
                            timezone.utc
                        ).isoformat()
                }

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
        print(
            "NO NEW OBV HH -> HL -> HH"
        )

        report = format_no_signal_report(
            len(contracts)
        )

        success = send_telegram(report)

        if success:

            print(
                "Scan report sent to Telegram."
            )

        else:

            print(
                "Scan report failed to send."
            )

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
# START
# ============================================================

if __name__ == "__main__":
    main()
