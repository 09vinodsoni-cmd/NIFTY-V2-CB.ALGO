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


def get_premium_ltp(groww, trading_symbol):
    resp = groww.get_ltp(
        segment=groww.SEGMENT_FNO,
        exchange_trading_symbols=[trading_symbol],
    )
    # NOTE: exact response shape should be verified against current growwapi docs
    return resp[trading_symbol]["ltp"]


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

    ltp = get_premium_ltp(groww, trade["trading_symbol"])

    # ---- Check SL ----
    if ltp <= trade["sl_current_premium"]:
        # The standing SL-Market order on the broker should already be filling/filled;
        # we just need to confirm and update our own state/logging.
        was_full_loss = (trade["r_step"] == 1)
        send_telegram(f"🛑 SL hit on {trade['trading_symbol']} at ~{ltp}. "
                      f"R reached before SL: {trade['r_step'] - 1}.")
        finalize_trade(state, trade, outcome="sl_hit", full_loss=was_full_loss)
        return

    # ---- Check target ----
    if ltp >= trade["target_premium"]:
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
    (for the possible reversal), per the SL<=80pt reversal-eligibility rule."""
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
