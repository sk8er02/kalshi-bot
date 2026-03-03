"""
kalshi/markets.py — Market discovery, filtering, and opportunity scoring.

Uses an EVENTS-FIRST approach to find tradeable markets:
  1. Fetch events from interesting categories (Economics, Politics, Financials, Crypto)
  2. Extract nested markets from each event
  3. Filter out MVE/combo markets and zero-liquidity markets
  4. Score by opportunity (uncertainty, spread, volume, time)

This avoids scanning 4800+ open markets (99% are zero-liquidity sports combos).
"""

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import config
from kalshi.client import get_client
from utils.logger import get_logger

logger = get_logger(__name__)

# Market discovery cache — avoid re-scanning 1600 events every cycle
_market_cache: list | None = None
_market_cache_time: float = 0
_MARKET_CACHE_TTL: float = 600  # 10 minutes (shorter for crypto freshness)

# Categories worth trading — skip Sports and Entertainment
TRADEABLE_CATEGORIES = {
    "Economics",
    "Politics",
    "Elections",
    "Financials",
    "Companies",
    "Climate and Weather",
    "Science and Technology",
    "World",
    "Health",
    "Social",
    "Transportation",
    "Crypto",              # Short-term crypto markets (BTC, ETH, etc.)
    "Digital Assets",      # Alternate category name for crypto
}


def is_crypto_market(ticker: str) -> bool:
    """Check if a market ticker belongs to a crypto asset."""
    ticker_upper = ticker.upper()
    for prefix in config.CRYPTO_TICKER_PREFIXES:
        if ticker_upper.startswith(prefix):
            return True
    return False


@dataclass
class MarketInfo:
    ticker: str
    title: str
    event_ticker: str       # parent event
    series_ticker: str      # derived from event or ticker prefix
    yes_bid: int            # cents — highest buy offer for YES
    yes_ask: int            # cents — lowest sell offer for YES
    no_bid: int             # cents
    no_ask: int             # cents
    volume: int             # total contracts traded
    close_time: datetime
    open_interest: int
    category: str = ""
    opportunity_score: float = 0.0
    keywords: list[str] = field(default_factory=list)

    @property
    def spread_cents(self) -> int:
        return self.yes_ask - self.yes_bid

    @property
    def mid_price(self) -> float:
        return (self.yes_bid + self.yes_ask) / 2

    @property
    def days_to_resolution(self) -> float:
        delta = self.close_time - datetime.now(timezone.utc)
        return max(0, delta.total_seconds() / 86400)

    @property
    def minutes_to_resolution(self) -> float:
        delta = self.close_time - datetime.now(timezone.utc)
        return max(0, delta.total_seconds() / 60)

    @property
    def is_crypto(self) -> bool:
        return is_crypto_market(self.ticker)

    @property
    def is_tradeable(self) -> bool:
        # Basic checks for all markets
        if not (config.MIN_PRICE_CENTS <= self.yes_ask <= config.MAX_PRICE_CENTS):
            return False
        if self.spread_cents > 12 or self.yes_bid <= 0 or self.yes_ask >= 100:
            return False

        # Crypto: allow very short-term (down to 10 minutes) but skip yearly markets
        if self.is_crypto:
            if self.minutes_to_resolution < config.CRYPTO_MIN_MINUTES_TO_RESOLUTION:
                return False
            if self.days_to_resolution > config.CRYPTO_MAX_DAYS_TO_RESOLUTION:
                return False  # Skip yearly crypto markets (KXBTCMAXY, etc.)
            return True

        # Non-crypto: require at least MIN_DAYS_TO_RESOLUTION
        return self.days_to_resolution >= config.MIN_DAYS_TO_RESOLUTION


def _parse_datetime(ts_str: Optional[str]) -> Optional[datetime]:
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _extract_keywords(title: str) -> list[str]:
    """Extract meaningful keywords from a market title for news searching."""
    stop_words = {
        "will", "the", "a", "an", "be", "in", "on", "by", "of", "to",
        "is", "are", "at", "for", "and", "or", "not", "this", "that",
        "from", "with", "above", "below", "than", "more", "less",
    }
    words = [
        w.strip("?,.:()[]").lower()
        for w in title.split()
        if len(w) > 3
    ]
    return [w for w in words if w not in stop_words][:6]


def _derive_series_ticker(market_raw: dict, event_raw: dict) -> str:
    """
    Derive series_ticker for candlestick API.
    Try event's series_ticker first, then extract prefix from ticker.
    e.g. KXFED-26MAR14 → KXFED, INXY-26MAR03-B5.5 → INXY
    """
    # Check if event has series_ticker
    series = event_raw.get("series_ticker", "")
    if series:
        return series

    # Extract from market ticker: take everything before the first dash
    ticker = market_raw.get("ticker", "")
    if "-" in ticker:
        return ticker.split("-")[0]
    return ticker


def _is_mve_or_combo(market_raw: dict) -> bool:
    """Filter out MVE (multi-variate event) and combo markets."""
    # Markets with strike_type == "custom" are often combos
    if market_raw.get("strike_type") == "custom":
        return True
    # Markets belonging to an MVE collection
    if market_raw.get("mve_collection_ticker"):
        return True
    # Check for combo/parlay indicators in the ticker
    ticker = market_raw.get("ticker", "")
    if "COMBO" in ticker.upper() or "PARLAY" in ticker.upper():
        return True
    return False


def _score_market(market: MarketInfo) -> float:
    """
    Compute an opportunity score (higher = better trade candidate).

    Factors:
    - Uncertainty: closer to 50 cents = more room for mispricing
    - Spread: tighter spread = cheaper to enter and exit
    - Volume: more volume = better price discovery exists
    - Time: more time for the trade to play out (different for crypto)
    - Crypto boost: short-term crypto markets get a priority bonus
    """
    if not market.is_tradeable:
        return 0.0

    # Distance from 50 cents (0 at extremes, 1 at 50 cents)
    uncertainty = 1 - abs(market.mid_price - 50) / 50

    # Spread penalty (0 = 12c spread, 1 = 0c spread)
    spread_score = max(0, 1 - market.spread_cents / 12)

    # Volume score (log scale, capped)
    volume_score = min(1.0, math.log10(max(1, market.volume)) / 4)

    if market.is_crypto:
        # Crypto time scoring: favor 15 min to 4 hour windows
        minutes = market.minutes_to_resolution
        if minutes < 10:
            time_score = 0.0
        elif minutes <= 60:
            time_score = 0.9  # Sweet spot: 10-60 min
        elif minutes <= 240:
            time_score = 0.7  # OK: 1-4 hours
        else:
            time_score = 0.4  # Long crypto, less interesting

        # Crypto gets a priority boost
        crypto_boost = 0.15

        score = (
            uncertainty * 0.25
            + spread_score * 0.30
            + volume_score * 0.15
            + time_score * 0.15
            + crypto_boost
        )
    else:
        # Non-crypto time scoring: favor 3-30 day windows
        days = market.days_to_resolution
        if days < 2:
            time_score = 0.3
        elif days <= 30:
            time_score = min(1.0, days / 10)
        else:
            time_score = max(0.5, 1 - (days - 30) / 100)

        score = (
            uncertainty * 0.35
            + spread_score * 0.30
            + volume_score * 0.20
            + time_score * 0.15
        )

    return round(score, 4)


def _parse_market(market_raw: dict, event_raw: dict) -> Optional[MarketInfo]:
    """Parse a raw market dict into a MarketInfo, or None if unparseable."""
    close_time = _parse_datetime(market_raw.get("close_time"))
    if not close_time:
        return None

    yes_bid = market_raw.get("yes_bid", 0) or 0
    yes_ask = market_raw.get("yes_ask", 100) or 100

    market = MarketInfo(
        ticker=market_raw.get("ticker", ""),
        title=market_raw.get("title", ""),
        event_ticker=market_raw.get("event_ticker", event_raw.get("event_ticker", "")),
        series_ticker=_derive_series_ticker(market_raw, event_raw),
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=100 - yes_ask,
        no_ask=100 - yes_bid,
        volume=market_raw.get("volume", 0) or 0,
        close_time=close_time,
        open_interest=market_raw.get("open_interest", 0) or 0,
        category=event_raw.get("category", ""),
    )
    market.keywords = _extract_keywords(market.title)
    market.opportunity_score = _score_market(market)
    return market


def fetch_open_markets(max_pages: int = 8, force_refresh: bool = False) -> list[MarketInfo]:
    """
    Fetch tradeable markets using an events-first approach.
    Results are cached for 30 minutes to avoid re-scanning every cycle.

    1. Paginate through open events (with nested markets)
    2. Filter by category (skip Sports, Entertainment)
    3. Filter out MVE/combo markets and zero-liquidity markets
    4. Score and rank by opportunity

    max_pages limits API calls (~200 events/page). 8 pages covers ~1600 events
    which is enough to find all Economics, Politics, Financials markets.
    """
    global _market_cache, _market_cache_time

    now = time.time()
    if not force_refresh and _market_cache is not None and (now - _market_cache_time) < _MARKET_CACHE_TTL:
        logger.debug(
            f"Using cached market data ({len(_market_cache)} markets, "
            f"{int(now - _market_cache_time)}s old)"
        )
        return _market_cache

    client = get_client()
    all_markets: list[MarketInfo] = []
    events_seen = 0
    events_matched = 0
    markets_raw_count = 0
    cursor = None

    for page in range(max_pages):
        try:
            data = client.get_events(
                status="open",
                limit=200,
                cursor=cursor,
                with_nested_markets=True,
            )
        except Exception as e:
            logger.error(f"Failed to fetch events (page {page}): {e}")
            break

        events = data.get("events", [])
        if not events:
            break

        for event in events:
            events_seen += 1
            event_category = event.get("category", "")

            # Filter by category
            if event_category not in TRADEABLE_CATEGORIES:
                continue

            events_matched += 1

            # Extract nested markets from event
            nested_markets = event.get("markets", [])
            for mkt_raw in nested_markets:
                markets_raw_count += 1

                # Skip MVE/combo markets
                if _is_mve_or_combo(mkt_raw):
                    continue

                # Skip non-active markets (Kalshi uses "active" not "open")
                mkt_status = mkt_raw.get("status", "")
                if mkt_status not in ("open", "active"):
                    continue

                parsed = _parse_market(mkt_raw, event)
                if parsed and parsed.opportunity_score > 0:
                    all_markets.append(parsed)

        cursor = data.get("cursor")
        if not cursor:
            break

    logger.debug(
        f"Scanned {events_seen} events ({events_matched} in target categories) "
        f"across {min(page + 1, max_pages)} pages"
    )

    # Deduplicate by ticker (events can overlap)
    seen_tickers = set()
    unique_markets = []
    for m in all_markets:
        if m.ticker not in seen_tickers:
            seen_tickers.add(m.ticker)
            unique_markets.append(m)

    unique_markets.sort(key=lambda m: m.opportunity_score, reverse=True)
    tradeable = [m for m in unique_markets if m.is_tradeable]

    logger.info(
        f"Market discovery: {events_seen} events scanned, "
        f"{markets_raw_count} raw markets, "
        f"{len(tradeable)} tradeable out of {len(unique_markets)} scored"
    )

    if tradeable:
        top = tradeable[0]
        logger.info(
            f"Top opportunity: {top.ticker} | {top.title[:60]} | "
            f"bid={top.yes_bid}c ask={top.yes_ask}c spread={top.spread_cents}c | "
            f"vol={top.volume} | score={top.opportunity_score}"
        )

    # Cache results to avoid re-scanning every cycle
    _market_cache = tradeable
    _market_cache_time = time.time()

    return tradeable


def fetch_crypto_markets(force_refresh: bool = False) -> list[MarketInfo]:
    """
    Return only SHORT-TERM crypto markets from the full market list,
    sorted by opportunity score. Filters out yearly markets (>7 days).
    """
    all_markets = fetch_open_markets(force_refresh=force_refresh)
    # is_crypto check + is_tradeable already filters by max days
    crypto = [m for m in all_markets if m.is_crypto]
    crypto.sort(key=lambda m: m.opportunity_score, reverse=True)
    if crypto:
        logger.info(
            f"Short-term crypto: {len(crypto)} found (≤{config.CRYPTO_MAX_DAYS_TO_RESOLUTION}d) | "
            f"top: {crypto[0].ticker} (score={crypto[0].opportunity_score}, "
            f"{crypto[0].minutes_to_resolution:.0f}min to resolution)"
        )
    else:
        logger.debug("No short-term crypto markets found (all filtered out)")
    return crypto
