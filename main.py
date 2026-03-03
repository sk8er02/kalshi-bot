"""
main.py — Kalshi AI Trading Bot entry point.

Runs scheduled jobs:
  1. trading_cycle()         every 5 min  — general market discovery + signals + orders
  2. crypto_trading_cycle()  every 2 min  — fast cycle for short-term BTC/ETH/crypto
  3. position_monitor()      every 15 min — check P&L, close positions at targets/stops
  4. stale_order_cleanup()   every 30 min — cancel unfilled GTC orders older than 60 min
  5. daily_reset()           midnight     — log P&L summary, reset kill switch

Start with: python main.py
Stop with:  Ctrl+C
"""

import sys
import signal
import time as _time
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED

import config
from config import validate_config
from utils.logger import get_logger
from utils.state import init_db, get_daily_stats, set_kill_switch
from kalshi.client import get_client
from kalshi.markets import fetch_open_markets, fetch_crypto_markets, is_crypto_market
from kalshi.orders import place_limit_buy, place_limit_sell, cancel_stale_orders
from kalshi.portfolio import get_open_positions, get_current_market_price, log_balance_summary
from signals.signal_engine import analyze_markets_batch
from risk.risk_manager import get_risk_manager, invalidate_position_cache
from utils.state import (
    get_open_trades, record_close_trade, get_open_trade_by_ticker
)
from utils.notifications import (
    notify_order_placed, notify_position_closed, notify_kill_switch,
    notify_daily_summary, notify_stale_orders_cancelled, notify_risk_blocked,
)

logger = get_logger(__name__)
scheduler = BlockingScheduler(timezone=config.TIMEZONE)

# Analyzed-market memory: avoid re-running AI on the same market every cycle
# {ticker: timestamp_of_last_analysis}
_analyzed_tickers: dict[str, float] = {}
_ANALYSIS_COOLDOWN: float = 3600        # Don't re-analyze non-crypto for 60 min
_CRYPTO_ANALYSIS_COOLDOWN: float = 300  # Re-analyze crypto every 5 min (fast moving)


# ============================================================
# Job 1: Trading Cycle (every 5 minutes)
# ============================================================

def _is_recently_analyzed(ticker: str) -> bool:
    """Check if we've already analyzed this market recently."""
    last_time = _analyzed_tickers.get(ticker, 0)
    cooldown = _CRYPTO_ANALYSIS_COOLDOWN if is_crypto_market(ticker) else _ANALYSIS_COOLDOWN
    return (_time.time() - last_time) < cooldown


def _mark_analyzed(ticker: str) -> None:
    """Record that we just analyzed this market."""
    _analyzed_tickers[ticker] = _time.time()
    # Prune old entries to prevent unbounded growth
    cutoff = _time.time() - _ANALYSIS_COOLDOWN * 2
    stale_keys = [k for k, v in _analyzed_tickers.items() if v < cutoff]
    for k in stale_keys:
        del _analyzed_tickers[k]


def trading_cycle() -> None:
    """
    Main trading loop:
    1. Fetch and score all open markets
    2. Filter out recently-analyzed markets (60-min cooldown)
    3. Run news + AI + TA analysis on top candidates
    4. Place limit orders for actionable signals that pass risk checks
    """
    logger.info("=== Trading cycle started ===")

    rm = get_risk_manager()
    rm.log_status()

    if rm.check_kill_switch():
        logger.warning("Kill switch active, skipping trading cycle")
        return

    # Discover markets
    markets = fetch_open_markets()
    if not markets:
        logger.warning("No tradeable markets found")
        return

    # Filter out recently-analyzed markets to save AI calls
    fresh_markets = [m for m in markets if not _is_recently_analyzed(m.ticker)]
    skipped = len(markets) - len(fresh_markets)
    if skipped > 0:
        logger.info(f"Skipping {skipped} recently-analyzed markets (cooldown)")

    if not fresh_markets:
        logger.info("All top markets were recently analyzed, nothing new to evaluate")
        return

    logger.info(f"Top fresh market: {fresh_markets[0].ticker} (score={fresh_markets[0].opportunity_score})")

    # Mark these markets as analyzed before running AI
    for m in fresh_markets[:config.AI_ANALYZE_TOP_N]:
        _mark_analyzed(m.ticker)

    # Generate signals for top fresh markets
    signals = analyze_markets_batch(fresh_markets)

    # Process actionable signals
    orders_placed = 0
    for sig in signals:
        if sig.action == "skip":
            logger.debug(f"SKIP {sig.ticker}: {sig.skip_reason}")
            continue

        cost_cents = sig.suggested_price * sig.suggested_contracts
        allowed, reason = rm.can_trade(sig.ticker, cost_cents)

        if not allowed:
            logger.info(f"Risk blocked {sig.ticker}: {reason}")
            notify_risk_blocked(sig.ticker, reason)
            continue

        # Place the order
        result = place_limit_buy(
            ticker=sig.ticker,
            side=sig.suggested_side,
            price_cents=sig.suggested_price,
            contracts=sig.suggested_contracts,
            ai_estimate_cents=sig.ai_estimate,
            edge_cents=sig.edge_cents,
        )

        if result:
            orders_placed += 1
            invalidate_position_cache()  # refresh live positions
            logger.info(
                f"Order placed: {sig.ticker} BUY {sig.suggested_side.upper()} "
                f"{sig.suggested_contracts}x @ {sig.suggested_price}c "
                f"| edge={sig.edge_cents}c | AI={sig.ai_estimate}c "
                f"| reasoning: {sig.ai_reasoning[:80]}"
            )
            notify_order_placed(
                ticker=sig.ticker,
                side=sig.suggested_side,
                contracts=sig.suggested_contracts,
                price_cents=sig.suggested_price,
                edge_cents=sig.edge_cents,
                ai_estimate=sig.ai_estimate,
                reasoning=sig.ai_reasoning,
            )

        # Don't flood the market — stop after 2 orders per cycle
        if orders_placed >= 2:
            logger.info("Placed 2 orders this cycle, stopping to avoid overtrading")
            break

    logger.info(f"=== Trading cycle done: {orders_placed} orders placed ===")


# ============================================================
# Job 2: Crypto Fast Cycle (every 2 minutes)
# ============================================================

def crypto_trading_cycle() -> None:
    """
    Fast trading loop focused on short-term crypto markets (BTC, ETH, etc.).
    Runs every 2 minutes to catch 15-min and hourly crypto markets.
    Uses shorter analysis cooldown (5 min) since prices move fast.
    """
    logger.info("=== Crypto cycle started ===")

    rm = get_risk_manager()
    if rm.check_kill_switch():
        return

    # Get crypto markets only
    crypto_markets = fetch_crypto_markets()
    if not crypto_markets:
        logger.debug("No tradeable crypto markets found")
        return

    # Filter recently analyzed
    fresh = [m for m in crypto_markets if not _is_recently_analyzed(m.ticker)]
    if not fresh:
        logger.debug("All crypto markets recently analyzed")
        return

    logger.info(
        f"Crypto: {len(fresh)} fresh markets | "
        f"top: {fresh[0].ticker} ({fresh[0].minutes_to_resolution:.0f}min left, "
        f"score={fresh[0].opportunity_score})"
    )

    # Mark and analyze
    for m in fresh[:config.CRYPTO_AI_ANALYZE_TOP_N]:
        _mark_analyzed(m.ticker)

    signals = analyze_markets_batch(
        fresh,
        ai_analyze_limit=config.CRYPTO_AI_ANALYZE_TOP_N,
        news_fetch_limit=config.CRYPTO_AI_ANALYZE_TOP_N,
    )

    orders_placed = 0
    for sig in signals:
        if sig.action == "skip":
            continue

        cost_cents = sig.suggested_price * sig.suggested_contracts
        allowed, reason = rm.can_trade(sig.ticker, cost_cents)
        if not allowed:
            logger.info(f"Risk blocked crypto {sig.ticker}: {reason}")
            continue

        result = place_limit_buy(
            ticker=sig.ticker,
            side=sig.suggested_side,
            price_cents=sig.suggested_price,
            contracts=sig.suggested_contracts,
            ai_estimate_cents=sig.ai_estimate,
            edge_cents=sig.edge_cents,
        )

        if result:
            orders_placed += 1
            invalidate_position_cache()
            logger.info(
                f"CRYPTO order: {sig.ticker} BUY {sig.suggested_side.upper()} "
                f"{sig.suggested_contracts}x @ {sig.suggested_price}c "
                f"| edge={sig.edge_cents}c | AI={sig.ai_estimate}c"
            )
            notify_order_placed(
                ticker=sig.ticker,
                side=sig.suggested_side,
                contracts=sig.suggested_contracts,
                price_cents=sig.suggested_price,
                edge_cents=sig.edge_cents,
                ai_estimate=sig.ai_estimate,
                reasoning=f"[CRYPTO] {sig.ai_reasoning}",
            )
            if orders_placed >= 2:
                break

    logger.info(f"=== Crypto cycle done: {orders_placed} orders placed ===")


# ============================================================
# Job 3: Position Monitor (every 15 min general, every 3 min crypto)
# ============================================================

def position_monitor() -> None:
    """
    Check ALL open positions (both bot-placed and manual website trades).

    Uses LIVE Kalshi positions as the source of truth, not just the local DB.
    Closes positions if:
    - Profit target reached (+15%)
    - Stop-loss triggered (-20%)
    - Market resolves within CLOSE_BEFORE_RESOLUTION_MINUTES
    """
    logger.info("--- Position monitor started ---")

    # Get live positions from Kalshi (includes manual website trades)
    live_positions = get_open_positions()
    # Get our bot-tracked open trades from DB
    db_trades = get_open_trades()

    if not live_positions:
        logger.debug("No open positions on Kalshi")
        return

    # Index DB trades by ticker for quick lookup
    db_by_ticker = {}
    for t in db_trades:
        db_by_ticker[t["ticker"]] = t

    logger.info(
        f"Monitoring {len(live_positions)} live positions "
        f"({len(db_trades)} bot-tracked)"
    )

    positions_closed = 0

    for pos in live_positions:
        ticker = pos.ticker
        side = pos.side
        contracts = pos.contracts
        avg_cost_cents = pos.avg_price_cents
        db_trade = db_by_ticker.get(ticker)
        source = "bot" if db_trade else "manual"

        # Get current market price for our side
        current_price = get_current_market_price(ticker, side)
        if current_price is None:
            logger.warning(f"Could not get price for {ticker}, skipping")
            continue

        # Calculate unrealized P&L
        pnl_cents = (current_price - avg_cost_cents) * contracts
        pnl_pct = (current_price - avg_cost_cents) / max(1, avg_cost_cents)

        logger.debug(
            f"[{source}] {ticker} {side.upper()}: {contracts}x avg={avg_cost_cents}c, "
            f"current={current_price}c, P&L={pnl_pct:+.1%} (${pnl_cents / 100:+.2f})"
        )

        close_reason = None

        # Check profit target
        if pnl_pct >= config.PROFIT_TARGET_PCT:
            close_reason = "profit_target"
            logger.info(
                f"PROFIT TARGET HIT [{source}]: {ticker} +{pnl_pct:.1%} "
                f"(${pnl_cents / 100:+.2f})"
            )

        # Check stop-loss
        elif pnl_pct <= -config.STOP_LOSS_PCT:
            close_reason = "stop_loss"
            logger.info(
                f"STOP LOSS TRIGGERED [{source}]: {ticker} {pnl_pct:.1%} "
                f"(${pnl_cents / 100:+.2f})"
            )

        # Check if market is resolving soon
        else:
            try:
                market_data = get_client().get_market(ticker)
                market_info = market_data.get("market", market_data)
                close_time_str = market_info.get("close_time")
                if close_time_str:
                    close_time = datetime.fromisoformat(
                        close_time_str.replace("Z", "+00:00")
                    )
                    minutes_left = (
                        close_time - datetime.now(timezone.utc)
                    ).total_seconds() / 60
                    # Use shorter close window for crypto
                    close_window = (
                        config.CRYPTO_CLOSE_BEFORE_RESOLUTION_MINUTES
                        if is_crypto_market(ticker)
                        else config.CLOSE_BEFORE_RESOLUTION_MINUTES
                    )
                    if minutes_left <= close_window:
                        close_reason = "approaching_resolution"
                        logger.info(
                            f"CLOSING [{source}] (resolution in {minutes_left:.0f}min): {ticker}"
                        )
            except Exception as e:
                logger.warning(f"Could not check resolution time for {ticker}: {e}")

        if close_reason:
            # Place sell order at slightly below current price (ensure fill)
            sell_price = max(1, current_price - 2)
            result = place_limit_sell(
                ticker=ticker,
                side=side,
                price_cents=sell_price,
                contracts=contracts,
            )

            if result or config.DRY_RUN:
                # Record in DB if we have a tracked trade
                if db_trade:
                    record_close_trade(
                        trade_id=db_trade["id"],
                        close_reason=close_reason,
                        realized_pnl_cents=pnl_cents,
                    )
                positions_closed += 1
                notify_position_closed(
                    ticker=ticker,
                    side=side,
                    reason=close_reason,
                    pnl_cents=pnl_cents,
                    pnl_pct=pnl_pct,
                    source=source,
                )

    log_balance_summary()
    logger.info(f"--- Position monitor done: {positions_closed} positions closed ---")


# ============================================================
# Job 4: Stale Order Cleanup (every 30 minutes)
# ============================================================

def stale_order_cleanup() -> None:
    """Cancel unfilled GTC orders older than 60 minutes."""
    logger.debug("--- Stale order cleanup ---")
    try:
        count = cancel_stale_orders(max_age_minutes=60)
        if count > 0:
            logger.info(f"Cancelled {count} stale unfilled orders")
            notify_stale_orders_cancelled(count)
    except Exception as e:
        logger.error(f"Stale order cleanup failed: {e}")


# ============================================================
# Job 5: Daily Reset (midnight)
# ============================================================

def daily_reset() -> None:
    """Log daily P&L summary and reset the kill switch for a new day."""
    stats = get_daily_stats()
    pnl = stats.get("realized_pnl_cents", 0)
    spent = stats.get("total_spent_cents", 0)
    trades = stats.get("trades_placed", 0)

    logger.info(
        f"=== DAILY SUMMARY ==="
        f"\n  Trades placed: {trades}"
        f"\n  Total spent:   ${spent / 100:.2f}"
        f"\n  Realized P&L:  ${pnl / 100:+.2f}"
        f"\n  Kill switch:   {'TRIPPED' if stats.get('kill_switch_tripped') else 'OK'}"
    )

    # Get live positions for balance in notification
    try:
        live_pos = get_open_positions()
        balance = get_client().get_balance()
        balance_cents = balance.get("balance", 0)
    except Exception:
        live_pos = []
        balance_cents = 0

    notify_daily_summary(
        trades=trades,
        spent_cents=spent,
        pnl_cents=pnl,
        open_positions=len(live_pos),
        balance_cents=balance_cents,
    )

    # Reset kill switch for the new day
    set_kill_switch(False)
    # Clear analyzed-market memory for fresh start
    _analyzed_tickers.clear()
    logger.info("Kill switch reset for new trading day")


# ============================================================
# Scheduler event listener
# ============================================================

def _on_job_event(event) -> None:
    if event.exception:
        logger.error(
            f"Job '{event.job_id}' raised an exception: {event.exception}",
            exc_info=True,
        )


# ============================================================
# Startup
# ============================================================

def _startup_checks() -> None:
    """Verify configuration and connectivity before starting the scheduler."""
    from kalshi.client import MockKalshiClient

    errors = validate_config()
    if errors:
        for e in errors:
            logger.error(f"Config error: {e}")
        if not config.DRY_RUN:
            logger.error("Fix configuration errors before running in live mode")
            sys.exit(1)
        else:
            logger.warning("Config errors present but DRY_RUN=true, continuing...")

    # Initialize database first
    init_db()

    # Test Kalshi connectivity (or use mock)
    try:
        client = get_client()
        if isinstance(client, MockKalshiClient):
            logger.info(
                "Running with MOCK market data (no Kalshi credentials). "
                "Simulated balance: $100.00"
            )
        else:
            balance = client.get_balance()
            bal_cents = balance.get("balance", 0)
            logger.info(f"Kalshi connected. Available balance: ${bal_cents / 100:.2f}")
    except Exception as e:
        logger.error(f"Cannot connect to Kalshi API: {e}")
        if not config.DRY_RUN:
            sys.exit(1)
        else:
            logger.warning("Kalshi connection failed but DRY_RUN=true, continuing...")

    mode = "DRY RUN (no real orders)" if config.DRY_RUN else "LIVE TRADING"
    logger.info(f"Bot starting in {mode} mode")
    logger.info(
        f"Risk limits: ${config.MAX_TRADE_COST_CENTS / 100:.0f}/trade | "
        f"${config.MAX_DAILY_SPEND_CENTS / 100:.0f}/day | "
        f"kill switch at -${config.DAILY_LOSS_KILL_SWITCH_CENTS / 100:.0f}"
    )


def _handle_shutdown(signum, frame) -> None:
    logger.info("Shutdown signal received, stopping scheduler...")
    scheduler.shutdown(wait=False)
    sys.exit(0)


def main() -> None:
    _startup_checks()

    # Register signal handlers for clean shutdown
    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    # Add jobs
    scheduler.add_job(
        trading_cycle,
        "interval",
        minutes=config.DISCOVERY_INTERVAL_MINUTES,
        id="trading_cycle",
        max_instances=1,          # Never run overlapping cycles
        misfire_grace_time=60,    # Skip if delayed >60s
        next_run_time=datetime.now(timezone.utc),  # Run immediately on start
    )

    scheduler.add_job(
        crypto_trading_cycle,
        "interval",
        minutes=config.CRYPTO_CYCLE_INTERVAL_MINUTES,
        id="crypto_trading_cycle",
        max_instances=1,
        misfire_grace_time=30,
        # Stagger 90s after main cycle to avoid simultaneous AI request bursts
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=90),
    )

    scheduler.add_job(
        position_monitor,
        "interval",
        minutes=config.POSITION_CHECK_INTERVAL_MINUTES,
        id="position_monitor",
        max_instances=1,
        misfire_grace_time=60,
    )

    scheduler.add_job(
        stale_order_cleanup,
        "interval",
        minutes=30,
        id="stale_order_cleanup",
        max_instances=1,
        misfire_grace_time=60,
    )

    scheduler.add_job(
        daily_reset,
        "cron",
        hour=0,
        minute=1,
        id="daily_reset",
    )

    scheduler.add_listener(_on_job_event, EVENT_JOB_ERROR | EVENT_JOB_EXECUTED)

    logger.info("Scheduler started. Press Ctrl+C to stop.")
    scheduler.start()


if __name__ == "__main__":
    main()
