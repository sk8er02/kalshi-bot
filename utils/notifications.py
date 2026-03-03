"""
utils/notifications.py — Push notifications via Telegram.

Sends alerts for: orders placed, positions closed, kill switch, daily summary.
Configure TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.

To set up:
1. Message @BotFather on Telegram → /newbot → copy the token
2. Message your new bot, then visit:
   https://api.telegram.org/bot<TOKEN>/getUpdates
   to find your chat_id
"""

import threading
from typing import Optional

import requests

import config
from utils.logger import get_logger

logger = get_logger(__name__)

_TELEGRAM_BOT_TOKEN: str = ""
_TELEGRAM_CHAT_ID: str = ""


def _load_config() -> None:
    """Lazy-load Telegram config from env."""
    global _TELEGRAM_BOT_TOKEN, _TELEGRAM_CHAT_ID
    import os
    _TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    _TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def is_configured() -> bool:
    if not _TELEGRAM_BOT_TOKEN:
        _load_config()
    return bool(_TELEGRAM_BOT_TOKEN and _TELEGRAM_CHAT_ID)


def _send_raw(text: str) -> None:
    """Send a message via Telegram Bot API. Runs in a background thread."""
    if not is_configured():
        return

    def _do_send():
        try:
            url = f"https://api.telegram.org/bot{_TELEGRAM_BOT_TOKEN}/sendMessage"
            resp = requests.post(
                url,
                json={
                    "chat_id": _TELEGRAM_CHAT_ID,
                    "text": text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            if not resp.ok:
                logger.warning(f"Telegram send failed: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            logger.warning(f"Telegram send error: {e}")

    # Fire and forget — don't block the trading loop
    threading.Thread(target=_do_send, daemon=True).start()


# ------------------------------------------------------------------ #
# High-level notification helpers
# ------------------------------------------------------------------ #

def notify_order_placed(
    ticker: str,
    side: str,
    contracts: int,
    price_cents: int,
    edge_cents: int,
    ai_estimate: int,
    reasoning: str,
) -> None:
    cost = price_cents * contracts
    _send_raw(
        f"📈 *Order Placed*\n"
        f"`{ticker}` BUY {side.upper()}\n"
        f"{contracts}x @ {price_cents}¢ = ${cost / 100:.2f}\n"
        f"Edge: {edge_cents}¢ | AI: {ai_estimate}¢\n"
        f"_{reasoning[:120]}_"
    )


def notify_position_closed(
    ticker: str,
    side: str,
    reason: str,
    pnl_cents: int,
    pnl_pct: float,
    source: str = "bot",
) -> None:
    emoji = "✅" if pnl_cents >= 0 else "🔴"
    _send_raw(
        f"{emoji} *Position Closed* [{source}]\n"
        f"`{ticker}` {side.upper()}\n"
        f"Reason: {reason}\n"
        f"P&L: ${pnl_cents / 100:+.2f} ({pnl_pct:+.1%})"
    )


def notify_kill_switch(daily_loss_cents: int) -> None:
    _send_raw(
        f"🚨 *KILL SWITCH TRIPPED*\n"
        f"Daily loss: ${abs(daily_loss_cents) / 100:.2f}\n"
        f"All trading halted. Pending orders cancelled."
    )


def notify_daily_summary(
    trades: int,
    spent_cents: int,
    pnl_cents: int,
    open_positions: int,
    balance_cents: int,
) -> None:
    emoji = "📊" if pnl_cents >= 0 else "📉"
    _send_raw(
        f"{emoji} *Daily Summary*\n"
        f"Trades: {trades}\n"
        f"Spent: ${spent_cents / 100:.2f}\n"
        f"P&L: ${pnl_cents / 100:+.2f}\n"
        f"Open positions: {open_positions}\n"
        f"Balance: ${balance_cents / 100:.2f}"
    )


def notify_stale_orders_cancelled(count: int) -> None:
    if count > 0:
        _send_raw(
            f"🧹 Cancelled {count} stale unfilled order{'s' if count != 1 else ''}"
        )


def notify_risk_blocked(ticker: str, reason: str) -> None:
    _send_raw(
        f"⚠️ *Risk Blocked*\n"
        f"`{ticker}`: {reason}"
    )
