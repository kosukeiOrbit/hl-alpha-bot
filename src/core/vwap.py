"""VWAP計算と保有中追跡（章6）。

- calculate_vwap_from_volume: HL APIの volume データから当日VWAPを算出
- VWAPState: 保有中の追跡状態（不変データクラス）
- update_vwap_state: 純関数で新状態を返す
- vwap_state_to_record: DB保存用の dict 形式へ変換

純関数のみ・I/O一切なし（章11.1）。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any


def calculate_vwap_from_volume(
    day_volume_usd: Decimal,
    day_volume_base: Decimal,
    fallback_price: Decimal,
) -> Decimal:
    """当日VWAP = 累積取引代金 / 累積取引数量（章6.2・純関数）。

    HyperLiquid API の dayNtlVlm / dayBaseVlm から計算する。
    出来高ゼロや異常値（負）の場合は fallback_price を返す。
    """
    if day_volume_base <= 0:
        return fallback_price
    return day_volume_usd / day_volume_base


@dataclass(frozen=True)
class VWAPState:
    """保有中VWAP挙動の状態（不変・章6.4）。

    cross_count       : 保有中VWAPを跨いだ回数
    above_seconds     : VWAP上に滞在した累積秒数
    below_seconds     : VWAP下に滞在した累積秒数
    min_distance_pct  : 保有中の最小VWAP乖離(%)（最接近・LONGなら割れに近い）
    max_distance_pct  : 保有中の最大VWAP乖離(%)
    last_above        : 直前の状態（True=上, False=下, None=未observed）
    """

    cross_count: int = 0
    above_seconds: int = 0
    below_seconds: int = 0
    min_distance_pct: float = float("inf")
    max_distance_pct: float = float("-inf")
    last_above: bool | None = None


def update_vwap_state(
    state: VWAPState,
    current_price: float,
    vwap: float,
    elapsed_sec: int,
) -> VWAPState:
    """純関数：新しいVWAPStateを返す（元のstateは変更しない・章6.4）。

    クロス検出は last_above が None でない場合のみ判定する
    （初回updateではクロス判定しない）。
    is_above は厳密に current_price > vwap で判定するため、
    完全一致は below 扱いになる（実用上ほぼ起きない）。
    """
    distance_pct = (current_price - vwap) / vwap * 100
    is_above = current_price > vwap

    new_cross_count = state.cross_count
    if state.last_above is not None and is_above != state.last_above:
        new_cross_count += 1

    return replace(
        state,
        cross_count=new_cross_count,
        above_seconds=state.above_seconds + (elapsed_sec if is_above else 0),
        below_seconds=state.below_seconds + (0 if is_above else elapsed_sec),
        min_distance_pct=min(state.min_distance_pct, distance_pct),
        max_distance_pct=max(state.max_distance_pct, distance_pct),
        last_above=is_above,
    )


def vwap_state_to_record(state: VWAPState) -> dict[str, Any]:
    """trades テーブル保存用のレコード形式に変換（章6.4・純関数）。

    inf/-inf は None に変換して JSON シリアライズを安全にする。
    held_above_pct は 0.0〜1.0 の比率（観測ゼロなら 0）。
    """
    total = state.above_seconds + state.below_seconds
    return {
        "vwap_cross_count": state.cross_count,
        "vwap_held_above_pct": state.above_seconds / total if total > 0 else 0,
        "min_vwap_distance_pct": (
            state.min_distance_pct
            if state.min_distance_pct != float("inf")
            else None
        ),
        "max_vwap_distance_pct": (
            state.max_distance_pct
            if state.max_distance_pct != float("-inf")
            else None
        ),
    }
