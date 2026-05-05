"""FundingRateSentimentProvider: HL Funding Rate ベースの SentimentProvider（PR7.5e-1）。

Claude API 不要・コスト $0 の本実装第一弾。Phase 1 で profile 経由で
FixedSentimentProvider と差し替えて使う想定。

論理:
    Funding Rate は perp 市場の偏りの指標：
    - Funding > 0  → 過剰 LONG → 巻き戻しリスク → sentiment は bearish 寄り
    - Funding < 0  → 過剰 SHORT → ショートカバー余地 → sentiment は bullish 寄り
    つまり sentiment = -funding_rate * scale_factor（contrarian view）。

    score を [-1, 1] にクリップ。CORE 層 entry_judge の閾値と整合させて
    direction を bullish / bearish / neutral に分類する。

設計判断:
- fetch_sources は空 tuple を返す（ニュースソース無しが本 Provider の特徴）
- judge は cache を経由しない一発計算
- judge_cached_or_fresh は dedup_window（既定 5 分）で同 symbol を抑制
- ExchangeError → SentimentError に変換（呼び出し側でハンドリング可能に）
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from src.adapters.exchange import ExchangeError, ExchangeProtocol
from src.adapters.sentiment import (
    SentimentError,
    SentimentResult,
    SentimentSource,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FundingRateSentimentConfig:
    """FundingRateSentimentProvider の設定。"""

    # |funding_rate_8h| × scale_factor が [-1, 1] にスケールされる。
    # 既定 10000 → funding=0.0001 (0.01%) で score=±1.0 に飽和。
    # 通常 funding は ±0.00001〜0.00005 程度なので score はだいたい ±0.1〜0.5。
    scale_factor: Decimal = Decimal("10000")

    # 同一 symbol の cache window（秒）。Funding は 1h 単位の値なので 5 分で十分。
    cache_window_seconds: int = 300

    # 固定 confidence。Funding 自体の信頼度は安定（Claude API のような
    # スコアブレが無い）ので 0.8 を既定値に。
    confidence: Decimal = Decimal("0.8")


class FundingRateSentimentProvider:
    """HL Funding Rate ベースの SentimentProvider（contrarian view）。

    Args:
        exchange: ExchangeProtocol 実装。get_funding_rate_8h を使う。
        config: FundingRateSentimentConfig（None なら全デフォルト）。

    Usage::

        provider = FundingRateSentimentProvider(exchange)
        result = await provider.judge_cached_or_fresh("BTC", "LONG")
        # result.score は funding を反転して [-1, 1] にスケール済み
    """

    def __init__(
        self,
        exchange: ExchangeProtocol,
        config: FundingRateSentimentConfig | None = None,
    ) -> None:
        self.exchange = exchange
        self.config = config or FundingRateSentimentConfig()
        self._cache: dict[str, tuple[float, SentimentResult]] = {}

    # ─── SentimentProvider Protocol 実装 ─────

    async def fetch_sources(
        self,
        symbol: str,
        lookback_hours: int = 6,
        max_count: int = 10,
    ) -> tuple[SentimentSource, ...]:
        """Funding ベースなのでニュースソースは無し（空 tuple）。"""
        del symbol, lookback_hours, max_count
        return ()

    async def judge(
        self,
        symbol: str,
        sources: tuple[SentimentSource, ...],
        direction: Literal["LONG", "SHORT"],
    ) -> SentimentResult:
        """sources / direction を無視して funding rate から SentimentResult。

        contrarian view なので direction で結果は変わらない。
        cache は経由しない（fresh 計算）。
        """
        del sources, direction
        return await self._compute(symbol, cached=False)

    async def judge_cached_or_fresh(
        self,
        symbol: str,
        direction: Literal["LONG", "SHORT"],
    ) -> SentimentResult:
        """cache window 内なら cached=True で返し、外なら再計算。"""
        del direction

        now = time.time()
        cached = self._cache.get(symbol)
        if cached is not None:
            cached_at, cached_result = cached
            if now - cached_at < self.config.cache_window_seconds:
                return SentimentResult(
                    score=cached_result.score,
                    confidence=cached_result.confidence,
                    direction=cached_result.direction,
                    reasoning=cached_result.reasoning,
                    source_count=cached_result.source_count,
                    cached=True,
                )

        result = await self._compute(symbol, cached=False)
        self._cache[symbol] = (now, result)
        return result

    # ─── 内部実装 ──────────────────────────

    async def _compute(self, symbol: str, cached: bool) -> SentimentResult:
        """Funding rate を取得して SentimentResult を組み立てる。"""
        try:
            funding_rate_8h = await self.exchange.get_funding_rate_8h(symbol)
        except ExchangeError as e:
            logger.warning(
                "failed to fetch funding rate for %s: %s", symbol, e
            )
            raise SentimentError(
                f"Funding rate fetch failed: {e}"
            ) from e

        # contrarian: funding > 0 → bearish (score < 0)
        raw_score = -funding_rate_8h * self.config.scale_factor
        score = max(Decimal("-1"), min(Decimal("1"), raw_score))

        sentiment_dir: Literal["bullish", "bearish", "neutral"]
        if score > Decimal("0.6"):
            sentiment_dir = "bullish"
        elif score < Decimal("-0.3"):
            sentiment_dir = "bearish"
        else:
            sentiment_dir = "neutral"

        reasoning = (
            f"Funding rate (8h): {funding_rate_8h:.6f} → "
            f"contrarian score: {score:.4f}"
        )
        return SentimentResult(
            score=score,
            confidence=self.config.confidence,
            direction=sentiment_dir,
            reasoning=reasoning,
            source_count=0,
            cached=cached,
        )
