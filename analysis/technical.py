"""
analysis/technical.py — Technical analysis on Kalshi market price history.

Fetches candlestick data from Kalshi and applies indicators via pandas-ta.
Prediction market prices behave as bounded time series (0-100), so standard
TA indicators still apply but need adapted interpretation.

Key insight: in prediction markets, prices converge to 0 or 100 at resolution.
RSI and momentum work well for detecting short-term mispricings.
"""

from dataclasses import dataclass
from typing import Optional

import pandas as pd

try:
    import pandas_ta as ta
    _HAS_PANDAS_TA = True
except ImportError:
    _HAS_PANDAS_TA = False

from kalshi.client import get_client, KalshiAPIError
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class TechnicalSignal:
    ticker: str
    signal: str             # "buy", "sell", or "neutral"
    rsi: Optional[float]    # RSI(14) value
    trend: str              # "up", "down", or "flat"
    momentum: str           # "bullish", "bearish", or "neutral"
    price_change_3h: float  # % price change over last 3 candles (15min each)
    confidence: float       # 0-1, how strong the signal is


def _compute_rsi(series: pd.Series, period: int = 14) -> Optional[float]:
    """Pure-Python RSI fallback if pandas-ta is unavailable."""
    if len(series) < period + 1:
        return None
    if _HAS_PANDAS_TA:
        result = ta.rsi(series, length=period)
        if result is not None and not result.empty:
            return float(result.iloc[-1])
    # Manual RSI calculation
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.rolling(period).mean().iloc[-1]
    avg_loss = loss.rolling(period).mean().iloc[-1]
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def fetch_technical_signal(
    series_ticker: str,
    market_ticker: str,
) -> TechnicalSignal:
    """
    Fetch last 50 15-minute candles and compute technical signal.

    Returns a neutral signal on any data error.
    """
    _neutral = TechnicalSignal(
        ticker=market_ticker,
        signal="neutral",
        rsi=None,
        trend="flat",
        momentum="neutral",
        price_change_3h=0.0,
        confidence=0.0,
    )

    if not series_ticker:
        logger.debug(f"No series_ticker for {market_ticker}, skipping TA")
        return _neutral

    try:
        client = get_client()
        data = client.get_candlesticks(
            series_ticker=series_ticker,
            market_ticker=market_ticker,
            period_seconds=900,   # 15-minute candles
            limit=50,
        )
    except KalshiAPIError as e:
        logger.debug(f"Could not fetch candlesticks for {market_ticker}: {e}")
        return _neutral
    except Exception as e:
        logger.debug(f"Unexpected error fetching candlesticks for {market_ticker}: {e}")
        return _neutral

    candles = data.get("candlesticks", [])
    if len(candles) < 15:
        logger.debug(f"Too few candles for {market_ticker} ({len(candles)}), skipping TA")
        return _neutral

    # Build DataFrame
    rows = []
    for c in candles:
        rows.append({
            "ts": c.get("ts", c.get("end_period_ts", 0)),
            "open": c.get("open", {}).get("yes", 50),
            "high": c.get("high", {}).get("yes", 50),
            "low": c.get("low", {}).get("yes", 50),
            "close": c.get("close", {}).get("yes", 50),
            "volume": c.get("volume", 0),
        })

    df = pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)
    closes = df["close"].astype(float)

    # RSI
    rsi_value = _compute_rsi(closes, period=14)

    # EMA trend (9 vs 21 period)
    if _HAS_PANDAS_TA and len(closes) >= 21:
        ema9 = ta.ema(closes, length=9)
        ema21 = ta.ema(closes, length=21)
        if ema9 is not None and ema21 is not None and not ema9.empty and not ema21.empty:
            trend = "up" if float(ema9.iloc[-1]) > float(ema21.iloc[-1]) else "down"
        else:
            trend = "flat"
    elif len(closes) >= 9:
        recent_avg = closes.iloc[-9:].mean()
        older_avg = closes.iloc[-21:-9].mean() if len(closes) >= 21 else closes.mean()
        trend = "up" if recent_avg > older_avg else "down"
    else:
        trend = "flat"

    # 3-candle momentum (last 45 minutes)
    if len(closes) >= 4:
        price_change_3h = float((closes.iloc[-1] - closes.iloc[-4]) / max(1, closes.iloc[-4]))
    else:
        price_change_3h = 0.0

    momentum = (
        "bullish" if price_change_3h > 0.02
        else "bearish" if price_change_3h < -0.02
        else "neutral"
    )

    # Determine signal
    signal = "neutral"
    confidence = 0.0

    if rsi_value is not None:
        if rsi_value < 35 and trend == "down":
            # Oversold and downtrending → mean reversion buy opportunity
            signal = "buy"
            confidence = min(1.0, (35 - rsi_value) / 25)
        elif rsi_value > 65 and trend == "up":
            # Overbought → exit or avoid buying
            signal = "sell"
            confidence = min(1.0, (rsi_value - 65) / 25)
        elif rsi_value < 40 and momentum == "bullish":
            signal = "buy"
            confidence = 0.3
        elif rsi_value > 60 and momentum == "bearish":
            signal = "sell"
            confidence = 0.3

    return TechnicalSignal(
        ticker=market_ticker,
        signal=signal,
        rsi=rsi_value,
        trend=trend,
        momentum=momentum,
        price_change_3h=price_change_3h,
        confidence=confidence,
    )
