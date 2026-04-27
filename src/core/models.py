"""CORE層のドメインモデル（不変・純粋）。

設計書 11.4 の MarketSnapshot / EntryDecision を中心に、
判定に必要な情報を1つのDTOに集約する。
ここに I/O や副作用は一切持たせない（章11.1 原則1）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class MarketSnapshot:
    """エントリー判定に必要な全データを束ねたDTO（章11.4）。

    このスナップショットさえあれば judge_long_entry / judge_short_entry が
    動く。データ収集（HL API・Claude API）は APPLICATION 層が担当し、
    ここには既に集まった値だけを渡す。

    価格は float で扱う（判定は比率と閾値の比較が中心のため）。
    実発注時のサイズ・価格は Decimal で扱う（章13.2 SizingInput 参照）。
    """

    symbol: str
    current_price: float

    # VWAP（章6: 当日VWAP・UTC 00:00からの累積）
    vwap: float

    # モメンタム（章4 ① MOMENTUM・5本前比%）
    momentum_5bar_pct: float

    # 価格基準3点（章5: PriceContext）
    utc_open_price: float
    rolling_24h_open: float
    high_24h: float
    low_24h: float

    # フロー（章4 ② FLOW）
    flow_buy_sell_ratio: float
    flow_large_order_count: int
    volume_surge_ratio: float

    # センチメント（章4 ③・章7）
    sentiment_score: float
    sentiment_confidence: float
    sentiment_flags: Mapping[str, bool] = field(default_factory=dict)

    # レジーム（章4 ④）
    btc_ema_trend: str = "UPTREND"  # 'UPTREND' / 'DOWNTREND' / 'CHOP'
    btc_atr_pct: float = 0.0
    funding_rate: float = 0.0  # 8h相当

    # OI（章13.5 レジーム判定の代替）
    open_interest: float = 0.0
    open_interest_1h_ago: float = 0.0

    # ─────────────────────────────────────────────
    # 派生プロパティ（章5 PriceContext 由来）
    # ─────────────────────────────────────────────

    @property
    def vwap_distance_pct(self) -> float:
        """VWAPからの乖離率(%)。LONGは正・SHORTは負を期待。"""
        if self.vwap == 0:
            return 0.0
        return (self.current_price - self.vwap) / self.vwap * 100

    @property
    def utc_day_change_pct(self) -> float:
        """UTC始値からの変化率（章5 基準A・小数 0.05 = +5%）。"""
        if self.utc_open_price == 0:
            return 0.0
        return (self.current_price - self.utc_open_price) / self.utc_open_price

    @property
    def rolling_24h_change_pct(self) -> float:
        """24h前からの変化率（章5 基準B・小数）。"""
        if self.rolling_24h_open == 0:
            return 0.0
        return (self.current_price - self.rolling_24h_open) / self.rolling_24h_open

    @property
    def position_in_24h_range(self) -> float:
        """24h高安レンジ内の位置（章5 基準C・0.0=安値, 1.0=高値）。"""
        if self.high_24h == self.low_24h:
            return 0.5
        return (self.current_price - self.low_24h) / (self.high_24h - self.low_24h)

    @property
    def oi_change_1h_pct(self) -> float:
        """1時間前からのOI変化率(%)・章13.5 レジーム判定用。"""
        if self.open_interest_1h_ago == 0:
            return 0.0
        return (
            (self.open_interest - self.open_interest_1h_ago)
            / self.open_interest_1h_ago
            * 100
        )


@dataclass(frozen=True)
class EntryDecision:
    """4層AND判定の結果（章11.4）。

    should_enter=True かつ direction が 'LONG' or 'SHORT' なら発注、
    False のときは rejection_reason に最初に落ちた層の理由が入る。
    層ごとの結果は layer_results に boolean で残る（後でログ・分析に使う）。
    """

    should_enter: bool
    direction: str | None  # 'LONG' / 'SHORT' / None
    rejection_reason: str | None
    layer_results: Mapping[str, bool]
