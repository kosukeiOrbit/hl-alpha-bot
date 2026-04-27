"""サーキットブレーカー（章9.7）。

7段階多層防御の判定ロジックを純関数で提供する。
状態（直近損失・連敗・APIエラー率等）を引数で受け取り、
発動レベルと理由を返す。

副作用なし・I/O なし（章11.1）。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class BreakReason(StrEnum):
    """サーキットブレーカー発動理由（章9.7 の7段階）。"""

    DAILY_LOSS = "DAILY_LOSS"  # Layer 1: 日次-3%
    WEEKLY_LOSS = "WEEKLY_LOSS"  # Layer 2: 週次-8%
    CONSECUTIVE_LOSS = "CONSECUTIVE_LOSS"  # Layer 3: 連敗3回
    FLASH_CRASH = "FLASH_CRASH"  # Layer 4: 1分5%変動
    BTC_ANOMALY = "BTC_ANOMALY"  # Layer 5: BTC 5分3%変動
    API_INSTABILITY = "API_INSTABILITY"  # Layer 6: APIエラー率30%超
    POSITION_OVERFLOW = "POSITION_OVERFLOW"  # Layer 7: ポジション数上限超

    @property
    def severity(self) -> str:
        """発動時の対応レベル（章9.7 の発動時挙動表）。"""
        mapping: dict[BreakReason, str] = {
            BreakReason.DAILY_LOSS: "halt_today",
            BreakReason.WEEKLY_LOSS: "halt_this_week",
            BreakReason.CONSECUTIVE_LOSS: "halt_today",
            BreakReason.FLASH_CRASH: "close_affected_only",
            BreakReason.BTC_ANOMALY: "halt_until_manual",
            BreakReason.API_INSTABILITY: "pause_new_only",
            BreakReason.POSITION_OVERFLOW: "halt_until_manual",
        }
        return mapping[self]


@dataclass(frozen=True)
class BreakerInput:
    """サーキットブレーカー判定の入力（不変）。

    全フィールドは判定時のスナップショット値。
    """

    # Layer 1-3: 損失系
    daily_loss_pct: Decimal  # 例: -2.5 = -2.5%
    weekly_loss_pct: Decimal
    consecutive_losses: int

    # Layer 4: 銘柄ごとの1分価格変動率（リスト）
    symbol_1min_changes_pct: tuple[tuple[str, Decimal], ...]

    # Layer 5: BTC の5分変動率
    btc_5min_change_pct: Decimal

    # Layer 6: APIエラー率（直近5分・0.0〜1.0）
    api_error_rate_5min: Decimal

    # Layer 7: ポジション数
    position_count: int
    max_position_count: int

    # 設定値（章23踏襲）
    daily_loss_limit_pct: Decimal  # 例: 3.0
    weekly_loss_limit_pct: Decimal  # 例: 8.0
    consecutive_loss_limit: int  # 例: 3
    flash_crash_threshold_pct: Decimal  # 例: 5.0
    btc_anomaly_threshold_pct: Decimal  # 例: 3.0
    api_error_rate_max: Decimal  # 例: 0.30
    position_overflow_multiplier: Decimal  # 例: 1.5


@dataclass(frozen=True)
class BreakerResult:
    """判定結果。"""

    triggered: bool
    reason: BreakReason | None = None
    affected_symbols: tuple[str, ...] = ()  # FLASH_CRASH 時の対象銘柄


def check_circuit_breaker(input: BreakerInput) -> BreakerResult:
    """7段階のサーキットブレーカー判定（純関数・章9.7）。

    各 Layer を順番にチェックし、最初に該当したものを返す。
    重要度の高い順（Layer 1 → 7）でチェック。
    """
    # Layer 1: 日次損失
    if input.daily_loss_pct <= -input.daily_loss_limit_pct:
        return BreakerResult(triggered=True, reason=BreakReason.DAILY_LOSS)

    # Layer 2: 週次損失
    if input.weekly_loss_pct <= -input.weekly_loss_limit_pct:
        return BreakerResult(triggered=True, reason=BreakReason.WEEKLY_LOSS)

    # Layer 3: 連敗
    if input.consecutive_losses >= input.consecutive_loss_limit:
        return BreakerResult(triggered=True, reason=BreakReason.CONSECUTIVE_LOSS)

    # Layer 4: フラッシュクラッシュ（銘柄個別・複数まとめて返す）
    crashed = tuple(
        symbol
        for symbol, change in input.symbol_1min_changes_pct
        if abs(change) >= input.flash_crash_threshold_pct
    )
    if crashed:
        return BreakerResult(
            triggered=True,
            reason=BreakReason.FLASH_CRASH,
            affected_symbols=crashed,
        )

    # Layer 5: BTC異常変動
    if abs(input.btc_5min_change_pct) >= input.btc_anomaly_threshold_pct:
        return BreakerResult(triggered=True, reason=BreakReason.BTC_ANOMALY)

    # Layer 6: API不安定
    if input.api_error_rate_5min >= input.api_error_rate_max:
        return BreakerResult(triggered=True, reason=BreakReason.API_INSTABILITY)

    # Layer 7: ポジション数オーバーフロー
    overflow_threshold = int(input.max_position_count * input.position_overflow_multiplier)
    if input.position_count > overflow_threshold:
        return BreakerResult(triggered=True, reason=BreakReason.POSITION_OVERFLOW)

    return BreakerResult(triggered=False)
