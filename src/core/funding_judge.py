"""Funding Rate 連動の手仕舞い判定（章13.4）。

HyperLiquid は 1 時間ごとに Funding 精算（章22.6）。
精算前に支払い側で含み益が薄い場合は手仕舞いする。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class FundingExitInput:
    """Funding 手仕舞い判定の入力。"""

    direction: str  # 'LONG' or 'SHORT'
    funding_rate: Decimal  # 8h相当値（HL API の値）
    unrealized_pnl_pct: Decimal  # 含み益率(%)・例: 0.5 = +0.5%
    minutes_to_funding: int  # 次回精算までの分
    threshold_minutes: int  # config.risk.funding_exit_minutes_before


def should_exit_before_funding(input: FundingExitInput) -> bool:
    """Funding 精算前の手仕舞い判定（純関数）。

    判定ロジック:
    1. 精算まで threshold_minutes 超 → 維持（False）
    2. ポジション方向と funding 符号が「受取側」 → 維持（False）
    3. 支払い側で含み益が薄い（< +0.5%） → 手仕舞い（True）
    4. それ以外（厚い含み益で粘る） → 維持（False）
    """
    if input.direction not in ("LONG", "SHORT"):
        raise ValueError(f"direction must be LONG or SHORT, got {input.direction}")

    if input.minutes_to_funding > input.threshold_minutes:
        return False

    # LONG + funding > 0 → LONG が支払い
    # SHORT + funding < 0 → SHORT が支払い
    is_paying_funding = (
        (input.direction == "LONG" and input.funding_rate > 0)
        or (input.direction == "SHORT" and input.funding_rate < 0)
    )
    if not is_paying_funding:
        return False

    return input.unrealized_pnl_pct < Decimal("0.5")
