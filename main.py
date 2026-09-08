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

# Number of candles on each side used to confirm
# an OBV swing high.
#
# Example:
# PIVOT_LEFT = 2
# PIVOT_RIGHT = 2
#
# A candle must have a higher OBV than the
# 2 candles before it AND the 2 candles after it.
PIVOT_LEFT = 2
PIVOT_RIGHT = 2

# Number of Daily candles requested.
CANDLE_LIMIT = 100

# Keep workers low to avoid Gate API 429 errors.
MAX_WORKERS = 4

# Minimum delay between Gate API requests.
REQUEST_DELAY = 0.08

# Maximum retries for HTTP 429/network errors.
MAX_RETRIES = 5


# ============================================================
# TELEGRAM
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN"
)

TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID"
)


# ============================================================
# SIGNAL HISTORY
# ============================================================

HISTORY_FILE = "signals.json"


# ============================================================
# HTTP SESSION
# ============================================================

HEADERS = {
    "User-Agent": "Daily-OBV-Higher-High-Bot/1.0"
}

session = requests.Session()
session.headers.update(HEADERS)

request_lock = threading.Lock()
last_request_time = 0.0


# ============================================================
# RATE-LIMITED GATE REQUEST
# ============================================================

def gate_get(url, params=None, timeout=20):

    global last_request_time

    for attempt in range(MAX_RETRIES):

        with request_lock:

            now = time.monotonic()

            elapsed = (
                now - last_request_time
            )

            if elapsed < REQUEST_DELAY:

                time.sleep(
                    REQUEST_DELAY - elapsed
                )

            last_request_time = (
                time.monotonic()
            )

        try:

            response = session.get(
                url,
                params=params,
                timeout=timeout
            )

        except requests.RequestException as e:

            if attempt < MAX_RETRIES - 1:

                wait_time = 2 ** attempt

                print(
                    f"Network error: {e}"
                )

                print(
                    f"Retrying in {wait_time}s..."
                )

                time.sleep(
                    wait_time
                )

                continue

            raise

        if response.ok:

            return response

        if response.status_code == 429:

            retry_after = (
                response.headers.get(
                    "Retry-After"
                )
            )

            if retry_after:

                try:

                    wait_time = float(
                        retry_after
                    )

                except ValueError:

                    wait_time = 3.0

            else:

                wait_time = 2 ** attempt

            wait_time += 0.5

            print(
                f"Gate rate limit (429). "
                f"Retry {attempt + 1}/"
                f"{MAX_RETRIES} "
                f"after {wait_time:.1f}s"
            )

            time.sleep(
                wait_time
            )

            continue

        response.raise_for_status()

    raise RuntimeError(
        "Gate API rate limit retries exhausted."
    )


# ============================================================
# HISTORY
# ============================================================

def load_history():

    if not os.path.exists(
        HISTORY_FILE
    ):

        return {}

    try:

        with open(
            HISTORY_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

        if isinstance(
            data,
            dict
        ):

            return data

    except Exception as e:

        print(
            f"History load error: {e}"
        )

    return {}


def save_history(history):

    temp_file = (
        HISTORY_FILE + ".tmp"
    )

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
# GET GATE USDT FUTURES CONTRACTS
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

    if not isinstance(
        contracts,
        list
    ):

        return []

    result = []

    for contract in contracts:

        name = contract.get(
            "name",
            ""
        )

        status = contract.get(
            "status",
            ""
        )

        if not name.endswith(
            "_USDT"
        ):

            continue

        if status != "trading":

            continue

        result.append(
            name
        )

    return sorted(
        set(result)
    )


# ============================================================
# GET DAILY CANDLES
# ============================================================

def get_daily_candles(
    contract
):

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

    if not isinstance(
        candles,
        list
    ):

        return []

    parsed = []

    for candle in candles:

        try:

            timestamp = int(
                candle["t"]
            )

            volume = float(
                candle["v"]
            )

            close = float(
                candle["c"]
            )

            parsed.append({
                "timestamp": timestamp,
                "volume": volume,
                "close": close
            })

        except (
            KeyError,
            TypeError,
            ValueError
        ):

            continue

    parsed.sort(
        key=lambda x:
        x["timestamp"]
    )

    return parsed


# ============================================================
# REMOVE CURRENT FORMING DAILY CANDLE
# ============================================================

def get_completed_daily_candles(
    candles
):

    now = int(
        time.time()
    )

    completed = []

    for candle in candles:

        candle_end = (
            candle["timestamp"]
            + 86400
        )

        if candle_end <= now:

            completed.append(
                candle
            )

    return completed


# ============================================================
# CALCULATE CLASSIC OBV
# ============================================================

def calculate_obv(
    candles
):

    if not candles:

        return []

    obv_values = [
        0.0
    ]

    for i in range(
        1,
        len(candles)
    ):

        previous_close = (
            candles[i - 1]["close"]
        )

        current_close = (
            candles[i]["close"]
        )

        current_volume = (
            candles[i]["volume"]
        )

        previous_obv = (
            obv_values[-1]
        )

        if current_close > previous_close:

            current_obv = (
                previous_obv
                + current_volume
            )

        elif current_close < previous_close:

            current_obv = (
                previous_obv
                - current_volume
            )

        else:

            current_obv = (
                previous_obv
            )

        obv_values.append(
            current_obv
        )

    return obv_values


# ============================================================
# FIND CONFIRMED OBV SWING HIGHS
# ============================================================

def find_obv_swing_highs(
    obv
):

    swing_highs = []

    start = PIVOT_LEFT

    end = (
        len(obv)
        - PIVOT_RIGHT
    )

    for i in range(
        start,
        end
    ):

        current_obv = obv[i]

        is_swing_high = True

        # ----------------------------------------------------
        # Check candles to the LEFT
        # ----------------------------------------------------

        for j in range(
            i - PIVOT_LEFT,
            i
        ):

            if current_obv <= obv[j]:

                is_swing_high = False

                break

        if not is_swing_high:

            continue

        # ----------------------------------------------------
        # Check candles to the RIGHT
        # ----------------------------------------------------

        for j in range(
            i + 1,
            i + PIVOT_RIGHT + 1
        ):

            if current_obv <= obv[j]:

                is_swing_high = False

                break

        if not is_swing_high:

            continue

        swing_highs.append(
            i
        )

    return swing_highs


# ============================================================
# CHECK STRICT OBV HIGHER-HIGH
# ============================================================

def check_signal(
    contract
):

    try:

        candles = get_daily_candles(
            contract
        )

        if not candles:

            return None

        # Remove current forming Daily candle.
        candles = (
            get_completed_daily_candles(
                candles
            )
        )

        minimum_required = (
            PIVOT_LEFT
            + PIVOT_RIGHT
            + 10
        )

        if len(candles) < minimum_required:

            return None

        # Calculate OBV.
        obv = calculate_obv(
            candles
        )

        if not obv:

            return None

        # Find confirmed OBV swing highs.
        swing_highs = (
            find_obv_swing_highs(
                obv
            )
        )

        # Need at least two swing highs:
        #
        # Previous High
        # +
        # Latest High
        #
        if len(swing_highs) < 2:

            return None

        # ----------------------------------------------------
        # IMPORTANT:
        #
        # Because PIVOT_RIGHT = 2, the newest confirmed
        # swing high can only be 2 completed candles old.
        #
        # This prevents using an unconfirmed/current high.
        # ----------------------------------------------------

        latest_index = (
            swing_highs[-1]
        )

        previous_index = (
            swing_highs[-2]
        )

        latest_high = (
            obv[latest_index]
        )

        previous_high = (
            obv[previous_index]
        )

        # ====================================================
        # STRICT HIGHER-HIGH CONDITION
        # ====================================================

        if latest_high <= previous_high:

            return None

        # ----------------------------------------------------
        # Confirm OBV was actually rising into the
        # latest swing high.
        #
        # The candle immediately before the swing high
        # must have lower OBV.
        # ----------------------------------------------------

        if latest_index <= 0:

            return None

        if obv[latest_index] <= obv[
            latest_index - 1
        ]:

            return None

        # ----------------------------------------------------
        # SIGNAL INFORMATION
        # ----------------------------------------------------

        signal_timestamp = (
            candles[latest_index]["timestamp"]
        )

        signal_key = (
            f"{contract}"
            f"|DAILY"
            f"|OBV_HIGHER_HIGH"
            f"|{signal_timestamp}"
        )

        higher_high_amount = (
            latest_high
            - previous_high
        )

        if previous_high != 0:

            higher_high_percent = (
                higher_high_amount
                / abs(previous_high)
            ) * 100

        else:

            higher_high_percent = 0.0

        return {
            "contract": contract,
            "timestamp": signal_timestamp,
            "signal_key": signal_key,

            "current_obv": latest_high,

            "previous_high": previous_high,

            "higher_high_amount":
                higher_high_amount,

            "higher_high_percent":
                higher_high_percent
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

def send_telegram(
    message
):

    if not TELEGRAM_BOT_TOKEN:

        print(
            "ERROR: "
            "TELEGRAM_BOT_TOKEN is missing."
        )

        return False

    if not TELEGRAM_CHAT_ID:

        print(
            "ERROR: "
            "TELEGRAM_CHAT_ID is missing."
        )

        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    try:

        response = session.post(
            url,
            data={
                "chat_id":
                    TELEGRAM_CHAT_ID,

                "text":
                    message,

                "parse_mode":
                    "HTML",

                "disable_web_page_preview":
                    True
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

def format_signal(
    signal
):

    dt = datetime.fromtimestamp(
        signal["timestamp"],
        tz=timezone.utc
    )

    return (
        "📈 <b>DAILY OBV HIGHER HIGH</b>\n"
        "\n"
        f"🪙 <b>{signal['contract']}</b>\n"
        "📊 Signal: <b>OBV HIGHER HIGH</b>\n"
        "⏱ Timeframe: <b>DAILY</b>\n"
        "\n"
        f"Latest OBV High: "
        f"<b>{signal['current_obv']:,.2f}</b>\n"
        f"Previous OBV High: "
        f"<b>{signal['previous_high']:,.2f}</b>\n"
        f"Higher High Amount: "
        f"<b>{signal['higher_high_amount']:,.2f}</b>\n"
        f"Higher High: "
        f"<b>{signal['higher_high_percent']:.2f}%</b>\n"
        "\n"
        f"📅 Swing High Candle: "
        f"<b>{dt.strftime('%Y-%m-%d')}</b>\n"
        "\n"
        "📈 <i>OBV uptrend / higher-high signal.</i>\n"
        "<i>No price, EMA, SMA, RSI, BOS "
        "or divergence condition.</i>"
    )


# ============================================================
# NO SIGNAL REPORT
# ============================================================

def format_no_signal_report(
    contract_count
):

    return (
        "📊 <b>DAILY OBV SCAN</b>\n"
        "\n"
        "No new OBV higher-high signal found.\n"
        "\n"
        f"🪙 Contracts scanned: "
        f"<b>{contract_count}</b>\n"
        "⏱ Timeframe: <b>DAILY</b>\n"
        "📈 Structure: "
        "<b>OBV Higher High</b>\n"
        "\n"
        "<b>Signal condition:</b>\n"
        "The latest confirmed Daily OBV swing high "
        "must be strictly higher than the previous "
        "confirmed Daily OBV swing high.\n"
        "\n"
        "No price condition is used."
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 60)
    print("DAILY OBV HIGHER-HIGH BOT")
    print("=" * 60)

    print(
        "Timeframe: DAILY"
    )

    print(
        "Structure: "
        "OBV HIGHER HIGH"
    )

    print(
        f"Pivot left: "
        f"{PIVOT_LEFT}"
    )

    print(
        f"Pivot right: "
        f"{PIVOT_RIGHT}"
    )

    print(
        f"Candle limit: "
        f"{CANDLE_LIMIT}"
    )

    print(
        f"Max workers: "
        f"{MAX_WORKERS}"
    )

    print(
        f"Request delay: "
        f"{REQUEST_DELAY}s"
    )

    print()

    # --------------------------------------------------------
    # TELEGRAM CONFIG
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

    print(
        "Telegram configuration: OK"
    )

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
            f"Failed to get Gate contracts: "
            f"{e}"
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

        for future in as_completed(
            futures
        ):

            contract = futures[
                future
            ]

            try:

                result = future.result()

                completed_count += 1

                if result:

                    signals.append(
                        result
                    )

            except Exception as e:

                completed_count += 1

                print(
                    f"{contract}: "
                    f"worker error - {e}"
                )

            if (
                completed_count % 100 == 0
                or completed_count ==
                    len(contracts)
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
        f"OBV higher highs found: "
        f"{len(signals)}"
    )

    signals.sort(
        key=lambda x:
        x["higher_high_amount"],
        reverse=True
    )

    # --------------------------------------------------------
    # DUPLICATE PROTECTION
    # --------------------------------------------------------

    new_signals = []

    for signal in signals:

        key = signal[
            "signal_key"
        ]

        if key in history:

            continue

        new_signals.append(
            signal
        )

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

            message = format_signal(
                signal
            )

            print(
                f"NEW SIGNAL: "
                f"{signal['contract']} | "
                f"Higher High: "
                f"{signal['higher_high_percent']:.2f}%"
            )

            success = send_telegram(
                message
            )

            if success:

                print(
                    f"Telegram sent: "
                    f"{signal['contract']}"
                )

                # Save only after Telegram
                # successfully sends.
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
            "NO NEW OBV HIGHER HIGH"
        )

        report = (
            format_no_signal_report(
                len(contracts)
            )
        )

        success = send_telegram(
            report
        )

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

    save_history(
        history
    )

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
