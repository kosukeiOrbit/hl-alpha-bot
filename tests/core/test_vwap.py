"""core/vwap のテスト（章6・章11.7-11.8）。

- calculate_vwap_from_volume の境界
- update_vwap_state の初回・クロス検出・距離極値・累積
- vwap_state_to_record の dict 変換
- property-based で全域定義性
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src.core.vwap import (
    VWAPState,
    calculate_vwap_from_volume,
    update_vwap_state,
    vwap_state_to_record,
)

# ────────────────────────────────────────────────
# A. calculate_vwap_from_volume
# ────────────────────────────────────────────────


class TestCalculateVWAP:
    def test_basic_calculation(self) -> None:
        vwap = calculate_vwap_from_volume(
            day_volume_usd=Decimal("100000"),
            day_volume_base=Decimal("10"),
            fallback_price=Decimal("9000"),
        )
        assert vwap == Decimal("10000")

    def test_zero_base_volume_returns_fallback(self) -> None:
        vwap = calculate_vwap_from_volume(
            day_volume_usd=Decimal("0"),
            day_volume_base=Decimal("0"),
            fallback_price=Decimal("65000"),
        )
        assert vwap == Decimal("65000")

    def test_negative_base_volume_returns_fallback(self) -> None:
        # 異常値（負）でも fallback を返す（防御的）。
        vwap = calculate_vwap_from_volume(
            day_volume_usd=Decimal("100"),
            day_volume_base=Decimal("-1"),
            fallback_price=Decimal("50000"),
        )
        assert vwap == Decimal("50000")

    def test_realistic_btc_value(self) -> None:
        # BTC: $100M volume / 1500 BTC ≒ $66,666 VWAP
        vwap = calculate_vwap_from_volume(
            day_volume_usd=Decimal("100000000"),
            day_volume_base=Decimal("1500"),
            fallback_price=Decimal("0"),
        )
        assert vwap == Decimal("100000000") / Decimal("1500")

    def test_decimal_precision_preserved(self) -> None:
        # Decimalで割った結果がfloat演算と区別できる精度で保たれること。
        vwap = calculate_vwap_from_volume(
            day_volume_usd=Decimal("1000.123"),
            day_volume_base=Decimal("3"),
            fallback_price=Decimal("0"),
        )
        assert vwap == Decimal("1000.123") / Decimal("3")


# ────────────────────────────────────────────────
# B. update_vwap_state: 初回update（last_above=None からの遷移）
# ────────────────────────────────────────────────


class TestVWAPStateInitialUpdate:
    def test_above_vwap_no_cross(self) -> None:
        state = VWAPState()
        new = update_vwap_state(state, current_price=100.5, vwap=100.0, elapsed_sec=3)
        assert new.cross_count == 0
        assert new.above_seconds == 3
        assert new.below_seconds == 0
        assert new.last_above is True

    def test_below_vwap_no_cross(self) -> None:
        state = VWAPState()
        new = update_vwap_state(state, current_price=99.5, vwap=100.0, elapsed_sec=5)
        assert new.cross_count == 0
        assert new.above_seconds == 0
        assert new.below_seconds == 5
        assert new.last_above is False

    def test_exactly_at_vwap_treated_as_below(self) -> None:
        # current == vwap は below 扱い（is_above = current > vwap の strict 判定）。
        state = VWAPState()
        new = update_vwap_state(state, current_price=100.0, vwap=100.0, elapsed_sec=1)
        assert new.last_above is False
        assert new.below_seconds == 1


# ────────────────────────────────────────────────
# C. クロス検出
# ────────────────────────────────────────────────


class TestVWAPCrossDetection:
    def test_cross_from_above_to_below(self) -> None:
        state = VWAPState(last_above=True, above_seconds=10)
        new = update_vwap_state(state, current_price=99.5, vwap=100.0, elapsed_sec=1)
        assert new.cross_count == 1
        assert new.last_above is False

    def test_cross_from_below_to_above(self) -> None:
        state = VWAPState(last_above=False, below_seconds=10)
        new = update_vwap_state(state, current_price=100.5, vwap=100.0, elapsed_sec=1)
        assert new.cross_count == 1
        assert new.last_above is True

    def test_no_cross_when_staying_above(self) -> None:
        state = VWAPState(last_above=True, above_seconds=10)
        new = update_vwap_state(state, current_price=100.5, vwap=100.0, elapsed_sec=3)
        assert new.cross_count == 0
        assert new.above_seconds == 13

    def test_no_cross_when_staying_below(self) -> None:
        state = VWAPState(last_above=False, below_seconds=20)
        new = update_vwap_state(state, current_price=99.5, vwap=100.0, elapsed_sec=2)
        assert new.cross_count == 0
        assert new.below_seconds == 22

    def test_alternating_cross_pattern(self) -> None:
        # A→B→A→B のように4回中3クロス。
        state = VWAPState()
        state = update_vwap_state(state, 100.5, 100.0, 1)  # 初回 above
        assert state.cross_count == 0
        state = update_vwap_state(state, 99.5, 100.0, 1)  # cross 1
        assert state.cross_count == 1
        state = update_vwap_state(state, 100.5, 100.0, 1)  # cross 2
        assert state.cross_count == 2
        state = update_vwap_state(state, 99.5, 100.0, 1)  # cross 3
        assert state.cross_count == 3


# ────────────────────────────────────────────────
# D. 距離（min/max）の追跡
# ────────────────────────────────────────────────


class TestVWAPDistanceExtremes:
    def test_initial_min_max(self) -> None:
        state = VWAPState()
        new = update_vwap_state(state, current_price=100.5, vwap=100.0, elapsed_sec=1)
        # distance_pct = +0.5
        assert new.min_distance_pct == pytest.approx(0.5)
        assert new.max_distance_pct == pytest.approx(0.5)

    def test_max_updated_on_higher(self) -> None:
        state = VWAPState(min_distance_pct=0.0, max_distance_pct=0.3)
        new = update_vwap_state(state, current_price=101.0, vwap=100.0, elapsed_sec=1)
        # distance_pct = +1.0
        assert new.max_distance_pct == pytest.approx(1.0)
        assert new.min_distance_pct == pytest.approx(0.0)

    def test_min_updated_on_lower(self) -> None:
        state = VWAPState(min_distance_pct=0.3, max_distance_pct=1.0)
        new = update_vwap_state(state, current_price=99.5, vwap=100.0, elapsed_sec=1)
        # distance_pct = -0.5
        assert new.min_distance_pct == pytest.approx(-0.5)
        assert new.max_distance_pct == pytest.approx(1.0)

    def test_tracks_negative_distance(self) -> None:
        # VWAP下の時は負の distance_pct。
        state = VWAPState()
        new = update_vwap_state(state, current_price=98.0, vwap=100.0, elapsed_sec=1)
        assert new.min_distance_pct == pytest.approx(-2.0)
        assert new.max_distance_pct == pytest.approx(-2.0)


# ────────────────────────────────────────────────
# E. 累積動作・不変性
# ────────────────────────────────────────────────


class TestVWAPStateAccumulation:
    def test_multiple_updates_above(self) -> None:
        state = VWAPState()
        for _ in range(5):
            state = update_vwap_state(state, 100.5, 100.0, 3)
        assert state.above_seconds == 15
        assert state.below_seconds == 0
        assert state.cross_count == 0
        assert state.last_above is True

    def test_state_is_immutable(self) -> None:
        # update_vwap_state は元のstateを変更しない（dataclass.replace の純関数性）。
        state = VWAPState()
        new = update_vwap_state(state, 100.5, 100.0, 1)
        # 元のstateは初期値のまま
        assert state.above_seconds == 0
        assert state.last_above is None
        # 新しいstateだけ更新されている
        assert new.above_seconds == 1
        assert new.last_above is True

    def test_frozen_dataclass_rejects_attribute_assignment(self) -> None:
        state = VWAPState()
        with pytest.raises(FrozenInstanceError):
            state.cross_count = 99  # type: ignore[misc]

    def test_above_below_sum_equals_total_elapsed(self) -> None:
        # 4回 update して累積秒の合計が elapsed の合計と一致する。
        state = VWAPState()
        state = update_vwap_state(state, 100.5, 100.0, 3)  # above 3
        state = update_vwap_state(state, 99.5, 100.0, 2)  # below 2
        state = update_vwap_state(state, 100.5, 100.0, 5)  # above 5
        state = update_vwap_state(state, 99.5, 100.0, 1)  # below 1
        assert state.above_seconds + state.below_seconds == 11
        assert state.cross_count == 3


# ────────────────────────────────────────────────
# F. vwap_state_to_record
# ────────────────────────────────────────────────


class TestVWAPStateToRecord:
    def test_record_with_data(self) -> None:
        state = VWAPState(
            cross_count=2,
            above_seconds=600,
            below_seconds=400,
            min_distance_pct=-0.3,
            max_distance_pct=1.2,
            last_above=True,
        )
        record = vwap_state_to_record(state)
        assert record["vwap_cross_count"] == 2
        assert record["vwap_held_above_pct"] == pytest.approx(0.6)
        assert record["min_vwap_distance_pct"] == pytest.approx(-0.3)
        assert record["max_vwap_distance_pct"] == pytest.approx(1.2)

    def test_record_with_no_data(self) -> None:
        # 初期VWAPState（一度もupdateされていない）。
        state = VWAPState()
        record = vwap_state_to_record(state)
        assert record["vwap_cross_count"] == 0
        assert record["vwap_held_above_pct"] == 0
        # inf/-inf は None に変換される。
        assert record["min_vwap_distance_pct"] is None
        assert record["max_vwap_distance_pct"] is None

    def test_held_above_pct_zero_when_only_below(self) -> None:
        state = VWAPState(below_seconds=100, above_seconds=0)
        record = vwap_state_to_record(state)
        assert record["vwap_held_above_pct"] == pytest.approx(0.0)

    def test_held_above_pct_one_when_only_above(self) -> None:
        state = VWAPState(below_seconds=0, above_seconds=100)
        record = vwap_state_to_record(state)
        assert record["vwap_held_above_pct"] == pytest.approx(1.0)

    def test_record_keys_are_db_friendly(self) -> None:
        # 章8.2 trades テーブルのカラム名と一致するキー集合。
        record = vwap_state_to_record(VWAPState())
        assert set(record.keys()) == {
            "vwap_cross_count",
            "vwap_held_above_pct",
            "min_vwap_distance_pct",
            "max_vwap_distance_pct",
        }


# ────────────────────────────────────────────────
# G. プロパティベース（全域定義性）
# ────────────────────────────────────────────────


class TestPropertyBased:
    @given(
        current_price=st.floats(
            min_value=0.01, max_value=1_000_000.0, allow_nan=False, allow_infinity=False
        ),
        vwap=st.floats(
            min_value=0.01, max_value=1_000_000.0, allow_nan=False, allow_infinity=False
        ),
        elapsed=st.integers(min_value=0, max_value=86400),
    )
    def test_first_update_invariants(self, current_price: float, vwap: float, elapsed: int) -> None:
        # 初回updateの不変条件: above + below == elapsed, cross_count == 0。
        state = VWAPState()
        new = update_vwap_state(state, current_price, vwap, elapsed)

        assert new.above_seconds + new.below_seconds == elapsed
        assert new.cross_count == 0
        assert isinstance(new.last_above, bool)

    @given(n_updates=st.integers(min_value=1, max_value=100))
    def test_cross_count_never_decreases(self, n_updates: int) -> None:
        # update_vwap_state は cross_count を減らさない（単調非減少）。
        state = VWAPState()
        prev_cross = 0
        for i in range(n_updates):
            price = 101.0 if i % 2 == 0 else 99.0
            state = update_vwap_state(state, price, 100.0, 1)
            assert state.cross_count >= prev_cross
            prev_cross = state.cross_count

    @given(
        prices=st.lists(
            st.floats(min_value=50.0, max_value=150.0, allow_nan=False),
            min_size=1,
            max_size=50,
        )
    )
    def test_seconds_are_non_negative(self, prices: list[float]) -> None:
        state = VWAPState()
        for price in prices:
            state = update_vwap_state(state, price, 100.0, 1)
        assert state.above_seconds >= 0
        assert state.below_seconds >= 0
