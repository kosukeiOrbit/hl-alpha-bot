"""core/stop_loss のテスト（章13.3・章11.7）。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from src.core.stop_loss import StopLossInput, calculate_sl_tp


# ────────────────────────────────────────────────
# A. LONG 基本
# ────────────────────────────────────────────────


class TestLongStopLoss:
    def test_basic_long(self) -> None:
        # SL = 100 - 2*1.5 = 97; TP = 100 + 2*2.5 = 105
        result = calculate_sl_tp(
            StopLossInput(
                direction="LONG",
                entry_price=Decimal("100"),
                atr_value=Decimal("2"),
                sl_multiplier=Decimal("1.5"),
                tp_multiplier=Decimal("2.5"),
                tick_size=Decimal("0.1"),
            )
        )
        assert result.sl_price == Decimal("97.0")
        assert result.tp_price == Decimal("105.0")

    def test_long_sl_below_entry_tp_above(self) -> None:
        result = calculate_sl_tp(
            StopLossInput(
                direction="LONG",
                entry_price=Decimal("100"),
                atr_value=Decimal("1"),
                sl_multiplier=Decimal("1.5"),
                tp_multiplier=Decimal("2.5"),
                tick_size=Decimal("0.1"),
            )
        )
        assert result.sl_price < Decimal("100")
        assert result.tp_price > Decimal("100")

    def test_long_min_tick_buffer(self) -> None:
        # ATR 極小 → 距離が tick 以下になる場合でも最低 1tick 差を保証。
        result = calculate_sl_tp(
            StopLossInput(
                direction="LONG",
                entry_price=Decimal("100"),
                atr_value=Decimal("0.001"),
                sl_multiplier=Decimal("1"),
                tp_multiplier=Decimal("1"),
                tick_size=Decimal("0.1"),
            )
        )
        assert result.sl_price <= Decimal("100") - Decimal("0.1")
        assert result.tp_price >= Decimal("100") + Decimal("0.1")

    def test_long_high_atr_does_not_clip(self) -> None:
        # ATR 十分大きい場合、min_diff クランプは発動せず ATR 距離が反映される。
        result = calculate_sl_tp(
            StopLossInput(
                direction="LONG",
                entry_price=Decimal("100"),
                atr_value=Decimal("5"),
                sl_multiplier=Decimal("1.5"),
                tp_multiplier=Decimal("2.5"),
                tick_size=Decimal("0.1"),
            )
        )
        # SL = 100 - 7.5 = 92.5; TP = 100 + 12.5 = 112.5
        assert result.sl_price == Decimal("92.5")
        assert result.tp_price == Decimal("112.5")


# ────────────────────────────────────────────────
# B. SHORT 基本
# ────────────────────────────────────────────────


class TestShortStopLoss:
    def test_basic_short(self) -> None:
        # SL = 100 + 3 = 103; TP = 100 - 5 = 95
        result = calculate_sl_tp(
            StopLossInput(
                direction="SHORT",
                entry_price=Decimal("100"),
                atr_value=Decimal("2"),
                sl_multiplier=Decimal("1.5"),
                tp_multiplier=Decimal("2.5"),
                tick_size=Decimal("0.1"),
            )
        )
        assert result.sl_price == Decimal("103.0")
        assert result.tp_price == Decimal("95.0")

    def test_short_sl_above_entry_tp_below(self) -> None:
        result = calculate_sl_tp(
            StopLossInput(
                direction="SHORT",
                entry_price=Decimal("100"),
                atr_value=Decimal("1"),
                sl_multiplier=Decimal("1.5"),
                tp_multiplier=Decimal("2.5"),
                tick_size=Decimal("0.1"),
            )
        )
        assert result.sl_price > Decimal("100")
        assert result.tp_price < Decimal("100")

    def test_short_min_tick_buffer(self) -> None:
        result = calculate_sl_tp(
            StopLossInput(
                direction="SHORT",
                entry_price=Decimal("100"),
                atr_value=Decimal("0.001"),
                sl_multiplier=Decimal("1"),
                tp_multiplier=Decimal("1"),
                tick_size=Decimal("0.1"),
            )
        )
        assert result.sl_price >= Decimal("100") + Decimal("0.1")
        assert result.tp_price <= Decimal("100") - Decimal("0.1")


# ────────────────────────────────────────────────
# C. tick_size 丸め
# ────────────────────────────────────────────────


class TestTickSizeRounding:
    def test_decimal_tick(self) -> None:
        # tick=0.1 → 結果は 0.1 単位
        result = calculate_sl_tp(
            StopLossInput(
                direction="LONG",
                entry_price=Decimal("65432.5"),
                atr_value=Decimal("100"),
                sl_multiplier=Decimal("1.5"),
                tp_multiplier=Decimal("2.5"),
                tick_size=Decimal("0.1"),
            )
        )
        # SL/TP が tick=0.1 の倍数（×10で整数）。
        assert (result.sl_price * 10) % 1 == 0
        assert (result.tp_price * 10) % 1 == 0

    def test_btc_integer_tick(self) -> None:
        # tick=1（整数銘柄想定）。SL/TP が整数になる。
        result = calculate_sl_tp(
            StopLossInput(
                direction="LONG",
                entry_price=Decimal("65000"),
                atr_value=Decimal("100"),
                sl_multiplier=Decimal("1.5"),
                tp_multiplier=Decimal("2.5"),
                tick_size=Decimal("1"),
            )
        )
        assert result.sl_price % 1 == 0
        assert result.tp_price % 1 == 0
        # SL = 65000 - 150 = 64850; TP = 65000 + 250 = 65250
        assert result.sl_price == Decimal("64850")
        assert result.tp_price == Decimal("65250")

    def test_fractional_atr_rounded_to_tick(self) -> None:
        # ATR=0.27, tick=0.1 → 距離 0.405 → tick 0.1 に丸めて 0.4 になる
        result = calculate_sl_tp(
            StopLossInput(
                direction="LONG",
                entry_price=Decimal("100"),
                atr_value=Decimal("0.27"),
                sl_multiplier=Decimal("1.5"),
                tp_multiplier=Decimal("2.5"),
                tick_size=Decimal("0.1"),
            )
        )
        # 100 - 0.405 = 99.595 → tick丸め → 99.6
        assert result.sl_price == Decimal("99.6")
        # 100 + 0.675 = 100.675 → 100.7
        assert result.tp_price == Decimal("100.7")

    def test_small_tick_preserves_precision(self) -> None:
        # tick=0.001（ALT で典型）
        result = calculate_sl_tp(
            StopLossInput(
                direction="LONG",
                entry_price=Decimal("1.234"),
                atr_value=Decimal("0.005"),
                sl_multiplier=Decimal("1.5"),
                tp_multiplier=Decimal("2.5"),
                tick_size=Decimal("0.001"),
            )
        )
        # SL = 1.234 - 0.0075 = 1.2265 → tick=0.001 で 1.227 (HALF_UP)
        # TP = 1.234 + 0.0125 = 1.2465 → 1.247
        # tick の倍数（×1000で整数）。
        assert (result.sl_price * 1000) % 1 == 0
        assert (result.tp_price * 1000) % 1 == 0


# ────────────────────────────────────────────────
# D. invalid direction
# ────────────────────────────────────────────────


class TestInvalidInput:
    def test_invalid_direction_raises(self) -> None:
        with pytest.raises(ValueError, match="direction must be"):
            calculate_sl_tp(
                StopLossInput(
                    direction="BOTH",
                    entry_price=Decimal("100"),
                    atr_value=Decimal("1"),
                    sl_multiplier=Decimal("1.5"),
                    tp_multiplier=Decimal("2.5"),
                    tick_size=Decimal("0.1"),
                )
            )

    def test_lowercase_direction_raises(self) -> None:
        with pytest.raises(ValueError):
            calculate_sl_tp(
                StopLossInput(
                    direction="long",
                    entry_price=Decimal("100"),
                    atr_value=Decimal("1"),
                    sl_multiplier=Decimal("1.5"),
                    tp_multiplier=Decimal("2.5"),
                    tick_size=Decimal("0.1"),
                )
            )
