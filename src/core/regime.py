"""レジーム判定（章13.5）。

HL公式APIで他人の清算データを直接取得できないため、清算カスケード予測の
代替として Funding + OI 変動 + BTCトレンド/ボラから「ポジション偏り・過熱」
を検出する。章4 ④REGIME で使用される。

純関数のみ・I/O一切なし（章11.1）。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class RegimeInput:
    """レジーム判定の入力。"""

    funding_rate_8h: Decimal  # 8時間相当の Funding（HL APIの値）
    open_interest: Decimal  # 現在のOI
    open_interest_1h_ago: Decimal  # 1時間前のOI
    btc_ema_short: Decimal  # BTC EMA(20)
    btc_ema_long: Decimal  # BTC EMA(50)
    btc_atr_pct: Decimal  # BTC ATR%

    # 設定値（章23踏襲）
    funding_max_long: Decimal  # 例: 0.0001 (0.01%)
    funding_min_short: Decimal  # 例: 0.0003 (0.03%)
    oi_change_max_pct: Decimal  # 例: 10.0
    btc_atr_max_pct: Decimal  # 例: 5.0


def judge_regime_long(input: RegimeInput) -> tuple[bool, str | None]:
    """LONGエントリーのレジーム判定（純関数）。

    清算予測の代替: Funding + OI 変動 + BTC トレンドで「過熱」を検出。

    Returns:
        (合格, 拒否理由 or None)
    """
    if input.btc_ema_short <= input.btc_ema_long:
        return False, "btc_downtrend"

    if input.btc_atr_pct > input.btc_atr_max_pct:
        return False, "btc_volatility_extreme"

    # Funding が買い側に偏った（>= 閾値）→ 過熱
    if input.funding_rate_8h >= input.funding_max_long:
        return False, "funding_overheated"

    oi_change_pct = _calc_oi_change_pct(input.open_interest, input.open_interest_1h_ago)
    if oi_change_pct is None:
        return False, "oi_unavailable"
    if abs(oi_change_pct) > input.oi_change_max_pct:
        return False, "oi_extreme_change"

    return True, None


def judge_regime_short(input: RegimeInput) -> tuple[bool, str | None]:
    """SHORTエントリーのレジーム判定（純関数）。

    BTC上昇でも Funding が極端に高ければ「買い過熱からの反転」狙いで SHORT 許可。
    """
    # 上昇トレンドでも、Funding が過熱閾値以上なら SHORT 許可
    if input.btc_ema_short > input.btc_ema_long and input.funding_rate_8h < input.funding_min_short:
        return False, "btc_uptrend_no_overheat"

    if input.btc_atr_pct > input.btc_atr_max_pct:
        return False, "btc_volatility_extreme"

    oi_change_pct = _calc_oi_change_pct(input.open_interest, input.open_interest_1h_ago)
    if oi_change_pct is None:
        return False, "oi_unavailable"
    if abs(oi_change_pct) > input.oi_change_max_pct:
        return False, "oi_extreme_change"

    return True, None


def _calc_oi_change_pct(current: Decimal, past: Decimal) -> Decimal | None:
    """OI変化率を計算（純関数）。past <= 0 なら計算不可で None。"""
    if past <= Decimal("0"):
        return None
    return (current - past) / past * Decimal("100")
