import os
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests


# ============================================================
# DAILY OBV BEAUTIFUL HH -> HL -> HH BOT
# ============================================================

GATE_URL = "https://api.gateio.ws/api/v4"

TIMEFRAME = "1d"

# We only need enough history to identify recent OBV structure.
CANDLE_LIMIT = 100

# Confirmed OBV pivots.
PIVOT_LEFT = 2
PIVOT_RIGHT = 2

# Faster controlled scanning.
MAX_WORKERS = 12
REQUEST_DELAY = 0.03

# Only setups scoring 85 or higher are sent.
BEAUTIFUL_MIN_SCORE = 85

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
    "User-Agent": "Daily-OBV-Beautiful-Bot/2.0"
}

# Prevent requests from being fired at exactly the same moment.
request_lock = threading.Lock()


# ============================================================
# GATE API REQUEST
# ============================================================

def gate_get(url, params=None, retries=5):

    for attempt in range(retries):

        try:

            # Small controlled delay.
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

            # ------------------------------------------------
            # Rate limit.
            # ------------------------------------------------

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

        # Write once at the end of the scan.
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
        "interval": "1d",
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

    # --------------------------------------------------------
    # Remove currently forming Daily candle.
    # --------------------------------------------------------

    now = int(
        time.time()
    )

    completed = []

    for candle in candles:

        candle_start = candle[
            "timestamp"
        ]

        candle_end = (
            candle_start + 86400
        )

        if candle_end <= now:
            completed.append(
                candle
            )

    return completed


# ============================================================
# CLASSIC OBV
# ============================================================

def calculate_obv(candles):

    if not candles:
        return []

    obv = [0.0]

    for i in range(
        1,
        len(candles)
    ):

        previous_close = candles[
            i - 1
        ]["close"]

        current_close = candles[
            i
        ]["close"]

        volume = candles[
            i
        ]["volume"]

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

        obv.append(
            current_obv
        )

    return obv


# ============================================================
# CONFIRMED SWING HIGHS
# ============================================================

def find_swing_highs(values):

    highs = []

    start = PIVOT_LEFT

    end = (
        len(values)
        - PIVOT_RIGHT
    )

    for i in range(
        start,
        end
    ):

        current = values[i]

        left = values[
            i - PIVOT_LEFT:i
        ]

        right = values[
            i + 1:
            i + PIVOT_RIGHT + 1
        ]

        if (
            all(
                current > x
                for x in left
            )
            and
            all(
                current > x
                for x in right
            )
        ):

            highs.append(i)

    return highs


# ============================================================
# CONFIRMED SWING LOWS
# ============================================================

def find_swing_lows(values):

    lows = []

    start = PIVOT_LEFT

    end = (
        len(values)
        - PIVOT_RIGHT
    )

    for i in range(
        start,
        end
    ):

        current = values[i]

        left = values[
            i - PIVOT_LEFT:i
        ]

        right = values[
            i + 1:
            i + PIVOT_RIGHT + 1
        ]

        if (
            all(
                current < x
                for x in left
            )
            and
            all(
                current < x
                for x in right
            )
        ):

            lows.append(i)

    return lows


# ============================================================
# BEAUTIFUL SCORE
# ============================================================

def calculate_beautiful_score(
    obv,
    previous_low_index,
    hh1_index,
    hl_index,
    hh2_index
):
    """
    Objective OBV-only quality score.

    Structure:

        HH1
          \
           HL
             \
              HH2

    Score components:

        40 points = Higher High strength
        35 points = Higher Low strength
        25 points = Final recovery strength

    The calculations use RELATIVE MOVEMENT inside the
    structure, not the absolute cumulative OBV number.

    This avoids the huge / meaningless percentages that
    occurred in the previous version.
    """

    previous_low = obv[
        previous_low_index
    ]

    hh1 = obv[
        hh1_index
    ]

    hl = obv[
        hl_index
    ]

    hh2 = obv[
        hh2_index
    ]

    # --------------------------------------------------------
    # Total structural range.
    # --------------------------------------------------------

    structure_range = (
        hh2 - previous_low
    )

    if structure_range <= 0:
        return 0.0

    # --------------------------------------------------------
    # 1. Higher High strength
    #
    # How much higher HH2 is than HH1.
    # --------------------------------------------------------

    hh_move = (
        hh2 - hh1
    )

    hh_strength = (
        hh_move / structure_range
    )

    # --------------------------------------------------------
    # 2. Higher Low strength
    #
    # How much higher HL is than previous low.
    # --------------------------------------------------------

    hl_move = (
        hl - previous_low
    )

    hl_strength = (
        hl_move / structure_range
    )

    # --------------------------------------------------------
    # 3. Recovery strength
    #
    # How strongly OBV moved from HL back toward HH2.
    # --------------------------------------------------------

    recovery_move = (
        hh2 - hl
    )

    recovery_strength = (
        recovery_move / structure_range
    )

    # --------------------------------------------------------
    # Convert each component to points.
    #
    # Cap each one so extreme OBV values cannot distort
    # the score.
    # --------------------------------------------------------

    hh_points = min(
        1.0,
        max(0.0, hh_strength * 2.5)
    ) * 40

    hl_points = min(
        1.0,
        max(0.0, hl_strength * 2.5)
    ) * 35

    recovery_points = min(
        1.0,
        max(0.0, recovery_strength * 1.5)
    ) * 25

    score = (
        hh_points
        + hl_points
        + recovery_points
    )

    return round(
        max(
            0.0,
            min(
                100.0,
                score
            )
        ),
        2
    )


# ============================================================
# CHECK ONE CONTRACT
# ============================================================

def check_signal(contract):

    candles = get_daily_candles(
        contract
    )

    if len(candles) < 40:
        return None

    obv = calculate_obv(
        candles
    )

    if len(obv) < 40:
        return None

    swing_highs = find_swing_highs(
        obv
    )

    swing_lows = find_swing_lows(
        obv
    )

    if len(swing_highs) < 2:
        return None

    if len(swing_lows) < 2:
        return None

    # --------------------------------------------------------
    # Latest confirmed HH.
    # --------------------------------------------------------

    hh2_index = swing_highs[-1]

    # Previous confirmed HH.
    hh1_index = swing_highs[-2]

    if hh2_index <= hh1_index:
        return None

    # --------------------------------------------------------
    # HH2 must actually be higher than HH1.
    # --------------------------------------------------------

    if obv[hh2_index] <= obv[hh1_index]:
        return None

    # --------------------------------------------------------
    # EXACT structure:
    #
    # HH1 -> HL -> HH2
    #
    # Only ONE confirmed swing low between the two HHs.
    # --------------------------------------------------------

    lows_between = [
        i
        for i in swing_lows
        if hh1_index < i < hh2_index
    ]

    if len(lows_between) != 1:
        return None

    hl_index = lows_between[0]

    # --------------------------------------------------------
    # Previous swing low before HL.
    # --------------------------------------------------------

    previous_lows = [
        i
        for i in swing_lows
        if i < hl_index
    ]

    if not previous_lows:
        return None

    previous_low_index = (
        previous_lows[-1]
    )

    # --------------------------------------------------------
    # HL must be higher than previous low.
    # --------------------------------------------------------

    if (
        obv[hl_index]
        <= obv[previous_low_index]
    ):
        return None

    # --------------------------------------------------------
    # Exact chronological order.
    # --------------------------------------------------------

    if not (
        previous_low_index
        < hh1_index
        < hl_index
        < hh2_index
    ):
        return None

    # --------------------------------------------------------
    # HH2 must be the latest confirmed HH.
    # --------------------------------------------------------

    if hh2_index != swing_highs[-1]:
        return None

    # --------------------------------------------------------
    # BEAUTIFUL SCORE
    # --------------------------------------------------------

    score = calculate_beautiful_score(
        obv,
        previous_low_index,
        hh1_index,
        hl_index,
        hh2_index
    )

    # --------------------------------------------------------
    # Only 85+.
    # --------------------------------------------------------

    if score < BEAUTIFUL_MIN_SCORE:
        return None

    # --------------------------------------------------------
    # Real timestamps.
    # --------------------------------------------------------

    previous_low_timestamp = candles[
        previous_low_index
    ]["timestamp"]

    hh1_timestamp = candles[
        hh1_index
    ]["timestamp"]

    hl_timestamp = candles[
        hl_index
    ]["timestamp"]

    hh2_timestamp = candles[
        hh2_index
    ]["timestamp"]

    # --------------------------------------------------------
    # Unique signal.
    # --------------------------------------------------------

    signal_key = (
        f"{contract}|DAILY|"
        f"OBV_BEAUTIFUL_V2|"
        f"{hh2_timestamp}"
    )

    return {
        "key": signal_key,
        "contract": contract,
        "timeframe": "DAILY",
        "score": score,

        "previous_low": obv[
            previous_low_index
        ],

        "hh1": obv[
            hh1_index
        ],

        "hl": obv[
            hl_index
        ],

        "hh2": obv[
            hh2_index
        ],

        "previous_low_timestamp":
            previous_low_timestamp,

        "hh1_timestamp":
            hh1_timestamp,

        "hl_timestamp":
            hl_timestamp,

        "hh2_timestamp":
            hh2_timestamp
    }


# ============================================================
# DATE FORMAT
# ============================================================

def format_date(timestamp):

    return datetime.fromtimestamp(
        timestamp,
        tz=timezone.utc
    ).strftime("%Y-%m-%d")


# ============================================================
# MAIN
# ============================================================

def main():

    start_time = time.time()

    print("=" * 60)
    print(
        "DAILY OBV BEAUTIFUL "
        "HH -> HL -> HH BOT"
    )
    print("=" * 60)

    print("Timeframe: DAILY")

    print(
        "Structure: "
        "OBV HIGHER HIGH -> "
        "HIGHER LOW -> "
        "HIGHER HIGH"
    )

    print(
        f"Pivot left: {PIVOT_LEFT}"
    )

    print(
        f"Pivot right: {PIVOT_RIGHT}"
    )

    print(
        f"Candle limit: {CANDLE_LIMIT}"
    )

    print(
        f"Beautiful minimum score: "
        f"{BEAUTIFUL_MIN_SCORE}"
    )

    print(
        f"Max workers: {MAX_WORKERS}"
    )

    print(
        f"Request delay: "
        f"{REQUEST_DELAY}s"
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

    # --------------------------------------------------------
    # History
    # --------------------------------------------------------

    history = load_history()

    print(
        f"Previously recorded signals: "
        f"{len(history)}"
    )

    # --------------------------------------------------------
    # Contracts
    # --------------------------------------------------------

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

    total = len(
        contracts
    )

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

                result = future.result()

                if result:
                    results.append(
                        result
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
    # Highest beauty first.
    # --------------------------------------------------------

    results.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    print(
        f"Beautiful OBV setups found: "
        f"{len(results)}"
    )

    # --------------------------------------------------------
    # NEW SIGNALS ONLY
    # --------------------------------------------------------

    new_signals = [
        signal
        for signal in results
        if signal["key"] not in history
    ]

    print(
        f"New beautiful signals: "
        f"{len(new_signals)}"
    )

    # --------------------------------------------------------
    # TELEGRAM
    #
    # ONE MESSAGE
    # ONE COIN PER LINE
    # --------------------------------------------------------

    if new_signals:

        print(
            "Sending ONE beautiful OBV list..."
        )

        lines = [
            "📊 DAILY OBV — BEAUTIFUL SETUPS",
            "",
            "HH → HL → HH",
            ""
        ]

        for number, signal in enumerate(
            new_signals,
            start=1
        ):

            lines.append(
                f"{number}. "
                f"{signal['contract']} "
                f"⭐ {signal['score']:.0f}"
            )

        lines.extend([
            "",
            f"Total: {len(new_signals)}",
            "",
            "OBV ONLY • DAILY • "
            "COMPLETED CANDLES"
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
            "NO NEW BEAUTIFUL "
            "OBV SETUPS"
        )

        report = (
            "📊 DAILY OBV — "
            "BEAUTIFUL SETUPS\n\n"
            "No new beautiful "
            "HH → HL → HH setup.\n\n"
            f"Current beautiful setups: "
            f"{len(results)}\n"
            f"Previously recorded: "
            f"{len(history)}"
        )

        if telegram_ok:

            send_telegram(
                report
            )

    # --------------------------------------------------------
    # SAVE QUALIFYING SIGNALS
    # --------------------------------------------------------

    for signal in results:

        history[
            signal["key"]
        ] = {
            "contract":
                signal["contract"],

            "timeframe":
                signal["timeframe"],

            "score":
                signal["score"],

            "previous_low_timestamp":
                signal[
                    "previous_low_timestamp"
                ],

            "hh1_timestamp":
                signal[
                    "hh1_timestamp"
                ],

            "hl_timestamp":
                signal[
                    "hl_timestamp"
                ],

            "hh2_timestamp":
                signal[
                    "hh2_timestamp"
                ],

            "created_at":
                int(time.time())
        }

    save_history(
        history
    )

    # --------------------------------------------------------
    # RUNTIME
    # --------------------------------------------------------

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
