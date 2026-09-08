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

# STRICT OBV BREAKOUT:
# Latest completed Daily OBV must be greater than
# the highest OBV of the previous 20 completed Daily candles.
OBV_LOOKBACK = 20

# 20 previous candles + latest candle + extra history.
CANDLE_LIMIT = 100

# Keep low to avoid Gate API 429 errors.
MAX_WORKERS = 4

# Minimum delay between Gate API requests.
REQUEST_DELAY = 0.08

# Maximum retries for rate limits/network errors.
MAX_RETRIES = 5


# ============================================================
# TELEGRAM
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


# ============================================================
# SIGNAL HISTORY
# ============================================================

HISTORY_FILE = "signals.json"


# ============================================================
# HTTP SESSION
# ============================================================

HEADERS = {
    "User-Agent": "Daily-OBV-Breakout-Bot/4.0"
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

        # ----------------------------------------------------
        # Control request speed
        # ----------------------------------------------------

        with request_lock:

            now = time.monotonic()

            elapsed = now - last_request_time

            if elapsed < REQUEST_DELAY:

                time.sleep(
                    REQUEST_DELAY - elapsed
                )

            last_request_time = time.monotonic()

        # ----------------------------------------------------
        # Send request
        # ----------------------------------------------------

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

                time.sleep(wait_time)

                continue

            raise

        # ----------------------------------------------------
        # Successful response
        # ----------------------------------------------------

        if response.ok:

            return response

        # ----------------------------------------------------
        # Gate API rate limit
        # ----------------------------------------------------

        if response.status_code == 429:

            retry_after = response.headers.get(
                "Retry-After"
            )

            if retry_after:

                try:

                    wait_time = float(
                        retry_after
                    )

                except ValueError:

                    wait_time = 3.0

            else:

                # Progressive backoff:
                # 2s, 4s, 8s, 16s, 32s
                wait_time = 2 ** attempt

            wait_time += 0.5

            print(
                f"Gate rate limit (429). "
                f"Retry {attempt + 1}/{MAX_RETRIES} "
                f"after {wait_time:.1f}s"
            )

            time.sleep(wait_time)

            continue

        # ----------------------------------------------------
        # Other HTTP errors
        # ----------------------------------------------------

        response.raise_for_status()

    raise RuntimeError(
        "Gate API rate limit retries exhausted."
    )


# ============================================================
# LOAD SIGNAL HISTORY
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


# ============================================================
# SAVE SIGNAL HISTORY
# ============================================================

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

        # Only USDT contracts.
        if not name.endswith(
            "_USDT"
        ):

            continue

        # Only active trading contracts.
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
# REMOVE CURRENTLY FORMING DAILY CANDLE
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

    # Starting OBV value.
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
# CHECK ONE CONTRACT
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

        # Need:
        # previous 20 completed candles
        # +
        # latest completed candle.
        minimum_required = (
            OBV_LOOKBACK + 1
        )

        if len(candles) < minimum_required:

            return None

        # Calculate OBV.
        obv = calculate_obv(
            candles
        )

        if len(obv) < minimum_required:

            return None

        # ----------------------------------------------------
        # LATEST COMPLETED DAILY CANDLE
        # ----------------------------------------------------

        current_index = (
            len(obv) - 1
        )

        current_obv = (
            obv[current_index]
        )

        # ----------------------------------------------------
        # PREVIOUS 20 OBV VALUES
        #
        # The latest candle is NOT included.
        # ----------------------------------------------------

        previous_obv_values = obv[
            current_index - OBV_LOOKBACK:
            current_index
        ]

        if len(
            previous_obv_values
        ) != OBV_LOOKBACK:

            return None

        previous_high = max(
            previous_obv_values
        )

        # ====================================================
        # ONLY SIGNAL CONDITION
        #
        # Latest completed Daily OBV must be STRICTLY
        # greater than the highest OBV of the previous
        # 20 completed Daily candles.
        #
        # NO PRICE CONDITION.
        # NO EMA.
        # NO SMA.
        # NO RSI.
        # NO BOS.
        # NO DIVERGENCE.
        # ====================================================

        if current_obv <= previous_high:

            return None

        # ----------------------------------------------------
        # SIGNAL CANDLE
        # ----------------------------------------------------

        signal_candle = (
            candles[current_index]
        )

        signal_timestamp = (
            signal_candle["timestamp"]
        )

        # Unique signal key.
        signal_key = (
            f"{contract}"
            f"|DAILY"
            f"|OBV_BREAKOUT"
            f"|{signal_timestamp}"
        )

        breakout_amount = (
            current_obv
            - previous_high
        )

        if previous_high != 0:

            breakout_percent = (
                breakout_amount
                / abs(previous_high)
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
# SEND TELEGRAM MESSAGE
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
# FORMAT OBV SIGNAL
# ============================================================

def format_signal(
    signal
):

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
# NO NEW SIGNAL REPORT
# ============================================================

def format_no_signal_report(
    contract_count
):

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

    print(
        "Timeframe: DAILY"
    )

    print(
        f"OBV Lookback: "
        f"{OBV_LOOKBACK} completed candles"
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
    # TELEGRAM CONFIGURATION
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
    # SCAN CONTRACTS
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

            # Show progress every 100 contracts.
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
        f"OBV breakouts found: "
        f"{len(signals)}"
    )

    # Sort by largest absolute OBV breakout.
    signals.sort(
        key=lambda x:
        x["breakout_amount"],
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
                f"OBV breakout: "
                f"{signal['breakout_percent']:.2f}%"
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
            "NO NEW OBV BREAKOUT"
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
