"""
V²-CB Auto-Trading Bot - Layer 1: Command & Mode State Machine
==================================================================
This layer handles:
- Polling Telegram for new commands (/on, /off, /bullishonly, /bearishonly, /manual, /setsl, /auto)
- Maintaining the day's mode state
- Entry signal detection (virtual or real, depending on mode)

Order PLACEMENT (Layer 2) and OCO/ratchet-trailing MANAGEMENT (Layer 3) are
separate modules that this layer will call into - kept separate so each can
be tested independently before wiring them together live.

MODES:
  off           - no trading today at all
  on            - full auto: whichever direction breaks out first is taken for real;
                   if it hits SL and reversal is eligible, the reversal is taken for real too
  bullishonly   - only LONG trades taken for real (direct breakout OR reversal-into-long);
                   any short-direction signal is tracked virtually only, no real order
  bearishonly   - only SHORT trades taken for real (direct breakdown OR reversal-into-short);
                   any long-direction signal is tracked virtually only, no real order
  manual        - current open trade handed to user; NO new trades rest of day

ENV VARS NEEDED (GitHub Secrets):
  GROWW_TOTP_API_KEY, GROWW_TOTP_SECRET, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import os
import json
import requests
import pyotp
from datetime import datetime
import pytz

GROWW_TOTP_API_KEY = os.environ.get("GROWW_TOTP_API_KEY", "")
GROWW_TOTP_SECRET = os.environ.get("GROWW_TOTP_SECRET", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

IST = pytz.timezone("Asia/Kolkata")
STATE_FILE = "state.json"

VALID_MODES = {"off", "on", "bullishonly", "bearishonly", "manual"}


def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print("Telegram send failed:", e)


def get_groww_access_token():
    totp_gen = pyotp.TOTP(GROWW_TOTP_SECRET)
    totp = totp_gen.now()
    from growwapi import GrowwAPI
    return GrowwAPI.get_access_token(api_key=GROWW_TOTP_API_KEY, totp=totp)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def fetch_new_telegram_commands(state):
    """Poll Telegram getUpdates for any new messages since last processed update_id."""
    last_update_id = state.get("last_telegram_update_id", 0)
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"offset": last_update_id + 1, "timeout": 0}
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print("Telegram getUpdates failed:", e)
        return []

    commands = []
    max_update_id = last_update_id
    for update in data.get("result", []):
        update_id = update.get("update_id", 0)
        max_update_id = max(max_update_id, update_id)
        msg = update.get("message", {})
        text = msg.get("text", "").strip()
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id != str(TELEGRAM_CHAT_ID):
            continue  # ignore messages from anyone else
        if text:
            commands.append(text)

    state["last_telegram_update_id"] = max_update_id
    return commands


def handle_command(state, cmd_text):
    """Parse a single command string and update state accordingly. Never raises."""
    try:
        parts = cmd_text.strip().split()
        cmd = parts[0].lower()

        if cmd == "/on":
            state["mode"] = "on"
            resync_open_trade_from_broker(state)
            send_telegram("✅ Mode: ON — full auto trading resumed for today.")

        elif cmd == "/off":
            state["mode"] = "off"
            send_telegram("⏸️ Mode: OFF — no trading today.")

        elif cmd == "/bullishonly":
            state["mode"] = "bullishonly"
            send_telegram("🔼 Mode: BULLISH-ONLY — only LONG trades will be taken for real "
                          "(whether it's the direct breakout or a reversal from a failed short). "
                          "Any short-direction signal will only be tracked virtually.")

        elif cmd == "/bearishonly":
            state["mode"] = "bearishonly"
            send_telegram("🔽 Mode: BEARISH-ONLY — only SHORT trades will be taken for real "
                          "(whether it's the direct breakdown or a reversal from a failed long). "
                          "Any long-direction signal will only be tracked virtually.")

        elif cmd == "/manual":
            if state.get("open_trade") is None:
                send_telegram("⚠️ No open trade right now — nothing to hand over. Mode unchanged.")
            else:
                state["open_trade"]["control"] = "manual"
                state["mode"] = "manual"
                send_telegram("🖐️ Control handed to you for the current open trade. "
                              "Algo will NOT modify its SL/exit anymore. "
                              "No new trades will be taken today unless you send /on.")

        elif cmd == "/auto":
            state["mode"] = "on"
            resync_open_trade_from_broker(state)
            send_telegram("🔄 Control resynced from broker and handed back to algo.")

        elif cmd == "/setsl":
            if len(parts) < 2:
                send_telegram("⚠️ Usage: /setsl <price>  e.g. /setsl 24150")
                return
            try:
                new_sl = float(parts[1])
            except ValueError:
                send_telegram(f"⚠️ '{parts[1]}' doesn't look like a valid price. Usage: /setsl 24150")
                return
            if state.get("open_trade") is None:
                send_telegram("⚠️ No open trade right now — nothing to update.")
                return
            apply_manual_sl_override(state, new_sl)
            send_telegram(f"✅ SL override accepted: {new_sl}. Algo will continue trailing from this level.")

        else:
            send_telegram(f"❓ Unrecognized command: {cmd_text}\n"
                          "Valid commands: /on /off /bullishonly /bearishonly /manual /setsl <price> /auto")

    except Exception as e:
        # Never let a bad command crash the run - state must still get saved.
        print("Error handling command:", cmd_text, "-", e)
        send_telegram(f"⚠️ Something went wrong processing '{cmd_text}': {e}. State unchanged, please retry.")


def resync_open_trade_from_broker(state):
    """
    Placeholder for Layer 2/3: when control returns to the algo (/on or /auto),
    fetch the ACTUAL live position + active SL order from Groww (not our
    possibly-stale internal record) and rebuild state['open_trade'] from that
    ground truth before resuming automated management.
    """
    if state.get("open_trade") is None:
        return
    state["open_trade"]["control"] = "auto"
    # TODO (Layer 2): call groww.get_position(...) and groww.get_order_status(...)
    # to pull the real current SL price and reconcile state['open_trade']['sl_current']
    # with it, rather than trusting our last-known value.


def apply_manual_sl_override(state, new_sl_price):
    """
    Placeholder for Layer 2/3: cancel the currently active SL order on the broker
    and place a new SL-Market order at new_sl_price. Update state so future
    ratchet-trailing steps use this as the new baseline reference.
    """
    state["open_trade"]["sl_current"] = new_sl_price
    state["open_trade"]["sl_manually_overridden"] = True
    # TODO (Layer 2): call groww.cancel_order(...) then groww.place_order(...) for the new SL


def main():
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    state = load_state()

    if state.get("date") != today_str:
        state = {
            "date": today_str, "mode": "off",  # SAFE DEFAULT: must send /on to activate each day
            "marked_high": None, "marked_low": None,
            "trades_today": 0, "open_trade": None, "last_processed": None,
            "last_telegram_update_id": state.get("last_telegram_update_id", 0),
        }

    # 1) Process any new Telegram commands first, always - regardless of mode
    commands = fetch_new_telegram_commands(state)
    for cmd_text in commands:
        handle_command(state, cmd_text)

    # 2) If mode is off or manual (paused), do nothing further this run
    if state["mode"] in ("off", "manual"):
        save_state(state)
        print(f"Mode is '{state['mode']}' - skipping strategy processing this run.")
        return

    # 3) Otherwise (on / bullishonly / bearishonly) - proceed to signal detection (Layer 2 hooks in here)
    save_state(state)
    print(f"Mode is '{state['mode']}' - proceeding to signal detection (Layer 2).")


if __name__ == "__main__":
    main()
