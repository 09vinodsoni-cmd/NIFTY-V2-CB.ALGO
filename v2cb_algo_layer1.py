"""
V²-CB Auto-Trading Bot - LAYER 1
=================================
Telegram command + inline button control layer.
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


# ============================================================
# TELEGRAM MENU
# ============================================================

def get_main_menu():
    return {
        "inline_keyboard": [
            [
                {"text": "🟢 ON", "callback_data": "/on"},
                {"text": "🔴 OFF", "callback_data": "/off"},
            ],
            [
                {"text": "🔼 BULLISH ONLY", "callback_data": "/bullishonly"},
            ],
            [
                {"text": "🔽 BEARISH ONLY", "callback_data": "/bearishonly"},
            ],
            [
                {"text": "🖐️ MANUAL", "callback_data": "/manual"},
                {"text": "🔄 AUTO", "callback_data": "/auto"},
            ],
            [
                {"text": "🎯 SET SL", "callback_data": "/setsl"},
            ],
        ]
    }


def send_telegram(message: str, show_menu=False):
    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }

    if show_menu:
        data["reply_markup"] = json.dumps(get_main_menu())

    try:
        requests.post(
            url,
            data=data,
            timeout=10
        )
    except Exception as e:
        print("Telegram send failed:", e)


def answer_callback_query(callback_query_id):
    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    )

    try:
        requests.post(
            url,
            data={
                "callback_query_id": callback_query_id
            },
            timeout=10
        )
    except Exception as e:
        print("Callback answer failed:", e)


# ============================================================
# GROWW
# ============================================================

def get_groww_access_token():
    totp_gen = pyotp.TOTP(GROWW_TOTP_SECRET)
    totp = totp_gen.now()

    from growwapi import GrowwAPI

    return GrowwAPI.get_access_token(
        api_key=GROWW_TOTP_API_KEY,
        totp=totp
    )


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
        json.dump(
            state,
            f,
            indent=2,
            default=str
        )


# ============================================================
# TELEGRAM UPDATES
# ============================================================

def fetch_new_telegram_commands(state):
    """
    Fetch new Telegram messages and inline button callbacks.

    Returns:
        list of event dictionaries
    """

    last_update_id = state.get(
        "last_telegram_update_id",
        0
    )

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/getUpdates"
    )

    params = {
        "offset": last_update_id + 1,
        "timeout": 0,
    }

    try:
        resp = requests.get(
            url,
            params=params,
            timeout=10
        )

        resp.raise_for_status()
        data = resp.json()

    except Exception as e:
        print("Telegram getUpdates failed:", e)
        return []

    events = []
    max_update_id = last_update_id

    for update in data.get("result", []):

        update_id = update.get(
            "update_id",
            0
        )

        max_update_id = max(
            max_update_id,
            update_id
        )

        # ====================================================
        # INLINE BUTTON CALLBACK
        # ====================================================

        callback_query = update.get(
            "callback_query"
        )

        if callback_query:

            callback_id = callback_query.get(
                "id"
            )

            callback_data = callback_query.get(
                "data",
                ""
            )

            callback_chat_id = str(
                callback_query.get(
                    "message",
                    {}
                ).get(
                    "chat",
                    {}
                ).get(
                    "id",
                    ""
                )
            )

            if callback_chat_id != str(
                TELEGRAM_CHAT_ID
            ):
                continue

            events.append({
                "type": "callback",
                "text": callback_data,
                "callback_id": callback_id,
            })

            continue

        # ====================================================
        # NORMAL TEXT MESSAGE
        # ====================================================

        msg = update.get(
            "message",
            {}
        )

        text = msg.get(
            "text",
            ""
        ).strip()

        chat_id = str(
            msg.get(
                "chat",
                {}
            ).get(
                "id",
                ""
            )
        )

        if chat_id != str(
            TELEGRAM_CHAT_ID
        ):
            continue

        if text:

            events.append({
                "type": "message",
                "text": text,
            })

    state["last_telegram_update_id"] = max_update_id

    return events


# ============================================================
# COMMAND HANDLER
# ============================================================

def handle_command(state, cmd_event):
    """
    Handles both:
    - Normal Telegram commands/messages
    - Inline keyboard callbacks
    """

    try:

        # ----------------------------------------------------
        # EVENT NORMALIZATION
        # ----------------------------------------------------

        if isinstance(cmd_event, dict):

            event_type = cmd_event.get(
                "type",
                "message"
            )

            cmd_text = cmd_event.get(
                "text",
                ""
            ).strip()

            if event_type == "callback":

                callback_id = cmd_event.get(
                    "callback_id"
                )

                answer_callback_query(
                    callback_id
                )

        else:

            # Backward compatibility
            event_type = "message"

            cmd_text = str(
                cmd_event
            ).strip()

        # ----------------------------------------------------
        # SET SL NUMBER INPUT
        # ----------------------------------------------------

        if (
            event_type == "message"
            and state.get("awaiting_setsl") is True
            and not cmd_text.startswith("/")
        ):

            try:

                new_sl = float(
                    cmd_text
                )

            except ValueError:

                send_telegram(
                    "⚠️ Please enter a valid SL price.\n"
                    "Example: <code>24150</code>",
                    show_menu=True
                )

                return

            if state.get("open_trade") is None:

                state["awaiting_setsl"] = False

                send_telegram(
                    "⚠️ No open trade right now — "
                    "nothing to update.",
                    show_menu=True
                )

                return

            apply_manual_sl_override(
                state,
                new_sl
            )

            state["awaiting_setsl"] = False

            send_telegram(
                f"✅ SL override accepted: "
                f"<b>{new_sl}</b>\n"
                f"Algo will continue trailing from this level.",
                show_menu=True
            )

            return

        # ----------------------------------------------------
        # NORMAL COMMAND
        # ----------------------------------------------------

        parts = cmd_text.split()

        if not parts:
            return

        cmd = parts[0].lower()

        # ====================================================
        # START / MENU
        # ====================================================

        if cmd == "/start":

            state["awaiting_setsl"] = False

            send_telegram(
                "🤖 <b>NIFTY V²-CB ALGO</b>\n\n"
                "Select an action:",
                show_menu=True
            )

        # ====================================================
        # ON
        # ====================================================

        elif cmd == "/on":

            state["awaiting_setsl"] = False
            state["mode"] = "on"

            resync_open_trade_from_broker(
                state
            )

            send_telegram(
                "✅ Mode: ON — full auto trading resumed for today.",
                show_menu=True
            )

        # ====================================================
        # OFF
        # ====================================================

        elif cmd == "/off":

            state["awaiting_setsl"] = False
            state["mode"] = "off"

            send_telegram(
                "⏸️ Mode: OFF — no trading today.",
                show_menu=True
            )

        # ====================================================
        # BULLISH ONLY
        # ====================================================

        elif cmd == "/bullishonly":

            state["awaiting_setsl"] = False
            state["mode"] = "bullishonly"

            send_telegram(
                "🔼 Mode: BULLISH-ONLY\n\n"
                "Only LONG trades will be taken for real.",
                show_menu=True
            )

        # ====================================================
        # BEARISH ONLY
        # ====================================================

        elif cmd == "/bearishonly":

            state["awaiting_setsl"] = False
            state["mode"] = "bearishonly"

            send_telegram(
                "🔽 Mode: BEARISH-ONLY\n\n"
                "Only SHORT trades will be taken for real.",
                show_menu=True
            )

        # ====================================================
        # MANUAL
        # ====================================================

        elif cmd == "/manual":

            state["awaiting_setsl"] = False

            if state.get("open_trade") is None:

                send_telegram(
                    "⚠️ No open trade right now — "
                    "nothing to hand over. Mode unchanged.",
                    show_menu=True
                )

            else:

                state["open_trade"]["control"] = "manual"
                state["mode"] = "manual"

                send_telegram(
                    "🖐️ Control handed to you.\n\n"
                    "Algo will NOT modify the current "
                    "trade SL/exit anymore.",
                    show_menu=True
                )

        # ====================================================
        # AUTO
        # ====================================================

        elif cmd == "/auto":

            state["awaiting_setsl"] = False
            state["mode"] = "on"

            resync_open_trade_from_broker(
                state
            )

            send_telegram(
                "🔄 Control resynced from broker "
                "and handed back to algo.",
                show_menu=True
            )

        # ====================================================
        # SET SL
        # ====================================================

        elif cmd == "/setsl":

            # If price is directly written:
            # /setsl 24150
            if len(parts) >= 2:

                try:

                    new_sl = float(
                        parts[1]
                    )

                except ValueError:

                    send_telegram(
                        f"⚠️ '{parts[1]}' is not a valid price.",
                        show_menu=True
                    )

                    return

                if state.get("open_trade") is None:

                    send_telegram(
                        "⚠️ No open trade right now — "
                        "nothing to update.",
                        show_menu=True
                    )

                    return

                apply_manual_sl_override(
                    state,
                    new_sl
                )

                send_telegram(
                    f"✅ SL override accepted: "
                    f"<b>{new_sl}</b>",
                    show_menu=True
                )

            else:

                # Button click
                state["awaiting_setsl"] = True

                send_telegram(
                    "🎯 <b>SET SL</b>\n\n"
                    "Enter new SL price.\n"
                    "Example: <code>24150</code>",
                    show_menu=True
                )

        # ====================================================
        # UNKNOWN COMMAND
        # ====================================================

        else:

            send_telegram(
                "❓ Unrecognized command.\n\n"
                "Use the buttons below.",
                show_menu=True
            )

    except Exception as e:

        print(
            "Error handling command:",
            cmd_event,
            "-",
            e
        )

        send_telegram(
            f"⚠️ Something went wrong:\n{e}",
            show_menu=True
        )


# ============================================================
# BROKER CONTROL
# ============================================================

def resync_open_trade_from_broker(state):
    """
    When control returns to algo (/on or /auto),
    mark the trade as auto-controlled.
    """

    if state.get("open_trade") is None:
        return

    state["open_trade"]["control"] = "auto"

    # TODO:
    # Fetch actual live position and active SL order
    # from Groww and reconcile state.


def apply_manual_sl_override(state, new_sl_price):
    """
    Apply manual SL override.

    Currently updates internal state.
    Broker order replacement remains Layer 2/3 hook.
    """

    state["open_trade"]["sl_current"] = new_sl_price

    state["open_trade"][
        "sl_manually_overridden"
    ] = True

    # TODO:
    # Cancel current SL order
    # Place new SL-Market order
