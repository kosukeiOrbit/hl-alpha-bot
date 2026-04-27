"""MarketSnapshot の派生プロパティ単体テスト。

設計書 11.4 の MarketSnapshot に定義されたプロパティが、
章5 PriceContext と同じ意味の値を返すかを検証する。
"""
from __future__ import annotations

import pytest

from src.core.models import MarketSnapshot
from tests.core.helpers import make_snapshot


class TestVwapDistance:
    def test_above_vwap_returns_positive_pct(self) -> None:
        snap = make_snapshot(current_price=101.0, vwap=100.0)
        assert snap.vwap_distance_pct == pytest.approx(1.0)

    def test_below_vwap_returns_negative_pct(self) -> None:
        snap = make_snapshot(current_price=99.5, vwap=100.0)
        assert snap.vwap_distance_pct == pytest.approx(-0.5)

    def test_at_vwap_returns_zero(self) -> None:
        snap = make_snapshot(current_price=100.0, vwap=100.0)
        assert snap.vwap_distance_pct == 0.0

    def test_zero_vwap_falls_back_to_zero(self) -> None:
        snap = make_snapshot(current_price=100.0, vwap=0.0)
        assert snap.vwap_distance_pct == 0.0


class TestUtcDayChange:
    def test_returns_decimal_ratio_not_percent(self) -> None:
        # 章5 と章23 は decimal 表現（+5% → 0.05）。
        snap = make_snapshot(current_price=105.0, utc_open_price=100.0)
        assert snap.utc_day_change_pct == pytest.approx(0.05)

    def test_negative_when_below_open(self) -> None:
        snap = make_snapshot(current_price=95.0, utc_open_price=100.0)
        assert snap.utc_day_change_pct == pytest.approx(-0.05)

    def test_zero_open_falls_back_to_zero(self) -> None:
        snap = make_snapshot(current_price=100.0, utc_open_price=0.0)
        assert snap.utc_day_change_pct == 0.0


class TestRolling24hChange:
    def test_positive_when_above_24h_ago(self) -> None:
        snap = make_snapshot(current_price=110.0, rolling_24h_open=100.0)
        assert snap.rolling_24h_change_pct == pytest.approx(0.10)

    def test_zero_24h_open_falls_back_to_zero(self) -> None:
        snap = make_snapshot(current_price=100.0, rolling_24h_open=0.0)
        assert snap.rolling_24h_change_pct == 0.0


class TestPositionIn24hRange:
    def test_at_low_returns_zero(self) -> None:
        snap = make_snapshot(current_price=100.0, low_24h=100.0, high_24h=110.0)
        assert snap.position_in_24h_range == 0.0

    def test_at_high_returns_one(self) -> None:
        snap = make_snapshot(current_price=110.0, low_24h=100.0, high_24h=110.0)
        assert snap.position_in_24h_range == 1.0

    def test_midpoint_returns_half(self) -> None:
        snap = make_snapshot(current_price=105.0, low_24h=100.0, high_24h=110.0)
        assert snap.position_in_24h_range == pytest.approx(0.5)

    def test_high_equals_low_falls_back_to_half(self) -> None:
        # 板スカスカで高安が同じ値の銘柄（除外候補）の安全側挙動。
        snap = make_snapshot(current_price=100.0, low_24h=100.0, high_24h=100.0)
        assert snap.position_in_24h_range == 0.5


class TestOIChange1h:
    def test_positive_when_oi_grew(self) -> None:
        snap = make_snapshot(open_interest=1_100_000, open_interest_1h_ago=1_000_000)
        assert snap.oi_change_1h_pct == pytest.approx(10.0)

    def test_negative_when_oi_shrank(self) -> None:
        snap = make_snapshot(open_interest=900_000, open_interest_1h_ago=1_000_000)
        assert snap.oi_change_1h_pct == pytest.approx(-10.0)

    def test_zero_oi_1h_ago_falls_back_to_zero(self) -> None:
        snap = make_snapshot(open_interest=1_000_000, open_interest_1h_ago=0)
        assert snap.oi_change_1h_pct == 0.0


class TestImmutability:
    def test_market_snapshot_is_frozen(self) -> None:
        # CORE層DTOはfrozen（章11.1 原則1: 不変・純関数指向）。
        snap = make_snapshot()
        with pytest.raises(Exception):
            snap.current_price = 999.0  # type: ignore[misc]


class TestHelperDefaults:
    def test_defaults_pass_layer1_thresholds(self) -> None:
        # ヘルパーは「全層通過」前提なので、章4のLONG各層閾値を満たすか確認。
        snap = make_snapshot()
        assert 0.0 < snap.vwap_distance_pct < 0.5
        assert snap.utc_day_change_pct < 0.05
        assert snap.rolling_24h_change_pct < 0.10
        assert snap.position_in_24h_range < 0.85
        assert snap.momentum_5bar_pct > 0.3
        assert snap.flow_buy_sell_ratio > 1.5
        assert snap.volume_surge_ratio > 1.5
        assert snap.sentiment_score > 0.6
        assert snap.sentiment_confidence > 0.7
