"""
V²-CB Auto-Trading Bot - Layer 2: Entry Order Placement
==========================================================
Responsibilities:
- Detect entry signal from Nifty 15m candles (marking via candle 1&2, breakout on candle 3+)
- Select the option strike whose delta is closest to our target band (0.60-0.80)
- Place the entry MARKET order (BUY CE or BUY PE)
- Convert all SL/target R-levels into PREMIUM points using the ENTRY-TIME delta
  (fixed at entry - not re-fetched later, matching the backtest's approximation)
- Place the initial SL order (SL-Market) on the premium

After this point, Layer 3 takes over: it watches the PREMIUM price only (not Nifty)
to manage ratchet-trailing and eventual exit.
"""

import os
import pyotp
from datetime import datetime
import pytz

GROWW_TOTP_API_KEY = os.environ.get("GROWW_TOTP_API_KEY", "")
GROWW_TOTP_SECRET = os.environ.get("GROWW_TOTP_SECRET", "")
IST = pytz.timezone("Asia/Kolkata")

# ---- V2-CB strategy constants ----
LOT_SIZE = 65
MAX_LOSS_RUPEES = 3500
MIN_DELTA = 0.60
MAX_DELTA = 0.80
SL_BUFFER_NIFTY = 3          # points, applied to the full-range SL before scaling
SL_SCALE_PCT = 0.65
BUFFER_POINTS_NIFTY = 7      # ratchet buffer, in Nifty points (converted via entry delta)
SKIP_THRESHOLD_NIFTY = 80    # SL (nifty pts, scaled) above this -> no reversal allowed


def get_groww_client():
    from growwapi import GrowwAPI
    totp_gen = pyotp.TOTP(GROWW_TOTP_SECRET)
    totp = totp_gen.now()
    access_token = GrowwAPI.get_access_token(api_key=GROWW_TOTP_API_KEY, totp=totp)
    return GrowwAPI(access_token)


def compute_nifty_sl_points(entry_price, direction, marked_high, marked_low):
    """Same logic as backtest_v2cb.py's detect_breakout - full range scaled to 65%."""
    if direction == "long":
        full_level = marked_low - SL_BUFFER_NIFTY
        full_sl_pts = entry_price - full_level
    else:
        full_level = marked_high + SL_BUFFER_NIFTY
        full_sl_pts = full_level - entry_price
    scaled_sl_pts = full_sl_pts * SL_SCALE_PCT
    return scaled_sl_pts


def select_strike(groww, direction, sl_points_nifty, expiry_date):
    """
    Fetch the option chain for the current week's expiry, and pick the strike
    (CE for long, PE for short) whose delta is closest to being feasible within
    our Rs 3500 budget, constrained to the 0.60-0.80 delta band.

    Returns: dict with trading_symbol, delta, ltp (premium price) - or None if
    no strike satisfies the risk budget (skip this signal).
    """
    required_delta = MAX_LOSS_RUPEES / (sl_points_nifty * LOT_SIZE)
    if required_delta < MIN_DELTA:
        return None  # SL too wide even at floor delta - skip trade entirely
    target_delta = min(required_delta, MAX_DELTA)

    option_type = "CE" if direction == "long" else "PE"

    # NOTE: exact method name/params for Groww's option-chain API should be verified
    # against current growwapi docs before going live - placeholder call shown below.
    chain = groww.get_option_chain(
        exchange=groww.EXCHANGE_NSE,
        segment=groww.SEGMENT_FNO,
        trading_symbol="NIFTY",
        expiry_date=expiry_date,
    )

    candidates = [row for row in chain if row.get("option_type") == option_type]
    if not candidates:
        return None

    # pick the strike whose delta is closest to target_delta, but not below MIN_DELTA
    valid = [r for r in candidates if r.get("delta") is not None and abs(r["delta"]) >= MIN_DELTA]
    if not valid:
        return None
    best = min(valid, key=lambda r: abs(abs(r["delta"]) - target_delta))
    return {
        "trading_symbol": best["trading_symbol"],
        "delta": abs(best["delta"]),
        "ltp": best.get("ltp"),
    }


def place_entry_order(groww, trading_symbol, quantity=1):
    """Places a MARKET BUY order for 1 lot of the selected option."""
    order = groww.place_order(
        trading_symbol=trading_symbol,
        quantity=quantity * LOT_SIZE,
        validity=groww.VALIDITY_DAY,
        exchange=groww.EXCHANGE_NSE,
        segment=groww.SEGMENT_FNO,
        product=groww.PRODUCT_MIS,
        order_type=groww.ORDER_TYPE_MARKET,
        transaction_type=groww.TRANSACTION_TYPE_BUY,
    )
    return order


def build_trade_state(direction, nifty_entry_price, sl_points_nifty, strike_info, entry_order_id):
    """
    Constructs the trade record that Layer 3 will use for premium-based tracking.
    All R-levels and the ratchet buffer are pre-computed in PREMIUM points using
    the entry-time delta - fixed for the life of the trade.
    """
    delta = strike_info["delta"]
    entry_premium = strike_info["ltp"]

    sl_points_premium = sl_points_nifty * delta
    buffer_premium = BUFFER_POINTS_NIFTY * delta

    return {
        "control": "auto",
        "direction": direction,
        "trading_symbol": strike_info["trading_symbol"],
        "entry_order_id": entry_order_id,
        "entry_premium": entry_premium,
        "entry_delta": delta,
        "sl_points_nifty": sl_points_nifty,           # kept for reference / reversal-eligibility check
        "sl_points_premium": sl_points_premium,
        "buffer_premium": buffer_premium,
        "sl_current_premium": entry_premium - sl_points_premium,   # premium SL is always a downside stop (long option)
        "r_step": 1,
        "target_premium": entry_premium + sl_points_premium,       # next target (1:1) in premium terms
        "hit_1_1": False,
        "hit_1_2": False,
        "sl_order_id": None,   # filled in once the initial SL order is placed (Layer 3)
        "sl_manually_overridden": False,
    }
