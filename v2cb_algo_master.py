"""
V²-CB Auto-Trading Bot - MASTER script
=========================================
Combines Layer 1 (commands/mode), Layer 2 (entry), Layer 3 (management).
Runs ONCE per invocation - meant to be triggered every 2-5 minutes by a
GitHub Actions cron during market hours.

*** PAPER_MODE SAFETY SWITCH ***
PAPER_MODE = True  -> NO real orders are ever placed. Every action that would
                      place/cancel/modify a real order instead just sends a
                      Telegram message describing what WOULD have happened.
                      Use this for at least a few days before flipping to False.
PAPER_MODE = False -> Real orders are placed on Groww with real money.

Set PAPER_MODE below. Do not flip to False until you've verified paper-mode
output looks correct for at least a few real trading days.
"""

import json
import requests
from datetime import datetime
import pytz

import v2cb_algo_layer1 as L1
import v2cb_algo_layer2 as L2
import v2cb_algo_layer3 as L3

# ================= SAFETY SWITCH =================
PAPER_MODE = True
# ==================================================

IST = pytz.timezone("Asia/Kolkata")
INSTRUMENT_KEY = "NIFTY"


class PaperGroww:
    """Drop-in stand-in for the real GrowwAPI client during paper-mode testing.
    Every order-affecting method just logs what it WOULD have done."""

    EXCHANGE_NSE = "NSE"
    SEGMENT_FNO = "FNO"
    SEGMENT_CASH = "CASH"
    PRODUCT_MIS = "MIS"
    VALIDITY_DAY = "DAY"
    ORDER_TYPE_MARKET = "MARKET"
    ORDER_TYPE_SL_MARKET = "SL_MARKET"
    TRANSACTION_TYPE_BUY = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"

    def __init__(self, real_client_for_reads):
        self._real = real_client_for_reads  # still used for read-only calls (LTP, option chain)

    def place_order(self, **kwargs):
        print("[PAPER] Would place_order:", kwargs)
        return {"order_id": f"PAPER-{datetime.now(IST).timestamp()}"}

    def cancel_order(self, **kwargs):
        print("[PAPER] Would cancel_order:", kwargs)

    def get_ltp(self, **kwargs):
        return self._real.get_ltp(**kwargs)

    def get_option_chain(self, **kwargs):
        return self._real.get_option_chain(**kwargs)


def fetch_todays_nifty_candles(access_token):
    today = datetime.now(IST).strftime("%Y-%m-%d")
    url = "https://api.groww.in/v1/historical/candles"
    params = {
        "exchange": "NSE", "segment": "CASH", "groww_symbol": "NSE-NIFTY",
        "start_time": f"{today} 09:15:00", "end_time": f"{today} 15:30:00",
        "candle_interval": "15minute",
    }
    headers = {"Accept": "application/json", "Authorization": f"Bearer {access_token}", "X-API-VERSION": "1.0"}
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Nifty candle fetch failed (market closed/holiday?): {e}")
        return []
    data = resp.json()
    candles = []
    for row in data.get("payload", {}).get("candles", []):
        ts = row[0]
        dt = datetime.fromtimestamp(ts, IST) if isinstance(ts, (int, float)) else IST.localize(datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S"))
        candles.append({"dt": dt, "open": row[1], "high": row[2], "low": row[3], "close": row[4]})
    return sorted(candles, key=lambda c: c["dt"])


def detect_entry_signal(state, candles):
    """Marking (candle1&2) + breakout detection (candle3+). Returns (direction, entry_price) or (None, None)."""
    if len(candles) < 2:
        return None, None
    c1, c2 = candles[0], candles[1]
    if state.get("marked_high") is None:
        state["marked_high"] = max(c1["high"], c2["high"])
        state["marked_low"] = min(c1["low"], c2["low"])

    if len(candles) < 3:
        return None, None

    latest = candles[-1]
    if state.get("last_processed") == latest["dt"].isoformat():
        return None, None
    state["last_processed"] = latest["dt"].isoformat()

    if latest["close"] > state["marked_high"]:
        return "long", latest["close"]
    elif latest["close"] < state["marked_low"]:
        return "short", latest["close"]
    return None, None


def detect_reversal_confirmation(state, candles):
    """After Trade 1 hit its SL, watch for a candle closing beyond the OPPOSITE
    marked level to confirm the reversal trade."""
    if len(candles) < 3:
        return None, None
    latest = candles[-1]
    if state.get("last_processed") == latest["dt"].isoformat():
        return None, None
    state["last_processed"] = latest["dt"].isoformat()

    last_trade_dir = state["last_closed_trade"]["direction"]
    if last_trade_dir == "long":
        # trade 1 was long, reversal confirms with a close BELOW marked_low
        if latest["close"] < state["marked_low"]:
            state["awaiting_reversal_confirmation"] = False
            return "short", latest["close"]
    else:
        if latest["close"] > state["marked_high"]:
            state["awaiting_reversal_confirmation"] = False
            return "long", latest["close"]
    return None, None


def compute_nifty_sl_level(entry_price, direction, marked_high, marked_low):
    """Same formula as L2.compute_nifty_sl_points, but also returns the actual level."""
    if direction == "long":
        full_level = marked_low - L2.SL_BUFFER_NIFTY
        full_sl_pts = entry_price - full_level
    else:
        full_level = marked_high + L2.SL_BUFFER_NIFTY
        full_sl_pts = full_level - entry_price
    scaled_sl_pts = full_sl_pts * L2.SL_SCALE_PCT
    sl_level = entry_price - scaled_sl_pts if direction == "long" else entry_price + scaled_sl_pts
    return sl_level, scaled_sl_pts


def monitor_virtual_trade(state, candles):
    """Checks if the virtually-tracked (non-desired-direction) trade has hit ITS
    SL, using the same touch-based (low/high) check a real SL order would use.
    Returns True if it just hit (caller should notify)."""
    vt = state.get("virtual_trade")
    if vt is None or len(candles) < 3:
        return False
    latest = candles[-1]
    if state.get("last_processed_virtual") == latest["dt"].isoformat():
        return False
    state["last_processed_virtual"] = latest["dt"].isoformat()

    direction = vt["direction"]
    sl_hit = (latest["low"] <= vt["sl_level"]) if direction == "long" else (latest["high"] >= vt["sl_level"])
    if sl_hit:
        state["last_closed_trade"] = {"direction": direction, "outcome": "sl_hit_virtual",
                                       "sl_points_nifty": vt["sl_points"], "r_reached": 0}
        state["virtual_trade"] = None
        state["awaiting_reversal_confirmation"] = vt["sl_points"] <= 80
        return True
    return False


def direction_allowed(state, direction):
    mode = state["mode"]
    if mode == "on":
        return True
    if mode == "bullishonly":
        return direction == "long"
    if mode == "bearishonly":
        return direction == "short"
    return False


def main():
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    state = L1.load_state()

    if state.get("date") != today_str:
        state = {
            "date": today_str, "mode": "off",
            "marked_high": None, "marked_low": None,
            "trades_today": 0, "open_trade": None, "last_processed": None,
            "last_telegram_update_id": state.get("last_telegram_update_id", 0),
            "awaiting_reversal_confirmation": False, "last_closed_trade": None,
            "virtual_trade": None, "last_processed_virtual": None,
        }

    # 1) Process Telegram commands (always, regardless of mode)
    commands = L1.fetch_new_telegram_commands(state)
    for cmd_text in commands:
        L1.handle_command(state, cmd_text)

    if state["mode"] in ("off", "manual"):
        L1.save_state(state)
        print(f"Mode '{state['mode']}' - nothing further to do this run.")
        return

    # 2) Set up Groww client (real for reads always; paper wrapper for writes if PAPER_MODE)
    access_token = L1.get_groww_access_token()
    from growwapi import GrowwAPI
    real_groww = GrowwAPI(access_token)
    groww = PaperGroww(real_groww) if PAPER_MODE else real_groww

    def send_telegram(msg):
        prefix = "[PAPER MODE] " if PAPER_MODE else ""
        L1.send_telegram(prefix + msg)

    # 3) If a trade is currently open, manage it (Layer 3) and stop here this run
    if state.get("open_trade") is not None:
        L3.manage_open_trade(groww, state, send_telegram)
        L1.save_state(state)
        return

    # 4) No open trade - fetch Nifty candles
    candles = fetch_todays_nifty_candles(access_token)

    # 4a) If we're virtually tracking a non-desired-direction trade, check if IT hit its SL
    if state.get("virtual_trade") is not None:
        hit = monitor_virtual_trade(state, candles)
        if hit:
            send_telegram("👁️ Virtually-tracked trade hit its SL. Now watching for reversal confirmation "
                          "into your desired direction.")
        L1.save_state(state)
        return

    if state.get("awaiting_reversal_confirmation"):
        direction, entry_price = detect_reversal_confirmation(state, candles)
    else:
        direction, entry_price = detect_entry_signal(state, candles)

    if direction is None:
        L1.save_state(state)
        return

    if state["trades_today"] >= 2:
        L1.save_state(state)
        return

    sl_points_nifty = L2.compute_nifty_sl_points(entry_price, direction, state["marked_high"], state["marked_low"])

    if not direction_allowed(state, direction):
        sl_level, _ = compute_nifty_sl_level(entry_price, direction, state["marked_high"], state["marked_low"])
        state["virtual_trade"] = {"direction": direction, "entry_price": entry_price,
                                   "sl_level": sl_level, "sl_points": sl_points_nifty}
        send_telegram(f"👁️ Virtual-tracking {direction} signal at {entry_price} (not your desired direction). "
                      f"Virtual SL: {sl_level:.2f}. Watching for it to hit, which would confirm a reversal "
                      f"into your direction.")
        state["trades_today"] += 1  # counts toward the 2/day cap same as a real trade would
        L1.save_state(state)
        return

    strike_info = L2.select_strike(groww, direction, sl_points_nifty, expiry_date=today_str)
    if strike_info is None:
        send_telegram(f"⚠️ {direction} signal at {entry_price} SKIPPED - no strike found within Rs 3500 risk budget.")
        L1.save_state(state)
        return

    entry_order = L2.place_entry_order(groww, strike_info["trading_symbol"], quantity=1)
    trade = L2.build_trade_state(direction, entry_price, sl_points_nifty, strike_info, entry_order.get("order_id"))

    sl_order_id = L3.place_sl_order(groww, trade["trading_symbol"], trade["sl_current_premium"], quantity_lots=1)
    trade["sl_order_id"] = sl_order_id

    state["open_trade"] = trade
    state["trades_today"] += 1

    send_telegram(
        f"🚨 ENTRY: {'LONG' if direction=='long' else 'SHORT'} | {trade['trading_symbol']}\n"
        f"Premium entry: {trade['entry_premium']} | Delta: {trade['entry_delta']:.2f}\n"
        f"SL: {trade['sl_current_premium']:.2f} | Target 1:1: {trade['target_premium']:.2f}"
    )
    L1.save_state(state)


if __name__ == "__main__":
    main()
