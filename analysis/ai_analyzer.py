"""
analysis/ai_analyzer.py — AI-powered probability estimation via OpenRouter.

Uses an LLM to estimate the probability of a prediction market resolving YES,
given the market question and recent news context.

The key signal is: AI_estimate vs current_market_price = EDGE
If |edge| > MIN_EDGE_CENTS, it suggests the market is mispriced.
"""

import re
from dataclasses import dataclass
from typing import Optional

from openai import OpenAI, APIError

import config
from analysis.news import NewsArticle
from utils.logger import get_logger

logger = get_logger(__name__)

_openrouter_client: Optional[OpenAI] = None


def _get_client() -> OpenAI:
    global _openrouter_client
    if _openrouter_client is None:
        _openrouter_client = OpenAI(
            base_url=config.OPENROUTER_BASE_URL,
            api_key=config.OPENROUTER_API_KEY,
            default_headers={
                "HTTP-Referer": "https://github.com/kalshi-bot",
                "X-Title": "Kalshi Trading Bot",
            },
        )
    return _openrouter_client


@dataclass
class AIEstimate:
    probability: int    # 0-100 cents (same scale as Kalshi prices)
    confidence: float   # 0-1
    reasoning: str
    model_used: str
    success: bool = True
    error: str = ""


_SYSTEM_PROMPT = """You are an expert prediction market analyst. Your task is to estimate
the probability of a binary event occurring. Be objective and base your estimates on
available evidence. Do not add moral commentary. Output ONLY the requested format.

IMPORTANT: The market price reflects crowd consensus. Only deviate significantly from
the market price if you have strong evidence. A market at 60c means the crowd thinks
there's a 60% chance of YES. Your edge comes from identifying when the crowd is WRONG,
not from having a vague opinion."""

_USER_PROMPT_TEMPLATE = """Market Question: "{question}"

Current market price: {market_price}c (crowd estimates {market_price}% chance of YES)
Market closes: {close_date}

Recent relevant news:
{news_context}

Based on the available information, estimate:
1. The probability (0-100) that this market resolves YES
2. Your confidence in this estimate (0=very uncertain, 100=very confident)
3. A one-sentence reasoning that explains WHY you agree or disagree with the market price

Reply in EXACTLY this format (no other text):
PROBABILITY: <integer 0-100>
CONFIDENCE: <integer 0-100>
REASONING: <one sentence>"""


def _parse_response(text: str) -> tuple[int, float, str]:
    """
    Parse the structured AI response.
    Returns (probability_0_to_100, confidence_0_to_1, reasoning).
    """
    prob_match = re.search(r"PROBABILITY:\s*(\d+)", text, re.IGNORECASE)
    conf_match = re.search(r"CONFIDENCE:\s*(\d+)", text, re.IGNORECASE)
    reason_match = re.search(r"REASONING:\s*(.+)", text, re.IGNORECASE)

    probability = int(prob_match.group(1)) if prob_match else 50
    confidence_raw = int(conf_match.group(1)) if conf_match else 50
    reasoning = reason_match.group(1).strip() if reason_match else "No reasoning provided"

    # Clamp to valid ranges
    probability = max(1, min(99, probability))
    confidence = max(0.0, min(1.0, confidence_raw / 100))

    return probability, confidence, reasoning


def estimate_probability(
    market_question: str,
    news_articles: list[NewsArticle],
    model: Optional[str] = None,
    market_price_cents: Optional[int] = None,
    close_date: Optional[str] = None,
) -> AIEstimate:
    """
    Call OpenRouter to estimate the probability of a market resolving YES.

    Tries primary model first, falls back to free model on error.
    Returns a failed AIEstimate (success=False) if both fail.

    market_price_cents: current YES ask price (so AI knows what the crowd thinks)
    close_date: when the market resolves (so AI can factor in time)
    """
    if not config.OPENROUTER_API_KEY:
        return AIEstimate(
            probability=50, confidence=0.0,
            reasoning="OpenRouter API key not configured",
            model_used="none", success=False,
            error="OPENROUTER_API_KEY not set",
        )

    # Format news context
    if news_articles:
        news_context = "\n".join(
            f"• {a.to_context_string()}" for a in news_articles[:5]
        )
    else:
        news_context = "No recent relevant news found."

    user_msg = _USER_PROMPT_TEMPLATE.format(
        question=market_question,
        news_context=news_context,
        market_price=market_price_cents or 50,
        close_date=close_date or "unknown",
    )

    models_to_try = [
        model or config.OPENROUTER_MODEL,
        config.OPENROUTER_FALLBACK_MODEL,
    ]

    client = _get_client()

    for model_name in models_to_try:
        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.1,   # Low temperature for consistent, factual output
                max_tokens=150,
            )

            raw_text = response.choices[0].message.content or ""
            probability, confidence, reasoning = _parse_response(raw_text)

            logger.debug(
                f"AI estimate for '{market_question[:60]}': "
                f"{probability}% (conf={confidence:.2f}) via {model_name}"
            )

            return AIEstimate(
                probability=probability,
                confidence=confidence,
                reasoning=reasoning,
                model_used=model_name,
                success=True,
            )

        except APIError as e:
            logger.warning(f"OpenRouter error with model {model_name}: {e}")
            continue
        except Exception as e:
            logger.warning(f"Unexpected error with model {model_name}: {e}")
            continue

    return AIEstimate(
        probability=50,
        confidence=0.0,
        reasoning="All AI models failed",
        model_used="none",
        success=False,
        error="All OpenRouter models failed",
    )
