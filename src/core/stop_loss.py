"""SL/TP価格計算（章13.3）。

ATR ベースで損切り・利確価格を計算する純関数。
HL API の tick_size に合わせて丸め、最低 1tick 差を保証する
（auto-daytrade 4/17 の既知の罠回避）。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal


@dataclass(frozen=True)
class StopLossInput:
    """SL/TP 計算の入力。"""

    direction: str  # 'LONG' or 'SHORT'
    entry_price: Decimal
    atr_value: Decimal  # ATR(1h, 14)
    sl_multiplier: Decimal  # 例: 1.5
    tp_multiplier: Decimal  # 例: 2.5
    tick_size: Decimal  # 銘柄ごとに異なる


@dataclass(frozen=True)
class StopLossResult:
    """SL/TP 価格。"""

    sl_price: Decimal
    tp_price: Decimal


def calculate_sl_tp(input: StopLossInput) -> StopLossResult:
    """ATR ベースの SL/TP 計算（純関数・章13.3）。

    1. SL/TP 距離 = ATR × multiplier
    2. LONG: SL = entry - sl_distance / TP = entry + tp_distance
       SHORT は逆向き
    3. tick_size で丸め
    4. 最低 1tick 差を保証（ATR 極小時の防御）
    """
    if input.direction not in ("LONG", "SHORT"):
        raise ValueError(f"direction must be LONG or SHORT, got {input.direction}")

    sl_distance = input.atr_value * input.sl_multiplier
    tp_distance = input.atr_value * input.tp_multiplier

    if input.direction == "LONG":
        sl_raw = input.entry_price - sl_distance
        tp_raw = input.entry_price + tp_distance
    else:  # SHORT
        sl_raw = input.entry_price + sl_distance
        tp_raw = input.entry_price - tp_distance

    sl_price = _round_to_tick(sl_raw, input.tick_size)
    tp_price = _round_to_tick(tp_raw, input.tick_size)

    # 最低 1tick 差を保証
    min_diff = input.tick_size
    if input.direction == "LONG":
        sl_price = min(sl_price, input.entry_price - min_diff)
        tp_price = max(tp_price, input.entry_price + min_diff)
    else:  # SHORT
        sl_price = max(sl_price, input.entry_price + min_diff)
        tp_price = min(tp_price, input.entry_price - min_diff)

    return StopLossResult(sl_price=sl_price, tp_price=tp_price)


def _round_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    """tick_size の倍数に丸める（純関数）。"""
    return (price / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick
