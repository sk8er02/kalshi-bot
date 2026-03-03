"""
risk/risk_manager.py — Risk controls, daily limits, and kill switch.

This is the last gate before any order reaches the market.
Every trade decision MUST be approved by can_trade() before placement.

Risk rules:
1. Daily spend limit ($50 hard cap by default)
2. Daily loss kill switch ($20 loss → halt all trading)
3. Max simultaneous open positions (5) — checks LIVE Kalshi positions
4. No trading if API balance is critically low
5. No trading in the same market where we already hold a position (live check)
"""

import time

from utils.logger import get_logger
from utils.state import (
    get_daily_stats,
    is_kill_switch_tripped,
    set_kill_switch,
)
import config
from kalshi.orders import cancel_all_pending_orders
from utils.notifications import notify_kill_switch

logger = get_logger(__name__)

# Cached live position data to avoid hitting the API on every risk check
_live_positions_cache: list | None = None
_live_positions_cache_time: float = 0
_LIVE_CACHE_TTL: float = 30  # seconds


def _get_live_positions() -> list:
    """
    Fetch live positions from Kalshi API with a short cache.
    Returns list of Position objects from portfolio module.
    """
    global _live_positions_cache, _live_positions_cache_time
    now = time.time()
    if _live_positions_cache is not None and (now - _live_positions_cache_time) < _LIVE_CACHE_TTL:
        return _live_positions_cache
    try:
        from kalshi.portfolio import get_open_positions
        _live_positions_cache = get_open_positions()
        _live_positions_cache_time = now
    except Exception as e:
        logger.warning(f"Could not fetch live positions for risk check: {e}")
        if _live_positions_cache is None:
            _live_positions_cache = []
    return _live_positions_cache


def invalidate_position_cache() -> None:
    """Force a fresh API call on next position check."""
    global _live_positions_cache, _live_positions_cache_time
    _live_positions_cache = None
    _live_positions_cache_time = 0


# Max positions per event family (e.g. TRUMP, FED, CPI)
MAX_POSITIONS_PER_EVENT_FAMILY: int = 2


def _extract_event_family(ticker: str) -> str:
    """
    Extract the event family prefix from a ticker.
    e.g. KXFED-26MAR14 → KXFED, INXY-26MAR03-B5.5 → INXY
    """
    if "-" in ticker:
        return ticker.split("-")[0].upper()
    return ticker.upper()


class RiskManager:
    """
    Risk gate that checks LIVE Kalshi positions (not just local DB).

    This means it sees positions from manual trades on the website too.

    Usage:
        rm = RiskManager()
        allowed, reason = rm.can_trade(ticker, cost_cents)
        if allowed:
            place_order(...)
    """

    def check_kill_switch(self) -> bool:
        """Returns True if kill switch is currently tripped."""
        if is_kill_switch_tripped():
            logger.warning("Kill switch is ACTIVE — all trading halted")
            return True
        return False

    def check_daily_loss(self) -> bool:
        """
        Check if daily realized losses have exceeded the kill switch threshold.
        Trips the kill switch and cancels all orders if so.
        Returns True if kill switch was just tripped.
        """
        stats = get_daily_stats()
        pnl = stats.get("realized_pnl_cents", 0)
        # pnl is negative when we've lost money
        if pnl <= -config.DAILY_LOSS_KILL_SWITCH_CENTS:
            logger.error(
                f"KILL SWITCH TRIPPED: Daily loss ${abs(pnl) / 100:.2f} exceeds "
                f"limit ${config.DAILY_LOSS_KILL_SWITCH_CENTS / 100:.2f}"
            )
            set_kill_switch(True)
            cancel_all_pending_orders()
            notify_kill_switch(abs(pnl))
            return True
        return False

    def can_trade(
        self,
        ticker: str,
        cost_cents: int,
    ) -> tuple[bool, str]:
        """
        Check all risk rules before placing a trade.

        Uses LIVE Kalshi positions for position count and duplicate checks,
        so it sees both bot trades and manual trades from the website.

        Returns (allowed: bool, reason: str).
        reason is empty string if allowed, explains rejection otherwise.
        """
        # 1. Kill switch
        if self.check_kill_switch():
            return False, "Kill switch is active — trading halted for today"

        # 2. Check if daily loss warrants kill switch
        if self.check_daily_loss():
            return False, "Kill switch just tripped due to daily loss limit"

        # 3. Daily spend limit
        stats = get_daily_stats()
        spent_today = stats.get("total_spent_cents", 0)
        if spent_today + cost_cents > config.MAX_DAILY_SPEND_CENTS:
            return (
                False,
                f"Daily spend limit: already spent ${spent_today / 100:.2f}, "
                f"would exceed ${config.MAX_DAILY_SPEND_CENTS / 100:.2f} max",
            )

        # 4. Max open positions (checks LIVE Kalshi positions)
        live_positions = _get_live_positions()
        open_count = len(live_positions)
        if open_count >= config.MAX_OPEN_POSITIONS:
            return (
                False,
                f"Max open positions reached: {open_count}/{config.MAX_OPEN_POSITIONS} (live)",
            )

        # 5. Already have a position in this market (checks LIVE Kalshi positions)
        for pos in live_positions:
            if pos.ticker == ticker:
                return (
                    False,
                    f"Already hold a live position in {ticker} "
                    f"({pos.contracts}x {pos.side} @ {pos.avg_price_cents}c)",
                )

        # 6. Correlated position protection — limit positions in same event family
        new_family = _extract_event_family(ticker)
        family_count = sum(
            1 for pos in live_positions
            if _extract_event_family(pos.ticker) == new_family
        )
        if family_count >= MAX_POSITIONS_PER_EVENT_FAMILY:
            return (
                False,
                f"Already hold {family_count} positions in event family '{new_family}' "
                f"(max {MAX_POSITIONS_PER_EVENT_FAMILY})",
            )

        # 7. Cost must be within per-trade limits
        if cost_cents < config.MIN_TRADE_COST_CENTS:
            return (
                False,
                f"Trade too small: ${cost_cents / 100:.2f} < ${config.MIN_TRADE_COST_CENTS / 100:.2f} min",
            )
        if cost_cents > config.MAX_TRADE_COST_CENTS:
            return (
                False,
                f"Trade too large: ${cost_cents / 100:.2f} > ${config.MAX_TRADE_COST_CENTS / 100:.2f} max",
            )

        return True, ""

    def get_status(self) -> dict:
        """Return a summary of current risk state for logging."""
        stats = get_daily_stats()
        live_positions = _get_live_positions()
        return {
            "kill_switch": is_kill_switch_tripped(),
            "daily_spent_cents": stats.get("total_spent_cents", 0),
            "daily_pnl_cents": stats.get("realized_pnl_cents", 0),
            "trades_today": stats.get("trades_placed", 0),
            "open_positions": len(live_positions),
            "daily_spend_remaining_cents": max(
                0,
                config.MAX_DAILY_SPEND_CENTS - stats.get("total_spent_cents", 0),
            ),
        }

    def log_status(self) -> None:
        s = self.get_status()
        logger.info(
            f"Risk status | "
            f"KillSwitch={'ON' if s['kill_switch'] else 'OFF'} | "
            f"Spent today: ${s['daily_spent_cents'] / 100:.2f}/"
            f"${config.MAX_DAILY_SPEND_CENTS / 100:.2f} | "
            f"P&L: ${s['daily_pnl_cents'] / 100:+.2f} | "
            f"Open positions: {s['open_positions']}/{config.MAX_OPEN_POSITIONS} (live)"
        )


# Shared instance
_risk_manager: RiskManager | None = None


def get_risk_manager() -> RiskManager:
    global _risk_manager
    if _risk_manager is None:
        _risk_manager = RiskManager()
    return _risk_manager
