"""
kalshi/orders.py — Order placement and cancellation.

Always uses LIMIT orders to avoid paying the full spread.
In DRY_RUN mode, logs what would happen without touching the API.
"""

import time
from typing import Optional

import config
from kalshi.client import get_client, KalshiAPIError
from utils.logger import get_logger
from utils.state import record_open_trade, add_daily_spend

logger = get_logger(__name__)


def place_limit_buy(
    ticker: str,
    side: str,              # "yes" or "no"
    price_cents: int,       # limit price to pay
    contracts: int,
    ai_estimate_cents: Optional[int] = None,
    edge_cents: Optional[int] = None,
) -> Optional[dict]:
    """
    Place a GTC limit buy order.

    Returns the Kalshi order response dict, or None if DRY_RUN or error.
    """
    total_cost_cents = price_cents * contracts

    if config.DRY_RUN:
        logger.info(
            f"[DRY RUN] BUY {contracts}x {ticker} {side.upper()} @ {price_cents}c "
            f"(total: ${total_cost_cents / 100:.2f})"
        )
        trade_id = record_open_trade(
            ticker=ticker,
            side=side,
            contracts=contracts,
            price_cents=price_cents,
            total_cost_cents=total_cost_cents,
            order_id="dry-run",
            ai_estimate_cents=ai_estimate_cents,
            edge_cents=edge_cents,
        )
        add_daily_spend(total_cost_cents)
        return {"order_id": "dry-run", "trade_db_id": trade_id}

    order_body = {
        "ticker": ticker,
        "action": "buy",
        "side": side,
        "count": contracts,
        "time_in_force": "good_till_canceled",
        "client_order_id": f"bot_{int(time.time())}",
    }
    if side == "yes":
        order_body["yes_price"] = price_cents
    else:
        order_body["no_price"] = price_cents

    try:
        client = get_client()
        resp = client.create_order(order_body)
        order_id = resp.get("order", {}).get("order_id") or resp.get("order_id")

        trade_id = record_open_trade(
            ticker=ticker,
            side=side,
            contracts=contracts,
            price_cents=price_cents,
            total_cost_cents=total_cost_cents,
            order_id=order_id,
            ai_estimate_cents=ai_estimate_cents,
            edge_cents=edge_cents,
        )
        add_daily_spend(total_cost_cents)

        logger.info(
            f"Order placed: BUY {contracts}x {ticker} {side.upper()} @ {price_cents}c "
            f"order_id={order_id} db_id={trade_id}"
        )
        return {**resp, "trade_db_id": trade_id}

    except KalshiAPIError as e:
        logger.error(f"Failed to place order for {ticker}: {e}")
        return None


def place_limit_sell(
    ticker: str,
    side: str,           # "yes" or "no" — must match the side you bought
    price_cents: int,    # minimum price to accept
    contracts: int,
) -> Optional[dict]:
    """
    Place an IOC limit sell order to close a position.
    Uses reduce_only=True to prevent accidentally opening a short.
    """
    if config.DRY_RUN:
        logger.info(
            f"[DRY RUN] SELL {contracts}x {ticker} {side.upper()} @ {price_cents}c min"
        )
        return {"order_id": "dry-run-sell"}

    order_body = {
        "ticker": ticker,
        "action": "sell",
        "side": side,
        "count": contracts,
        "time_in_force": "immediate_or_cancel",
        "reduce_only": True,
    }
    if side == "yes":
        order_body["yes_price"] = price_cents
    else:
        order_body["no_price"] = price_cents

    try:
        client = get_client()
        resp = client.create_order(order_body)
        order_id = resp.get("order", {}).get("order_id") or resp.get("order_id")
        logger.info(
            f"Sell order placed: SELL {contracts}x {ticker} {side.upper()} "
            f"@ {price_cents}c min order_id={order_id}"
        )
        return resp
    except KalshiAPIError as e:
        logger.error(f"Failed to place sell order for {ticker}: {e}")
        return None


def cancel_stale_orders(max_age_minutes: int = 60) -> int:
    """
    Cancel GTC orders that have been resting (unfilled) for longer than max_age_minutes.
    Returns count of cancelled orders.
    """
    if config.DRY_RUN:
        logger.info(f"[DRY RUN] Would cancel stale orders older than {max_age_minutes} min")
        return 0

    client = get_client()
    try:
        orders_data = client.get_orders(status="resting")
        orders = orders_data.get("orders", [])
        if not orders:
            return 0

        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        cancelled = 0

        for order in orders:
            oid = order.get("order_id")
            created_str = order.get("created_time", "")
            if not oid or not created_str:
                continue

            try:
                created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                age_minutes = (now - created).total_seconds() / 60

                if age_minutes >= max_age_minutes:
                    try:
                        client.cancel_order(oid)
                        cancelled += 1
                        ticker = order.get("ticker", "unknown")
                        logger.info(
                            f"Cancelled stale order {oid} for {ticker} "
                            f"(age: {age_minutes:.0f} min)"
                        )
                    except KalshiAPIError as e:
                        logger.warning(f"Could not cancel stale order {oid}: {e}")
            except (ValueError, AttributeError):
                continue

        return cancelled
    except KalshiAPIError as e:
        logger.error(f"Failed to fetch orders for stale cleanup: {e}")
        return 0


def cancel_all_pending_orders() -> int:
    """Cancel all resting GTC orders. Returns count cancelled."""
    if config.DRY_RUN:
        logger.info("[DRY RUN] Would cancel all pending orders")
        return 0

    client = get_client()
    try:
        orders_data = client.get_orders(status="resting")
        orders = orders_data.get("orders", [])
        cancelled = 0
        for order in orders:
            oid = order.get("order_id")
            if oid:
                try:
                    client.cancel_order(oid)
                    cancelled += 1
                except KalshiAPIError as e:
                    logger.warning(f"Could not cancel order {oid}: {e}")
        logger.info(f"Cancelled {cancelled} pending orders")
        return cancelled
    except KalshiAPIError as e:
        logger.error(f"Failed to fetch pending orders: {e}")
        return 0


def calculate_position_size(
    price_cents: int,
    edge_cents: int,
) -> int:
    """
    Calculate how many contracts to buy based on edge magnitude and risk limits.

    Scales bet size with edge: larger edge → larger (but still conservative) position.
    """
    # Kelly-inspired: bet more when edge is bigger
    # Max edge we'll see is ~30 cents, max spend $5
    edge_fraction = min(1.0, edge_cents / 30)
    target_spend = config.MIN_TRADE_COST_CENTS + int(
        (config.MAX_TRADE_COST_CENTS - config.MIN_TRADE_COST_CENTS) * edge_fraction
    )
    contracts = max(1, target_spend // price_cents)
    # Cap so we never exceed MAX_TRADE_COST_CENTS
    while contracts * price_cents > config.MAX_TRADE_COST_CENTS and contracts > 1:
        contracts -= 1
    return contracts
