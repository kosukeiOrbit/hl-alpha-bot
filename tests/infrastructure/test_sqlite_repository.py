"""SQLiteRepository のテスト。

各テストごとに新しいメモリ DB を作って独立性を保つ。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from src.adapters.exchange import Fill
from src.adapters.repository import (
    IncidentLog,
    Repository,
    SignalLog,
    TradeCloseRequest,
    TradeOpenRequest,
)
from src.core.models import EntryDecision
from src.infrastructure.sqlite_repository import (
    SQLiteRepository,
    _dt_iso,
)


def make_decision() -> EntryDecision:
    return EntryDecision(
        should_enter=True,
        direction="LONG",
        rejection_reason=None,
        layer_results={
            "momentum": True,
            "flow": True,
            "regime": True,
            "sentiment": True,
        },
    )


def make_open_request(**overrides: Any) -> TradeOpenRequest:
    base: dict[str, Any] = {
        "symbol": "BTC",
        "direction": "LONG",
        "entry_price": Decimal("65000"),
        "size_coins": Decimal("0.0002"),
        "sl_price": Decimal("64000"),
        "tp_price": Decimal("67000"),
        "leverage": 3,
        "is_dry_run": False,
        "decision": make_decision(),
    }
    base.update(overrides)
    return TradeOpenRequest(**base)


def make_close_request(trade_id: int, **overrides: Any) -> TradeCloseRequest:
    base: dict[str, Any] = {
        "trade_id": trade_id,
        "exit_price": Decimal("67000"),
        "exit_reason": "TP",
        "pnl_usd": Decimal("4.0"),
        "fee_usd_total": Decimal("1.0"),
        "funding_paid_usd": Decimal("0"),
        "mfe_pct": Decimal("3.0"),
        "mae_pct": Decimal("0"),
    }
    base.update(overrides)
    return TradeCloseRequest(**base)


@pytest.fixture
async def repo() -> AsyncIterator[SQLiteRepository]:
    r = SQLiteRepository(":memory:")
    await r.initialize()
    try:
        yield r
    finally:
        await r.close()


# ─── 初期化 ──────────────────────────────


class TestInitialize:
    @pytest.mark.asyncio
    async def test_creates_schema(self) -> None:
        r = SQLiteRepository(":memory:")
        await r.initialize()
        assert r._db is not None
        async with r._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='trades'"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        await r.close()
        assert r._db is None

    @pytest.mark.asyncio
    async def test_uninitialized_raises(self) -> None:
        r = SQLiteRepository(":memory:")
        with pytest.raises(RuntimeError, match="not initialized"):
            await r.get_open_trades()

    @pytest.mark.asyncio
    async def test_close_when_not_initialized_is_noop(self) -> None:
        r = SQLiteRepository(":memory:")
        await r.close()  # 例外にならない

    @pytest.mark.asyncio
    async def test_satisfies_protocol(self, repo: SQLiteRepository) -> None:
        # 構造的型互換: SQLiteRepository は Repository Protocol を満たす
        check: Repository = repo
        assert check is repo

    @pytest.mark.asyncio
    async def test_initialize_file_db_uses_wal(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        db_path = tmp_path / "test.db"
        r = SQLiteRepository(db_path)
        await r.initialize()
        assert r._db is not None
        async with r._db.execute("PRAGMA journal_mode") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0].lower() == "wal"
        await r.close()

    @pytest.mark.asyncio
    async def test_missing_schema_raises(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from pathlib import Path

        import src.infrastructure.sqlite_repository as mod

        monkeypatch.setattr(mod, "SCHEMA_PATH", Path("/nonexistent/schema.sql"))
        r = SQLiteRepository(":memory:")
        with pytest.raises(FileNotFoundError):
            await r.initialize()


# ─── Trades 作成・取得 ──────────────────


class TestTradeBasic:
    @pytest.mark.asyncio
    async def test_open_returns_id(self, repo: SQLiteRepository) -> None:
        tid = await repo.open_trade(make_open_request())
        assert tid == 1

    @pytest.mark.asyncio
    async def test_open_then_get_returns_same_data(
        self, repo: SQLiteRepository
    ) -> None:
        tid = await repo.open_trade(make_open_request())
        trade = await repo.get_trade(tid)
        assert trade is not None
        assert trade.symbol == "BTC"
        assert trade.direction == "LONG"
        assert trade.entry_price == Decimal("65000")
        assert trade.size_coins == Decimal("0.0002")
        assert trade.is_filled is False
        assert trade.is_dry_run is False
        assert trade.leverage == 3

    @pytest.mark.asyncio
    async def test_get_trade_missing_returns_none(
        self, repo: SQLiteRepository
    ) -> None:
        trade = await repo.get_trade(9999)
        assert trade is None

    @pytest.mark.asyncio
    async def test_dry_run_flag_persisted(
        self, repo: SQLiteRepository
    ) -> None:
        tid = await repo.open_trade(make_open_request(is_dry_run=True))
        trade = await repo.get_trade(tid)
        assert trade is not None
        assert trade.is_dry_run is True

    @pytest.mark.asyncio
    async def test_get_open_trades_excludes_closed(
        self, repo: SQLiteRepository
    ) -> None:
        tid1 = await repo.open_trade(make_open_request(symbol="BTC"))
        tid2 = await repo.open_trade(make_open_request(symbol="ETH"))
        await repo.close_trade(make_close_request(tid1))
        trades = await repo.get_open_trades()
        assert {t.id for t in trades} == {tid2}

    @pytest.mark.asyncio
    async def test_get_recent_trades_orders_desc(
        self, repo: SQLiteRepository
    ) -> None:
        for sym in ("BTC", "ETH", "SOL"):
            await repo.open_trade(make_open_request(symbol=sym))
        recent = await repo.get_recent_trades(limit=2)
        assert [t.symbol for t in recent] == ["SOL", "ETH"]

    @pytest.mark.asyncio
    async def test_close_trade_persists_pnl(
        self, repo: SQLiteRepository
    ) -> None:
        tid = await repo.open_trade(make_open_request())
        await repo.close_trade(make_close_request(tid))
        trade = await repo.get_trade(tid)
        assert trade is not None
        assert trade.exit_price == Decimal("67000")
        assert trade.exit_reason == "TP"
        assert trade.pnl_usd == Decimal("4.0")
        assert trade.exit_time is not None
        assert trade.closed_at is not None


# ─── State updates (PR7.2) ─────────────


class TestStateUpdates:
    @pytest.mark.asyncio
    async def test_mark_trade_filled(self, repo: SQLiteRepository) -> None:
        tid = await repo.open_trade(make_open_request())
        fill_t = datetime.now(UTC)
        await repo.mark_trade_filled(tid, Decimal("65010"), fill_t)
        trade = await repo.get_trade(tid)
        assert trade is not None
        assert trade.is_filled is True
        assert trade.actual_entry_price == Decimal("65010")

    @pytest.mark.asyncio
    async def test_update_tp_sl_order_ids(
        self, repo: SQLiteRepository
    ) -> None:
        tid = await repo.open_trade(make_open_request())
        await repo.update_tp_sl_order_ids(tid, 999, 888)
        trade = await repo.get_trade(tid)
        assert trade is not None
        assert trade.tp_order_id == 999
        assert trade.sl_order_id == 888

    @pytest.mark.asyncio
    async def test_update_tp_sl_with_none(
        self, repo: SQLiteRepository
    ) -> None:
        tid = await repo.open_trade(make_open_request())
        await repo.update_tp_sl_order_ids(tid, None, None)
        trade = await repo.get_trade(tid)
        assert trade is not None
        assert trade.tp_order_id is None
        assert trade.sl_order_id is None

    @pytest.mark.asyncio
    async def test_update_mfe_mae(self, repo: SQLiteRepository) -> None:
        tid = await repo.open_trade(make_open_request())
        await repo.update_mfe_mae(tid, Decimal("3.5"), Decimal("-1.0"))
        trade = await repo.get_trade(tid)
        assert trade is not None
        assert trade.mfe_pct == Decimal("3.5")
        assert trade.mae_pct == Decimal("-1.0")

    @pytest.mark.asyncio
    async def test_update_trade_vwap_metrics(
        self, repo: SQLiteRepository
    ) -> None:
        tid = await repo.open_trade(make_open_request())
        await repo.update_trade_vwap_metrics(tid, {"distance_pct": 0.3})
        # 直接検証
        async with repo._db.execute(  # type: ignore[union-attr]
            "SELECT vwap_metrics FROM trades WHERE id = ?", (tid,)
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert json.loads(row["vwap_metrics"]) == {"distance_pct": 0.3}

    @pytest.mark.asyncio
    async def test_mark_resumed(self, repo: SQLiteRepository) -> None:
        tid = await repo.open_trade(make_open_request())
        await repo.mark_resumed(tid)
        async with repo._db.execute(  # type: ignore[union-attr]
            "SELECT resumed_at FROM trades WHERE id = ?", (tid,)
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["resumed_at"] is not None

    @pytest.mark.asyncio
    async def test_correct_position(self, repo: SQLiteRepository) -> None:
        tid = await repo.open_trade(make_open_request())
        await repo.correct_position(
            tid, actual_size=Decimal("0.0003"), actual_entry=Decimal("65050")
        )
        trade = await repo.get_trade(tid)
        assert trade is not None
        assert trade.size_coins == Decimal("0.0003")
        assert trade.actual_entry_price == Decimal("65050")

    @pytest.mark.asyncio
    async def test_close_trade_from_fill(
        self, repo: SQLiteRepository
    ) -> None:
        tid = await repo.open_trade(make_open_request())
        fill = Fill(
            order_id=12345,
            symbol="BTC",
            side="sell",
            size=Decimal("0.0002"),
            price=Decimal("66500"),
            fee_usd=Decimal("0.5"),
            timestamp_ms=int(datetime.now(UTC).timestamp() * 1000),
            closed_pnl=Decimal("3.0"),
        )
        await repo.close_trade_from_fill(tid, fill)
        trade = await repo.get_trade(tid)
        assert trade is not None
        assert trade.exit_reason == "MANUAL"
        assert trade.exit_price == Decimal("66500")
        assert trade.pnl_usd == Decimal("3.0")
        assert trade.exit_time is not None

    @pytest.mark.asyncio
    async def test_register_external_position_long(
        self, repo: SQLiteRepository
    ) -> None:
        tid = await repo.register_external_position(
            "ETH", size=Decimal("0.1"), entry_price=Decimal("3000")
        )
        trade = await repo.get_trade(tid)
        assert trade is not None
        assert trade.symbol == "ETH"
        assert trade.direction == "LONG"
        assert trade.size_coins == Decimal("0.1")
        assert trade.is_filled is True

    @pytest.mark.asyncio
    async def test_register_external_position_short(
        self, repo: SQLiteRepository
    ) -> None:
        tid = await repo.register_external_position(
            "ETH", size=Decimal("-0.1"), entry_price=Decimal("3000")
        )
        trade = await repo.get_trade(tid)
        assert trade is not None
        assert trade.direction == "SHORT"
        assert trade.size_coins == Decimal("0.1")  # 絶対値で保存

    @pytest.mark.asyncio
    async def test_mark_manual_review(self, repo: SQLiteRepository) -> None:
        tid = await repo.open_trade(make_open_request())
        await repo.mark_manual_review(tid)
        async with repo._db.execute(  # type: ignore[union-attr]
            "SELECT is_manual_review FROM trades WHERE id = ?", (tid,)
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["is_manual_review"] == 1


# ─── 連敗計算 ────────────────────────────


class TestConsecutiveLosses:
    @pytest.mark.asyncio
    async def test_no_trades_returns_zero(
        self, repo: SQLiteRepository
    ) -> None:
        assert await repo.get_consecutive_losses() == 0

    @pytest.mark.asyncio
    async def test_two_losses_then_win_resets(
        self, repo: SQLiteRepository
    ) -> None:
        # 古い順に: 負け, 負け, 勝ち, 負け, 負け
        for pnl in [-1.0, -1.0, 5.0, -2.0, -3.0]:
            tid = await repo.open_trade(make_open_request())
            await repo.close_trade(
                make_close_request(tid, pnl_usd=Decimal(str(pnl)))
            )
        # 直近 2 連敗
        assert await repo.get_consecutive_losses() == 2

    @pytest.mark.asyncio
    async def test_all_losses(self, repo: SQLiteRepository) -> None:
        for pnl in [-1.0, -2.0, -3.0]:
            tid = await repo.open_trade(make_open_request())
            await repo.close_trade(
                make_close_request(tid, pnl_usd=Decimal(str(pnl)))
            )
        assert await repo.get_consecutive_losses() == 3

    @pytest.mark.asyncio
    async def test_dry_run_excluded(self, repo: SQLiteRepository) -> None:
        # dry_run の負けは数えない
        tid = await repo.open_trade(make_open_request(is_dry_run=True))
        await repo.close_trade(
            make_close_request(tid, pnl_usd=Decimal("-1.0"))
        )
        assert await repo.get_consecutive_losses() == 0


# ─── 日次 PnL ────────────────────────────


class TestDailyPnl:
    @pytest.mark.asyncio
    async def test_no_trades_returns_zero(
        self, repo: SQLiteRepository
    ) -> None:
        today = datetime.now(UTC)
        assert await repo.get_daily_pnl_usd(today) == Decimal("0")

    @pytest.mark.asyncio
    async def test_sums_today(self, repo: SQLiteRepository) -> None:
        for pnl in [3.0, -1.5, 2.5]:
            tid = await repo.open_trade(make_open_request())
            await repo.close_trade(
                make_close_request(tid, pnl_usd=Decimal(str(pnl)))
            )
        result = await repo.get_daily_pnl_usd(datetime.now(UTC))
        assert result == Decimal("4.0")

    @pytest.mark.asyncio
    async def test_excludes_dry_run(self, repo: SQLiteRepository) -> None:
        tid = await repo.open_trade(make_open_request(is_dry_run=True))
        await repo.close_trade(
            make_close_request(tid, pnl_usd=Decimal("100"))
        )
        result = await repo.get_daily_pnl_usd(datetime.now(UTC))
        assert result == Decimal("0")

    @pytest.mark.asyncio
    async def test_naive_datetime_treated_as_utc(
        self, repo: SQLiteRepository
    ) -> None:
        tid = await repo.open_trade(make_open_request())
        await repo.close_trade(
            make_close_request(tid, pnl_usd=Decimal("1.0"))
        )
        # tzinfo なしでも UTC として扱われ範囲に入る
        naive_today = datetime.now(UTC).replace(tzinfo=None)
        result = await repo.get_daily_pnl_usd(naive_today)
        assert result == Decimal("1.0")


# ─── get_pnl_since（PR7.4-real Layer 2） ──


class TestGetPnlSince:
    @pytest.mark.asyncio
    async def test_no_trades_returns_zero(
        self, repo: SQLiteRepository
    ) -> None:
        since = datetime.now(UTC) - timedelta(days=7)
        assert await repo.get_pnl_since(since) == Decimal("0")

    @pytest.mark.asyncio
    async def test_sums_recent_trades(
        self, repo: SQLiteRepository
    ) -> None:
        for pnl in [2.0, -0.5, 1.0]:
            tid = await repo.open_trade(make_open_request())
            await repo.close_trade(
                make_close_request(tid, pnl_usd=Decimal(str(pnl)))
            )
        since = datetime.now(UTC) - timedelta(days=7)
        assert await repo.get_pnl_since(since) == Decimal("2.5")

    @pytest.mark.asyncio
    async def test_excludes_dry_run(
        self, repo: SQLiteRepository
    ) -> None:
        tid = await repo.open_trade(make_open_request(is_dry_run=True))
        await repo.close_trade(
            make_close_request(tid, pnl_usd=Decimal("100"))
        )
        since = datetime.now(UTC) - timedelta(days=7)
        assert await repo.get_pnl_since(since) == Decimal("0")

    @pytest.mark.asyncio
    async def test_naive_datetime_treated_as_utc(
        self, repo: SQLiteRepository
    ) -> None:
        tid = await repo.open_trade(make_open_request())
        await repo.close_trade(
            make_close_request(tid, pnl_usd=Decimal("3.0"))
        )
        naive_since = (datetime.now(UTC) - timedelta(hours=1)).replace(
            tzinfo=None
        )
        assert await repo.get_pnl_since(naive_since) == Decimal("3.0")


# ─── balance_history ───────────────────


class TestBalanceHistory:
    @pytest.mark.asyncio
    async def test_record_and_get(self, repo: SQLiteRepository) -> None:
        ts1 = datetime.now(UTC) - timedelta(days=2)
        ts2 = datetime.now(UTC) - timedelta(days=1)
        ts3 = datetime.now(UTC)
        await repo.record_balance_snapshot(ts1, Decimal("1000"))
        await repo.record_balance_snapshot(ts2, Decimal("1010"))
        await repo.record_balance_snapshot(ts3, Decimal("1020"))

        history = await repo.get_account_balance_history(days=7)
        assert len(history) == 3
        assert history[0][1] == Decimal("1000")

    @pytest.mark.asyncio
    async def test_filters_by_days(self, repo: SQLiteRepository) -> None:
        old = datetime.now(UTC) - timedelta(days=10)
        recent = datetime.now(UTC) - timedelta(days=1)
        await repo.record_balance_snapshot(old, Decimal("500"))
        await repo.record_balance_snapshot(recent, Decimal("1000"))

        # 5 日以内 → recent のみ
        history = await repo.get_account_balance_history(days=5)
        assert len(history) == 1
        assert history[0][1] == Decimal("1000")


# ─── Signals ─────────────────────────────


class TestSignals:
    @pytest.mark.asyncio
    async def test_log_and_query_today(
        self, repo: SQLiteRepository
    ) -> None:
        for layer in ("MOMENTUM", "FLOW", "SENTIMENT", "REGIME"):
            await repo.log_signal(
                SignalLog(
                    timestamp=datetime.now(UTC),
                    symbol="BTC",
                    direction="LONG",
                    layer=layer,
                    passed=True,
                    rejection_reason=None,
                    snapshot_excerpt='{"price":65000}',
                )
            )
        signals = await repo.get_signals_today()
        assert len(signals) == 4
        assert {s.layer for s in signals} == {
            "MOMENTUM",
            "FLOW",
            "SENTIMENT",
            "REGIME",
        }

    @pytest.mark.asyncio
    async def test_get_signals_today_filters_by_symbol(
        self, repo: SQLiteRepository
    ) -> None:
        await repo.log_signal(
            SignalLog(
                timestamp=datetime.now(UTC),
                symbol="BTC",
                direction="LONG",
                layer="MOMENTUM",
                passed=True,
                rejection_reason=None,
                snapshot_excerpt="",
            )
        )
        await repo.log_signal(
            SignalLog(
                timestamp=datetime.now(UTC),
                symbol="ETH",
                direction="LONG",
                layer="MOMENTUM",
                passed=True,
                rejection_reason=None,
                snapshot_excerpt="",
            )
        )
        btc_only = await repo.get_signals_today(symbol="BTC")
        assert len(btc_only) == 1
        assert btc_only[0].symbol == "BTC"

    @pytest.mark.asyncio
    async def test_get_signals_today_excludes_yesterday(
        self, repo: SQLiteRepository
    ) -> None:
        yesterday = datetime.now(UTC) - timedelta(days=1)
        await repo.log_signal(
            SignalLog(
                timestamp=yesterday,
                symbol="BTC",
                direction="LONG",
                layer="MOMENTUM",
                passed=True,
                rejection_reason=None,
                snapshot_excerpt="",
            )
        )
        signals = await repo.get_signals_today()
        assert len(signals) == 0

    @pytest.mark.asyncio
    async def test_signal_passed_false_persisted(
        self, repo: SQLiteRepository
    ) -> None:
        await repo.log_signal(
            SignalLog(
                timestamp=datetime.now(UTC),
                symbol="BTC",
                direction="LONG",
                layer="FLOW",
                passed=False,
                rejection_reason="layer_flow_failed",
                snapshot_excerpt="",
            )
        )
        signals = await repo.get_signals_today()
        assert signals[0].passed is False
        assert signals[0].rejection_reason == "layer_flow_failed"


# ─── Incidents ───────────────────────────


class TestIncidents:
    @pytest.mark.asyncio
    async def test_log_incident(self, repo: SQLiteRepository) -> None:
        await repo.log_incident(
            IncidentLog(
                timestamp=datetime.now(UTC),
                severity="WARNING",
                event="ws_disconnect",
                details='{"duration_sec":15}',
            )
        )
        async with repo._db.execute(  # type: ignore[union-attr]
            "SELECT COUNT(*) AS c FROM incidents"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["c"] == 1


# ─── OI 履歴 ─────────────────────────────


class TestOiHistory:
    @pytest.mark.asyncio
    async def test_record_and_get(self, repo: SQLiteRepository) -> None:
        ts = datetime.now(UTC)
        await repo.record_oi("BTC", ts, Decimal("1000000"))
        result = await repo.get_oi_at("BTC", ts, tolerance_minutes=5)
        assert result == Decimal("1000000")

    @pytest.mark.asyncio
    async def test_returns_none_when_no_data(
        self, repo: SQLiteRepository
    ) -> None:
        result = await repo.get_oi_at(
            "BTC", datetime.now(UTC), tolerance_minutes=5
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_outside_tolerance_returns_none(
        self, repo: SQLiteRepository
    ) -> None:
        old = datetime.now(UTC) - timedelta(hours=1)
        await repo.record_oi("BTC", old, Decimal("1000000"))
        result = await repo.get_oi_at(
            "BTC", datetime.now(UTC), tolerance_minutes=5
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_picks_closest_within_tolerance(
        self, repo: SQLiteRepository
    ) -> None:
        target = datetime.now(UTC)
        # ±2 分の 2 件のうち、より近い方が返る
        await repo.record_oi(
            "BTC", target - timedelta(minutes=2), Decimal("100")
        )
        await repo.record_oi(
            "BTC", target + timedelta(seconds=30), Decimal("200")
        )
        result = await repo.get_oi_at("BTC", target, tolerance_minutes=5)
        assert result == Decimal("200")


# ─── 変換ヘルパー ────────────────────────


class TestDateTimeHelpers:
    def test_dt_iso_none_returns_none(self) -> None:
        assert _dt_iso(None) is None

    def test_dt_iso_naive_treated_as_utc(self) -> None:
        naive = datetime(2026, 4, 29, 10, 30, 0)
        result = _dt_iso(naive)
        assert result is not None
        assert result.endswith("+00:00")

    def test_dt_iso_aware_preserves_tz(self) -> None:
        aware = datetime(2026, 4, 29, 10, 30, 0, tzinfo=UTC)
        assert _dt_iso(aware) == "2026-04-29T10:30:00+00:00"
