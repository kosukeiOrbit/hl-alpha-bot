"""APPLICATION 層 reconciliation のテスト。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from src.adapters.exchange import (
    ExchangeError,
    Fill,
    Order,
    Position,
)
from src.adapters.repository import Trade
from src.application.reconciliation import (
    ReconcileSummary,
    ReconciliationConfig,
    StateReconciler,
)
from src.core.reconciliation import (
    ActionType,
    DBTrade,
    HLFill,
    ReconcileAction,
)


def make_config(**overrides: Any) -> ReconciliationConfig:
    base: dict[str, Any] = {
        "fills_lookback_hours": 24,
        "stale_order_cleanup_seconds": 30,
    }
    base.update(overrides)
    return ReconciliationConfig(**base)


def make_position(**overrides: Any) -> Position:
    base: dict[str, Any] = {
        "symbol": "BTC",
        "size": Decimal("0.0002"),
        "entry_price": Decimal("65000"),
        "unrealized_pnl": Decimal("0"),
        "leverage": 3,
        "liquidation_price": Decimal("60000"),
    }
    base.update(overrides)
    return Position(**base)


def make_trade(**overrides: Any) -> Trade:
    base: dict[str, Any] = {
        "id": 1,
        "symbol": "BTC",
        "direction": "LONG",
        "entry_time": datetime.now(UTC),
        "entry_price": Decimal("65000"),
        "size_coins": Decimal("0.0002"),
        "sl_price": Decimal("64000"),
        "tp_price": Decimal("67000"),
        "leverage": 3,
        "is_dry_run": False,
        "exit_time": None,
        "exit_price": None,
        "exit_reason": None,
        "pnl_usd": None,
        "fee_usd_total": None,
        "funding_paid_usd": None,
        "mfe_pct": None,
        "mae_pct": None,
        "closed_at": None,
        "is_filled": True,
        "actual_entry_price": Decimal("65000"),
        "tp_order_id": None,
        "sl_order_id": None,
    }
    base.update(overrides)
    return Trade(**base)


def make_order(**overrides: Any) -> Order:
    base: dict[str, Any] = {
        "order_id": 100,
        "client_order_id": None,
        "symbol": "BTC",
        "side": "buy",
        "size": Decimal("0.0002"),
        "price": Decimal("65000"),
        "tif": "Alo",
        "timestamp_ms": int(datetime.now(UTC).timestamp() * 1000),
    }
    base.update(overrides)
    return Order(**base)


def make_fill(**overrides: Any) -> Fill:
    base: dict[str, Any] = {
        "order_id": 12345,
        "symbol": "BTC",
        "side": "sell",
        "size": Decimal("0.0002"),
        "price": Decimal("66000"),
        "fee_usd": Decimal("0.5"),
        "timestamp_ms": int(datetime.now(UTC).timestamp() * 1000),
        "closed_pnl": Decimal("0.2"),
    }
    base.update(overrides)
    return Fill(**base)


def build_reconciler(
    *,
    positions: tuple[Position, ...] = (),
    open_trades: tuple[Trade, ...] = (),
    orders: tuple[Order, ...] = (),
    fills: tuple[Fill, ...] = (),
    config: ReconciliationConfig | None = None,
    positions_side_effect: BaseException | None = None,
    orders_side_effect: BaseException | None = None,
    fills_side_effect: BaseException | None = None,
    open_trades_side_effect: BaseException | None = None,
    cancel_order_result: bool = True,
    cancel_order_side_effect: BaseException | None = None,
    register_external_side_effect: BaseException | None = None,
    notifier_side_effect: BaseException | None = None,
) -> tuple[StateReconciler, Any, Any, Any]:
    exchange = AsyncMock()
    exchange.get_positions = AsyncMock(
        side_effect=positions_side_effect, return_value=positions
    ) if positions_side_effect is None else AsyncMock(
        side_effect=positions_side_effect
    )
    if positions_side_effect is None:
        exchange.get_positions = AsyncMock(return_value=positions)
    if orders_side_effect is not None:
        exchange.get_open_orders = AsyncMock(side_effect=orders_side_effect)
    else:
        exchange.get_open_orders = AsyncMock(return_value=orders)
    if fills_side_effect is not None:
        exchange.get_fills = AsyncMock(side_effect=fills_side_effect)
    else:
        exchange.get_fills = AsyncMock(return_value=fills)
    if cancel_order_side_effect is not None:
        exchange.cancel_order = AsyncMock(side_effect=cancel_order_side_effect)
    else:
        exchange.cancel_order = AsyncMock(return_value=cancel_order_result)

    repo = AsyncMock()
    if open_trades_side_effect is not None:
        repo.get_open_trades = AsyncMock(side_effect=open_trades_side_effect)
    else:
        repo.get_open_trades = AsyncMock(return_value=open_trades)
    if register_external_side_effect is not None:
        repo.register_external_position = AsyncMock(
            side_effect=register_external_side_effect
        )
    else:
        repo.register_external_position = AsyncMock(return_value=99)
    repo.mark_resumed = AsyncMock()
    repo.correct_position = AsyncMock()
    repo.close_trade_from_fill = AsyncMock()
    repo.mark_manual_review = AsyncMock()

    notifier = AsyncMock()
    if notifier_side_effect is not None:
        notifier.send_signal = AsyncMock(side_effect=notifier_side_effect)
        notifier.send_alert = AsyncMock(side_effect=notifier_side_effect)

    reconciler = StateReconciler(
        exchange=exchange,
        repo=repo,
        notifier=notifier,
        config=config or make_config(),
    )
    return reconciler, exchange, repo, notifier


# ─── 起動時復元 / 正常系 ─────────────────


class TestRestoreOnStartup:
    @pytest.mark.asyncio
    async def test_empty_state(self) -> None:
        reconciler, _, repo, notifier = build_reconciler()
        summary = await reconciler.restore_on_startup()
        assert summary == ReconcileSummary(
            hl_position_count=0,
            db_open_trade_count=0,
            actions_executed=0,
            stale_orders_cancelled=0,
            errors=(),
        )
        # 完了通知（成功）が 1 回送られる
        notifier.send_signal.assert_awaited_once()
        notifier.send_alert.assert_not_awaited()
        repo.register_external_position.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resume_monitoring_path(self) -> None:
        # HL ポジション と DB trade が一致 → RESUME_MONITORING
        pos = make_position()
        trade = make_trade()
        reconciler, _, repo, _ = build_reconciler(
            positions=(pos,), open_trades=(trade,)
        )
        summary = await reconciler.restore_on_startup()
        assert summary.actions_executed == 1
        repo.mark_resumed.assert_awaited_once_with(trade.id)

    @pytest.mark.asyncio
    async def test_register_external_path(self) -> None:
        # HL に ETH ポジション、DB に対応 trade なし → REGISTER_EXTERNAL
        pos = make_position(symbol="ETH")
        reconciler, _, repo, notifier = build_reconciler(
            positions=(pos,), open_trades=()
        )
        summary = await reconciler.restore_on_startup()
        assert summary.actions_executed == 1
        repo.register_external_position.assert_awaited_once_with(
            symbol="ETH",
            size=pos.size,
            entry_price=pos.entry_price,
        )
        notifier.send_alert.assert_awaited()

    @pytest.mark.asyncio
    async def test_correct_db_path(self) -> None:
        # HL ポジションと DB trade のサイズが食い違う → CORRECT_DB
        pos = make_position(size=Decimal("0.001"))
        trade = make_trade(size_coins=Decimal("0.0002"))
        reconciler, _, repo, _ = build_reconciler(
            positions=(pos,), open_trades=(trade,)
        )
        summary = await reconciler.restore_on_startup()
        assert summary.actions_executed == 1
        repo.correct_position.assert_awaited_once_with(
            trade_id=trade.id,
            actual_size=pos.size,
            actual_entry=pos.entry_price,
        )

    @pytest.mark.asyncio
    async def test_close_from_fill_path(self) -> None:
        # DB に LONG trade、HL にポジションなし、対応する sell fill あり → CLOSE_FROM_FILL
        trade = make_trade()
        fill = make_fill(
            symbol="BTC",
            side="sell",
            size=trade.size_coins,
            price=Decimal("66000"),
            closed_pnl=Decimal("0.2"),
        )
        reconciler, _, repo, notifier = build_reconciler(
            positions=(), open_trades=(trade,), fills=(fill,)
        )
        summary = await reconciler.restore_on_startup()
        assert summary.actions_executed == 1
        repo.close_trade_from_fill.assert_awaited_once_with(
            trade_id=trade.id, fill=fill
        )
        notifier.send_signal.assert_awaited()

    @pytest.mark.asyncio
    async def test_manual_review_path(self) -> None:
        # DB に trade、HL にポジションなし、fill もなし → MANUAL_REVIEW
        trade = make_trade()
        reconciler, _, repo, notifier = build_reconciler(
            positions=(), open_trades=(trade,), fills=()
        )
        summary = await reconciler.restore_on_startup()
        assert summary.actions_executed == 1
        repo.mark_manual_review.assert_awaited_once_with(trade.id)
        notifier.send_alert.assert_awaited()


# ─── stale order cleanup ────────────────


class TestStaleOrderCleanup:
    @pytest.mark.asyncio
    async def test_old_order_cancelled_new_kept(self) -> None:
        old_ts = int(
            (datetime.now(UTC) - timedelta(minutes=1)).timestamp() * 1000
        )
        new_ts = int(
            (datetime.now(UTC) - timedelta(seconds=5)).timestamp() * 1000
        )
        old_order = make_order(order_id=100, timestamp_ms=old_ts)
        new_order = make_order(order_id=101, timestamp_ms=new_ts)
        reconciler, exchange, _, _ = build_reconciler(
            orders=(old_order, new_order)
        )
        summary = await reconciler.restore_on_startup()
        assert summary.stale_orders_cancelled == 1
        cancelled_ids = [
            c.kwargs.get("order_id")
            for c in exchange.cancel_order.await_args_list
        ]
        assert 100 in cancelled_ids
        assert 101 not in cancelled_ids

    @pytest.mark.asyncio
    async def test_cancel_returns_false_does_not_count(self) -> None:
        old_ts = int(
            (datetime.now(UTC) - timedelta(minutes=1)).timestamp() * 1000
        )
        old_order = make_order(timestamp_ms=old_ts)
        reconciler, _, _, _ = build_reconciler(
            orders=(old_order,), cancel_order_result=False
        )
        summary = await reconciler.restore_on_startup()
        assert summary.stale_orders_cancelled == 0

    @pytest.mark.asyncio
    async def test_cancel_exchange_error_recorded_and_continues(self) -> None:
        old_ts = int(
            (datetime.now(UTC) - timedelta(minutes=1)).timestamp() * 1000
        )
        order1 = make_order(order_id=100, timestamp_ms=old_ts)
        order2 = make_order(order_id=200, timestamp_ms=old_ts)
        # 1 回目の cancel は失敗、2 回目は成功
        attempt = {"n": 0}

        async def cancel(*args: Any, **kwargs: Any) -> bool:
            attempt["n"] += 1
            if attempt["n"] == 1:
                raise ExchangeError("boom")
            return True

        reconciler, exchange, _, _ = build_reconciler(
            orders=(order1, order2)
        )
        exchange.cancel_order = AsyncMock(side_effect=cancel)
        summary = await reconciler.restore_on_startup()
        assert summary.stale_orders_cancelled == 1
        assert any("cancel_stale_100" in e for e in summary.errors)


# ─── 定期実行 ──────────────────────────


class TestPeriodicCheck:
    @pytest.mark.asyncio
    async def test_skips_cleanup(self) -> None:
        old_ts = int(
            (datetime.now(UTC) - timedelta(minutes=1)).timestamp() * 1000
        )
        old_order = make_order(timestamp_ms=old_ts)
        reconciler, exchange, _, _ = build_reconciler(orders=(old_order,))
        summary = await reconciler.run_periodic_check()
        assert summary.stale_orders_cancelled == 0
        exchange.cancel_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_completion_notification(self) -> None:
        reconciler, _, _, notifier = build_reconciler()
        await reconciler.run_periodic_check()
        notifier.send_signal.assert_not_awaited()
        notifier.send_alert.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_still_applies_actions(self) -> None:
        # 定期実行でも正常な action は実行される
        pos = make_position(symbol="ETH")
        reconciler, _, repo, _ = build_reconciler(positions=(pos,))
        summary = await reconciler.run_periodic_check()
        assert summary.actions_executed == 1
        repo.register_external_position.assert_awaited_once()


# ─── エラーハンドリング ───────────────


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_get_positions_error_continues(self) -> None:
        reconciler, _, _, notifier = build_reconciler(
            positions_side_effect=ExchangeError("api down")
        )
        summary = await reconciler.restore_on_startup()
        assert any("get_positions" in e for e in summary.errors)
        # errors ありなので alert 通知
        notifier.send_alert.assert_awaited()
        notifier.send_signal.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_get_open_orders_error_continues(self) -> None:
        reconciler, _, _, _ = build_reconciler(
            orders_side_effect=ExchangeError("orders down")
        )
        summary = await reconciler.restore_on_startup()
        assert any("get_open_orders" in e for e in summary.errors)

    @pytest.mark.asyncio
    async def test_get_fills_error_continues(self) -> None:
        reconciler, _, _, _ = build_reconciler(
            fills_side_effect=ExchangeError("fills down")
        )
        summary = await reconciler.restore_on_startup()
        assert any("get_fills" in e for e in summary.errors)

    @pytest.mark.asyncio
    async def test_get_open_trades_error_continues(self) -> None:
        reconciler, _, _, _ = build_reconciler(
            open_trades_side_effect=ExchangeError("db down")
        )
        summary = await reconciler.restore_on_startup()
        assert any("get_open_trades" in e for e in summary.errors)

    @pytest.mark.asyncio
    async def test_apply_action_error_recorded(self) -> None:
        # register_external が失敗 → errors に記録
        pos = make_position(symbol="ETH")
        reconciler, _, _, _ = build_reconciler(
            positions=(pos,),
            register_external_side_effect=RuntimeError("repo broken"),
        )
        summary = await reconciler.restore_on_startup()
        assert summary.actions_executed == 0
        assert any(
            "apply_action(REGISTER_EXTERNAL)" in e for e in summary.errors
        )

    @pytest.mark.asyncio
    async def test_completion_notification_failure_swallowed(self) -> None:
        # notifier 失敗でも全体は完走
        reconciler, _, _, _ = build_reconciler(
            notifier_side_effect=RuntimeError("discord down")
        )
        summary = await reconciler.restore_on_startup()
        # 例外は伝播せず summary が返る
        assert summary.hl_position_count == 0


# ─── 内部分岐（CORE 契約外のケース） ───


class TestApplyActionDispatch:
    @pytest.mark.asyncio
    async def test_unknown_action_type_logged_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        reconciler, _, _, _ = build_reconciler()
        action = ReconcileAction(
            type=ActionType.MANUAL_REVIEW,
            db_trade=DBTrade(
                trade_id=1,
                symbol="BTC",
                direction="LONG",
                size=Decimal("0.0002"),
                entry_price=Decimal("65000"),
            ),
        )
        # type を unknown 文字列に差し替え
        object.__setattr__(action, "type", "BOGUS")
        with caplog.at_level("WARNING"):
            await reconciler._apply_action(action, ())
        assert any("unknown action type" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_close_from_fill_with_no_matching_adapter_fill(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # CORE が CLOSE_FROM_FILL を返したが、adapter_fills に対応 Fill が無いケース。
        # 非マッチ fill を入れて _find_adapter_fill のループバック分岐も踏ませる。
        reconciler, _, repo, _ = build_reconciler()
        hl_fill = HLFill(
            symbol="BTC",
            side="sell",
            size=Decimal("0.0002"),
            price=Decimal("66000"),
            timestamp=123,
        )
        non_matching = make_fill(
            symbol="BTC",
            price=Decimal("66001"),  # 価格が違うのでマッチしない
        )
        action = ReconcileAction(
            type=ActionType.CLOSE_FROM_FILL,
            db_trade=DBTrade(
                trade_id=1,
                symbol="BTC",
                direction="LONG",
                size=Decimal("0.0002"),
                entry_price=Decimal("65000"),
            ),
            fill=hl_fill,
        )
        with caplog.at_level("WARNING"):
            await reconciler._apply_action(action, (non_matching,))
        repo.close_trade_from_fill.assert_not_awaited()
        assert any(
            "matching adapter fill not found" in r.message
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_reconcile_positions_exception_is_caught(self) -> None:
        # CORE 関数が予期せぬ例外を投げても、サマリーに errors として記録され全体は完走
        pos = make_position()
        trade = make_trade()
        reconciler, _, _, _ = build_reconciler(
            positions=(pos,), open_trades=(trade,)
        )
        with patch(
            "src.application.reconciliation.reconcile_positions",
            side_effect=RuntimeError("core broken"),
        ):
            summary = await reconciler.restore_on_startup()
        assert summary.actions_executed == 0
        assert any("reconcile_positions" in e for e in summary.errors)


# ─── 設定オブジェクト ────────────────


class TestSummaryShape:
    def test_summary_is_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        summary = ReconcileSummary(
            hl_position_count=0,
            db_open_trade_count=0,
            actions_executed=0,
            stale_orders_cancelled=0,
            errors=(),
        )
        with pytest.raises(FrozenInstanceError):
            summary.actions_executed = 1  # type: ignore[misc]
