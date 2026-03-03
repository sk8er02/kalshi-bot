"""
kalshi/client.py — Authenticated Kalshi API HTTP client.

Handles RSA request signing required by Kalshi's v2 API.
All other modules import `get_client()` to get a shared instance.

When DRY_RUN=true and no API credentials are configured, a MockKalshiClient
is returned instead, providing realistic fake market data so you can test
the full pipeline offline.
"""

import base64
import math
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

import config
from utils.logger import get_logger

logger = get_logger(__name__)

_client_instance = None


class KalshiAPIError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"Kalshi API error {status_code}: {message}")


# ================================================================== #
# Real Kalshi API client
# ================================================================== #

class KalshiClient:
    """
    Thin wrapper around Kalshi's REST API with RSA authentication.

    Kalshi requires each request to be signed with your RSA private key:
    - KALSHI-ACCESS-KEY: your API key ID
    - KALSHI-ACCESS-TIMESTAMP: current Unix timestamp in milliseconds
    - KALSHI-ACCESS-SIGNATURE: base64(RSA_sign(timestamp + method + path))
    """

    def __init__(self) -> None:
        self.base_url = config.KALSHI_BASE_URL.rstrip("/")
        self.api_key_id = config.KALSHI_API_KEY_ID
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})
        self._last_request_time: float = 0.0
        self._min_request_interval: float = 0.35  # ~3 requests/second max

        # Load private key once at startup
        try:
            with open(config.KALSHI_PRIVATE_KEY_PATH, "rb") as f:
                self._private_key = serialization.load_pem_private_key(
                    f.read(), password=None
                )
            logger.info("Kalshi RSA private key loaded successfully")
        except FileNotFoundError:
            raise RuntimeError(
                f"Private key not found at {config.KALSHI_PRIVATE_KEY_PATH}. "
                "Download it from kalshi.com/profile/api-keys"
            )

    def _sign(self, timestamp_ms: int, method: str, full_path: str) -> str:
        """Create RSA-PSS SHA256 signature for the request."""
        message = f"{timestamp_ms}{method.upper()}{full_path}".encode()
        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode()

    def _auth_headers(self, method: str, full_path: str) -> dict[str, str]:
        ts = int(time.time() * 1000)
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": str(ts),
            "KALSHI-ACCESS-SIGNATURE": self._sign(ts, method, full_path),
        }

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        require_auth: bool = True,
    ) -> Any:
        url = f"{self.base_url}{path}"
        # Signing requires the FULL URI path (e.g. /trade-api/v2/portfolio/balance)
        from urllib.parse import urlparse
        full_path = urlparse(url).path
        headers = self._auth_headers(method, full_path) if require_auth else {}

        # Rate limiting: wait between requests to avoid 429s
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < self._min_request_interval:
            time.sleep(self._min_request_interval - elapsed)

        try:
            resp = self._session.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                timeout=15,
            )
        except requests.RequestException as e:
            raise KalshiAPIError(0, f"Network error: {e}") from e
        finally:
            self._last_request_time = time.time()

        # Handle rate limiting with retry
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "2"))
            logger.warning(f"Rate limited, sleeping {retry_after}s before retry")
            time.sleep(retry_after)
            return self._request(method, path, params, json_body, require_auth)

        if not resp.ok:
            raise KalshiAPIError(resp.status_code, resp.text[:500])

        return resp.json()

    # ---------------------------------------------------------------- #
    # Public market data (no auth needed)
    # ---------------------------------------------------------------- #

    def get_events(
        self,
        status: str = "open",
        limit: int = 200,
        cursor: Optional[str] = None,
        series_ticker: Optional[str] = None,
        with_nested_markets: bool = True,
    ) -> dict:
        params = {"status": status, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        if series_ticker:
            params["series_ticker"] = series_ticker
        if with_nested_markets:
            params["with_nested_markets"] = "true"
        return self._request("GET", "/events", params=params, require_auth=False)

    def get_event(self, event_ticker: str) -> dict:
        return self._request(
            "GET", f"/events/{event_ticker}", require_auth=False
        )

    def get_markets(
        self,
        status: str = "open",
        limit: int = 200,
        cursor: Optional[str] = None,
        event_ticker: Optional[str] = None,
        series_ticker: Optional[str] = None,
    ) -> dict:
        params = {"status": status, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        if event_ticker:
            params["event_ticker"] = event_ticker
        if series_ticker:
            params["series_ticker"] = series_ticker
        return self._request("GET", "/markets", params=params, require_auth=False)

    def get_market(self, ticker: str) -> dict:
        return self._request("GET", f"/markets/{ticker}", require_auth=False)

    def get_orderbook(self, ticker: str) -> dict:
        return self._request("GET", f"/markets/{ticker}/orderbook", require_auth=False)

    def get_candlesticks(
        self,
        series_ticker: str,
        market_ticker: str,
        period_seconds: int = 900,
        limit: int = 50,
    ) -> dict:
        path = f"/series/{series_ticker}/markets/{market_ticker}/candlesticks"
        params = {"period_seconds": period_seconds, "limit": limit}
        return self._request("GET", path, params=params, require_auth=False)

    # ---------------------------------------------------------------- #
    # Authenticated portfolio endpoints
    # ---------------------------------------------------------------- #

    def get_balance(self) -> dict:
        return self._request("GET", "/portfolio/balance")

    def get_positions(self) -> dict:
        return self._request("GET", "/portfolio/positions")

    def get_orders(self, status: Optional[str] = None) -> dict:
        params = {}
        if status:
            params["status"] = status
        return self._request("GET", "/portfolio/orders", params=params)

    def create_order(self, order: dict) -> dict:
        return self._request("POST", "/portfolio/orders", json_body=order)

    def cancel_order(self, order_id: str) -> dict:
        return self._request("DELETE", f"/portfolio/orders/{order_id}")


# ================================================================== #
# Mock client for offline dry-run testing
# ================================================================== #

_MOCK_MARKETS = [
    {"ticker": "KXFED-26MAR14", "series_ticker": "KXFED",
     "title": "Will the Fed cut rates at the March 2026 meeting?",
     "yes_bid": 38, "yes_ask": 42, "volume": 12450, "open_interest": 3200},
    {"ticker": "KXCPI-26FEB28", "series_ticker": "KXCPI",
     "title": "Will February 2026 CPI year-over-year be above 3.0%?",
     "yes_bid": 55, "yes_ask": 58, "volume": 8700, "open_interest": 2100},
    {"ticker": "KXBTC-26MAR31", "series_ticker": "KXBTC",
     "title": "Will Bitcoin be above $100,000 on March 31?",
     "yes_bid": 62, "yes_ask": 65, "volume": 31000, "open_interest": 8500},
    {"ticker": "KXGDP-26Q1", "series_ticker": "KXGDP",
     "title": "Will Q1 2026 GDP growth be above 2.0%?",
     "yes_bid": 48, "yes_ask": 52, "volume": 6300, "open_interest": 1800},
    {"ticker": "KXUNEMP-26MAR", "series_ticker": "KXUNEMP",
     "title": "Will March 2026 unemployment rate be below 4.0%?",
     "yes_bid": 70, "yes_ask": 73, "volume": 5400, "open_interest": 1500},
    {"ticker": "KXSP500-26MAR31", "series_ticker": "KXSP500",
     "title": "Will the S&P 500 close above 6,000 on March 31?",
     "yes_bid": 44, "yes_ask": 47, "volume": 18900, "open_interest": 4700},
    {"ticker": "KXGAS-26MAR", "series_ticker": "KXGAS",
     "title": "Will national average gas price exceed $3.50 in March?",
     "yes_bid": 30, "yes_ask": 34, "volume": 4100, "open_interest": 950},
    {"ticker": "KXGOLD-26MAR31", "series_ticker": "KXGOLD",
     "title": "Will gold be above $2,800 per ounce on March 31?",
     "yes_bid": 53, "yes_ask": 56, "volume": 7200, "open_interest": 2000},
    {"ticker": "KXTSLA-26Q1", "series_ticker": "KXTSLA",
     "title": "Will Tesla deliver more than 500,000 vehicles in Q1 2026?",
     "yes_bid": 35, "yes_ask": 39, "volume": 9800, "open_interest": 2600},
    {"ticker": "KXRAIN-26FEB28", "series_ticker": "KXRAIN",
     "title": "Will Los Angeles receive more than 2 inches of rain in February?",
     "yes_bid": 25, "yes_ask": 29, "volume": 2200, "open_interest": 600},
    {"ticker": "KXNFP-26MAR", "series_ticker": "KXNFP",
     "title": "Will March non-farm payrolls exceed 200,000?",
     "yes_bid": 58, "yes_ask": 61, "volume": 11200, "open_interest": 3100},
    {"ticker": "KXOIL-26MAR31", "series_ticker": "KXOIL",
     "title": "Will WTI crude oil be above $80 per barrel on March 31?",
     "yes_bid": 42, "yes_ask": 46, "volume": 8900, "open_interest": 2400},
]


class MockKalshiClient:
    """
    Returns realistic fake market data for offline dry-run testing.
    Prices jitter slightly on each call to simulate live-market behavior.
    """

    def __init__(self) -> None:
        logger.info(
            "[MOCK] Using simulated market data — "
            "no Kalshi API credentials configured"
        )

    @staticmethod
    def _jitter(price: int, amount: int = 3) -> int:
        return max(1, min(99, price + random.randint(-amount, amount)))

    def get_events(self, status="open", limit=200, cursor=None,
                   series_ticker=None, with_nested_markets=True) -> dict:
        now = datetime.now(timezone.utc)
        events = []
        # Group mock markets into events
        for i, m in enumerate(_MOCK_MARKETS):
            event = {
                "event_ticker": f"EVT-{m['series_ticker']}",
                "series_ticker": m["series_ticker"],
                "title": m["title"],
                "category": "Economics",
                "status": "open",
            }
            if with_nested_markets:
                yes_bid = self._jitter(m["yes_bid"], 2)
                yes_ask = max(yes_bid + 2, self._jitter(m["yes_ask"], 2))
                event["markets"] = [{
                    "ticker": m["ticker"],
                    "event_ticker": f"EVT-{m['series_ticker']}",
                    "title": m["title"],
                    "yes_bid": yes_bid,
                    "yes_ask": yes_ask,
                    "volume": m["volume"] + random.randint(0, 500),
                    "open_interest": m["open_interest"],
                    "close_time": (now + timedelta(days=random.randint(5, 40))).isoformat(),
                    "status": "open",
                }]
            events.append(event)
        return {"events": events, "cursor": None}

    def get_event(self, event_ticker: str) -> dict:
        return {"event": {"event_ticker": event_ticker, "category": "Economics"}}

    def get_markets(self, status="open", limit=200, cursor=None,
                    event_ticker=None, series_ticker=None) -> dict:
        now = datetime.now(timezone.utc)
        markets = []
        for m in _MOCK_MARKETS:
            yes_bid = self._jitter(m["yes_bid"], 2)
            yes_ask = max(yes_bid + 2, self._jitter(m["yes_ask"], 2))
            markets.append({
                "ticker": m["ticker"],
                "event_ticker": f"EVT-{m['series_ticker']}",
                "title": m["title"],
                "yes_bid": yes_bid,
                "yes_ask": yes_ask,
                "volume": m["volume"] + random.randint(0, 500),
                "open_interest": m["open_interest"],
                "close_time": (now + timedelta(days=random.randint(5, 40))).isoformat(),
                "status": "open",
            })
        return {"markets": markets, "cursor": None}

    def get_market(self, ticker: str) -> dict:
        for m in _MOCK_MARKETS:
            if m["ticker"] == ticker:
                now = datetime.now(timezone.utc)
                return {
                    "market": {
                        **m,
                        "close_time": (now + timedelta(days=15)).isoformat(),
                        "status": "open",
                    }
                }
        return {"market": {"ticker": ticker, "title": "Unknown", "status": "open",
                           "close_time": (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()}}

    def get_orderbook(self, ticker: str) -> dict:
        base_price = 50
        for m in _MOCK_MARKETS:
            if m["ticker"] == ticker:
                base_price = (m["yes_bid"] + m["yes_ask"]) // 2
                break
        return {
            "orderbook": {
                "yes": [[self._jitter(base_price - 2, 1), random.randint(30, 200)]],
                "no": [[self._jitter(100 - base_price - 2, 1), random.randint(30, 200)]],
            }
        }

    def get_candlesticks(self, series_ticker, market_ticker, period_seconds=900, limit=50) -> dict:
        base = 50
        for m in _MOCK_MARKETS:
            if m["ticker"] == market_ticker:
                base = (m["yes_bid"] + m["yes_ask"]) // 2
                break
        now = int(time.time())
        candles = []
        price = base
        for i in range(limit):
            drift = random.uniform(-2, 2)
            price = max(5, min(95, price + drift))
            o = price
            h = price + random.uniform(0, 3)
            l = price - random.uniform(0, 3)
            c = price + random.uniform(-1, 1)
            candles.append({
                "ts": now - (limit - i) * period_seconds,
                "open": {"yes": round(o)},
                "high": {"yes": round(h)},
                "low": {"yes": round(l)},
                "close": {"yes": round(c)},
                "volume": random.randint(10, 300),
            })
        return {"candlesticks": candles}

    def get_balance(self) -> dict:
        return {"balance": 10000, "portfolio_value": 0}

    def get_positions(self) -> dict:
        return {"market_positions": []}

    def get_orders(self, status=None) -> dict:
        return {"orders": []}

    def create_order(self, order: dict) -> dict:
        return {"order": {"order_id": f"mock-{int(time.time())}"}}

    def cancel_order(self, order_id: str) -> dict:
        return {"order_id": order_id, "status": "cancelled"}


# ================================================================== #
# Client factory
# ================================================================== #

def _has_credentials() -> bool:
    """Check if Kalshi API credentials are configured."""
    return (
        bool(config.KALSHI_API_KEY_ID)
        and config.KALSHI_API_KEY_ID != "your_api_key_id_here"
        and Path(config.KALSHI_PRIVATE_KEY_PATH).exists()
    )


def get_client():
    """
    Return the shared client instance (lazy init).

    Returns MockKalshiClient when DRY_RUN=true and no credentials exist,
    so the bot can run the full pipeline offline.
    """
    global _client_instance
    if _client_instance is None:
        if _has_credentials():
            _client_instance = KalshiClient()
        elif config.DRY_RUN:
            _client_instance = MockKalshiClient()
        else:
            raise RuntimeError(
                "Kalshi credentials not configured. Set KALSHI_API_KEY_ID "
                "and KALSHI_PRIVATE_KEY_PATH in .env, or use DRY_RUN=true"
            )
    return _client_instance
