"""core/funding_judge のテスト（章13.4・章11.7-11.8）。"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src.core.funding_judge import FundingExitInput, should_exit_before_funding

# ────────────────────────────────────────────────
# A. 時間ゲート
# ────────────────────────────────────────────────


class TestTimingGate:
    def test_no_action_when_far_from_funding(self) -> None:
        # 精算まで遠い → 評価せず False
        result = should_exit_before_funding(
            FundingExitInput(
                direction="LONG",
                funding_rate=Decimal("0.001"),
                unrealized_pnl_pct=Decimal("0.1"),
                minutes_to_funding=30,
                threshold_minutes=5,
            )
        )
        assert result is False

    def test_evaluates_at_threshold(self) -> None:
        # minutes_to_funding == threshold（5 > 5 は False）→ 評価実施
        result = should_exit_before_funding(
            FundingExitInput(
                direction="LONG",
                funding_rate=Decimal("0.001"),
                unrealized_pnl_pct=Decimal("0.1"),
                minutes_to_funding=5,
                threshold_minutes=5,
            )
        )
        assert result is True

    def test_evaluates_below_threshold(self) -> None:
        result = should_exit_before_funding(
            FundingExitInput(
                direction="LONG",
                funding_rate=Decimal("0.001"),
                unrealized_pnl_pct=Decimal("0.1"),
                minutes_to_funding=2,
                threshold_minutes=5,
            )
        )
        assert result is True


# ────────────────────────────────────────────────
# B. 受取側は維持
# ────────────────────────────────────────────────


class TestReceivingFunding:
    def test_long_receives_negative_funding(self) -> None:
        # LONG + 負Funding → 受取 → 維持
        result = should_exit_before_funding(
            FundingExitInput(
                direction="LONG",
                funding_rate=Decimal("-0.001"),
                unrealized_pnl_pct=Decimal("0.1"),
                minutes_to_funding=2,
                threshold_minutes=5,
            )
        )
        assert result is False

    def test_short_receives_positive_funding(self) -> None:
        # SHORT + 正Funding → 受取 → 維持
        result = should_exit_before_funding(
            FundingExitInput(
                direction="SHORT",
                funding_rate=Decimal("0.001"),
                unrealized_pnl_pct=Decimal("0.1"),
                minutes_to_funding=2,
                threshold_minutes=5,
            )
        )
        assert result is False

    def test_zero_funding_treated_as_receiving(self) -> None:
        # funding=0 は支払い側ではない（strict > 0 / strict < 0 で判定）→ 維持
        result = should_exit_before_funding(
            FundingExitInput(
                direction="LONG",
                funding_rate=Decimal("0"),
                unrealized_pnl_pct=Decimal("0.1"),
                minutes_to_funding=2,
                threshold_minutes=5,
            )
        )
        assert result is False


# ────────────────────────────────────────────────
# C. 支払い側 + PnL判定
# ────────────────────────────────────────────────


class TestPayingFunding:
    def test_long_paying_thin_profit_exits(self) -> None:
        # LONG + 正Funding + 含み益 < 0.5% → 手仕舞い
        result = should_exit_before_funding(
            FundingExitInput(
                direction="LONG",
                funding_rate=Decimal("0.001"),
                unrealized_pnl_pct=Decimal("0.3"),
                minutes_to_funding=2,
                threshold_minutes=5,
            )
        )
        assert result is True

    def test_long_paying_thick_profit_holds(self) -> None:
        # LONG + 正Funding + 含み益 >= 0.5% → 維持
        result = should_exit_before_funding(
            FundingExitInput(
                direction="LONG",
                funding_rate=Decimal("0.001"),
                unrealized_pnl_pct=Decimal("1.0"),
                minutes_to_funding=2,
                threshold_minutes=5,
            )
        )
        assert result is False

    def test_long_paying_at_threshold_holds(self) -> None:
        # 境界: 0.5% 同値は < 0.5 で False → 維持
        result = should_exit_before_funding(
            FundingExitInput(
                direction="LONG",
                funding_rate=Decimal("0.001"),
                unrealized_pnl_pct=Decimal("0.5"),
                minutes_to_funding=2,
                threshold_minutes=5,
            )
        )
        assert result is False

    def test_short_paying_thin_profit_exits(self) -> None:
        # SHORT + 負Funding + 含み益薄 → 手仕舞い
        result = should_exit_before_funding(
            FundingExitInput(
                direction="SHORT",
                funding_rate=Decimal("-0.001"),
                unrealized_pnl_pct=Decimal("0.2"),
                minutes_to_funding=2,
                threshold_minutes=5,
            )
        )
        assert result is True

    def test_paying_with_negative_unrealized_exits(self) -> None:
        # 支払い側で含み損 → 当然手仕舞い（< 0.5 を満たす）。
        result = should_exit_before_funding(
            FundingExitInput(
                direction="LONG",
                funding_rate=Decimal("0.001"),
                unrealized_pnl_pct=Decimal("-0.5"),
                minutes_to_funding=2,
                threshold_minutes=5,
            )
        )
        assert result is True


# ────────────────────────────────────────────────
# D. invalid direction
# ────────────────────────────────────────────────


class TestInvalidInput:
    def test_invalid_direction_raises(self) -> None:
        with pytest.raises(ValueError, match="direction must be"):
            should_exit_before_funding(
                FundingExitInput(
                    direction="NEUTRAL",
                    funding_rate=Decimal("0.001"),
                    unrealized_pnl_pct=Decimal("0.1"),
                    minutes_to_funding=2,
                    threshold_minutes=5,
                )
            )

    def test_invalid_direction_validated_before_timing(self) -> None:
        # 時間ゲート前に direction を検証（minutes が遠くてもエラー）。
        with pytest.raises(ValueError):
            should_exit_before_funding(
                FundingExitInput(
                    direction="x",
                    funding_rate=Decimal("0.001"),
                    unrealized_pnl_pct=Decimal("0.1"),
                    minutes_to_funding=999,
                    threshold_minutes=5,
                )
            )


# ────────────────────────────────────────────────
# E. property-based
# ────────────────────────────────────────────────


class TestPropertyBased:
    @given(
        funding_rate=st.decimals(
            min_value=Decimal("-0.1"),
            max_value=Decimal("0.1"),
            allow_nan=False,
            allow_infinity=False,
            places=4,
        ),
        unrealized=st.decimals(
            min_value=Decimal("-100"),
            max_value=Decimal("100"),
            allow_nan=False,
            allow_infinity=False,
            places=2,
        ),
        minutes=st.integers(min_value=0, max_value=60),
    )
    def test_total_function(self, funding_rate: Decimal, unrealized: Decimal, minutes: int) -> None:
        result = should_exit_before_funding(
            FundingExitInput(
                direction="LONG",
                funding_rate=funding_rate,
                unrealized_pnl_pct=unrealized,
                minutes_to_funding=minutes,
                threshold_minutes=5,
            )
        )
        assert isinstance(result, bool)

    @given(
        funding_rate=st.decimals(
            min_value=Decimal("-0.1"),
            max_value=Decimal("0.1"),
            allow_nan=False,
            allow_infinity=False,
            places=4,
        ),
        minutes=st.integers(min_value=10, max_value=60),
    )
    def test_far_from_funding_always_holds(self, funding_rate: Decimal, minutes: int) -> None:
        # threshold=5 よりずっと先の精算なら、他の条件によらず False。
        result = should_exit_before_funding(
            FundingExitInput(
                direction="LONG",
                funding_rate=funding_rate,
                unrealized_pnl_pct=Decimal("0"),
                minutes_to_funding=minutes,
                threshold_minutes=5,
            )
        )
        assert result is False
