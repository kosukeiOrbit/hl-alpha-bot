"""CORE層テスト用ヘルパー（設計書 11.7）。

make_snapshot() は「全フィールドにデフォルト値を持つMarketSnapshot生成」用。
テストでは変えたい部分だけ overrides で指定する。
"""

from __future__ import annotations

from typing import Any

from src.core.models import MarketSnapshot

# デフォルトは「LONGエントリーが全層通過する」値にしておく。
# 各テストは「どの値を崩すと落ちるか」を verify する形になる。
_DEFAULTS: dict[str, Any] = dict(
    symbol="BTC",
    current_price=100.2,  # vwap +0.2%
    vwap=100.0,
    momentum_5bar_pct=0.5,  # > 0.3
    utc_open_price=98.0,  # +2.2% (< +5%)
    rolling_24h_open=95.0,  # +5.5% (< +10%)
    high_24h=101.0,
    low_24h=96.0,  # position ≒ 0.84 → 全層通過させるため少し下げる
    flow_buy_sell_ratio=2.0,  # > 1.5
    flow_large_order_count=3,
    volume_surge_ratio=1.8,  # > 1.5
    sentiment_score=0.7,  # > 0.6
    sentiment_confidence=0.8,  # > 0.7
    sentiment_flags={"has_hack": False, "has_regulation": False},
    btc_ema_trend="UPTREND",
    btc_atr_pct=2.0,
    funding_rate=0.005,  # < 0.01
    open_interest=1_000_000,
    open_interest_1h_ago=990_000,  # +1% (< 10%)
)


def make_snapshot(**overrides: Any) -> MarketSnapshot:
    """全層クリア（LONG）のスナップショットを生成し、上書きを適用する。"""
    return MarketSnapshot(**{**_DEFAULTS, **overrides})


# SHORT 全層通過のデフォルトは LONG と多くのフィールドが対称になる。
_SHORT_DEFAULTS: dict[str, Any] = {
    **_DEFAULTS,
    "current_price": 99.8,  # vwap -0.2%
    "momentum_5bar_pct": -0.5,  # < -0.3
    "utc_open_price": 102.0,  # -2.16% (> -5%)
    "rolling_24h_open": 105.0,  # -4.95% (> -10%)
    "high_24h": 103.0,
    "low_24h": 95.0,  # position ≒ 0.6
    "flow_buy_sell_ratio": 0.5,  # < 1/1.5
    "btc_ema_trend": "DOWNTREND",
    "sentiment_score": -0.5,  # < -0.3
}


def make_short_snapshot(**overrides: Any) -> MarketSnapshot:
    """全層クリア（SHORT）のスナップショットを生成し、上書きを適用する。"""
    return MarketSnapshot(**{**_SHORT_DEFAULTS, **overrides})
