"""
kalshi/portfolio.py — Account balance and position management.

Provides helpers for reading current positions and computing unrealized P&L.
"""

from dataclasses import dataclass
from typing import Optional

from kalshi.client import get_client, KalshiAPIError
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class Position:
    ticker: str
    side: str               # "yes" or "no" — which side we hold
    contracts: int          # number of contracts held
    avg_price_cents: int    # average cost per contract in cents
    current_price_cents: int = 0
    db_trade_id: Optional[int] = None

    @property
    def cost_basis_cents(self) -> int:
        return self.avg_price_cents * self.contracts

    @property
    def current_value_cents(self) -> int:
        return self.current_price_cents * self.contracts

    @property
    def unrealized_pnl_cents(self) -> int:
        return self.current_value_cents - self.cost_basis_cents

    @property
    def unrealized_pnl_pct(self) -> float:
        if self.cost_basis_cents == 0:
            return 0.0
        return self.unrealized_pnl_cents / self.cost_basis_cents


def get_balance() -> dict:
    """
    Returns account balance info.
    Keys: available_cents, portfolio_value_cents, total_cents
    """
    try:
        data = get_client().get_balance()
        balance = data.get("balance", 0)
        portfolio = data.get("portfolio_value", 0)
        return {
            "available_cents": balance,
            "portfolio_value_cents": portfolio,
            "total_cents": balance + portfolio,
        }
    except KalshiAPIError as e:
        logger.error(f"Failed to fetch balance: {e}")
        return {"available_cents": 0, "portfolio_value_cents": 0, "total_cents": 0}


def get_open_positions() -> list[Position]:
    """
    Fetch current open positions from Kalshi.

    Returns positions where we actually hold contracts (position != 0).
    """
    try:
        data = get_client().get_positions()
        raw_positions = data.get("market_positions", [])
    except KalshiAPIError as e:
        logger.error(f"Failed to fetch positions: {e}")
        return []

    positions = []
    for raw in raw_positions:
        pos_size = raw.get("position", 0)
        if pos_size == 0:
            continue

        # Kalshi reports position as positive for YES, negative for NO
        side = "yes" if pos_size > 0 else "no"
        contracts = abs(pos_size)

        # Calculate average cost per contract from total_traded / contracts
        # total_traded = total cents spent on this position
        total_traded = raw.get("total_traded", 0) or 0
        if contracts > 0 and total_traded > 0:
            avg_price = total_traded // contracts
        else:
            avg_price = 0

        positions.append(
            Position(
                ticker=raw.get("ticker", ""),
                side=side,
                contracts=contracts,
                avg_price_cents=avg_price,
            )
        )

    logger.debug(f"Found {len(positions)} open positions")
    return positions


def get_current_market_price(ticker: str, side: str) -> Optional[int]:
    """
    Get the current best bid for our side (what we could sell at right now).
    Returns None if the market can't be fetched.
    """
    try:
        data = get_client().get_orderbook(ticker)
        ob = data.get("orderbook", {})
        if side == "yes":
            bids = ob.get("yes", [])
        else:
            bids = ob.get("no", [])
        if bids:
            # Bids are sorted ascending [price, quantity]; last is highest (best sell price)
            best_bid = bids[-1][0] if isinstance(bids[-1], list) else bids[-1].get("price", 0)
            return int(best_bid)
    except Exception as e:
        logger.warning(f"Could not fetch orderbook for {ticker}: {e}")
    return None


def log_balance_summary() -> None:
    """Log a human-readable account summary."""
    bal = get_balance()
    positions = get_open_positions()
    logger.info(
        f"Account: ${bal['available_cents'] / 100:.2f} cash | "
        f"${bal['portfolio_value_cents'] / 100:.2f} in positions | "
        f"{len(positions)} open trades"
    )
