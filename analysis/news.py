"""
analysis/news.py — Free news fetching via RSS feeds and Google News.

No API key required. Uses feedparser to parse RSS/Atom feeds and
Google News RSS for keyword-targeted searches.

Results are cached in-memory for NEWS_CACHE_TTL_SECONDS to avoid
hammering news sources on every market analysis cycle.
"""

import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote_plus

import feedparser
import requests

import config
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class NewsArticle:
    title: str
    summary: str
    source: str
    published: str = ""

    def to_context_string(self) -> str:
        """Format as a single string for passing to the AI."""
        text = f"{self.title}"
        if self.summary and self.summary != self.title:
            text += f" — {self.summary[:200]}"
        return text


# In-memory cache: {cache_key: (fetch_time, [NewsArticle])}
_cache: dict[str, tuple[float, list[NewsArticle]]] = {}


def _is_cache_fresh(key: str) -> bool:
    if key not in _cache:
        return False
    fetch_time, _ = _cache[key]
    return (time.time() - fetch_time) < config.NEWS_CACHE_TTL_SECONDS


def _parse_feed(url: str, source_name: str) -> list[NewsArticle]:
    """Parse an RSS/Atom feed URL and return articles."""
    try:
        # feedparser handles redirects and encoding automatically
        feed = feedparser.parse(url)
        articles = []
        for entry in feed.entries[:10]:
            title = getattr(entry, "title", "").strip()
            summary = getattr(entry, "summary", "").strip()
            # Strip HTML tags from summary
            if "<" in summary:
                import re
                summary = re.sub(r"<[^>]+>", " ", summary).strip()
            published = getattr(entry, "published", "")
            if title:
                articles.append(
                    NewsArticle(
                        title=title,
                        summary=summary[:300],
                        source=source_name,
                        published=published,
                    )
                )
        return articles
    except Exception as e:
        logger.warning(f"Failed to parse feed {url}: {e}")
        return []


def fetch_general_news() -> list[NewsArticle]:
    """Fetch top headlines from configured RSS feeds (cached)."""
    cache_key = "general"
    if _is_cache_fresh(cache_key):
        return _cache[cache_key][1]

    articles: list[NewsArticle] = []
    for feed_url in config.NEWS_RSS_FEEDS:
        source = feed_url.split("/")[2]  # domain as source name
        articles.extend(_parse_feed(feed_url, source))

    # Deduplicate by title similarity (simple: exact title match)
    seen_titles: set[str] = set()
    unique: list[NewsArticle] = []
    for a in articles:
        key = a.title.lower()[:60]
        if key not in seen_titles:
            seen_titles.add(key)
            unique.append(a)

    _cache[cache_key] = (time.time(), unique)
    logger.debug(f"Fetched {len(unique)} general news articles")
    return unique


def fetch_news_for_market(
    keywords: list[str],
    market_title: str,
) -> list[NewsArticle]:
    """
    Fetch news articles relevant to a specific market using Google News RSS.

    Google News RSS doesn't require authentication and supports keyword search.
    Example: https://news.google.com/rss/search?q=federal+reserve+rate&hl=en-US
    """
    if not keywords:
        return fetch_general_news()[:config.NEWS_MAX_ARTICLES_PER_MARKET]

    # Build query from keywords — use top 4 most specific ones
    query = " ".join(keywords[:4])
    cache_key = f"market:{query}"

    if _is_cache_fresh(cache_key):
        return _cache[cache_key][1]

    google_news_url = (
        f"https://news.google.com/rss/search?"
        f"q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    )

    articles = _parse_feed(google_news_url, "Google News")

    # Also add relevant articles from general feeds by keyword matching
    general = fetch_general_news()
    query_words = set(query.lower().split())
    for article in general:
        combined = (article.title + " " + article.summary).lower()
        if any(w in combined for w in query_words if len(w) > 4):
            articles.append(article)

    # Deduplicate
    seen: set[str] = set()
    unique: list[NewsArticle] = []
    for a in articles:
        key = a.title.lower()[:60]
        if key not in seen:
            seen.add(key)
            unique.append(a)

    result = unique[:config.NEWS_MAX_ARTICLES_PER_MARKET]
    _cache[cache_key] = (time.time(), result)

    logger.debug(
        f"Fetched {len(result)} news articles for '{market_title[:50]}'"
    )
    return result


def clear_cache() -> None:
    """Clear the news cache (useful for testing)."""
    _cache.clear()
