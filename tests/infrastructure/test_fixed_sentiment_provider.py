"""FixedSentimentProvider のテスト。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.adapters.sentiment import SentimentProvider
from src.infrastructure.fixed_sentiment_provider import FixedSentimentProvider


class TestFixedSentimentProvider:
    @pytest.mark.asyncio
    async def test_returns_fixed_score_and_confidence(self) -> None:
        provider = FixedSentimentProvider(
            score=Decimal("0.8"), confidence=Decimal("0.9")
        )
        result = await provider.judge_cached_or_fresh("BTC", "LONG")
        assert result.score == Decimal("0.8")
        assert result.confidence == Decimal("0.9")

    @pytest.mark.asyncio
    async def test_independent_of_symbol(self) -> None:
        provider = FixedSentimentProvider(score=Decimal("0.5"))
        a = await provider.judge_cached_or_fresh("BTC", "LONG")
        b = await provider.judge_cached_or_fresh("ETH", "LONG")
        assert a.score == b.score

    @pytest.mark.asyncio
    async def test_independent_of_direction(self) -> None:
        provider = FixedSentimentProvider(score=Decimal("0.5"))
        a = await provider.judge_cached_or_fresh("BTC", "LONG")
        b = await provider.judge_cached_or_fresh("BTC", "SHORT")
        assert a.score == b.score

    @pytest.mark.asyncio
    async def test_high_score_is_bullish(self) -> None:
        # CORE LONG SENTIMENT 通過閾値 0.6 を超える
        provider = FixedSentimentProvider(score=Decimal("0.8"))
        result = await provider.judge_cached_or_fresh("BTC", "LONG")
        assert result.direction == "bullish"

    @pytest.mark.asyncio
    async def test_low_score_is_bearish(self) -> None:
        # CORE SHORT SENTIMENT 通過閾値 -0.3 を下回る
        provider = FixedSentimentProvider(score=Decimal("-0.5"))
        result = await provider.judge_cached_or_fresh("BTC", "LONG")
        assert result.direction == "bearish"

    @pytest.mark.asyncio
    async def test_mid_score_is_neutral(self) -> None:
        provider = FixedSentimentProvider(score=Decimal("0.0"))
        result = await provider.judge_cached_or_fresh("BTC", "LONG")
        assert result.direction == "neutral"

    @pytest.mark.asyncio
    async def test_score_at_long_threshold_is_neutral(self) -> None:
        # 0.6 ちょうどは bullish ではない（CORE は > 0.6 なので）
        provider = FixedSentimentProvider(score=Decimal("0.6"))
        result = await provider.judge_cached_or_fresh("BTC", "LONG")
        assert result.direction == "neutral"

    @pytest.mark.asyncio
    async def test_score_at_short_threshold_is_neutral(self) -> None:
        # -0.3 ちょうどは bearish ではない（CORE は < -0.3 なので）
        provider = FixedSentimentProvider(score=Decimal("-0.3"))
        result = await provider.judge_cached_or_fresh("BTC", "LONG")
        assert result.direction == "neutral"

    @pytest.mark.asyncio
    async def test_default_values(self) -> None:
        provider = FixedSentimentProvider()
        result = await provider.judge_cached_or_fresh("BTC", "LONG")
        assert result.score == Decimal("0")
        assert result.confidence == Decimal("0")
        assert result.direction == "neutral"

    @pytest.mark.asyncio
    async def test_default_reasoning_mentions_phase0(self) -> None:
        provider = FixedSentimentProvider()
        result = await provider.judge_cached_or_fresh("BTC", "LONG")
        assert "Phase 0" in result.reasoning

    @pytest.mark.asyncio
    async def test_custom_reasoning(self) -> None:
        provider = FixedSentimentProvider(reasoning="Test reason")
        result = await provider.judge_cached_or_fresh("BTC", "LONG")
        assert result.reasoning == "Test reason"

    @pytest.mark.asyncio
    async def test_source_count_is_zero(self) -> None:
        provider = FixedSentimentProvider()
        result = await provider.judge_cached_or_fresh("BTC", "LONG")
        assert result.source_count == 0

    @pytest.mark.asyncio
    async def test_cached_is_false(self) -> None:
        provider = FixedSentimentProvider()
        result = await provider.judge_cached_or_fresh("BTC", "LONG")
        assert result.cached is False

    @pytest.mark.asyncio
    async def test_fetch_sources_returns_empty(self) -> None:
        provider = FixedSentimentProvider()
        sources = await provider.fetch_sources("BTC")
        assert sources == ()

    @pytest.mark.asyncio
    async def test_judge_returns_fixed(self) -> None:
        provider = FixedSentimentProvider(
            score=Decimal("0.7"), confidence=Decimal("0.8")
        )
        result = await provider.judge("BTC", (), "LONG")
        assert result.score == Decimal("0.7")
        assert result.direction == "bullish"

    def test_invalid_score_too_high(self) -> None:
        with pytest.raises(ValueError, match="score"):
            FixedSentimentProvider(score=Decimal("1.5"))

    def test_invalid_score_too_low(self) -> None:
        with pytest.raises(ValueError, match="score"):
            FixedSentimentProvider(score=Decimal("-1.5"))

    def test_invalid_confidence_too_high(self) -> None:
        with pytest.raises(ValueError, match="confidence"):
            FixedSentimentProvider(confidence=Decimal("1.1"))

    def test_invalid_confidence_negative(self) -> None:
        with pytest.raises(ValueError, match="confidence"):
            FixedSentimentProvider(confidence=Decimal("-0.5"))


class TestProtocolConformance:
    def test_satisfies_sentiment_provider(self) -> None:
        provider: SentimentProvider = FixedSentimentProvider()
        assert provider is not None
