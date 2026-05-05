"""FundingRateSentimentProvider のテスト。

ExchangeProtocol は AsyncMock で差し替え。実 testnet には触れない。
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.adapters.exchange import ExchangeError
from src.adapters.sentiment import (
    SentimentError,
    SentimentProvider,
    SentimentResult,
)
from src.infrastructure.funding_rate_sentiment_provider import (
    FundingRateSentimentConfig,
    FundingRateSentimentProvider,
)


def make_config(**overrides: Any) -> FundingRateSentimentConfig:
    base: dict[str, Any] = {
        "scale_factor": Decimal("10000"),
        "cache_window_seconds": 300,
        "confidence": Decimal("0.8"),
    }
    base.update(overrides)
    return FundingRateSentimentConfig(**base)


def _make_exchange(funding: Decimal | None = None) -> Any:
    exchange = AsyncMock()
    if funding is not None:
        exchange.get_funding_rate_8h = AsyncMock(return_value=funding)
    return exchange


# ─── score 計算の正しさ ────────────────────


class TestScoreCalculation:
    @pytest.mark.asyncio
    async def test_zero_funding_score_zero_neutral(self) -> None:
        provider = FundingRateSentimentProvider(
            _make_exchange(Decimal("0")), make_config()
        )
        result = await provider.judge_cached_or_fresh("BTC", "LONG")
        assert result.score == Decimal("0")
        assert result.direction == "neutral"

    @pytest.mark.asyncio
    async def test_positive_funding_clipped_to_minus_one(self) -> None:
        # funding=+0.0001 × 10000 = 1 → 反転して -1
        provider = FundingRateSentimentProvider(
            _make_exchange(Decimal("0.0001")), make_config()
        )
        result = await provider.judge_cached_or_fresh("BTC", "LONG")
        assert result.score == Decimal("-1")
        assert result.direction == "bearish"

    @pytest.mark.asyncio
    async def test_negative_funding_clipped_to_plus_one(self) -> None:
        # funding=-0.0001 → +1 → bullish
        provider = FundingRateSentimentProvider(
            _make_exchange(Decimal("-0.0001")), make_config()
        )
        result = await provider.judge_cached_or_fresh("BTC", "LONG")
        assert result.score == Decimal("1")
        assert result.direction == "bullish"

    @pytest.mark.asyncio
    async def test_extreme_positive_clipped(self) -> None:
        # funding=0.001 × 10000 = 10 → 反転して -10、clip で -1
        provider = FundingRateSentimentProvider(
            _make_exchange(Decimal("0.001")), make_config()
        )
        result = await provider.judge_cached_or_fresh("BTC", "LONG")
        assert result.score == Decimal("-1")

    @pytest.mark.asyncio
    async def test_extreme_negative_clipped(self) -> None:
        provider = FundingRateSentimentProvider(
            _make_exchange(Decimal("-0.001")), make_config()
        )
        result = await provider.judge_cached_or_fresh("BTC", "LONG")
        assert result.score == Decimal("1")


# ─── direction 判定（CORE 閾値整合） ──────


class TestDirection:
    @pytest.mark.asyncio
    async def test_score_above_06_is_bullish(self) -> None:
        # funding=-0.00007 × 10000 = -0.7 → 反転 +0.7 > 0.6 → bullish
        provider = FundingRateSentimentProvider(
            _make_exchange(Decimal("-0.00007")), make_config()
        )
        result = await provider.judge_cached_or_fresh("BTC", "LONG")
        assert result.score == Decimal("0.7")
        assert result.direction == "bullish"

    @pytest.mark.asyncio
    async def test_score_below_minus_03_is_bearish(self) -> None:
        # funding=0.00004 × 10000 = 0.4 → 反転 -0.4 < -0.3 → bearish
        provider = FundingRateSentimentProvider(
            _make_exchange(Decimal("0.00004")), make_config()
        )
        result = await provider.judge_cached_or_fresh("BTC", "LONG")
        assert result.score == Decimal("-0.4")
        assert result.direction == "bearish"

    @pytest.mark.asyncio
    async def test_mid_score_is_neutral_lower(self) -> None:
        # funding=-0.00003 × 10000 = -0.3 → 反転 +0.3、0 < 0.3 < 0.6 → neutral
        provider = FundingRateSentimentProvider(
            _make_exchange(Decimal("-0.00003")), make_config()
        )
        result = await provider.judge_cached_or_fresh("BTC", "LONG")
        assert result.direction == "neutral"

    @pytest.mark.asyncio
    async def test_boundary_score_06_is_neutral(self) -> None:
        # score == 0.6 はちょうど境界（>0.6 の strict inequality で neutral）
        provider = FundingRateSentimentProvider(
            _make_exchange(Decimal("-0.00006")), make_config()
        )
        result = await provider.judge_cached_or_fresh("BTC", "LONG")
        assert result.score == Decimal("0.6")
        assert result.direction == "neutral"

    @pytest.mark.asyncio
    async def test_boundary_score_minus_03_is_neutral(self) -> None:
        # score == -0.3 はちょうど境界
        provider = FundingRateSentimentProvider(
            _make_exchange(Decimal("0.00003")), make_config()
        )
        result = await provider.judge_cached_or_fresh("BTC", "LONG")
        assert result.score == Decimal("-0.3")
        assert result.direction == "neutral"


# ─── キャッシュ ─────────────────────────


class TestCache:
    @pytest.mark.asyncio
    async def test_second_call_uses_cache(self) -> None:
        exchange = _make_exchange(Decimal("0.00005"))
        provider = FundingRateSentimentProvider(exchange, make_config())

        result1 = await provider.judge_cached_or_fresh("BTC", "LONG")
        result2 = await provider.judge_cached_or_fresh("BTC", "LONG")

        assert exchange.get_funding_rate_8h.await_count == 1
        assert result1.cached is False
        assert result2.cached is True
        assert result1.score == result2.score

    @pytest.mark.asyncio
    async def test_different_symbols_have_separate_cache(self) -> None:
        exchange = _make_exchange()
        exchange.get_funding_rate_8h = AsyncMock(
            side_effect=[Decimal("0"), Decimal("0.0001")]
        )
        provider = FundingRateSentimentProvider(exchange, make_config())

        await provider.judge_cached_or_fresh("BTC", "LONG")
        await provider.judge_cached_or_fresh("ETH", "LONG")

        assert exchange.get_funding_rate_8h.await_count == 2

    @pytest.mark.asyncio
    async def test_cache_expires_after_window(self) -> None:
        exchange = _make_exchange(Decimal("0"))
        provider = FundingRateSentimentProvider(
            exchange, make_config(cache_window_seconds=1)
        )

        await provider.judge_cached_or_fresh("BTC", "LONG")
        await asyncio.sleep(1.1)
        await provider.judge_cached_or_fresh("BTC", "LONG")

        assert exchange.get_funding_rate_8h.await_count == 2

    @pytest.mark.asyncio
    async def test_judge_does_not_use_cache(self) -> None:
        # judge() は fresh 計算のみ。cache を埋めることもない。
        exchange = _make_exchange(Decimal("0"))
        provider = FundingRateSentimentProvider(exchange, make_config())

        await provider.judge("BTC", (), "LONG")
        await provider.judge("BTC", (), "LONG")

        # 2 回とも API 経由
        assert exchange.get_funding_rate_8h.await_count == 2


# ─── Protocol メソッド ──────────────────


class TestProtocolMethods:
    @pytest.mark.asyncio
    async def test_fetch_sources_returns_empty(self) -> None:
        provider = FundingRateSentimentProvider(_make_exchange(), make_config())
        assert await provider.fetch_sources("BTC") == ()

    @pytest.mark.asyncio
    async def test_fetch_sources_ignores_kwargs(self) -> None:
        provider = FundingRateSentimentProvider(_make_exchange(), make_config())
        result = await provider.fetch_sources("BTC", lookback_hours=24, max_count=50)
        assert result == ()

    @pytest.mark.asyncio
    async def test_judge_returns_sentiment_result(self) -> None:
        exchange = _make_exchange(Decimal("0"))
        provider = FundingRateSentimentProvider(exchange, make_config())
        result = await provider.judge("BTC", (), "LONG")
        assert isinstance(result, SentimentResult)
        assert result.cached is False
        assert result.confidence == Decimal("0.8")

    def test_satisfies_sentiment_protocol(self) -> None:
        provider: SentimentProvider = FundingRateSentimentProvider(
            _make_exchange(), make_config()
        )
        assert provider is not None


# ─── エラーハンドリング ─────────────────


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_exchange_error_converted_to_sentiment_error(self) -> None:
        exchange = AsyncMock()
        exchange.get_funding_rate_8h = AsyncMock(
            side_effect=ExchangeError("API down")
        )
        provider = FundingRateSentimentProvider(exchange, make_config())
        with pytest.raises(SentimentError, match="Funding rate fetch failed"):
            await provider.judge_cached_or_fresh("BTC", "LONG")

    @pytest.mark.asyncio
    async def test_error_does_not_populate_cache(self) -> None:
        exchange = AsyncMock()
        exchange.get_funding_rate_8h = AsyncMock(
            side_effect=ExchangeError("boom")
        )
        provider = FundingRateSentimentProvider(exchange, make_config())
        with pytest.raises(SentimentError):
            await provider.judge_cached_or_fresh("BTC", "LONG")
        # 失敗時に cache を残すと次回 fresh 取得を阻害する
        assert "BTC" not in provider._cache


# ─── config 連動 ───────────────────────


class TestConfigInfluence:
    @pytest.mark.asyncio
    async def test_custom_scale_factor_changes_score(self) -> None:
        # scale=5000 で funding=-0.0001 → score=0.5（既定 10000 だと 1.0）
        provider = FundingRateSentimentProvider(
            _make_exchange(Decimal("-0.0001")),
            make_config(scale_factor=Decimal("5000")),
        )
        result = await provider.judge_cached_or_fresh("BTC", "LONG")
        assert result.score == Decimal("0.5")

    @pytest.mark.asyncio
    async def test_custom_confidence(self) -> None:
        provider = FundingRateSentimentProvider(
            _make_exchange(Decimal("0")),
            make_config(confidence=Decimal("0.5")),
        )
        result = await provider.judge_cached_or_fresh("BTC", "LONG")
        assert result.confidence == Decimal("0.5")

    @pytest.mark.asyncio
    async def test_default_config_when_none(self) -> None:
        # config=None でデフォルト値が使われる（デフォルト引数の経路カバー）
        provider = FundingRateSentimentProvider(_make_exchange(Decimal("0")))
        result = await provider.judge_cached_or_fresh("BTC", "LONG")
        assert result.confidence == Decimal("0.8")
