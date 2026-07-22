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


def get_nearest_expiry(groww):
    """Fetch the actual nearest upcoming weekly expiry date for NIFTY from Groww,
    instead of assuming today is the expiry date."""
    today = datetime.now(IST).date()
    for year, month in [(today.year, today.month), (today.year, today.month + 1)]:
        try:
            resp = groww.get_expiries(
                exchange=groww.EXCHANGE_NSE,
                underlying_symbol="NIFTY",
                year=year,
                month=month,
            )
        except Exception as e:
            print(f"get_expiries failed for {year}-{month}: {e}")
            continue
        dates = resp if isinstance(resp, list) else resp.get("expiries", [])
        future_dates = sorted(d for d in dates if datetime.strptime(d, "%Y-%m-%d").date() >= today)
        if future_dates:
            return future_dates[0]
    return None


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

    chain = groww.get_option_chain(
        exchange=groww.EXCHANGE_NSE,
        underlying="NIFTY",
        expiry_date=expiry_date,
    )

    strikes = chain.get("strikes", {})
    candidates = []
    for strike_price, row in strikes.items():
        opt = row.get(option_type)
        if opt is None:
            continue
        delta = opt.get("greeks", {}).get("delta")
        if delta is None:
            continue
        candidates.append({
            "trading_symbol": opt["trading_symbol"],
            "delta": abs(delta),
            "ltp": opt.get("ltp"),
        })

    print(f"[select_strike] direction={direction} target_delta={target_delta:.3f} "
          f"total_strikes_in_chain={len(strikes)} candidates_with_delta={len(candidates)}")
    if candidates:
        print(f"[select_strike] delta range found: "
              f"{min(c['delta'] for c in candidates):.3f} to {max(c['delta'] for c in candidates):.3f}")

    valid = [r for r in candidates if abs(r["delta"]) >= MIN_DELTA]
    if not valid:
        print(f"[select_strike] No candidates with delta >= {MIN_DELTA} found - "
              f"this likely means the option-chain response wasn't parsed as expected "
              f"(check the raw 'chain' structure), not a genuine risk-budget issue.")
        return None
    best = min(valid, key=lambda r: abs(r["delta"] - target_delta))
    return best


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
        "last_checked_time": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
    }
