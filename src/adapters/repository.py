"""Repository Protocol（章8）。

データロギング・状態保存のインターフェース。
INFRASTRUCTURE層では SQLite 実装を入れる（章8.4）。
CORE層・APPLICATION層から具象DB依存を排除する。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Protocol

from src.core.models import EntryDecision

# ───────────────────────────────────────────────
# データ構造（DB レコード）
# ───────────────────────────────────────────────


@dataclass(frozen=True)
class Trade:
    """trades テーブル（章8.2）。

    is_filled / actual_entry_price / tp_order_id / sl_order_id は
    PR7.2 で追加。grouped 発注時点では tp/sl の order_id は不明で、
    entry が約定してから position_monitor が紐付ける（章14.6）。
    """

    id: int
    symbol: str
    direction: Literal["LONG", "SHORT"]
    entry_time: datetime
    entry_price: Decimal
    size_coins: Decimal
    sl_price: Decimal
    tp_price: Decimal
    leverage: int
    is_dry_run: bool
    exit_time: datetime | None
    exit_price: Decimal | None
    exit_reason: str | None
    pnl_usd: Decimal | None
    fee_usd_total: Decimal | None
    funding_paid_usd: Decimal | None
    mfe_pct: Decimal | None
    mae_pct: Decimal | None
    closed_at: datetime | None
    is_filled: bool = False
    actual_entry_price: Decimal | None = None
    tp_order_id: int | None = None
    sl_order_id: int | None = None


@dataclass(frozen=True)
class TradeOpenRequest:
    """新規ポジション登録リクエスト。"""

    symbol: str
    direction: Literal["LONG", "SHORT"]
    entry_price: Decimal
    size_coins: Decimal
    sl_price: Decimal
    tp_price: Decimal
    leverage: int
    is_dry_run: bool
    decision: EntryDecision  # 判定結果を保存（後で分析用）


@dataclass(frozen=True)
class TradeCloseRequest:
    """ポジション決済リクエスト。"""

    trade_id: int
    exit_price: Decimal
    exit_reason: Literal["TP", "SL", "FUNDING", "MANUAL", "TIMEOUT"]
    pnl_usd: Decimal
    fee_usd_total: Decimal
    funding_paid_usd: Decimal
    mfe_pct: Decimal
    mae_pct: Decimal


@dataclass(frozen=True)
class SignalLog:
    """signals テーブル（章8.3 4層各層の判定ログ）。"""

    timestamp: datetime
    symbol: str
    direction: Literal["LONG", "SHORT"]
    layer: Literal["MOMENTUM", "FLOW", "SENTIMENT", "REGIME"]
    passed: bool
    rejection_reason: str | None
    snapshot_excerpt: str  # JSON


@dataclass(frozen=True)
class IncidentLog:
    """incidents テーブル（章8.6 障害ログ）。"""

    timestamp: datetime
    severity: Literal["INFO", "WARNING", "ERROR", "CRITICAL"]
    event: str
    details: str  # JSON


# ───────────────────────────────────────────────
# Protocol
# ───────────────────────────────────────────────


class Repository(Protocol):
    """データ永続化プロバイダ。"""

    # ─── Trades ───
    async def open_trade(self, request: TradeOpenRequest) -> int: ...

    async def close_trade(self, request: TradeCloseRequest) -> None: ...

    async def get_trade(self, trade_id: int) -> Trade | None: ...

    async def get_open_trades(self) -> tuple[Trade, ...]: ...

    async def get_recent_trades(self, limit: int = 100) -> tuple[Trade, ...]: ...

    async def update_trade_vwap_metrics(
        self, trade_id: int, metrics: dict[str, Any]
    ) -> None: ...

    # ─── PR7.2 position_monitor 用 ───
    async def mark_trade_filled(
        self, trade_id: int, fill_price: Decimal, fill_time: datetime
    ) -> None: ...

    async def update_tp_sl_order_ids(
        self,
        trade_id: int,
        tp_order_id: int | None,
        sl_order_id: int | None,
    ) -> None: ...

    async def update_mfe_mae(
        self,
        trade_id: int,
        mfe_pct: Decimal,
        mae_pct: Decimal,
    ) -> None: ...

    # ─── Signals ───
    async def log_signal(self, signal: SignalLog) -> None: ...

    async def get_signals_today(self, symbol: str | None = None) -> tuple[SignalLog, ...]: ...

    # ─── Incidents ───
    async def log_incident(self, incident: IncidentLog) -> None: ...

    # ─── 集計（章15.4 日次サマリー用） ───
    async def get_daily_pnl_usd(self, date: datetime) -> Decimal: ...

    async def get_consecutive_losses(self) -> int: ...

    async def get_account_balance_history(
        self, days: int
    ) -> tuple[tuple[datetime, Decimal], ...]: ...

    # ─── 状態 ───
    async def register_external_position(
        self, symbol: str, size: Decimal, entry_price: Decimal
    ) -> int: ...

    async def correct_position(
        self, trade_id: int, actual_size: Decimal, actual_entry: Decimal
    ) -> None: ...

    async def mark_manual_review(self, trade_id: int) -> None: ...

    # ─── OI履歴（章13.5） ───
    async def record_oi(
        self, symbol: str, timestamp: datetime, oi_value: Decimal
    ) -> None: ...

    async def get_oi_at(
        self,
        symbol: str,
        target_time: datetime,
        tolerance_minutes: int,
    ) -> Decimal | None: ...
