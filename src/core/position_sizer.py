"""ポジションサイジング（章13.2）。

口座残高・レバレッジ・連敗状況からポジションサイズを計算する純関数群。
すべて Decimal で扱う（HL の szDecimals 精度に従って丸めるため）。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal


@dataclass(frozen=True)
class SizingInput:
    """ポジションサイズ計算の入力（不変）。"""

    account_balance_usd: Decimal
    entry_price: Decimal
    sl_price: Decimal
    leverage: int
    position_size_pct: Decimal  # 0.05 = 口座の5%
    sz_decimals: int  # HL銘柄の szDecimals（章22.5）
    consecutive_losses: int = 0


@dataclass(frozen=True)
class SizingResult:
    """ポジションサイズ計算の結果。"""

    size_coins: Decimal
    notional_usd: Decimal
    risk_per_unit: Decimal  # 1単位あたりのリスク（entry - SL の絶対値）
    risk_total_usd: Decimal  # 総リスク額（risk_per_unit × size）
    rejected_reason: str | None = None


def calculate_position_size(input: SizingInput) -> SizingResult:
    """ポジションサイズ計算（純関数・章13.2）。

    1. 連敗ペナルティ: 3連敗以上でサイズ半減（章10.7）
    2. notional = 口座 × position_size_pct × leverage × 連敗倍率
    3. raw_size = notional / entry_price
    4. szDecimals で切り捨て（オーバーポジション回避）
    5. 切り捨てで 0 になった場合は rejected
    """
    size_multiplier = (
        Decimal("0.5") if input.consecutive_losses >= 3 else Decimal("1.0")
    )

    notional = (
        input.account_balance_usd
        * input.position_size_pct
        * Decimal(input.leverage)
        * size_multiplier
    )
    raw_size = notional / input.entry_price

    quantizer = Decimal("1") / (Decimal("10") ** input.sz_decimals)
    size_coins = raw_size.quantize(quantizer, rounding=ROUND_DOWN)

    if size_coins <= 0:
        return SizingResult(
            size_coins=Decimal("0"),
            notional_usd=Decimal("0"),
            risk_per_unit=Decimal("0"),
            risk_total_usd=Decimal("0"),
            rejected_reason="size_too_small_after_rounding",
        )

    risk_per_unit = abs(input.entry_price - input.sl_price)
    risk_total = risk_per_unit * size_coins

    return SizingResult(
        size_coins=size_coins,
        notional_usd=size_coins * input.entry_price,
        risk_per_unit=risk_per_unit,
        risk_total_usd=risk_total,
        rejected_reason=None,
    )
