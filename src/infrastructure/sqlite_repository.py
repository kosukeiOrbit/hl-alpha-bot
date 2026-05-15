"""SQLite ベースの Repository 実装（章8）。

aiosqlite による非同期実装。メモリ DB / ファイル DB 両対応。
シングルスレッド非同期で使う前提（aiosqlite はスレッドセーフではない）。

設計上の注意:
- Decimal は SQLite が native サポートしないので REAL で保存し、読み出し時に
  str 経由で Decimal に戻す（精度をできるだけ保つ）。トレード規模なら
  float の 15 桁精度で十分。
- datetime は ISO8601 UTC 文字列で保存。tzinfo がない場合は UTC とみなす。
- 起動時に schema.sql を `IF NOT EXISTS` で適用（冪等）。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiosqlite

from src.adapters.exchange import Fill
from src.adapters.repository import (
    IncidentLog,
    SignalLog,
    Trade,
    TradeCloseRequest,
    TradeOpenRequest,
)

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).parent / "migrations" / "schema.sql"


class SQLiteRepository:
    """SQLite ベースの Repository 実装。

    使用例::

        repo = SQLiteRepository("data/bot.db")
        await repo.initialize()
        try:
            await repo.open_trade(...)
        finally:
            await repo.close()
    """

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        self._db: aiosqlite.Connection | None = None

    # ─── ライフサイクル ────────────────────

    async def initialize(self) -> None:
        """DB 接続 + スキーマ適用。"""
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA foreign_keys = ON")
        if self.db_path != ":memory:":
            await self._db.execute("PRAGMA journal_mode = WAL")

        if not SCHEMA_PATH.exists():
            raise FileNotFoundError(f"schema not found: {SCHEMA_PATH}")
        with SCHEMA_PATH.open("r", encoding="utf-8") as f:
            await self._db.executescript(f.read())
        await self._ensure_trades_columns()
        await self._db.commit()

    async def _ensure_trades_columns(self) -> None:
        """既存 DB の trades テーブルに後付けカラムを追加（冪等）。

        ``CREATE TABLE IF NOT EXISTS`` は既存テーブルにカラムを追加しない
        ため、PR B2 で追加した ``entry_order_id`` のような後付け列は
        ``PRAGMA table_info`` で存在確認した上で ``ALTER TABLE`` する。
        """
        db = self._require_db()
        async with db.execute("PRAGMA table_info(trades)") as cursor:
            rows = await cursor.fetchall()
        existing = {row[1] for row in rows}  # row[1] is column name
        if "entry_order_id" not in existing:
            await db.execute(
                "ALTER TABLE trades ADD COLUMN entry_order_id TEXT"
            )

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    # ─── Trades: 作成 / 決済 / 取得 ───────

    async def open_trade(self, request: TradeOpenRequest) -> int:
        db = self._require_db()
        cursor = await db.execute(
            """
            INSERT INTO trades (
                symbol, direction, leverage_used,
                size_coins, entry_price, sl_price, tp_price,
                is_dry_run, is_filled, entry_time, entry_order_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                request.symbol,
                request.direction,
                request.leverage,
                _dec_to_real(request.size_coins),
                _dec_to_real(request.entry_price),
                _dec_to_real(request.sl_price),
                _dec_to_real(request.tp_price),
                1 if request.is_dry_run else 0,
                _dt_iso(datetime.now(UTC)),
                (
                    str(request.entry_order_id)
                    if request.entry_order_id is not None
                    else None
                ),
            ),
        )
        await db.commit()
        return cursor.lastrowid or 0

    async def close_trade(self, request: TradeCloseRequest) -> None:
        db = self._require_db()
        now_iso = _dt_iso(datetime.now(UTC))
        await db.execute(
            """
            UPDATE trades SET
                exit_price = ?,
                exit_reason = ?,
                pnl_usd = ?,
                fee_usd_total = ?,
                funding_paid_usd = ?,
                mfe_pct = ?,
                mae_pct = ?,
                exit_time = ?,
                closed_at = ?
            WHERE id = ?
            """,
            (
                _dec_to_real(request.exit_price),
                request.exit_reason,
                _dec_to_real(request.pnl_usd),
                _dec_to_real(request.fee_usd_total),
                _dec_to_real(request.funding_paid_usd),
                _dec_to_real(request.mfe_pct),
                _dec_to_real(request.mae_pct),
                now_iso,
                now_iso,
                request.trade_id,
            ),
        )
        await db.commit()

    async def get_trade(self, trade_id: int) -> Trade | None:
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM trades WHERE id = ?", (trade_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_trade(row) if row is not None else None

    async def get_open_trades(self) -> tuple[Trade, ...]:
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM trades WHERE exit_time IS NULL ORDER BY id ASC"
        ) as cursor:
            rows = await cursor.fetchall()
        return tuple(_row_to_trade(r) for r in rows)

    async def get_recent_trades(self, limit: int = 100) -> tuple[Trade, ...]:
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
        return tuple(_row_to_trade(r) for r in rows)

    async def update_trade_vwap_metrics(
        self, trade_id: int, metrics: dict[str, Any]
    ) -> None:
        db = self._require_db()
        await db.execute(
            "UPDATE trades SET vwap_metrics = ? WHERE id = ?",
            (json.dumps(metrics), trade_id),
        )
        await db.commit()

    # ─── PR7.2 / PR7.3 ──────────────────────

    async def mark_trade_filled(
        self,
        trade_id: int,
        fill_price: Decimal,
        fill_time: datetime,
    ) -> None:
        db = self._require_db()
        await db.execute(
            """
            UPDATE trades SET
                is_filled = 1,
                actual_entry_price = ?,
                fill_time = ?
            WHERE id = ?
            """,
            (
                _dec_to_real(fill_price),
                _dt_iso(fill_time),
                trade_id,
            ),
        )
        await db.commit()

    async def update_tp_sl_order_ids(
        self,
        trade_id: int,
        tp_order_id: int | None,
        sl_order_id: int | None,
    ) -> None:
        db = self._require_db()
        await db.execute(
            """
            UPDATE trades SET tp_order_id = ?, sl_order_id = ?
            WHERE id = ?
            """,
            (
                str(tp_order_id) if tp_order_id is not None else None,
                str(sl_order_id) if sl_order_id is not None else None,
                trade_id,
            ),
        )
        await db.commit()

    async def update_mfe_mae(
        self,
        trade_id: int,
        mfe_pct: Decimal,
        mae_pct: Decimal,
    ) -> None:
        db = self._require_db()
        await db.execute(
            """
            UPDATE trades SET mfe_pct = ?, mae_pct = ?
            WHERE id = ?
            """,
            (
                _dec_to_real(mfe_pct),
                _dec_to_real(mae_pct),
                trade_id,
            ),
        )
        await db.commit()

    async def mark_resumed(self, trade_id: int) -> None:
        db = self._require_db()
        await db.execute(
            "UPDATE trades SET resumed_at = ? WHERE id = ?",
            (_dt_iso(datetime.now(UTC)), trade_id),
        )
        await db.commit()

    async def correct_position(
        self,
        trade_id: int,
        actual_size: Decimal,
        actual_entry: Decimal,
    ) -> None:
        db = self._require_db()
        await db.execute(
            """
            UPDATE trades SET
                size_coins = ?, actual_entry_price = ?
            WHERE id = ?
            """,
            (
                _dec_to_real(actual_size),
                _dec_to_real(actual_entry),
                trade_id,
            ),
        )
        await db.commit()

    async def close_trade_from_fill(
        self,
        trade_id: int,
        fill: Fill,
    ) -> None:
        db = self._require_db()
        fill_time = datetime.fromtimestamp(fill.timestamp_ms / 1000, tz=UTC)
        now_iso = _dt_iso(datetime.now(UTC))
        await db.execute(
            """
            UPDATE trades SET
                exit_price = ?,
                exit_reason = 'MANUAL',
                pnl_usd = ?,
                fee_usd_total = ?,
                exit_time = ?,
                closed_at = ?
            WHERE id = ?
            """,
            (
                _dec_to_real(fill.price),
                _dec_to_real(fill.closed_pnl),
                _dec_to_real(fill.fee_usd),
                _dt_iso(fill_time),
                now_iso,
                trade_id,
            ),
        )
        await db.commit()

    async def register_external_position(
        self,
        symbol: str,
        size: Decimal,
        entry_price: Decimal,
    ) -> int:
        db = self._require_db()
        direction = "LONG" if size > 0 else "SHORT"
        abs_size = abs(size)
        cursor = await db.execute(
            """
            INSERT INTO trades (
                symbol, direction, leverage_used,
                size_coins, entry_price, actual_entry_price,
                sl_price, tp_price,
                is_external, is_filled, is_dry_run, entry_time
            )
            VALUES (?, ?, 0, ?, ?, ?, 0, 0, 1, 1, 0, ?)
            """,
            (
                symbol,
                direction,
                _dec_to_real(abs_size),
                _dec_to_real(entry_price),
                _dec_to_real(entry_price),
                _dt_iso(datetime.now(UTC)),
            ),
        )
        await db.commit()
        return cursor.lastrowid or 0

    async def mark_manual_review(self, trade_id: int) -> None:
        db = self._require_db()
        await db.execute(
            "UPDATE trades SET is_manual_review = 1 WHERE id = ?",
            (trade_id,),
        )
        await db.commit()

    # ─── 集計 ───────────────────────────────

    async def get_consecutive_losses(self) -> int:
        """直近の連敗数（直近の決済済み実弾トレードを新しい順に見て、
        最初の勝ち pnl_usd >= 0 までの連続負け数）。"""
        db = self._require_db()
        async with db.execute(
            """
            SELECT pnl_usd FROM trades
            WHERE exit_time IS NOT NULL
              AND pnl_usd IS NOT NULL
              AND is_dry_run = 0
            ORDER BY exit_time DESC
            LIMIT 50
            """
        ) as cursor:
            rows = await cursor.fetchall()

        count = 0
        for row in rows:
            pnl = row["pnl_usd"]
            if pnl >= 0:
                break
            count += 1
        return count

    async def get_daily_pnl_usd(self, date: datetime) -> Decimal:
        """date と同日（UTC 00:00 起点）の累計 PnL。"""
        db = self._require_db()
        day_start = date.replace(hour=0, minute=0, second=0, microsecond=0)
        if day_start.tzinfo is None:
            day_start = day_start.replace(tzinfo=UTC)
        next_day = day_start + timedelta(days=1)
        async with db.execute(
            """
            SELECT COALESCE(SUM(pnl_usd), 0) AS total
            FROM trades
            WHERE exit_time IS NOT NULL
              AND exit_time >= ?
              AND exit_time < ?
              AND is_dry_run = 0
            """,
            (_dt_iso(day_start), _dt_iso(next_day)),
        ) as cursor:
            row = await cursor.fetchone()
        return Decimal("0") if row is None else Decimal(str(row["total"]))

    async def get_pnl_since(self, since: datetime) -> Decimal:
        """``since`` 以降に exit した実弾 trades の累計 PnL（章9.7 Layer 2）。"""
        db = self._require_db()
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        async with db.execute(
            """
            SELECT COALESCE(SUM(pnl_usd), 0) AS total
            FROM trades
            WHERE exit_time IS NOT NULL
              AND exit_time >= ?
              AND is_dry_run = 0
            """,
            (_dt_iso(since),),
        ) as cursor:
            row = await cursor.fetchone()
        return Decimal("0") if row is None else Decimal(str(row["total"]))

    async def get_account_balance_history(
        self, days: int
    ) -> tuple[tuple[datetime, Decimal], ...]:
        """直近 days 日分の残高履歴。"""
        db = self._require_db()
        cutoff = datetime.now(UTC) - timedelta(days=days)
        async with db.execute(
            """
            SELECT timestamp, balance_usd FROM balance_history
            WHERE timestamp >= ?
            ORDER BY timestamp ASC
            """,
            (_dt_iso(cutoff),),
        ) as cursor:
            rows = await cursor.fetchall()
        return tuple(
            (_iso_to_dt_required(r["timestamp"]), Decimal(str(r["balance_usd"])))
            for r in rows
        )

    async def record_balance_snapshot(
        self, timestamp: datetime, balance_usd: Decimal
    ) -> None:
        """残高スナップショットを記録（日次サマリー側から呼ばれる想定）。"""
        db = self._require_db()
        await db.execute(
            "INSERT INTO balance_history (timestamp, balance_usd) VALUES (?, ?)",
            (_dt_iso(timestamp), _dec_to_real(balance_usd)),
        )
        await db.commit()

    # ─── Signals / Incidents ───────────────

    async def log_signal(self, signal: SignalLog) -> None:
        db = self._require_db()
        await db.execute(
            """
            INSERT INTO signals (
                timestamp, symbol, direction, layer,
                passed, rejection_reason, snapshot_excerpt
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _dt_iso(signal.timestamp),
                signal.symbol,
                signal.direction,
                signal.layer,
                1 if signal.passed else 0,
                signal.rejection_reason,
                signal.snapshot_excerpt,
            ),
        )
        await db.commit()

    async def get_signals_today(
        self, symbol: str | None = None
    ) -> tuple[SignalLog, ...]:
        db = self._require_db()
        day_start = datetime.now(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        query = "SELECT * FROM signals WHERE timestamp >= ?"
        params: tuple[Any, ...] = (_dt_iso(day_start),)
        if symbol is not None:
            query += " AND symbol = ?"
            params = (*params, symbol)
        query += " ORDER BY timestamp ASC"
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
        return tuple(_row_to_signal(r) for r in rows)

    async def log_incident(self, incident: IncidentLog) -> None:
        db = self._require_db()
        await db.execute(
            """
            INSERT INTO incidents (timestamp, severity, event, details)
            VALUES (?, ?, ?, ?)
            """,
            (
                _dt_iso(incident.timestamp),
                incident.severity,
                incident.event,
                incident.details,
            ),
        )
        await db.commit()

    # ─── OI 履歴（章13.5） ─────────────────

    async def record_oi(
        self,
        symbol: str,
        timestamp: datetime,
        oi_value: Decimal,
    ) -> None:
        db = self._require_db()
        await db.execute(
            """
            INSERT INTO oi_history (symbol, timestamp, oi_value)
            VALUES (?, ?, ?)
            """,
            (symbol, _dt_iso(timestamp), _dec_to_real(oi_value)),
        )
        await db.commit()

    async def get_oi_at(
        self,
        symbol: str,
        target_time: datetime,
        tolerance_minutes: int,
    ) -> Decimal | None:
        db = self._require_db()
        from_iso = _dt_iso(target_time - timedelta(minutes=tolerance_minutes))
        to_iso = _dt_iso(target_time + timedelta(minutes=tolerance_minutes))
        target_iso = _dt_iso(target_time)
        async with db.execute(
            """
            SELECT oi_value FROM oi_history
            WHERE symbol = ? AND timestamp >= ? AND timestamp <= ?
            ORDER BY ABS(strftime('%s', timestamp) - strftime('%s', ?)) ASC
            LIMIT 1
            """,
            (symbol, from_iso, to_iso, target_iso),
        ) as cursor:
            row = await cursor.fetchone()
        return None if row is None else Decimal(str(row["oi_value"]))

    # ─── ヘルパー ──────────────────────────

    def _require_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError(
                "Repository not initialized. Call initialize() first."
            )
        return self._db


# ─── 変換ヘルパー（モジュールレベル） ──


def _dec_to_real(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _dt_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _iso_to_dt(s: str | None) -> datetime | None:
    if s is None:
        return None
    return datetime.fromisoformat(s)


def _iso_to_dt_required(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _row_to_trade(row: aiosqlite.Row) -> Trade:
    return Trade(
        id=row["id"],
        symbol=row["symbol"],
        direction=row["direction"],
        entry_time=_iso_to_dt_required(row["entry_time"]),
        entry_price=Decimal(str(row["entry_price"])),
        size_coins=Decimal(str(row["size_coins"])),
        sl_price=Decimal(str(row["sl_price"])),
        tp_price=Decimal(str(row["tp_price"])),
        leverage=int(row["leverage_used"]) if row["leverage_used"] else 0,
        is_dry_run=bool(row["is_dry_run"]),
        exit_time=_iso_to_dt(row["exit_time"]),
        exit_price=(
            Decimal(str(row["exit_price"]))
            if row["exit_price"] is not None
            else None
        ),
        exit_reason=row["exit_reason"],
        pnl_usd=(
            Decimal(str(row["pnl_usd"]))
            if row["pnl_usd"] is not None
            else None
        ),
        fee_usd_total=(
            Decimal(str(row["fee_usd_total"]))
            if row["fee_usd_total"] is not None
            else None
        ),
        funding_paid_usd=(
            Decimal(str(row["funding_paid_usd"]))
            if row["funding_paid_usd"] is not None
            else None
        ),
        mfe_pct=(
            Decimal(str(row["mfe_pct"]))
            if row["mfe_pct"] is not None
            else None
        ),
        mae_pct=(
            Decimal(str(row["mae_pct"]))
            if row["mae_pct"] is not None
            else None
        ),
        closed_at=_iso_to_dt(row["closed_at"]),
        is_filled=bool(row["is_filled"]),
        actual_entry_price=(
            Decimal(str(row["actual_entry_price"]))
            if row["actual_entry_price"] is not None
            else None
        ),
        tp_order_id=(
            int(row["tp_order_id"])
            if row["tp_order_id"] is not None
            else None
        ),
        sl_order_id=(
            int(row["sl_order_id"])
            if row["sl_order_id"] is not None
            else None
        ),
        fill_time=_iso_to_dt(row["fill_time"]),
        entry_order_id=(
            int(row["entry_order_id"])
            if row["entry_order_id"] is not None
            else None
        ),
    )


def _row_to_signal(row: aiosqlite.Row) -> SignalLog:
    return SignalLog(
        timestamp=_iso_to_dt_required(row["timestamp"]),
        symbol=row["symbol"],
        direction=row["direction"],
        layer=row["layer"],
        passed=bool(row["passed"]),
        rejection_reason=row["rejection_reason"],
        snapshot_excerpt=row["snapshot_excerpt"] or "",
    )
