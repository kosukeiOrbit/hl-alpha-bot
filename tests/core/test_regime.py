"""core/regime のテスト（章13.5・章11.7）。"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from src.core.regime import RegimeInput, judge_regime_long, judge_regime_short


def make_regime_input(**overrides: Any) -> RegimeInput:
    """LONG が通る安全側のデフォルト + 上書き。"""
    defaults: dict[str, Any] = {
        "funding_rate_8h": Decimal("0.00005"),
        "open_interest": Decimal("1000000"),
        "open_interest_1h_ago": Decimal("1000000"),
        "btc_ema_short": Decimal("65000"),
        "btc_ema_long": Decimal("64000"),
        "btc_atr_pct": Decimal("2.0"),
        "funding_max_long": Decimal("0.0001"),
        "funding_min_short": Decimal("0.0003"),
        "oi_change_max_pct": Decimal("10.0"),
        "btc_atr_max_pct": Decimal("5.0"),
    }
    return RegimeInput(**{**defaults, **overrides})


# ────────────────────────────────────────────────
# A. LONG 基本
# ────────────────────────────────────────────────


class TestRegimeLong:
    def test_passes_when_uptrend_calm_low_funding(self) -> None:
        ok, reason = judge_regime_long(make_regime_input())
        assert ok is True
        assert reason is None

    def test_rejects_btc_downtrend(self) -> None:
        ok, reason = judge_regime_long(
            make_regime_input(
                btc_ema_short=Decimal("63000"),
                btc_ema_long=Decimal("64000"),
            )
        )
        assert ok is False
        assert reason == "btc_downtrend"

    def test_rejects_btc_ema_equal(self) -> None:
        # short == long も上昇トレンドではないので拒否。
        ok, reason = judge_regime_long(
            make_regime_input(
                btc_ema_short=Decimal("64000"),
                btc_ema_long=Decimal("64000"),
            )
        )
        assert ok is False
        assert reason == "btc_downtrend"

    def test_rejects_high_volatility(self) -> None:
        ok, reason = judge_regime_long(make_regime_input(btc_atr_pct=Decimal("6.0")))
        assert ok is False
        assert reason == "btc_volatility_extreme"

    def test_rejects_funding_overheated(self) -> None:
        # >= funding_max_long で過熱判定
        ok, reason = judge_regime_long(
            make_regime_input(funding_rate_8h=Decimal("0.0002"))
        )
        assert ok is False
        assert reason == "funding_overheated"

    def test_rejects_funding_at_threshold(self) -> None:
        # 境界: funding == funding_max_long → >= で拒否
        ok, reason = judge_regime_long(
            make_regime_input(funding_rate_8h=Decimal("0.0001"))
        )
        assert ok is False
        assert reason == "funding_overheated"

    def test_rejects_oi_extreme_change(self) -> None:
        ok, reason = judge_regime_long(
            make_regime_input(
                open_interest_1h_ago=Decimal("900000"),
                open_interest=Decimal("1100000"),  # +22%
            )
        )
        assert ok is False
        assert reason == "oi_extreme_change"


# ────────────────────────────────────────────────
# B. SHORT 特殊ケース
# ────────────────────────────────────────────────


class TestRegimeShort:
    def test_short_passes_in_downtrend(self) -> None:
        ok, reason = judge_regime_short(
            make_regime_input(
                btc_ema_short=Decimal("63000"),
                btc_ema_long=Decimal("64000"),
            )
        )
        assert ok is True
        assert reason is None

    def test_short_rejects_uptrend_without_overheat(self) -> None:
        # btc 上昇 + funding 普通 → SHORT 拒否
        ok, reason = judge_regime_short(
            make_regime_input(
                btc_ema_short=Decimal("65000"),
                btc_ema_long=Decimal("64000"),
                funding_rate_8h=Decimal("0.0001"),
            )
        )
        assert ok is False
        assert reason == "btc_uptrend_no_overheat"

    def test_short_allows_uptrend_with_overheated_funding(self) -> None:
        # 章13.5: Funding 過熱なら上昇トレンドでも SHORT 許可
        ok, _ = judge_regime_short(
            make_regime_input(
                btc_ema_short=Decimal("65000"),
                btc_ema_long=Decimal("64000"),
                funding_rate_8h=Decimal("0.0005"),  # > funding_min_short
            )
        )
        assert ok is True

    def test_short_allows_uptrend_funding_at_threshold(self) -> None:
        # 境界: funding == funding_min_short → >= で SHORT 許可
        ok, _ = judge_regime_short(
            make_regime_input(
                btc_ema_short=Decimal("65000"),
                btc_ema_long=Decimal("64000"),
                funding_rate_8h=Decimal("0.0003"),
            )
        )
        assert ok is True

    def test_short_rejects_high_volatility(self) -> None:
        ok, reason = judge_regime_short(
            make_regime_input(
                btc_ema_short=Decimal("63000"),
                btc_ema_long=Decimal("64000"),
                btc_atr_pct=Decimal("6.0"),
            )
        )
        assert ok is False
        assert reason == "btc_volatility_extreme"

    def test_short_rejects_oi_extreme_change(self) -> None:
        ok, reason = judge_regime_short(
            make_regime_input(
                btc_ema_short=Decimal("63000"),
                btc_ema_long=Decimal("64000"),
                open_interest_1h_ago=Decimal("1000000"),
                open_interest=Decimal("850000"),  # -15%
            )
        )
        assert ok is False
        assert reason == "oi_extreme_change"

    def test_short_rejects_oi_unavailable(self) -> None:
        ok, reason = judge_regime_short(
            make_regime_input(
                btc_ema_short=Decimal("63000"),
                btc_ema_long=Decimal("64000"),
                open_interest_1h_ago=Decimal("0"),
            )
        )
        assert ok is False
        assert reason == "oi_unavailable"


# ────────────────────────────────────────────────
# C. OI 変化率の境界
# ────────────────────────────────────────────────


class TestOIChange:
    def test_oi_unavailable_when_past_zero(self) -> None:
        ok, reason = judge_regime_long(
            make_regime_input(open_interest_1h_ago=Decimal("0"))
        )
        assert ok is False
        assert reason == "oi_unavailable"

    def test_oi_unavailable_when_past_negative(self) -> None:
        # 入力値の防御。past < 0 でも fallback。
        ok, reason = judge_regime_long(
            make_regime_input(open_interest_1h_ago=Decimal("-1"))
        )
        assert ok is False
        assert reason == "oi_unavailable"

    def test_oi_change_just_above_threshold_rejected(self) -> None:
        # +10.0001% → > なので拒否
        ok, reason = judge_regime_long(
            make_regime_input(
                open_interest_1h_ago=Decimal("1000000"),
                open_interest=Decimal("1100001"),
            )
        )
        assert ok is False
        assert reason == "oi_extreme_change"

    def test_oi_change_at_threshold_passes(self) -> None:
        # 境界: ちょうど10% → > では拒否されない（abs > なので >ぴったり は通る）
        ok, _ = judge_regime_long(
            make_regime_input(
                open_interest_1h_ago=Decimal("1000000"),
                open_interest=Decimal("1100000"),  # +10.000%
            )
        )
        assert ok is True

    def test_oi_negative_change_also_rejected(self) -> None:
        ok, reason = judge_regime_long(
            make_regime_input(
                open_interest_1h_ago=Decimal("1000000"),
                open_interest=Decimal("850000"),  # -15%
            )
        )
        assert ok is False
        assert reason == "oi_extreme_change"
