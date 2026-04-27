"""core/position_sizer のテスト（章13.2・章11.7）。"""
from __future__ import annotations

from decimal import Decimal

from src.core.position_sizer import SizingInput, calculate_position_size


# ────────────────────────────────────────────────
# A. 基本動作
# ────────────────────────────────────────────────


class TestBasicSizing:
    def test_basic_long_position_sizing(self) -> None:
        # notional = 2000 × 0.05 × 3 = 300 USD
        # raw_size = 300 / 65000 = 0.00461538...
        # 5桁切り捨て → 0.00461
        result = calculate_position_size(
            SizingInput(
                account_balance_usd=Decimal("2000"),
                entry_price=Decimal("65000"),
                sl_price=Decimal("64000"),
                leverage=3,
                position_size_pct=Decimal("0.05"),
                sz_decimals=5,
            )
        )
        assert result.size_coins == Decimal("0.00461")
        assert result.rejected_reason is None

    def test_notional_calculation(self) -> None:
        # notional = 1000 × 0.10 × 2 = 200, size = 2.00
        result = calculate_position_size(
            SizingInput(
                account_balance_usd=Decimal("1000"),
                entry_price=Decimal("100"),
                sl_price=Decimal("99"),
                leverage=2,
                position_size_pct=Decimal("0.10"),
                sz_decimals=2,
            )
        )
        assert result.size_coins == Decimal("2.00")
        assert result.notional_usd == Decimal("200.00")

    def test_risk_calculation(self) -> None:
        # entry-SL=2, size=1.00, total risk=2.00
        result = calculate_position_size(
            SizingInput(
                account_balance_usd=Decimal("1000"),
                entry_price=Decimal("100"),
                sl_price=Decimal("98"),
                leverage=1,
                position_size_pct=Decimal("0.10"),
                sz_decimals=2,
            )
        )
        assert result.risk_per_unit == Decimal("2")
        assert result.risk_total_usd == Decimal("2.00")

    def test_risk_per_unit_is_absolute(self) -> None:
        # SHORT 等で sl_price > entry_price でも abs() で同じ。
        result = calculate_position_size(
            SizingInput(
                account_balance_usd=Decimal("1000"),
                entry_price=Decimal("100"),
                sl_price=Decimal("102"),  # SHORT想定
                leverage=1,
                position_size_pct=Decimal("0.10"),
                sz_decimals=2,
            )
        )
        assert result.risk_per_unit == Decimal("2")


# ────────────────────────────────────────────────
# B. 連敗ペナルティ
# ────────────────────────────────────────────────


class TestConsecutiveLossPenalty:
    def test_no_penalty_when_zero_losses(self) -> None:
        result = calculate_position_size(
            SizingInput(
                account_balance_usd=Decimal("1000"),
                entry_price=Decimal("100"),
                sl_price=Decimal("99"),
                leverage=1,
                position_size_pct=Decimal("0.10"),
                sz_decimals=2,
                consecutive_losses=0,
            )
        )
        assert result.size_coins == Decimal("1.00")

    def test_no_penalty_at_2_losses(self) -> None:
        result = calculate_position_size(
            SizingInput(
                account_balance_usd=Decimal("1000"),
                entry_price=Decimal("100"),
                sl_price=Decimal("99"),
                leverage=1,
                position_size_pct=Decimal("0.10"),
                sz_decimals=2,
                consecutive_losses=2,
            )
        )
        assert result.size_coins == Decimal("1.00")

    def test_half_penalty_at_3_losses(self) -> None:
        # 3連敗 → 0.5倍
        result = calculate_position_size(
            SizingInput(
                account_balance_usd=Decimal("1000"),
                entry_price=Decimal("100"),
                sl_price=Decimal("99"),
                leverage=1,
                position_size_pct=Decimal("0.10"),
                sz_decimals=2,
                consecutive_losses=3,
            )
        )
        assert result.size_coins == Decimal("0.50")

    def test_half_penalty_at_5_losses(self) -> None:
        # ペナルティは閾値超でフラット（指数的に減らない仕様）。
        result = calculate_position_size(
            SizingInput(
                account_balance_usd=Decimal("1000"),
                entry_price=Decimal("100"),
                sl_price=Decimal("99"),
                leverage=1,
                position_size_pct=Decimal("0.10"),
                sz_decimals=2,
                consecutive_losses=5,
            )
        )
        assert result.size_coins == Decimal("0.50")


# ────────────────────────────────────────────────
# C. szDecimals 丸め
# ────────────────────────────────────────────────


class TestSzDecimalsRounding:
    def test_btc_5_decimals_exponent(self) -> None:
        result = calculate_position_size(
            SizingInput(
                account_balance_usd=Decimal("1000"),
                entry_price=Decimal("65432.10"),
                sl_price=Decimal("64000"),
                leverage=3,
                position_size_pct=Decimal("0.05"),
                sz_decimals=5,
            )
        )
        # quantize で指数が -5 になる
        assert result.size_coins.as_tuple().exponent == -5

    def test_floor_rounding_not_round_up(self) -> None:
        # raw = 10 / 99.999 = 0.10000100...; ROUND_DOWN → 0.10
        result = calculate_position_size(
            SizingInput(
                account_balance_usd=Decimal("100"),
                entry_price=Decimal("99.999"),
                sl_price=Decimal("99"),
                leverage=1,
                position_size_pct=Decimal("0.10"),
                sz_decimals=2,
            )
        )
        assert result.size_coins == Decimal("0.10")

    def test_zero_decimals_works(self) -> None:
        # sz_decimals=0（整数銘柄）のときは小数なしに丸める。
        result = calculate_position_size(
            SizingInput(
                account_balance_usd=Decimal("10000"),
                entry_price=Decimal("3"),
                sl_price=Decimal("2.5"),
                leverage=1,
                position_size_pct=Decimal("0.10"),
                sz_decimals=0,
            )
        )
        # raw = 1000 / 3 = 333.33...; floor → 333
        assert result.size_coins == Decimal("333")

    def test_large_decimals_works(self) -> None:
        # sz_decimals=8 は spot 等で出る精度。
        result = calculate_position_size(
            SizingInput(
                account_balance_usd=Decimal("1000"),
                entry_price=Decimal("100"),
                sl_price=Decimal("99"),
                leverage=1,
                position_size_pct=Decimal("0.10"),
                sz_decimals=8,
            )
        )
        assert result.size_coins.as_tuple().exponent == -8


# ────────────────────────────────────────────────
# D. 棄却ケース
# ────────────────────────────────────────────────


class TestRejection:
    def test_rejects_when_size_too_small(self) -> None:
        # 0.05 / 100000 = 0.0000005 → 5桁切り捨て → 0
        result = calculate_position_size(
            SizingInput(
                account_balance_usd=Decimal("1"),
                entry_price=Decimal("100000"),
                sl_price=Decimal("99000"),
                leverage=1,
                position_size_pct=Decimal("0.05"),
                sz_decimals=5,
            )
        )
        assert result.size_coins == Decimal("0")
        assert result.rejected_reason == "size_too_small_after_rounding"
        assert result.notional_usd == Decimal("0")
        assert result.risk_total_usd == Decimal("0")

    def test_rejects_when_loss_penalty_zeros_size(self) -> None:
        # ペナルティで 0 になる境界。
        result = calculate_position_size(
            SizingInput(
                account_balance_usd=Decimal("2"),
                entry_price=Decimal("100000"),
                sl_price=Decimal("99000"),
                leverage=1,
                position_size_pct=Decimal("0.05"),
                sz_decimals=5,
                consecutive_losses=3,
            )
        )
        assert result.size_coins == Decimal("0")
        assert result.rejected_reason == "size_too_small_after_rounding"


# ────────────────────────────────────────────────
# E. 不変性
# ────────────────────────────────────────────────


class TestImmutability:
    def test_input_dataclass_is_frozen(self) -> None:
        import pytest

        x = SizingInput(
            account_balance_usd=Decimal("1000"),
            entry_price=Decimal("100"),
            sl_price=Decimal("99"),
            leverage=1,
            position_size_pct=Decimal("0.10"),
            sz_decimals=2,
        )
        with pytest.raises(Exception):
            x.account_balance_usd = Decimal("0")  # type: ignore[misc]
