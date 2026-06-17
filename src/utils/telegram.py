"""Minimal Telegram Bot API sender used to report appointment-slot results.

Reads the bot token and chat id from the `[telegram]` section of config.ini.
Sending is best-effort: any failure is logged and swallowed so it never breaks
the bot's main flow.
"""

import json
import logging
import urllib.parse
import urllib.request

from src.utils.config_reader import get_config_value


def is_configured() -> bool:
    """True if both a bot token and a chat id are present in config."""
    return bool(
        get_config_value("telegram", "bot_token")
        and get_config_value("telegram", "chat_id")
    )


def send_message(text: str) -> bool:
    """
    Sends `text` to the configured Telegram chat. Returns True on success.

    Never raises — logs and returns False if Telegram is unconfigured or the
    request fails, so callers can fire-and-forget.
    """
    token = get_config_value("telegram", "bot_token")
    chat_id = get_config_value("telegram", "chat_id")
    if not token or not chat_id:
        logging.warning(
            "Telegram not configured ([telegram] bot_token / chat_id) — skipping send."
        )
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}
    ).encode("utf-8")

    try:
        req = urllib.request.Request(url, data=payload)
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if body.get("ok"):
            logging.info("Telegram message sent.")
            return True
        logging.warning(f"Telegram API returned not-ok: {body}")
        return False
    except Exception as e:
        logging.warning(f"Failed to send Telegram message: {e}")
        return False
