"""価格基準・過熱フィルター（章5）。

3重チェック方式:
- 基準A: UTC 00:00 からの変化率（utc_day_change_pct）
- 基準B: 24時間前からの変化率（rolling_24h_change_pct・HL prevDayPx）
- 基準C: 24時間レンジ内の現在位置（position_in_24h_range）

純関数で I/O 一切なし（章11.1）。
閾値は将来 config から注入予定（章23）。現状はハードコード。
"""
from __future__ import annotations

from src.core.models import MarketSnapshot


def is_not_overheated_long(snap: MarketSnapshot) -> bool:
    """LONG過熱フィルター：3重チェック全クリアで True（章5.4）。

    - utc_day_change_pct < 0.05 (UTC始値+5%以内)
    - rolling_24h_change_pct < 0.10 (24h前から+10%以内)
    - position_in_24h_range < 0.85 (24h高値圏85%以下)
    """
    return (
        snap.utc_day_change_pct < 0.05
        and snap.rolling_24h_change_pct < 0.10
        and snap.position_in_24h_range < 0.85
    )


def is_not_overheated_short(snap: MarketSnapshot) -> bool:
    """SHORT過熱フィルター：下落の追随を回避（章5.4）。"""
    return (
        snap.utc_day_change_pct > -0.05
        and snap.rolling_24h_change_pct > -0.10
        and snap.position_in_24h_range > 0.15
    )
