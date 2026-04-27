"""core/circuit_breaker のテスト（章9.7・章11.7）。"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from src.core.circuit_breaker import (
    BreakerInput,
    BreakReason,
    check_circuit_breaker,
)


def make_breaker_input(**overrides: Any) -> BreakerInput:
    """全Layerクリア状態のデフォルト + 上書き。"""
    defaults: dict[str, Any] = {
        "daily_loss_pct": Decimal("0"),
        "weekly_loss_pct": Decimal("0"),
        "consecutive_losses": 0,
        "symbol_1min_changes_pct": (),
        "btc_5min_change_pct": Decimal("0"),
        "api_error_rate_5min": Decimal("0"),
        "position_count": 0,
        "max_position_count": 5,
        "daily_loss_limit_pct": Decimal("3.0"),
        "weekly_loss_limit_pct": Decimal("8.0"),
        "consecutive_loss_limit": 3,
        "flash_crash_threshold_pct": Decimal("5.0"),
        "btc_anomaly_threshold_pct": Decimal("3.0"),
        "api_error_rate_max": Decimal("0.30"),
        "position_overflow_multiplier": Decimal("1.5"),
    }
    return BreakerInput(**{**defaults, **overrides})


# ────────────────────────────────────────────────
# A. 各Layerが個別に発動
# ────────────────────────────────────────────────


class TestEachLayerTriggers:
    def test_no_trigger_when_all_clear(self) -> None:
        result = check_circuit_breaker(make_breaker_input())
        assert result.triggered is False
        assert result.reason is None
        assert result.affected_symbols == ()

    def test_layer1_daily_loss(self) -> None:
        result = check_circuit_breaker(make_breaker_input(daily_loss_pct=Decimal("-3.5")))
        assert result.triggered is True
        assert result.reason == BreakReason.DAILY_LOSS

    def test_layer2_weekly_loss(self) -> None:
        result = check_circuit_breaker(make_breaker_input(weekly_loss_pct=Decimal("-9.0")))
        assert result.triggered is True
        assert result.reason == BreakReason.WEEKLY_LOSS

    def test_layer3_consecutive_loss(self) -> None:
        result = check_circuit_breaker(make_breaker_input(consecutive_losses=3))
        assert result.triggered is True
        assert result.reason == BreakReason.CONSECUTIVE_LOSS

    def test_layer4_flash_crash(self) -> None:
        result = check_circuit_breaker(
            make_breaker_input(
                symbol_1min_changes_pct=(("BTC", Decimal("-6.0")),),
            )
        )
        assert result.triggered is True
        assert result.reason == BreakReason.FLASH_CRASH
        assert result.affected_symbols == ("BTC",)

    def test_layer5_btc_anomaly(self) -> None:
        result = check_circuit_breaker(make_breaker_input(btc_5min_change_pct=Decimal("-3.5")))
        assert result.triggered is True
        assert result.reason == BreakReason.BTC_ANOMALY

    def test_layer6_api_instability(self) -> None:
        result = check_circuit_breaker(
            make_breaker_input(api_error_rate_5min=Decimal("0.35"))
        )
        assert result.triggered is True
        assert result.reason == BreakReason.API_INSTABILITY

    def test_layer7_position_overflow(self) -> None:
        # max=5 × 1.5 = 7.5 → int(7); position 8 > 7 → 発動
        result = check_circuit_breaker(
            make_breaker_input(position_count=8, max_position_count=5)
        )
        assert result.triggered is True
        assert result.reason == BreakReason.POSITION_OVERFLOW


# ────────────────────────────────────────────────
# B. 境界値
# ────────────────────────────────────────────────


class TestBoundaries:
    def test_daily_loss_at_exact_threshold_triggers(self) -> None:
        # -3.0 と limit -3.0 → <= で発動
        result = check_circuit_breaker(make_breaker_input(daily_loss_pct=Decimal("-3.0")))
        assert result.triggered is True

    def test_daily_loss_just_above_threshold_no_trigger(self) -> None:
        result = check_circuit_breaker(make_breaker_input(daily_loss_pct=Decimal("-2.9")))
        assert result.triggered is False

    def test_consecutive_loss_just_below_limit(self) -> None:
        result = check_circuit_breaker(make_breaker_input(consecutive_losses=2))
        assert result.triggered is False

    def test_flash_crash_exact_threshold_triggers(self) -> None:
        # abs(change) >= 5.0
        result = check_circuit_breaker(
            make_breaker_input(symbol_1min_changes_pct=(("BTC", Decimal("5.0")),))
        )
        assert result.triggered is True

    def test_flash_crash_just_below_no_trigger(self) -> None:
        result = check_circuit_breaker(
            make_breaker_input(symbol_1min_changes_pct=(("BTC", Decimal("-4.99")),))
        )
        assert result.triggered is False

    def test_position_overflow_at_threshold_no_trigger(self) -> None:
        # max=5 × 1.5 = 7.5 → int 7; position == 7 で > にならない
        result = check_circuit_breaker(
            make_breaker_input(position_count=7, max_position_count=5)
        )
        assert result.triggered is False

    def test_position_overflow_just_above_triggers(self) -> None:
        result = check_circuit_breaker(
            make_breaker_input(position_count=8, max_position_count=5)
        )
        assert result.triggered is True


# ────────────────────────────────────────────────
# C. 優先順位（layer順）
# ────────────────────────────────────────────────


class TestLayerPriority:
    def test_layer1_takes_priority_over_layer2(self) -> None:
        # 両方該当 → Layer 1 が返る
        result = check_circuit_breaker(
            make_breaker_input(
                daily_loss_pct=Decimal("-4.0"),
                weekly_loss_pct=Decimal("-10.0"),
            )
        )
        assert result.reason == BreakReason.DAILY_LOSS

    def test_layer3_takes_priority_over_layer4(self) -> None:
        result = check_circuit_breaker(
            make_breaker_input(
                consecutive_losses=3,
                symbol_1min_changes_pct=(("BTC", Decimal("-6.0")),),
            )
        )
        assert result.reason == BreakReason.CONSECUTIVE_LOSS

    def test_multiple_flash_crashes_all_returned(self) -> None:
        result = check_circuit_breaker(
            make_breaker_input(
                symbol_1min_changes_pct=(
                    ("BTC", Decimal("-6.0")),
                    ("ETH", Decimal("7.0")),
                    ("SOL", Decimal("-1.0")),  # 閾値以下
                )
            )
        )
        assert result.affected_symbols == ("BTC", "ETH")


# ────────────────────────────────────────────────
# D. severity プロパティ
# ────────────────────────────────────────────────


class TestBreakReasonSeverity:
    def test_daily_loss_severity(self) -> None:
        assert BreakReason.DAILY_LOSS.severity == "halt_today"

    def test_weekly_loss_severity(self) -> None:
        assert BreakReason.WEEKLY_LOSS.severity == "halt_this_week"

    def test_consecutive_loss_severity(self) -> None:
        assert BreakReason.CONSECUTIVE_LOSS.severity == "halt_today"

    def test_flash_crash_severity(self) -> None:
        assert BreakReason.FLASH_CRASH.severity == "close_affected_only"

    def test_btc_anomaly_severity(self) -> None:
        assert BreakReason.BTC_ANOMALY.severity == "halt_until_manual"

    def test_api_instability_severity(self) -> None:
        assert BreakReason.API_INSTABILITY.severity == "pause_new_only"

    def test_position_overflow_severity(self) -> None:
        assert BreakReason.POSITION_OVERFLOW.severity == "halt_until_manual"
