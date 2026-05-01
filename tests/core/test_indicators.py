"""src/core/indicators.py のテスト。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.core.indicators import (
    calculate_atr,
    calculate_atr_pct,
    calculate_ema,
)


def D(x: int | str) -> Decimal:
    """Decimal リテラル簡略化。"""
    return Decimal(str(x))


# ─── calculate_ema ──────────────────────────


class TestCalculateEma:
    def test_constant_prices_return_same(self) -> None:
        prices = [D(100)] * 10
        assert calculate_ema(prices, period=5) == D(100)

    def test_uptrend_ema_above_seed(self) -> None:
        # 単調増加 → EMA は seed(SMA) より大きい
        prices = [D(p) for p in (100, 102, 104, 106, 108, 110)]
        seed = sum(prices[:3], Decimal(0)) / Decimal(3)
        ema = calculate_ema(prices, period=3)
        assert ema > seed

    def test_downtrend_ema_below_seed(self) -> None:
        prices = [D(p) for p in (110, 108, 106, 104, 102, 100)]
        seed = sum(prices[:3], Decimal(0)) / Decimal(3)
        ema = calculate_ema(prices, period=3)
        assert ema < seed

    def test_known_value_period_2(self) -> None:
        # period=2: alpha=2/3
        # seed SMA = (10+20)/2 = 15
        # next: 30 * 2/3 + 15 * 1/3 = 20 + 5 = 25
        prices = [D(10), D(20), D(30)]
        assert calculate_ema(prices, period=2) == D(25)

    def test_period_exact_length_returns_seed(self) -> None:
        # prices 数 == period なら更新ループに入らず seed = SMA をそのまま
        prices = [D(10), D(20), D(30), D(40)]
        sma = sum(prices, Decimal(0)) / Decimal(4)
        assert calculate_ema(prices, period=4) == sma

    def test_period_too_small_raises(self) -> None:
        with pytest.raises(ValueError, match="period must be"):
            calculate_ema([D(100), D(101)], period=0)

    def test_negative_period_raises(self) -> None:
        with pytest.raises(ValueError, match="period must be"):
            calculate_ema([D(100), D(101)], period=-1)

    def test_insufficient_prices_raises(self) -> None:
        with pytest.raises(ValueError, match="at least 5 prices"):
            calculate_ema([D(100)] * 3, period=5)


# ─── calculate_atr ──────────────────────────


class TestCalculateAtr:
    def test_constant_range_returns_range(self) -> None:
        # 16 本: high-low=20 の連続 → ATR=20
        n = 16
        highs = [D(110)] * n
        lows = [D(90)] * n
        closes = [D(100)] * n
        assert calculate_atr(highs, lows, closes, period=14) == D(20)

    def test_known_value_period_2(self) -> None:
        # 4 本でシンプルに検算
        # bar0: h=10 l=8 c=9
        # bar1: h=12 l=9 c=11   TR1 = max(3, |12-9|=3, |9-9|=0) = 3
        # bar2: h=14 l=10 c=13  TR2 = max(4, |14-11|=3, |10-11|=1) = 4
        # bar3: h=15 l=12 c=14  TR3 = max(3, |15-13|=2, |12-13|=1) = 3
        # ATR(2) seed = (TR1+TR2)/2 = 3.5
        # ATR(2) bar3 = (3.5*1 + 3)/2 = 3.25
        highs = [D(10), D(12), D(14), D(15)]
        lows = [D(8), D(9), D(10), D(12)]
        closes = [D(9), D(11), D(13), D(14)]
        atr = calculate_atr(highs, lows, closes, period=2)
        assert atr == D("3.25")

    def test_length_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="length mismatch"):
            calculate_atr(
                [D(110)] * 5, [D(90)] * 4, [D(100)] * 5, period=3
            )

    def test_insufficient_bars_raises(self) -> None:
        with pytest.raises(ValueError, match="at least 15 bars"):
            calculate_atr(
                [D(110)] * 10, [D(90)] * 10, [D(100)] * 10, period=14
            )

    def test_period_too_small_raises(self) -> None:
        with pytest.raises(ValueError, match="period must be"):
            calculate_atr(
                [D(110)] * 5, [D(90)] * 5, [D(100)] * 5, period=0
            )

    def test_uses_prev_close_when_gap(self) -> None:
        # ギャップアップで TR が h-prev_close 経由で大きくなるケース
        # bar0: c=100
        # bar1: h=120 l=115 c=118 → TR1 = max(5, |120-100|=20, |115-100|=15) = 20
        # bar2: h=121 l=119 c=120 → TR2 = max(2, |121-118|=3, |119-118|=1) = 3
        highs = [D(100), D(120), D(121)]
        lows = [D(98), D(115), D(119)]
        closes = [D(100), D(118), D(120)]
        atr = calculate_atr(highs, lows, closes, period=2)
        # seed = (20 + 3)/2 = 11.5、3 本だけなのでこれが最終
        assert atr == Decimal("11.5")


# ─── calculate_atr_pct ──────────────────────


class TestCalculateAtrPct:
    def test_basic_ratio(self) -> None:
        # ATR=20, latest close=100 → 20%
        highs = [D(110)] * 16
        lows = [D(90)] * 16
        closes = [D(100)] * 16
        assert calculate_atr_pct(highs, lows, closes, period=14) == D(20)

    def test_zero_close_returns_zero(self) -> None:
        # 最終 close が 0 でも DivisionByZero しない
        highs = [D(110)] * 16
        lows = [D(90)] * 16
        closes = [*([D(100)] * 15), D(0)]
        assert calculate_atr_pct(highs, lows, closes, period=14) == D(0)

    def test_propagates_input_validation(self) -> None:
        # 内部 calculate_atr のバリデーションが伝播
        with pytest.raises(ValueError, match="at least"):
            calculate_atr_pct([D(1)] * 3, [D(1)] * 3, [D(1)] * 3, period=14)
