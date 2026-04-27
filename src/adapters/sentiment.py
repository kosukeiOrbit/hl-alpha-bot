"""SentimentProvider Protocol（章7）。

ニュース・テキストから sentiment スコアと confidence を判定するインターフェース。
INFRASTRUCTURE層では Claude API を使った実装を入れる。
テスト時は固定値を返すモックで差し替え可能。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol


@dataclass(frozen=True)
class SentimentSource:
    """ニュースソース1件。"""

    source: str  # 例: 'coindesk' / 'cointelegraph'
    title: str
    body: str
    published_at_ms: int
    url: str


@dataclass(frozen=True)
class SentimentResult:
    """SENTIMENT判定結果。"""

    score: Decimal  # -1.0 〜 +1.0
    confidence: Decimal  # 0.0 〜 1.0
    direction: Literal["bullish", "bearish", "neutral"]
    reasoning: str  # Claude の判定根拠
    source_count: int  # 参照したソース数
    cached: bool


class SentimentError(Exception):
    """SENTIMENT判定の基礎例外。"""


class SentimentTimeoutError(SentimentError):
    """API タイムアウト（章9.10）。"""


class SentimentProvider(Protocol):
    """SENTIMENT判定プロバイダ。"""

    async def fetch_sources(
        self,
        symbol: str,
        lookback_hours: int = 6,
        max_count: int = 10,
    ) -> tuple[SentimentSource, ...]: ...

    async def judge(
        self,
        symbol: str,
        sources: tuple[SentimentSource, ...],
        direction: Literal["LONG", "SHORT"],
    ) -> SentimentResult: ...

    async def judge_cached_or_fresh(
        self,
        symbol: str,
        direction: Literal["LONG", "SHORT"],
    ) -> SentimentResult: ...
