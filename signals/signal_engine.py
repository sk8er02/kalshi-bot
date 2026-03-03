"""
signals/signal_engine.py — Combines AI, news, and TA signals into a TradeSignal.

The signal engine is the core decision-making layer. It takes a market,
runs all analysis, and produces a single actionable recommendation.

Signal logic:
1. AI must find an edge (estimate ≠ market price by at least MIN_EDGE_CENTS)
2. AI must be sufficiently confident
3. TA must not contradict the direction (confirmation, not required to agree)
4. Market must pass all filters

Only after all gates pass does it produce an actionable signal.
"""

from dataclasses import dataclass
from typing import Optional

import config
from analysis.ai_analyzer import AIEstimate, estimate_probability
from analysis.news import NewsArticle, fetch_news_for_market
from analysis.technical import TechnicalSignal, fetch_technical_signal
from kalshi.markets import MarketInfo, is_crypto_market
from kalshi.orders import calculate_position_size
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class TradeSignal:
    ticker: str
    market_title: str
    action: str             # "buy_yes", "buy_no", or "skip"
    skip_reason: str        # Why we're skipping (if action == "skip")

    # Prices & sizing
    current_yes_ask: int    # cents
    ai_estimate: int        # cents (AI's probability)
    edge_cents: int         # |ai_estimate - market_price|
    suggested_side: str     # "yes" or "no"
    suggested_price: int    # limit order price in cents
    suggested_contracts: int

    # Signal components
    ai_confidence: float
    rsi: Optional[float]
    ta_signal: str          # "buy", "sell", "neutral"

    # For logging
    ai_reasoning: str
    ai_model: str


def _direction_from_edge(market_price: int, ai_estimate: int) -> str:
    """If AI estimates higher than market → buy YES. Lower → buy NO."""
    return "yes" if ai_estimate > market_price else "no"


def _ta_contradicts(
    direction: str, ta: TechnicalSignal
) -> bool:
    """
    Returns True if TA signal strongly contradicts the intended direction.
    We allow neutral TA, but reject strong opposing signals.
    """
    if ta.signal == "neutral":
        return False
    if direction == "yes" and ta.signal == "sell" and ta.confidence > 0.5:
        return True
    if direction == "no" and ta.signal == "buy" and ta.confidence > 0.5:
        return True
    return False


def analyze_market(
    market: MarketInfo,
    news_articles: Optional[list[NewsArticle]] = None,
    ai_estimate: Optional[AIEstimate] = None,
) -> TradeSignal:
    """
    Full signal analysis pipeline for a single market.

    If news_articles or ai_estimate are pre-fetched (for batch efficiency),
    pass them in to avoid redundant API calls.
    """

    def skip(reason: str) -> TradeSignal:
        return TradeSignal(
            ticker=market.ticker,
            market_title=market.title,
            action="skip",
            skip_reason=reason,
            current_yes_ask=market.yes_ask,
            ai_estimate=50,
            edge_cents=0,
            suggested_side="yes",
            suggested_price=market.yes_ask,
            suggested_contracts=0,
            ai_confidence=0.0,
            rsi=None,
            ta_signal="neutral",
            ai_reasoning="",
            ai_model="",
        )

    # Gate 1: Basic market filters (should already be filtered, but double-check)
    if not market.is_tradeable:
        return skip("Market failed basic tradability filters")

    # Gate 2: Fetch news (if not already provided)
    if news_articles is None:
        news_articles = fetch_news_for_market(market.keywords, market.title)

    # Gate 3: AI probability estimate
    if ai_estimate is None:
        close_date_str = market.close_time.strftime("%B %d, %Y") if market.close_time else None
        ai_estimate = estimate_probability(
            market.title,
            news_articles,
            market_price_cents=market.yes_ask,
            close_date=close_date_str,
        )

    if not ai_estimate.success:
        return skip(f"AI analysis failed: {ai_estimate.error}")

    if ai_estimate.confidence < config.MIN_AI_CONFIDENCE:
        return skip(
            f"AI confidence too low: {ai_estimate.confidence:.2f} < {config.MIN_AI_CONFIDENCE}"
        )

    # Gate 4: Edge calculation
    market_price = market.yes_ask  # cost to buy YES
    edge = ai_estimate.probability - market_price
    abs_edge = abs(edge)

    # Crypto gets a lower edge requirement (higher volume = more efficient)
    min_edge = (
        config.CRYPTO_MIN_EDGE_CENTS
        if is_crypto_market(market.ticker)
        else config.MIN_EDGE_CENTS
    )

    if abs_edge < min_edge:
        return skip(
            f"Edge too small: {abs_edge}c < {min_edge}c minimum "
            f"(AI={ai_estimate.probability}c vs market={market_price}c)"
        )

    direction = _direction_from_edge(market_price, ai_estimate.probability)

    # Adjust market price based on direction
    # If buying NO, the effective price is the NO ask (= 100 - YES bid)
    if direction == "yes":
        entry_price = market.yes_ask
    else:
        entry_price = market.no_ask

    # Gate 5: Technical analysis confirmation
    ta = fetch_technical_signal(market.series_ticker, market.ticker)

    if _ta_contradicts(direction, ta):
        rsi_str = f"{ta.rsi:.1f}" if ta.rsi is not None else "N/A"
        return skip(
            f"TA contradicts signal: intended={direction}, TA={ta.signal} "
            f"(RSI={rsi_str}, conf={ta.confidence:.2f})"
        )

    # Gate 6: Calculate position size
    contracts = calculate_position_size(entry_price, abs_edge)
    if contracts < 1:
        return skip("Position size rounds to 0 contracts")

    logger.info(
        f"Signal FOUND: {market.ticker} | Buy {direction.upper()} | "
        f"AI={ai_estimate.probability}c vs market={market_price}c | "
        f"edge={abs_edge}c | RSI={ta.rsi} | {contracts} contracts @ {entry_price}c"
    )

    return TradeSignal(
        ticker=market.ticker,
        market_title=market.title,
        action=f"buy_{direction}",
        skip_reason="",
        current_yes_ask=market.yes_ask,
        ai_estimate=ai_estimate.probability,
        edge_cents=abs_edge,
        suggested_side=direction,
        suggested_price=entry_price,
        suggested_contracts=contracts,
        ai_confidence=ai_estimate.confidence,
        rsi=ta.rsi,
        ta_signal=ta.signal,
        ai_reasoning=ai_estimate.reasoning,
        ai_model=ai_estimate.model_used,
    )


def analyze_markets_batch(
    markets: list[MarketInfo],
    news_fetch_limit: int = None,
    ai_analyze_limit: int = None,
) -> list[TradeSignal]:
    """
    Analyze a batch of markets and return all signals (including skips for debugging).

    news_fetch_limit: how many markets to fetch news for (top N by score)
    ai_analyze_limit: how many markets to run AI on (most expensive step)
    """
    news_limit = news_fetch_limit or config.NEWS_FETCH_TOP_N
    ai_limit = ai_analyze_limit or config.AI_ANALYZE_TOP_N

    signals: list[TradeSignal] = []

    # Step 1: Fetch news for top N markets
    markets_with_news: list[tuple[MarketInfo, list[NewsArticle]]] = []
    for market in markets[:news_limit]:
        news = fetch_news_for_market(market.keywords, market.title)
        markets_with_news.append((market, news))

    # Step 2: Run AI analysis on top M markets (by score)
    for i, (market, news) in enumerate(markets_with_news[:ai_limit]):
        logger.debug(f"Analyzing market {i + 1}/{min(ai_limit, len(markets_with_news))}: {market.ticker}")
        signal = analyze_market(market, news_articles=news)
        signals.append(signal)

    actionable = [s for s in signals if s.action != "skip"]
    logger.info(
        f"Batch analysis complete: {len(actionable)} actionable signals "
        f"out of {len(signals)} analyzed"
    )
    return signals
