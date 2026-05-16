"""設定ファイルの pydantic スキーマ（章23.6）。

settings.yaml + profile_*.yaml をマージしたものを AppSettings として
バリデーションする。フィールド名は src/application/*.py の Config
dataclass 実体と一致させる（spec doc の名前と乖離していたため
実体に合わせた）。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ExchangeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    network: Literal["mainnet", "testnet"] = "testnet"


class TradingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    is_dry_run: bool = True
    leverage: int = Field(ge=1, le=10, default=3)
    flow_layer_enabled: bool = False
    position_size_pct: Decimal = Field(
        ge=Decimal("0"), le=Decimal("0.5"), default=Decimal("0.05")
    )
    sl_atr_mult: Decimal = Field(default=Decimal("1.5"))
    tp_atr_mult: Decimal = Field(default=Decimal("3.0"))


class WatchlistSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fixed: tuple[str, ...] = ("BTC", "ETH")
    directions: tuple[Literal["LONG", "SHORT"], ...] = ("LONG",)


class SentimentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # PR7.5e-1: どの SentimentProvider 実装を使うか
    # - "fixed":         FixedSentimentProvider（fixed_* で値を固定）
    # - "funding_rate":  FundingRateSentimentProvider（HL Funding ベース）
    provider: Literal["fixed", "funding_rate"] = "fixed"

    # FixedSentimentProvider 用
    fixed_score: Decimal = Field(
        ge=Decimal("-1"), le=Decimal("1"), default=Decimal("0")
    )
    fixed_confidence: Decimal = Field(
        ge=Decimal("0"), le=Decimal("1"), default=Decimal("0.5")
    )
    reasoning: str = "Phase 0 fixed value"

    # FundingRateSentimentProvider 用
    funding_scale_factor: Decimal = Field(
        gt=Decimal("0"), default=Decimal("10000")
    )
    funding_cache_window_seconds: int = Field(gt=0, default=300)
    funding_confidence: Decimal = Field(
        ge=Decimal("0"), le=Decimal("1"), default=Decimal("0.8")
    )


class StorageSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    db_path: str = "data/hl_bot.db"


class SchedulerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cycle_interval_seconds: float = Field(gt=0, default=10.0)
    reconcile_interval_seconds: float = Field(gt=0, default=300.0)
    circuit_breaker_enabled: bool = True
    max_position_count: int = Field(ge=1, default=5)
    daily_loss_limit_pct: Decimal = Field(default=Decimal("3.0"))
    weekly_loss_limit_pct: Decimal = Field(default=Decimal("8.0"))
    consecutive_loss_limit: int = Field(ge=1, default=3)
    flash_crash_threshold_pct: Decimal = Field(default=Decimal("5.0"))
    btc_anomaly_threshold_pct: Decimal = Field(default=Decimal("3.0"))
    api_error_rate_max: Decimal = Field(
        ge=Decimal("0"), le=Decimal("1"), default=Decimal("0.30")
    )
    position_overflow_multiplier: Decimal = Field(default=Decimal("1.5"))


class PositionMonitorSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    funding_close_enabled: bool = True
    funding_close_minutes_before: int = Field(ge=0, default=5)
    fills_lookback_seconds: int = Field(ge=1, default=300)
    force_close_slippage_tolerance_pct: Decimal = Field(
        default=Decimal("0.005")
    )


class ReconciliationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fills_lookback_hours: int = Field(ge=1, default=24)
    stale_order_cleanup_seconds: int = Field(ge=0, default=30)


class EntryFlowSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    oi_lookup_tolerance_minutes: int = Field(ge=0, default=5)


class MomentumSettings(BaseModel):
    """MOMENTUM 層の閾値（PR C1）。

    profile 未指定時のデフォルト ±0.5% は PR C1 以前のハードコード値。
    Phase 2 では profile_phase2.yaml で ±1.0% に緩和して mean-reversion
    band を広げる（強いトレンドの本体に入りにくい構造はそのまま）。
    """

    model_config = ConfigDict(extra="forbid")
    # SHORT: vwap_min_distance_pct < dist < 0
    vwap_min_distance_pct: Decimal = Field(default=Decimal("-0.5"))
    # LONG: 0 < dist < vwap_max_distance_pct
    vwap_max_distance_pct: Decimal = Field(default=Decimal("0.5"))


class RegimeSettings(BaseModel):
    """REGIME 層の挙動（PR D2）。

    ``trend_source``:
        - "btc": EMA トレンドと ATR を BTC レジームから採用（従来通り）。
          BTC と逆方向に動く局面でも EMA は BTC を見る前提。
        - "symbol": 銘柄自身の 15m EMA20/50 と ATR(14) を採用。

    どちらを採用しても entry_flow は両モードの判定結果を signals テーブル
    （snapshot_excerpt）に並べて記録するため、事後 SQL で「IF mode=X
    だったら何 cycle 通過したか」を再計算できる（PR C1 と同じ哲学）。

    Phase 4 で BTC.D / combine_mode（btc AND symbol 等）に拡張する余地
    があるが、PR D2 ではまず単純な切替のみ実装する。
    """

    model_config = ConfigDict(extra="forbid")
    trend_source: Literal["btc", "symbol"] = "btc"


class LoggingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_file: str = "logs/bot.log"
    rotation_when: str = "midnight"
    rotation_backup_count: int = Field(ge=0, default=30)


class AppSettings(BaseModel):
    """全設定のルート。"""

    model_config = ConfigDict(extra="forbid")

    phase: Literal[
        "phase_0", "phase_1", "phase_2", "phase_3", "phase_4"
    ] = "phase_0"
    exchange: ExchangeSettings = Field(default_factory=ExchangeSettings)
    trading: TradingSettings = Field(default_factory=TradingSettings)
    watchlist: WatchlistSettings = Field(default_factory=WatchlistSettings)
    sentiment: SentimentSettings = Field(default_factory=SentimentSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
    position_monitor: PositionMonitorSettings = Field(
        default_factory=PositionMonitorSettings
    )
    reconciliation: ReconciliationSettings = Field(
        default_factory=ReconciliationSettings
    )
    entry_flow: EntryFlowSettings = Field(default_factory=EntryFlowSettings)
    momentum: MomentumSettings = Field(default_factory=MomentumSettings)
    regime: RegimeSettings = Field(default_factory=RegimeSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
