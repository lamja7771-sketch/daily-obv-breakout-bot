import os
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests


# ============================================================
# CONFIGURATION
# ============================================================

GATE_URL = "https://api.gateio.ws/api/v4"

TIMEFRAME = "1d"
CANDLE_LIMIT = 120

PIVOT_LEFT = 2
PIVOT_RIGHT = 2

MAX_WORKERS = 4
REQUEST_DELAY = 0.08

OBV_LOOKBACK = 20

# Only the strongest / cleanest setups are sent.
BEAUTIFUL_MIN_SCORE = 75

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_CHAT_ID_2 = os.getenv("TELEGRAM_CHAT_ID_2", "").strip()

HISTORY_FILE = "signals.json"

HEADERS = {
    "User-Agent": "Daily-OBV-Beautiful-Bot/1.0"
}

request_lock = threading.Lock()


# ============================================================
# REQUEST HELPER
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
                    timeout=20
                )

            if response.status_code == 200:
                return response.json()

            if response.status_code == 429:
                wait_time = 2 + (attempt * 2)
                print(
                    f"429 Too Many Requests. "
                    f"Waiting {wait_time}s..."
                )
                time.sleep(wait_time)
                continue

            print(
                f"Gate API error {response.status_code}: "
                f"{response.text[:200]}"
            )

        except requests.RequestException as e:
            print(f"Request error: {e}")

            if attempt < retries - 1:
                time.sleep(2)

    return None


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram configuration missing.")
        return False

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    success = True

    chat_ids = [TELEGRAM_CHAT_ID]

    if TELEGRAM_CHAT_ID_2:
        chat_ids.append(TELEGRAM_CHAT_ID_2)

    for chat_id in chat_ids:
        try:
            response = requests.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": message,
                    "disable_web_page_preview": True
                },
                timeout=20
            )

            if response.status_code == 200:
                print(f"Telegram sent: {chat_id}")
            else:
                print(
                    f"Telegram error {response.status_code}: "
                    f"{response.text[:300]}"
                )
                success = False

        except requests.RequestException as e:
            print(f"Telegram request error: {e}")
            success = False

    return success


# ============================================================
# LOAD / SAVE HISTORY
# ============================================================

def load_history():
    if not os.path.exists(HISTORY_FILE):
        return {}

    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict):
            return data

        if isinstance(data, list):
            return {
                str(item): True
                for item in data
            }

    except Exception as e:
        print(f"Could not load history: {e}")

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
        print(f"Could not save history: {e}")


# ============================================================
# GET ACTIVE USDT FUTURES
# ============================================================

def get_usdt_contracts():
    url = f"{GATE_URL}/futures/usdt/contracts"

    data = gate_get(url)

    if not data:
        return []

    contracts = []

    for item in data:
        name = item.get("name", "")

        if not name:
            continue

        if not name.endswith("_USDT"):
            continue

        # Ignore expired contracts
        if item.get("in_delisting") is True:
            continue

        contracts.append(name)

    return sorted(set(contracts))


# ============================================================
# DAILY CANDLES
# ============================================================

def get_daily_candles(contract):
    url = f"{GATE_URL}/futures/usdt/candlesticks"

    params = {
        "contract": contract,
        "interval": "1d",
        "limit": CANDLE_LIMIT
    }

    data = gate_get(url, params=params)

    if not data:
        return []

    candles = []

    for item in data:
        try:
            timestamp = int(item["t"])
            open_price = float(item["o"])
            high_price = float(item["h"])
            low_price = float(item["l"])
            close_price = float(item["c"])

            # Gate futures API may provide:
            # v = contract volume
            # sum = quote volume
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

        except (KeyError, TypeError, ValueError):
            continue

    candles.sort(
        key=lambda x: x["timestamp"]
    )

    # --------------------------------------------------------
    # Remove the currently forming daily candle.
    # --------------------------------------------------------

    now = int(time.time())

    completed = []

    for candle in candles:
        candle_start = candle["timestamp"]

        # Daily candle is 86400 seconds.
        candle_end = candle_start + 86400

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
            current_obv = previous_obv + volume

        elif current_close < previous_close:
            current_obv = previous_obv - volume

        else:
            current_obv = previous_obv

        obv.append(current_obv)

    return obv


# ============================================================
# SWING HIGH / LOW
# ============================================================

def find_swing_highs(values):
    highs = []

    start = PIVOT_LEFT
    end = len(values) - PIVOT_RIGHT

    for i in range(start, end):
        current = values[i]

        left = values[
            i - PIVOT_LEFT:i
        ]

        right = values[
            i + 1:i + PIVOT_RIGHT + 1
        ]

        if all(current > x for x in left) and \
           all(current > x for x in right):

            highs.append(i)

    return highs


def find_swing_lows(values):
    lows = []

    start = PIVOT_LEFT
    end = len(values) - PIVOT_RIGHT

    for i in range(start, end):
        current = values[i]

        left = values[
            i - PIVOT_LEFT:i
        ]

        right = values[
            i + 1:i + PIVOT_RIGHT + 1
        ]

        if all(current < x for x in left) and \
           all(current < x for x in right):

            lows.append(i)

    return lows


# ============================================================
# BEAUTIFUL OBV SCORE
# ============================================================

def calculate_beauty_score(
    obv,
    hh1_index,
    hl_index,
    hh2_index,
    previous_low_index
):
    """
    Score the QUALITY of the OBV structure.

    We want:

        HH1
          \
           HL
             \
              HH2

    Stronger:
        - Higher HH improvement
        - Higher HL improvement
        - Clean structure
        - Strong final push
        - Limited noise
    """

    hh1 = obv[hh1_index]
    hl = obv[hl_index]
    hh2 = obv[hh2_index]
    previous_low = obv[previous_low_index]

    # --------------------------------------------------------
    # OBV ranges
    # --------------------------------------------------------

    total_range = abs(hh2 - previous_low)

    if total_range <= 0:
        return 0.0

    # --------------------------------------------------------
    # Higher-high strength
    # --------------------------------------------------------

    hh_improvement = hh2 - hh1

    hh_score = (
        hh_improvement / total_range
    ) * 100

    # --------------------------------------------------------
    # Higher-low strength
    # --------------------------------------------------------

    hl_improvement = hl - previous_low

    hl_score = (
        hl_improvement / total_range
    ) * 100

    # --------------------------------------------------------
    # Final HH should recover strongly from HL.
    # --------------------------------------------------------

    recovery = hh2 - hl

    recovery_score = (
        recovery / total_range
    ) * 100

    # --------------------------------------------------------
    # Clean structure score.
    #
    # We inspect the OBV path between:
    #
    # HH1 -> HL -> HH2
    #
    # and reward a clean movement.
    # --------------------------------------------------------

    segment1 = obv[
        hh1_index:hl_index + 1
    ]

    segment2 = obv[
        hl_index:hh2_index + 1
    ]

    noise_penalty = 0.0

    if len(segment1) >= 3:
        for i in range(1, len(segment1)):
            if segment1[i] > segment1[i - 1]:
                noise_penalty += 1

    if len(segment2) >= 3:
        for i in range(1, len(segment2)):
            if segment2[i] < segment2[i - 1]:
                noise_penalty += 1

    total_steps = max(
        1,
        len(segment1) + len(segment2) - 2
    )

    clean_ratio = max(
        0.0,
        1.0 - (
            noise_penalty / total_steps
        )
    )

    clean_score = clean_ratio * 25

    # --------------------------------------------------------
    # Combine scores.
    # --------------------------------------------------------

    raw_score = (
        hh_score * 0.30
        + hl_score * 0.25
        + recovery_score * 0.20
        + clean_score
    )

    # Normalize to 0-100.
    score = max(
        0.0,
        min(100.0, raw_score)
    )

    return score


# ============================================================
# CHECK STRICT BEAUTIFUL SETUP
# ============================================================

def check_signal(contract):
    candles = get_daily_candles(contract)

    if len(candles) < 40:
        return None

    obv = calculate_obv(candles)

    if len(obv) < 40:
        return None

    swing_highs = find_swing_highs(obv)
    swing_lows = find_swing_lows(obv)

    if len(swing_highs) < 2:
        return None

    if len(swing_lows) < 2:
        return None

    # --------------------------------------------------------
    # Latest confirmed swing high
    # --------------------------------------------------------

    hh2_index = swing_highs[-1]

    # Previous confirmed swing high
    hh1_index = swing_highs[-2]

    # Must be chronological.
    if hh2_index <= hh1_index:
        return None

    # --------------------------------------------------------
    # HH2 must actually be higher than HH1.
    # --------------------------------------------------------

    if obv[hh2_index] <= obv[hh1_index]:
        return None

    # --------------------------------------------------------
    # Find lows between HH1 and HH2.
    #
    # STRICT:
    # Exactly ONE confirmed swing low must exist.
    #
    # Therefore:
    #
    # HH1 -> HL -> HH2
    #
    # No extra swing-low structure.
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
    # Find previous swing low before HL.
    # --------------------------------------------------------

    previous_lows = [
        i
        for i in swing_lows
        if i < hl_index
    ]

    if not previous_lows:
        return None

    previous_low_index = previous_lows[-1]

    # --------------------------------------------------------
    # HL must be higher than previous swing low.
    # --------------------------------------------------------

    if obv[hl_index] <= obv[previous_low_index]:
        return None

    # --------------------------------------------------------
    # Exact sequence:
    #
    # Previous Low
    #       ↓
    #      HH1
    #       ↓
    #      HL
    #       ↓
    #      HH2
    # --------------------------------------------------------

    if not (
        previous_low_index
        < hh1_index
        < hl_index
        < hh2_index
    ):
        return None

    # --------------------------------------------------------
    # HH2 must be the latest confirmed swing high.
    # --------------------------------------------------------

    if hh2_index != swing_highs[-1]:
        return None

    # --------------------------------------------------------
    # Make sure HH2 is fresh enough to be useful.
    #
    # Because a pivot needs RIGHT candles for confirmation,
    # HH2 is expected to be a few candles old.
    #
    # We don't use an arbitrary age filter here.
    # --------------------------------------------------------

    # --------------------------------------------------------
    # Calculate BEAUTY score.
    # --------------------------------------------------------

    score = calculate_beauty_score(
        obv,
        hh1_index,
        hl_index,
        hh2_index,
        previous_low_index
    )

    if score < BEAUTIFUL_MIN_SCORE:
        return None

    # --------------------------------------------------------
    # Actual timestamps.
    # --------------------------------------------------------

    hh1_timestamp = candles[
        hh1_index
    ]["timestamp"]

    hl_timestamp = candles[
        hl_index
    ]["timestamp"]

    hh2_timestamp = candles[
        hh2_index
    ]["timestamp"]

    previous_low_timestamp = candles[
        previous_low_index
    ]["timestamp"]

    # --------------------------------------------------------
    # Signal key.
    #
    # HH2 timestamp makes the signal unique.
    # --------------------------------------------------------

    signal_key = (
        f"{contract}|DAILY|"
        f"OBV_BEAUTIFUL_HH_HL_HH|"
        f"{hh2_timestamp}"
    )

    return {
        "key": signal_key,
        "contract": contract,
        "timeframe": "DAILY",
        "score": round(score, 2),

        "hh1": obv[hh1_index],
        "hl": obv[hl_index],
        "hh2": obv[hh2_index],
        "previous_low": obv[previous_low_index],

        "hh1_timestamp": hh1_timestamp,
        "hl_timestamp": hl_timestamp,
        "hh2_timestamp": hh2_timestamp,
        "previous_low_timestamp": previous_low_timestamp,

        "candles": len(candles)
    }


# ============================================================
# FORMAT DATE
# ============================================================

def format_date(timestamp):
    return datetime.fromtimestamp(
        timestamp,
        tz=timezone.utc
    ).strftime("%Y-%m-%d")


# ============================================================
# MAIN SCAN
# ============================================================

def main():
    print("=" * 60)
    print("DAILY OBV BEAUTIFUL HH -> HL -> HH BOT")
    print("=" * 60)

    print("Timeframe: DAILY")
    print(
        "Structure: "
        "OBV HIGHER HIGH -> HIGHER LOW -> HIGHER HIGH"
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
        f"Beauty minimum score: "
        f"{BEAUTIFUL_MIN_SCORE}"
    )
    print(
        f"Max workers: {MAX_WORKERS}"
    )
    print(
        f"Request delay: {REQUEST_DELAY}s"
    )

    telegram_ok = bool(
        TELEGRAM_BOT_TOKEN
        and TELEGRAM_CHAT_ID
    )

    print(
        "Telegram configuration: "
        + ("OK" if telegram_ok else "MISSING")
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
        print("No USDT contracts found.")
        return

    print("Scanning...")

    results = []

    completed = 0
    total = len(contracts)

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
                    results.append(result)

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
    # Sort by beauty score.
    # Highest first.
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
    # Find new signals.
    # --------------------------------------------------------

    new_signals = []

    for signal in results:
        if signal["key"] not in history:
            new_signals.append(signal)

    print(
        f"New beautiful signals: "
        f"{len(new_signals)}"
    )

    # --------------------------------------------------------
    # Telegram
    # ONE MESSAGE
    # ONE COIN PER LINE
    # --------------------------------------------------------

    if new_signals:
        print(
            "Sending beautiful OBV list..."
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
            "OBV ONLY • DAILY • COMPLETED CANDLES"
        ])

        message = "\n".join(lines)

        if telegram_ok:
            send_telegram(message)

    else:
        print(
            "NO NEW BEAUTIFUL OBV SETUPS"
        )

        report = (
            "📊 DAILY OBV — BEAUTIFUL SETUPS\n\n"
            "No new beautiful HH → HL → HH setup.\n\n"
            f"Current beautiful setups: {len(results)}\n"
            f"Previously recorded: {len(history)}"
        )

        if telegram_ok:
            send_telegram(report)

    # --------------------------------------------------------
    # Save ALL qualifying signal keys.
    # --------------------------------------------------------

    for signal in results:
        history[signal["key"]] = {
            "contract": signal["contract"],
            "timeframe": signal["timeframe"],
            "score": signal["score"],
            "hh1_timestamp": signal["hh1_timestamp"],
            "hl_timestamp": signal["hl_timestamp"],
            "hh2_timestamp": signal["hh2_timestamp"],
            "created_at": int(time.time())
        }

    save_history(history)

    print("=" * 60)
    print("SCAN COMPLETE")
    print("=" * 60)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()
