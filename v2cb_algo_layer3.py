"""
V²-CB Auto-Trading Bot - Layer 3: Premium-Based Trade Management
====================================================================
Once a trade is open, this layer watches ONLY the premium price (never Nifty)
and manages the ratchet-trailing SL, exactly mirroring backtest_v2cb.py's logic
but expressed in premium points instead of Nifty points.

Responsibilities:
- Poll live premium LTP
- If LTP hits the current SL -> confirm exit, record outcome, hand control
  back to "watching Nifty" mode (for a possible reversal)
- If LTP hits the current target -> cancel old SL order, place new (trailed)
  SL order, advance to the next R-level target
- At end-of-day cutoff -> force a market-exit regardless of where price is
- Respects trade["control"] == "manual" -> does NOTHING if user has taken over
"""

from datetime import datetime, time as dtime
import pytz

IST = pytz.timezone("Asia/Kolkata")
EOD_SQUARE_OFF_TIME = dtime(15, 15)   # force-exit cutoff


def get_interval_ohlc(groww, trading_symbol, start_time_str, end_time_str):
    """
    Fetch 1-min candles for the premium between start_time and end_time (covering
    the gap since the last workflow run), and return (interval_high, interval_low,
    last_close) aggregated across that whole interval - so no SL/TP touch gets
    missed between 5-minute polling gaps.
    """
    try:
        resp = groww.get_historical_candle_data(
            trading_symbol=trading_symbol,
            exchange=groww.EXCHANGE_NSE,
            segment=groww.SEGMENT_FNO,
            start_time=start_time_str,
            end_time=end_time_str,
            interval_in_minutes=1,
        )
    except Exception as e:
        print(f"get_historical_candle_data failed: {e}")
        return None, None, None

    candles = resp.get("candles", []) if isinstance(resp, dict) else resp
    if not candles:
        return None, None, None

    highs = [c[2] for c in candles]
    lows = [c[3] for c in candles]
    closes = [c[4] for c in candles]
    return max(highs), min(lows), closes[-1]


def get_premium_ltp(groww, trading_symbol):
    key = f"NSE_{trading_symbol}"
    resp = groww.get_ltp(segment=groww.SEGMENT_FNO, exchange_trading_symbols=key)
    return resp[key]


def cancel_order_safe(groww, order_id):
    if not order_id:
        return
    try:
        groww.cancel_order(order_id=order_id, segment=groww.SEGMENT_FNO)
    except Exception as e:
        print(f"Warning: failed to cancel order {order_id}: {e}")


def place_sl_order(groww, trading_symbol, trigger_price, quantity_lots, lot_size=65):
    """SL-Market order: guarantees exit once triggered, accepting slippage."""
    order = groww.place_order(
        trading_symbol=trading_symbol,
        quantity=quantity_lots * lot_size,
        validity=groww.VALIDITY_DAY,
        exchange=groww.EXCHANGE_NSE,
        segment=groww.SEGMENT_FNO,
        product=groww.PRODUCT_MIS,
        order_type=groww.ORDER_TYPE_SL_MARKET,
        transaction_type=groww.TRANSACTION_TYPE_SELL,
        trigger_price=trigger_price,
    )
    return order.get("order_id")


def place_market_exit(groww, trading_symbol, quantity_lots, lot_size=65):
    order = groww.place_order(
        trading_symbol=trading_symbol,
        quantity=quantity_lots * lot_size,
        validity=groww.VALIDITY_DAY,
        exchange=groww.EXCHANGE_NSE,
        segment=groww.SEGMENT_FNO,
        product=groww.PRODUCT_MIS,
        order_type=groww.ORDER_TYPE_MARKET,
        transaction_type=groww.TRANSACTION_TYPE_SELL,
    )
    return order


def manage_open_trade(groww, state, send_telegram):
    """Called every run when state['open_trade'] is not None. Mutates state in place."""
    trade = state.get("open_trade")
    if trade is None:
        return

    if trade.get("control") == "manual":
        print("Trade is under manual control - Layer 3 will not touch it.")
        return

    now = datetime.now(IST).time()

    # ---- EOD forced square-off ----
    if now >= EOD_SQUARE_OFF_TIME:
        cancel_order_safe(groww, trade.get("sl_order_id"))
        place_market_exit(groww, trade["trading_symbol"], quantity_lots=1)
        send_telegram(f"🔔 EOD square-off: {trade['trading_symbol']} closed at market. "
                      f"Reached 1:{trade['r_step'] - 1 if trade['r_step'] > 1 else 0}.")
        finalize_trade(state, trade, outcome="eod_square_off")
        return

    now_str = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    start_str = trade.get("last_checked_time") or now_str
    interval_high, interval_low, last_close = get_interval_ohlc(
        groww, trade["trading_symbol"], start_str, now_str
    )
    trade["last_checked_time"] = now_str

    if interval_high is None:
        # No candle data back yet (e.g. very first check) - fall back to a single LTP read
        ltp = get_premium_ltp(groww, trade["trading_symbol"])
        interval_high = interval_low = ltp

    # ---- Check SL first (conservative): premium is always bought, so a LOW touch
    # on the SL level means a loss, regardless of whether direction is long/short ----
    if interval_low <= trade["sl_current_premium"]:
        was_full_loss = (trade["r_step"] == 1)
        send_telegram(f"🛑 SL hit on {trade['trading_symbol']} (interval low {interval_low:.2f} touched "
                      f"SL {trade['sl_current_premium']:.2f}). R reached before SL: {trade['r_step'] - 1}.")
        finalize_trade(state, trade, outcome="sl_hit", full_loss=was_full_loss)
        return

    # ---- Check target: a HIGH touch on the target level means the ratchet advances ----
    if interval_high >= trade["target_premium"]:
        advance_ratchet(groww, trade, send_telegram)


def advance_ratchet(groww, trade, send_telegram):
    r = trade["r_step"]
    entry = trade["entry_premium"]
    sl_pts = trade["sl_points_premium"]
    buf = trade["buffer_premium"]

    if r == 1:
        new_sl = entry + buf
        trade["hit_1_1"] = True
    else:
        if r == 2:
            trade["hit_1_2"] = True
        prev_target = entry + (r - 1) * sl_pts
        new_sl = prev_target + buf

    cancel_order_safe(groww, trade.get("sl_order_id"))
    new_sl_order_id = place_sl_order(groww, trade["trading_symbol"], new_sl, quantity_lots=1)
    trade["sl_order_id"] = new_sl_order_id
    trade["sl_current_premium"] = new_sl

    trade["r_step"] = r + 1
    trade["target_premium"] = entry + trade["r_step"] * sl_pts

    send_telegram(f"🎯 1:{r} hit on {trade['trading_symbol']}! SL trailed to {new_sl:.2f}. "
                  f"Next target 1:{r+1} = {trade['target_premium']:.2f}")


def finalize_trade(state, trade, outcome, full_loss=False):
    """Records the trade outcome and switches the bot back to 'watching Nifty' mode
    (for the possible reversal), per the SL<=80pt reversal-eligibility rule.
    If the close does NOT qualify for a reversal attempt, the day is marked done -
    no further fresh-entry scanning happens today (matches the strategy rule:
    a profitable/EOD close ends the day; only a genuine SL-hit can open a
    reversal opportunity)."""
    state["last_closed_trade"] = {
        "trading_symbol": trade["trading_symbol"],
        "direction": trade["direction"],
        "outcome": outcome,
        "sl_points_nifty": trade["sl_points_nifty"],
        "r_reached": trade["r_step"] - 1,
    }
    state["open_trade"] = None

    if outcome == "sl_hit" and full_loss and trade["sl_points_nifty"] <= 80 and state["trades_today"] < 2:
        state["awaiting_reversal_confirmation"] = True
    else:
        state["awaiting_reversal_confirmation"] = False
        state["day_done"] = True
