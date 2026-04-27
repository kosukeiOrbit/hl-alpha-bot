"""4層AND判定のテスト（章11.7-11.8）。

各層を1つずつ崩して落ちることを検証 +
パラメトリックで境界値を網羅 +
プロパティベースで全域定義性を確認。
"""
from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src.core.entry_judge import judge_long_entry, judge_short_entry
from tests.core.helpers import make_short_snapshot, make_snapshot


# ────────────────────────────────────────────────
# LONG: 全層通過と各層単独失敗
# ────────────────────────────────────────────────


class TestLongAllPass:
    def test_passes_when_all_conditions_met(self) -> None:
        decision = judge_long_entry(make_snapshot())
        assert decision.should_enter is True
        assert decision.direction == "LONG"
        assert decision.rejection_reason is None
        assert all(decision.layer_results.values())


class TestLongMomentumLayer:
    def test_rejected_when_below_vwap(self) -> None:
        decision = judge_long_entry(make_snapshot(current_price=99.5, vwap=100.0))
        assert decision.should_enter is False
        assert decision.rejection_reason == "layer_momentum_failed"

    def test_rejected_when_too_far_above_vwap(self) -> None:
        decision = judge_long_entry(make_snapshot(current_price=101.0, vwap=100.0))
        assert decision.rejection_reason == "layer_momentum_failed"

    def test_rejected_when_overheated_utc_day(self) -> None:
        # 章5: utc始値+5%以上は除外
        decision = judge_long_entry(
            make_snapshot(current_price=110.5, utc_open_price=100.0, vwap=110.4)
        )
        assert decision.rejection_reason == "layer_momentum_failed"

    def test_rejected_when_overheated_24h(self) -> None:
        decision = judge_long_entry(
            make_snapshot(current_price=110.5, rolling_24h_open=100.0, vwap=110.4)
        )
        assert decision.rejection_reason == "layer_momentum_failed"

    def test_rejected_when_in_24h_top_range(self) -> None:
        # range position 0.85 以上は除外
        decision = judge_long_entry(
            make_snapshot(current_price=100.95, low_24h=96.0, high_24h=101.0, vwap=100.85)
        )
        assert decision.rejection_reason == "layer_momentum_failed"

    def test_rejected_when_low_momentum(self) -> None:
        decision = judge_long_entry(make_snapshot(momentum_5bar_pct=0.1))
        assert decision.rejection_reason == "layer_momentum_failed"


class TestLongFlowLayer:
    def test_rejected_when_low_buy_sell_ratio(self) -> None:
        decision = judge_long_entry(make_snapshot(flow_buy_sell_ratio=1.2))
        assert decision.rejection_reason == "layer_flow_failed"

    def test_rejected_when_no_large_orders(self) -> None:
        decision = judge_long_entry(make_snapshot(flow_large_order_count=0))
        assert decision.rejection_reason == "layer_flow_failed"

    def test_rejected_when_low_volume_surge(self) -> None:
        decision = judge_long_entry(make_snapshot(volume_surge_ratio=1.0))
        assert decision.rejection_reason == "layer_flow_failed"


class TestLongRegimeLayer:
    def test_rejected_when_btc_downtrend(self) -> None:
        decision = judge_long_entry(make_snapshot(btc_ema_trend="DOWNTREND"))
        assert decision.rejection_reason == "layer_regime_failed"

    def test_rejected_when_btc_atr_extreme(self) -> None:
        decision = judge_long_entry(make_snapshot(btc_atr_pct=8.0))
        assert decision.rejection_reason == "layer_regime_failed"

    def test_rejected_when_funding_overheated(self) -> None:
        decision = judge_long_entry(make_snapshot(funding_rate=0.02))
        assert decision.rejection_reason == "layer_regime_failed"

    def test_rejected_when_oi_extreme_change(self) -> None:
        # OI 1h で +20%
        decision = judge_long_entry(
            make_snapshot(open_interest=1_200_000, open_interest_1h_ago=1_000_000)
        )
        assert decision.rejection_reason == "layer_regime_failed"


class TestLongSentimentLayer:
    def test_rejected_when_low_score(self) -> None:
        decision = judge_long_entry(make_snapshot(sentiment_score=0.4))
        assert decision.rejection_reason == "layer_sentiment_failed"

    def test_rejected_when_low_confidence(self) -> None:
        decision = judge_long_entry(make_snapshot(sentiment_confidence=0.5))
        assert decision.rejection_reason == "layer_sentiment_failed"

    def test_rejected_when_has_hack(self) -> None:
        decision = judge_long_entry(make_snapshot(sentiment_flags={"has_hack": True}))
        assert decision.rejection_reason == "layer_sentiment_failed"

    def test_rejected_when_has_regulation(self) -> None:
        decision = judge_long_entry(
            make_snapshot(sentiment_flags={"has_regulation": True})
        )
        assert decision.rejection_reason == "layer_sentiment_failed"


class TestLongRejectionOrder:
    def test_first_failed_layer_is_reported(self) -> None:
        # momentum と sentiment の両方が落ちる場合、dict 順で momentum が先。
        decision = judge_long_entry(
            make_snapshot(current_price=99.5, vwap=100.0, sentiment_score=0.0)
        )
        assert decision.rejection_reason == "layer_momentum_failed"
        assert decision.layer_results["momentum"] is False
        assert decision.layer_results["sentiment"] is False

    def test_layer_results_capture_all_layers(self) -> None:
        decision = judge_long_entry(make_snapshot(sentiment_score=0.0))
        assert set(decision.layer_results.keys()) == {
            "momentum",
            "flow",
            "regime",
            "sentiment",
        }


# ────────────────────────────────────────────────
# LONG: パラメトリック境界値テスト
# ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "current_price, expected_pass",
    [
        # vwap=100.0 固定で、距離(%)を直接指定。浮動小数点誤差を避ける。
        (99.5, False),  # -0.5% (VWAP下)
        (99.99, False),  # -0.01%
        (100.0, False),  # 同値
        (100.01, True),  # +0.01%
        (100.49, True),  # +0.49% 上限ぎりぎり
        (100.5, False),  # +0.5% 上限と等しい（厳密に <）
        (101.0, False),  # 大きく逸脱
    ],
)
def test_long_vwap_distance_thresholds(current_price: float, expected_pass: bool) -> None:
    # vwap 乖離だけが判定要因になるよう周辺フィールドを緩めに上書きする。
    snap = make_snapshot(
        current_price=current_price,
        vwap=100.0,
        utc_open_price=99.0,  # day change ≒ +1〜+1.5% (< 5%)
        rolling_24h_open=99.0,
        low_24h=50.0,
        high_24h=150.0,  # position ≒ 0.5
    )
    assert judge_long_entry(snap).should_enter is expected_pass


@pytest.mark.parametrize(
    "momentum, expected_pass",
    [
        (-0.5, False),
        (0.0, False),
        (0.3, False),  # 厳密に > のため境界値は不通
        (0.31, True),
        (1.0, True),
    ],
)
def test_long_momentum_thresholds(momentum: float, expected_pass: bool) -> None:
    snap = make_snapshot(momentum_5bar_pct=momentum)
    assert judge_long_entry(snap).should_enter is expected_pass


@pytest.mark.parametrize(
    "score, expected_pass",
    [
        (-1.0, False),
        (0.0, False),
        (0.6, False),  # 境界値（厳密に >）
        (0.61, True),
        (1.0, True),
    ],
)
def test_long_sentiment_score_thresholds(score: float, expected_pass: bool) -> None:
    snap = make_snapshot(sentiment_score=score)
    assert judge_long_entry(snap).should_enter is expected_pass


# ────────────────────────────────────────────────
# SHORT
# ────────────────────────────────────────────────


class TestShortAllPass:
    def test_passes_when_all_conditions_met(self) -> None:
        decision = judge_short_entry(make_short_snapshot())
        assert decision.should_enter is True
        assert decision.direction == "SHORT"
        assert decision.rejection_reason is None


class TestShortIndividualLayers:
    def test_rejected_when_above_vwap(self) -> None:
        decision = judge_short_entry(
            make_short_snapshot(current_price=100.5, vwap=100.0)
        )
        assert decision.rejection_reason == "layer_momentum_failed"

    def test_rejected_when_low_downside_momentum(self) -> None:
        decision = judge_short_entry(make_short_snapshot(momentum_5bar_pct=-0.1))
        assert decision.rejection_reason == "layer_momentum_failed"

    def test_rejected_when_buy_dominant_flow(self) -> None:
        decision = judge_short_entry(make_short_snapshot(flow_buy_sell_ratio=1.0))
        assert decision.rejection_reason == "layer_flow_failed"

    def test_rejected_when_btc_uptrend_and_no_funding_overheating(self) -> None:
        # btc UPTREND かつ funding が低 → OR 条件が両方 False で regime 不通過。
        decision = judge_short_entry(
            make_short_snapshot(btc_ema_trend="UPTREND", funding_rate=0.005)
        )
        assert decision.rejection_reason == "layer_regime_failed"

    def test_passes_when_btc_uptrend_but_funding_overheated(self) -> None:
        # 章4: BTC上昇でも Funding > 0.03 で過熱なら SHORT 候補。
        decision = judge_short_entry(
            make_short_snapshot(btc_ema_trend="UPTREND", funding_rate=0.05)
        )
        assert decision.should_enter is True

    def test_rejected_when_oi_extreme_change(self) -> None:
        decision = judge_short_entry(
            make_short_snapshot(open_interest=1_200_000, open_interest_1h_ago=1_000_000)
        )
        assert decision.rejection_reason == "layer_regime_failed"

    def test_rejected_when_sentiment_neutral(self) -> None:
        decision = judge_short_entry(make_short_snapshot(sentiment_score=0.0))
        assert decision.rejection_reason == "layer_sentiment_failed"


# ────────────────────────────────────────────────
# プロパティベース（章11.8 パターン2）
# ────────────────────────────────────────────────


@given(
    score=st.floats(min_value=-1, max_value=1, allow_nan=False),
    confidence=st.floats(min_value=0, max_value=1, allow_nan=False),
)
def test_long_judgment_is_total_function(score: float, confidence: float) -> None:
    """sentiment が任意の値でも例外を出さず bool を返す（全域定義性）。"""
    snap = make_snapshot(sentiment_score=score, sentiment_confidence=confidence)
    decision = judge_long_entry(snap)
    assert isinstance(decision.should_enter, bool)
    assert decision.direction in (None, "LONG")


@given(
    momentum=st.floats(min_value=-5, max_value=5, allow_nan=False),
    funding=st.floats(min_value=-1, max_value=1, allow_nan=False),
)
def test_short_judgment_is_total_function(momentum: float, funding: float) -> None:
    snap = make_short_snapshot(momentum_5bar_pct=momentum, funding_rate=funding)
    decision = judge_short_entry(snap)
    assert isinstance(decision.should_enter, bool)
    assert decision.direction in (None, "SHORT")
