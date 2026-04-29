"""FixedSentimentProvider: 固定値を返す Phase 0 用 SentimentProvider。

Claude API 等の本番実装の代わりに、
score / confidence を固定値で返すだけのテスト用実装。

Phase 0 の用途:
- 取引ロジック全体のフロー確認
- SENTIMENT 層が常に通る / 通らない のスイッチで段階検証

判定基準（CORE entry_judge と整合）:
- score > 0.6                → bullish（CORE LONG SENTIMENT 通過）
- score < -0.3               → bearish（CORE SHORT SENTIMENT 通過）
- それ以外                    → neutral

PR7.5e で ClaudeSentimentProvider に差し替え予定。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from src.adapters.sentiment import SentimentResult


class FixedSentimentProvider:
    """常に固定値を返す SentimentProvider（Phase 0 用）。

    Args:
        score: 固定 score（-1.0〜1.0）。
        confidence: 固定 confidence（0.0〜1.0）。
        reasoning: SentimentResult.reasoning に入れる文字列。
    """

    def __init__(
        self,
        score: Decimal = Decimal("0.0"),
        confidence: Decimal = Decimal("0.0"),
        reasoning: str = "Fixed value (Phase 0 dummy)",
    ) -> None:
        if not (Decimal("-1") <= score <= Decimal("1")):
            raise ValueError(f"score must be in [-1, 1], got {score}")
        if not (Decimal("0") <= confidence <= Decimal("1")):
            raise ValueError(
                f"confidence must be in [0, 1], got {confidence}"
            )
        self._score = score
        self._confidence = confidence
        self._reasoning = reasoning

    async def fetch_sources(
        self,
        symbol: str,
        lookback_hours: int = 6,
        max_count: int = 10,
    ) -> tuple:  # type: ignore[type-arg]
        """Phase 0 ではソース取得は無し（空 tuple）。"""
        del symbol, lookback_hours, max_count
        return ()

    async def judge(
        self,
        symbol: str,
        sources: tuple,  # type: ignore[type-arg]
        direction: Literal["LONG", "SHORT"],
    ) -> SentimentResult:
        """sources に依存せず固定値を返す。"""
        del symbol, sources, direction
        return self._build_result()

    async def judge_cached_or_fresh(
        self,
        symbol: str,
        direction: Literal["LONG", "SHORT"],
    ) -> SentimentResult:
        """常に固定値を返す（symbol / direction にも依存しない）。"""
        del symbol, direction
        return self._build_result()

    def _build_result(self) -> SentimentResult:
        sentiment_dir: Literal["bullish", "bearish", "neutral"]
        if self._score > Decimal("0.6"):
            sentiment_dir = "bullish"
        elif self._score < Decimal("-0.3"):
            sentiment_dir = "bearish"
        else:
            sentiment_dir = "neutral"
        return SentimentResult(
            score=self._score,
            confidence=self._confidence,
            direction=sentiment_dir,
            reasoning=self._reasoning,
            source_count=0,
            cached=False,
        )
