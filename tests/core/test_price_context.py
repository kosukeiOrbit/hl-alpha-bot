"""price_context: 過熱フィルター（章5）のテスト。

3基準それぞれの境界値 + AND条件 + LONG/SHORT対称性 + property-based。
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src.core.price_context import is_not_overheated_long, is_not_overheated_short
from tests.core.helpers import make_snapshot

# ────────────────────────────────────────────────
# A. LONG 各基準の境界値
# ────────────────────────────────────────────────


class TestLongOverheatedFilter:
    """3基準それぞれの境界値を網羅。"""

    @pytest.mark.parametrize(
        "utc_change, expected",
        [
            (-0.05, True),  # 下落でも LONG 過熱判定は通過
            (0.0, True),
            (0.04, True),
            (0.0499, True),  # 境界のすぐ下
            (0.05, False),  # 境界（厳密に <）
            (0.06, False),
            (0.20, False),
        ],
    )
    def test_utc_day_change_threshold(self, utc_change: float, expected: bool) -> None:
        snap = make_snapshot(
            utc_open_price=100.0,
            current_price=100.0 * (1 + utc_change),
            # 他の基準が干渉しないよう緩める
            rolling_24h_open=100.0 * (1 + utc_change),
            low_24h=50.0,
            high_24h=200.0,
        )
        assert is_not_overheated_long(snap) is expected

    @pytest.mark.parametrize(
        "change_24h, expected",
        [
            (0.0, True),
            (0.09, True),
            (0.0999, True),
            (0.10, False),
            (0.15, False),
        ],
    )
    def test_rolling_24h_change_threshold(self, change_24h: float, expected: bool) -> None:
        snap = make_snapshot(
            rolling_24h_open=100.0,
            current_price=100.0 * (1 + change_24h),
            # utc_day を緩める（同じ値）
            utc_open_price=100.0 * (1 + change_24h),
            low_24h=50.0,
            high_24h=200.0,
        )
        assert is_not_overheated_long(snap) is expected

    @pytest.mark.parametrize(
        "range_pos, expected",
        [
            (0.0, True),
            (0.50, True),
            (0.84, True),
            (0.8499, True),
            (0.85, False),  # 境界
            (0.95, False),
            (1.0, False),
        ],
    )
    def test_position_in_24h_range_threshold(self, range_pos: float, expected: bool) -> None:
        low = 100.0
        high = 110.0
        current = low + (high - low) * range_pos
        snap = make_snapshot(
            low_24h=low,
            high_24h=high,
            current_price=current,
            # 他の基準が干渉しないよう緩める（変化率を小さく）
            utc_open_price=current,
            rolling_24h_open=current,
        )
        assert is_not_overheated_long(snap) is expected


# ────────────────────────────────────────────────
# B. AND 条件
# ────────────────────────────────────────────────


class TestAndCondition:
    def test_passes_when_all_three_within_threshold(self) -> None:
        # ヘルパーのデフォルトは全層通過想定。
        assert is_not_overheated_long(make_snapshot()) is True

    def test_fails_when_only_utc_violated(self) -> None:
        # utc_day だけ +10%、他は緩める
        snap = make_snapshot(
            utc_open_price=100.0,
            current_price=110.0,
            rolling_24h_open=110.0,  # 24h は同値で 0%
            low_24h=50.0,
            high_24h=200.0,
        )
        assert is_not_overheated_long(snap) is False

    def test_fails_when_only_rolling_24h_violated(self) -> None:
        # 24h だけ +25%、utc は同値で 0%、range も中央
        snap = make_snapshot(
            utc_open_price=125.0,
            current_price=125.0,
            rolling_24h_open=100.0,
            low_24h=50.0,
            high_24h=200.0,
        )
        assert is_not_overheated_long(snap) is False

    def test_fails_when_only_range_position_violated(self) -> None:
        # range だけ高値圏（0.99）、utc/24h は同値で 0%
        snap = make_snapshot(
            utc_open_price=99.0,
            rolling_24h_open=99.0,
            current_price=99.0,
            low_24h=50.0,
            high_24h=99.5,  # position ≒ (99-50)/(99.5-50) = 49/49.5 ≒ 0.99
        )
        assert is_not_overheated_long(snap) is False


# ────────────────────────────────────────────────
# C. SHORT 境界 + LONG/SHORT 対称性
# ────────────────────────────────────────────────


class TestShortOverheatedFilter:
    @pytest.mark.parametrize(
        "utc_change, expected",
        [
            (0.0, True),
            (-0.04, True),
            (-0.0499, True),
            (-0.05, False),  # 境界
            (-0.10, False),
            (0.05, True),  # 上昇方向は SHORT 過熱判定では通過
        ],
    )
    def test_utc_day_change_threshold_short(self, utc_change: float, expected: bool) -> None:
        snap = make_snapshot(
            utc_open_price=100.0,
            current_price=100.0 * (1 + utc_change),
            rolling_24h_open=100.0 * (1 + utc_change),
            low_24h=50.0,
            high_24h=200.0,
        )
        assert is_not_overheated_short(snap) is expected

    @pytest.mark.parametrize(
        "change_24h, expected",
        [
            (0.0, True),
            (-0.09, True),
            (-0.0999, True),
            (-0.10, False),
            (-0.20, False),
            (0.10, True),  # 上昇でSHORT過熱判定は通過
        ],
    )
    def test_rolling_24h_change_threshold_short(self, change_24h: float, expected: bool) -> None:
        snap = make_snapshot(
            rolling_24h_open=100.0,
            current_price=100.0 * (1 + change_24h),
            utc_open_price=100.0 * (1 + change_24h),
            low_24h=50.0,
            high_24h=200.0,
        )
        assert is_not_overheated_short(snap) is expected

    @pytest.mark.parametrize(
        "range_pos, expected",
        [
            (0.0, False),  # 安値張り付き
            (0.10, False),
            (0.15, False),  # 境界（厳密に >）
            (0.16, True),
            (0.50, True),
            (1.0, True),
        ],
    )
    def test_position_in_24h_range_threshold_short(self, range_pos: float, expected: bool) -> None:
        low = 100.0
        high = 110.0
        current = low + (high - low) * range_pos
        snap = make_snapshot(
            low_24h=low,
            high_24h=high,
            current_price=current,
            utc_open_price=current,
            rolling_24h_open=current,
        )
        assert is_not_overheated_short(snap) is expected


class TestLongShortSymmetry:
    """LONG / SHORT が対称的に動くことを確認。"""

    def test_strong_uptrend_blocks_long_allows_short(self) -> None:
        snap = make_snapshot(
            utc_open_price=100.0,
            current_price=108.0,
            rolling_24h_open=108.0,
            low_24h=50.0,
            high_24h=200.0,
        )
        assert is_not_overheated_long(snap) is False
        assert is_not_overheated_short(snap) is True

    def test_strong_downtrend_allows_long_blocks_short(self) -> None:
        snap = make_snapshot(
            utc_open_price=100.0,
            current_price=92.0,
            rolling_24h_open=92.0,
            low_24h=50.0,
            high_24h=200.0,
        )
        assert is_not_overheated_long(snap) is True
        assert is_not_overheated_short(snap) is False


# ────────────────────────────────────────────────
# D. プロパティベース（全域定義性）
# ────────────────────────────────────────────────


class TestPropertyBased:
    @given(
        utc_open=st.floats(min_value=1.0, max_value=100_000.0, allow_nan=False),
        current=st.floats(min_value=1.0, max_value=200_000.0, allow_nan=False),
        rolling_24h_open=st.floats(min_value=1.0, max_value=100_000.0, allow_nan=False),
        low=st.floats(min_value=1.0, max_value=100_000.0, allow_nan=False),
        high=st.floats(min_value=1.0, max_value=200_000.0, allow_nan=False),
    )
    def test_long_total_function(
        self,
        utc_open: float,
        current: float,
        rolling_24h_open: float,
        low: float,
        high: float,
    ) -> None:
        """任意の入力で例外を出さず bool を返す。"""
        if high < low:
            low, high = high, low
        current = max(low, min(current, high))

        snap = make_snapshot(
            utc_open_price=utc_open,
            current_price=current,
            rolling_24h_open=rolling_24h_open,
            low_24h=low,
            high_24h=high,
        )
        assert isinstance(is_not_overheated_long(snap), bool)
        assert isinstance(is_not_overheated_short(snap), bool)
