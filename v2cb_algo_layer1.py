"""
V²-CB Auto-Trading Bot - LAYER 1
=================================
Telegram command + REPLY KEYBOARD control layer.

Unlike inline buttons (attached to one message, invisible in chat history,
answered via callback_query), reply keyboard buttons:
- Stay visible as a persistent keyboard at the bottom of the chat
- When tapped, send their label text as a REAL, VISIBLE, SAVED chat message
  (indistinguishable from the user typing it)
- Are read back via ordinary getUpdates "message" events - no callback
  handling needed at all
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

VALID_MODES = {
    "off",
    "on",
    "bullishonly",
    "bearishonly",
    "manual",
}

# Reply-keyboard button labels mapped to the underlying slash command they trigger.
BUTTON_TEXT_TO_COMMAND = {
    "🟢 ON": "/on",
    "🔴 OFF": "/off",
    "🔼 BULLISH ONLY": "/bullishonly",
    "🔽 BEARISH ONLY": "/bearishonly",
    "🖐️ MANUAL": "/manual",
    "🔄 AUTO": "/auto",
    "🎯 SET SL": "/setsl",
}


# ============================================================
# TELEGRAM MENU (Reply Keyboard)
# ============================================================

def get_main_menu():
    return {
        "keyboard": [
            [{"text": "🟢 ON"}, {"text": "🔴 OFF"}],
            [{"text": "🔼 BULLISH ONLY"}],
            [{"text": "🔽 BEARISH ONLY"}],
            [{"text": "🖐️ MANUAL"}, {"text": "🔄 AUTO"}],
            [{"text": "🎯 SET SL"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def send_telegram(message: str, show_menu=False):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }

    if show_menu:
        data["reply_markup"] = json.dumps(get_main_menu())

    try:
        requests.post(url, data=data, timeout=10)
    except Exception as e:
        print("Telegram send failed:", e)


# ============================================================
# GROWW
# ============================================================

def get_groww_access_token():
    totp_gen = pyotp.TOTP(GROWW_TOTP_SECRET)
    totp = totp_gen.now()
    from growwapi import GrowwAPI
    return GrowwAPI.get_access_token(api_key=GROWW_TOTP_API_KEY, totp=totp)


# ============================================================
# STATE
# ============================================================

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


# ============================================================
# TELEGRAM UPDATES
# ============================================================

def fetch_new_telegram_commands(state):
    """
    Fetch new Telegram messages. Reply-keyboard button presses arrive here
    as ordinary text messages (their label text) - no callback handling
    is needed with reply keyboards.

    Returns: list of plain text strings.
    """
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

    texts = []
    max_update_id = last_update_id

    for update in data.get("result", []):
        update_id = update.get("update_id", 0)
        max_update_id = max(max_update_id, update_id)

        msg = update.get("message", {})
        text = msg.get("text", "").strip()
        chat_id = str(msg.get("chat", {}).get("id", ""))

        if chat_id != str(TELEGRAM_CHAT_ID):
            continue
        if text:
            texts.append(text)

    state["last_telegram_update_id"] = max_update_id
    return texts


# ============================================================
# COMMAND HANDLER
# ============================================================

def handle_command(state, cmd_text):
    """Handles both typed slash commands and reply-keyboard button presses
    (whose label text gets mapped to the same underlying command)."""
    try:
        cmd_text = str(cmd_text).strip()

        # Reply-keyboard button press -> map its label to the real command
        if cmd_text in BUTTON_TEXT_TO_COMMAND:
            cmd_text = BUTTON_TEXT_TO_COMMAND[cmd_text]

        # ---- SET SL number input (after the SET SL button was pressed) ----
        if state.get("awaiting_setsl") is True and not cmd_text.startswith("/"):
            try:
                new_sl = float(cmd_text)
            except ValueError:
                send_telegram("⚠️ Please enter a valid SL price.\nExample: <code>24150</code>", show_menu=True)
                return

            if state.get("open_trade") is None:
                state["awaiting_setsl"] = False
                send_telegram("⚠️ No open trade right now — nothing to update.", show_menu=True)
                return

            apply_manual_sl_override(state, new_sl)
            state["awaiting_setsl"] = False
            send_telegram(f"✅ SL override accepted: <b>{new_sl}</b>\n"
                          f"Algo will continue trailing from this level.", show_menu=True)
            return

        parts = cmd_text.split()
        if not parts:
            return
        cmd = parts[0].lower()

        if cmd == "/start":
            state["awaiting_setsl"] = False
            send_telegram("🤖 <b>NIFTY V²-CB ALGO</b>\n\nSelect an action:", show_menu=True)

        elif cmd == "/on":
            state["awaiting_setsl"] = False
            state["mode"] = "on"
            resync_open_trade_from_broker(state)
            send_telegram("✅ Mode: ON — full auto trading resumed for today.", show_menu=True)

        elif cmd == "/off":
            state["awaiting_setsl"] = False
            state["mode"] = "off"
            send_telegram("⏸️ Mode: OFF — no trading today.", show_menu=True)

        elif cmd == "/bullishonly":
            state["awaiting_setsl"] = False
            state["mode"] = "bullishonly"
            send_telegram("🔼 Mode: BULLISH-ONLY\n\nOnly LONG trades will be taken for real.", show_menu=True)

        elif cmd == "/bearishonly":
            state["awaiting_setsl"] = False
            state["mode"] = "bearishonly"
            send_telegram("🔽 Mode: BEARISH-ONLY\n\nOnly SHORT trades will be taken for real.", show_menu=True)

        elif cmd == "/manual":
            state["awaiting_setsl"] = False
            if state.get("open_trade") is None:
                send_telegram("⚠️ No open trade right now — nothing to hand over. Mode unchanged.", show_menu=True)
            else:
                state["open_trade"]["control"] = "manual"
                state["mode"] = "manual"
                send_telegram("🖐️ Control handed to you.\n\n"
                              "Algo will NOT modify the current trade SL/exit anymore.", show_menu=True)

        elif cmd == "/auto":
            state["awaiting_setsl"] = False
            state["mode"] = "on"
            resync_open_trade_from_broker(state)
            send_telegram("🔄 Control resynced from broker and handed back to algo.", show_menu=True)

        elif cmd == "/setsl":
            if len(parts) >= 2:
                try:
                    new_sl = float(parts[1])
                except ValueError:
                    send_telegram(f"⚠️ '{parts[1]}' is not a valid price.", show_menu=True)
                    return
                if state.get("open_trade") is None:
                    send_telegram("⚠️ No open trade right now — nothing to update.", show_menu=True)
                    return
                apply_manual_sl_override(state, new_sl)
                send_telegram(f"✅ SL override accepted: <b>{new_sl}</b>", show_menu=True)
            else:
                state["awaiting_setsl"] = True
                send_telegram("🎯 <b>SET SL</b>\n\nEnter new SL price.\nExample: <code>24150</code>", show_menu=True)

        else:
            send_telegram("❓ Unrecognized command.\n\nUse the buttons below.", show_menu=True)

    except Exception as e:
        print("Error handling command:", cmd_text, "-", e)
        send_telegram(f"⚠️ Something went wrong:\n{e}", show_menu=True)


# ============================================================
# BROKER CONTROL
# ============================================================

def resync_open_trade_from_broker(state):
    if state.get("open_trade") is None:
        return
    state["open_trade"]["control"] = "auto"
    # TODO: fetch actual live position and active SL order from Groww and reconcile state.


def apply_manual_sl_override(state, new_sl_price):
    state["open_trade"]["sl_current"] = new_sl_price
    state["open_trade"]["sl_manually_overridden"] = True
    # TODO: cancel current SL order, place new SL-Market order.
