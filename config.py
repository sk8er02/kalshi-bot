"""
config.py — Central configuration for the Kalshi trading bot.

All tunable parameters live here. Import this module everywhere instead
of scattering magic numbers through the codebase. Values can be overridden
via environment variables in .env.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# Paths
# ============================================================
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

DB_PATH = DATA_DIR / "trades.db"
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# ============================================================
# Kalshi API
# ============================================================
KALSHI_API_KEY_ID: str = os.getenv("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY_PATH: str = os.getenv(
    "KALSHI_PRIVATE_KEY_PATH",
    str(BASE_DIR / "keys" / "private_key.pem"),
)
KALSHI_BASE_URL: str = os.getenv(
    "KALSHI_BASE_URL",
    "https://api.elections.kalshi.com/trade-api/v2",
)

# ============================================================
# OpenRouter / AI
# ============================================================
OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"

# Primary model: cost-efficient, high-quality reasoning
OPENROUTER_MODEL: str = "deepseek/deepseek-chat"
# Fallback: completely free (200 req/day limit)
OPENROUTER_FALLBACK_MODEL: str = "meta-llama/llama-3.3-70b-instruct:free"

# ============================================================
# Risk Management (all monetary values in cents)
# ============================================================
MAX_TRADE_COST_CENTS: int = int(os.getenv("MAX_TRADE_COST_CENTS", "500"))     # $5
MIN_TRADE_COST_CENTS: int = int(os.getenv("MIN_TRADE_COST_CENTS", "100"))     # $1
MAX_DAILY_SPEND_CENTS: int = int(os.getenv("MAX_DAILY_SPEND_CENTS", "5000"))  # $50
DAILY_LOSS_KILL_SWITCH_CENTS: int = int(
    os.getenv("DAILY_LOSS_KILL_SWITCH_CENTS", "2000")  # $20 loss → halt
)
MAX_OPEN_POSITIONS: int = 5

# ============================================================
# Signal Thresholds
# ============================================================
# Minimum price edge required between AI estimate and market price.
# Must exceed this to cover Kalshi's ~7% fee on winnings.
MIN_EDGE_CENTS: int = 8

# Minimum AI confidence (0-1) to act on a signal
MIN_AI_CONFIDENCE: float = 0.65

# Technical analysis signal thresholds
RSI_OVERSOLD: int = 35    # Below this → market undervalued (buy signal)
RSI_OVERBOUGHT: int = 65  # Above this → market overvalued (avoid/sell signal)

# ============================================================
# Position Management
# ============================================================
PROFIT_TARGET_PCT: float = 0.15     # Close at +15% gain
STOP_LOSS_PCT: float = 0.20         # Close at -20% loss
CLOSE_BEFORE_RESOLUTION_MINUTES: int = 60  # Exit 1 hour before resolution

# ============================================================
# Market Filtering
# ============================================================
MIN_PRICE_CENTS: int = 20           # Skip near-certain NO outcomes
MAX_PRICE_CENTS: int = 80           # Skip near-certain YES outcomes
MIN_DAYS_TO_RESOLUTION: float = 1.0  # Skip markets resolving very soon (non-crypto)
MIN_ORDERBOOK_DEPTH: int = 30       # Minimum contracts on each side

# How many markets to fetch news for (top N by opportunity score)
NEWS_FETCH_TOP_N: int = 10
# How many markets to run AI analysis on (most expensive step)
AI_ANALYZE_TOP_N: int = 5

# ============================================================
# Crypto Short-Term Trading
# ============================================================
# Ticker prefixes that identify crypto markets (BTC, ETH, SOL, etc.)
CRYPTO_TICKER_PREFIXES: list[str] = [
    "KXBTC", "KXETH", "KXSOL", "KXDOGE", "KXXRP", "KXADA",
    "KXBNB", "KXAVAX", "KXLINK", "KXDOT", "KXMATIC",
]
# Minimum time to resolution for crypto (10 minutes)
CRYPTO_MIN_MINUTES_TO_RESOLUTION: float = 10.0
# Maximum time to resolution for crypto — skip yearly markets (7 days = 10080 min)
CRYPTO_MAX_DAYS_TO_RESOLUTION: float = 7.0
# Close crypto positions before resolution (5 minutes before)
CRYPTO_CLOSE_BEFORE_RESOLUTION_MINUTES: int = 5
# Crypto trading cycle runs more frequently (every 2 minutes)
CRYPTO_CYCLE_INTERVAL_MINUTES: int = 2
# Crypto-specific position monitoring (every 3 minutes)
CRYPTO_POSITION_CHECK_MINUTES: int = 3
# How many crypto markets to analyze per cycle
CRYPTO_AI_ANALYZE_TOP_N: int = 3
# Lower edge requirement for crypto (high volume = lower fees effective)
CRYPTO_MIN_EDGE_CENTS: int = 5

# ============================================================
# News Sources (free RSS feeds, no API key required)
# ============================================================
NEWS_RSS_FEEDS: list[str] = [
    "https://feeds.reuters.com/reuters/topNews",
    "https://feeds.reuters.com/reuters/businessNews",
    "https://apnews.com/index.rss",
    "https://feeds.nbcnews.com/nbcnews/public/news",
]
NEWS_MAX_ARTICLES_PER_MARKET: int = 5
NEWS_CACHE_TTL_SECONDS: int = 300   # Cache news for 5 minutes

# ============================================================
# Scheduling
# ============================================================
DISCOVERY_INTERVAL_MINUTES: int = 5
POSITION_CHECK_INTERVAL_MINUTES: int = 15
TIMEZONE: str = "America/New_York"

# ============================================================
# Bot Behavior
# ============================================================
DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() == "true"
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

# ============================================================
# Validation
# ============================================================
def validate_config() -> list[str]:
    """Returns a list of configuration errors. Empty list means OK."""
    errors = []
    if not KALSHI_API_KEY_ID:
        errors.append("KALSHI_API_KEY_ID is not set in .env")
    if not Path(KALSHI_PRIVATE_KEY_PATH).exists():
        errors.append(f"Kalshi private key not found at: {KALSHI_PRIVATE_KEY_PATH}")
    if not OPENROUTER_API_KEY:
        errors.append("OPENROUTER_API_KEY is not set in .env")
    if MAX_TRADE_COST_CENTS < MIN_TRADE_COST_CENTS:
        errors.append("MAX_TRADE_COST_CENTS must be >= MIN_TRADE_COST_CENTS")
    return errors
