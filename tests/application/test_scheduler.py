"""APPLICATION 層 scheduler のテスト。"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.application.entry_flow import EntryAttempt
from src.application.position_monitor import MonitorCycleResult
from src.application.reconciliation import ReconcileSummary
from src.application.scheduler import (
    Scheduler,
    SchedulerConfig,
)
from src.core.circuit_breaker import BreakReason
from src.core.models import EntryDecision


def make_config(**overrides: Any) -> SchedulerConfig:
    base: dict[str, Any] = {
        "watchlist": ("BTC", "ETH"),
        "directions": ("LONG",),
        "cycle_interval_seconds": 0.05,
        "reconcile_interval_seconds": 300.0,
        "circuit_breaker_enabled": False,
        "max_position_count": 5,
        "daily_loss_limit_pct": Decimal("3.0"),
        "weekly_loss_limit_pct": Decimal("8.0"),
        "consecutive_loss_limit": 3,
        "flash_crash_threshold_pct": Decimal("5.0"),
        "btc_anomaly_threshold_pct": Decimal("3.0"),
        "api_error_rate_max": Decimal("0.30"),
        "position_overflow_multiplier": Decimal("1.5"),
    }
    base.update(overrides)
    return SchedulerConfig(**base)


def make_attempt(
    *,
    symbol: str = "BTC",
    direction: str = "LONG",
    executed: bool = False,
    is_dry_run: bool = False,
    should_enter: bool = False,
) -> EntryAttempt:
    decision = EntryDecision(
        should_enter=should_enter,
        direction=direction if should_enter else None,
        rejection_reason=None if should_enter else "test_skip",
        layer_results={
            "momentum": True,
            "flow": True,
            "regime": True,
            "sentiment": True,
        },
    )
    return EntryAttempt(
        symbol=symbol,
        direction=direction,  # type: ignore[arg-type]
        decision=decision,
        executed=executed,
        is_dry_run=is_dry_run,
        trade_id=42 if executed else None,
        rejected_reason=None if (executed or should_enter) else "test_skip",
        snapshot=MagicMock(),
    )


def make_monitor_result(
    *,
    filled: int = 0,
    closed: int = 0,
    forced: int = 0,
    errors: tuple[str, ...] = (),
) -> MonitorCycleResult:
    return MonitorCycleResult(
        trades_filled=filled,
        trades_closed=closed,
        open_position_count=0,
        forced_closes=forced,
        errors=errors,
    )


def make_summary() -> ReconcileSummary:
    return ReconcileSummary(
        hl_position_count=0,
        db_open_trade_count=0,
        actions_executed=0,
        stale_orders_cancelled=0,
        errors=(),
    )


def build_scheduler(
    *,
    config: SchedulerConfig | None = None,
    monitor_result: MonitorCycleResult | None = None,
    monitor_side_effect: BaseException | None = None,
    entry_attempt_factory: Any | None = None,
    entry_side_effect: BaseException | None = None,
    balance: Decimal = Decimal("1000"),
    daily_pnl: Decimal = Decimal("0"),
    consecutive_losses: int = 0,
    positions: tuple[Any, ...] = (),
    balance_side_effect: BaseException | None = None,
    restore_side_effect: BaseException | None = None,
    periodic_side_effect: BaseException | None = None,
    notifier_side_effect: BaseException | None = None,
) -> tuple[Scheduler, Any, Any, Any, Any, Any, Any]:
    exchange = AsyncMock()
    if balance_side_effect is not None:
        exchange.get_account_balance_usd = AsyncMock(
            side_effect=balance_side_effect
        )
    else:
        exchange.get_account_balance_usd = AsyncMock(return_value=balance)
    exchange.get_positions = AsyncMock(return_value=positions)

    repo = AsyncMock()
    repo.get_consecutive_losses = AsyncMock(return_value=consecutive_losses)
    repo.get_daily_pnl_usd = AsyncMock(return_value=daily_pnl)

    notifier = AsyncMock()
    if notifier_side_effect is not None:
        notifier.send_signal = AsyncMock(side_effect=notifier_side_effect)
        notifier.send_alert = AsyncMock(side_effect=notifier_side_effect)

    entry_flow = AsyncMock()
    if entry_side_effect is not None:
        entry_flow.evaluate_and_enter = AsyncMock(
            side_effect=entry_side_effect
        )
    elif entry_attempt_factory is not None:
        entry_flow.evaluate_and_enter = AsyncMock(
            side_effect=entry_attempt_factory
        )
    else:
        entry_flow.evaluate_and_enter = AsyncMock(
            side_effect=lambda symbol, direction: make_attempt(
                symbol=symbol, direction=direction
            )
        )

    position_monitor = AsyncMock()
    if monitor_side_effect is not None:
        position_monitor.run_cycle = AsyncMock(side_effect=monitor_side_effect)
    else:
        position_monitor.run_cycle = AsyncMock(
            return_value=monitor_result or make_monitor_result()
        )

    reconciler = AsyncMock()
    if restore_side_effect is not None:
        reconciler.restore_on_startup = AsyncMock(
            side_effect=restore_side_effect
        )
    else:
        reconciler.restore_on_startup = AsyncMock(return_value=make_summary())
    if periodic_side_effect is not None:
        reconciler.run_periodic_check = AsyncMock(
            side_effect=periodic_side_effect
        )
    else:
        reconciler.run_periodic_check = AsyncMock(return_value=make_summary())

    scheduler = Scheduler(
        exchange=exchange,
        repo=repo,
        notifier=notifier,
        entry_flow=entry_flow,
        position_monitor=position_monitor,
        reconciler=reconciler,
        config=config or make_config(),
    )
    return (
        scheduler,
        exchange,
        repo,
        notifier,
        entry_flow,
        position_monitor,
        reconciler,
    )


# ─── 1 サイクル ─────────────────────────


class TestRunCycleOnce:
    @pytest.mark.asyncio
    async def test_calls_monitor_and_entry_flow(self) -> None:
        scheduler, _, _, _, entry_flow, monitor, _ = build_scheduler()
        stats = await scheduler.run_cycle_once()
        monitor.run_cycle.assert_awaited_once()
        # watchlist=2 x directions=("LONG",) → 2 calls
        assert entry_flow.evaluate_and_enter.await_count == 2
        assert stats.entry_attempts == 2
        assert stats.circuit_breaker_active is False

    @pytest.mark.asyncio
    async def test_executed_attempt_increments_executed(self) -> None:
        scheduler, _, _, _, _, _, _ = build_scheduler(
            entry_attempt_factory=lambda symbol, direction: make_attempt(
                symbol=symbol, direction=direction,
                executed=True, should_enter=True,
            ),
        )
        stats = await scheduler.run_cycle_once()
        assert stats.entry_executed == 2

    @pytest.mark.asyncio
    async def test_dryrun_passing_attempt_increments_dryrun(self) -> None:
        scheduler, _, _, _, _, _, _ = build_scheduler(
            entry_attempt_factory=lambda symbol, direction: make_attempt(
                symbol=symbol, direction=direction,
                executed=False, is_dry_run=True, should_enter=True,
            ),
        )
        stats = await scheduler.run_cycle_once()
        assert stats.entry_dryrun == 2
        assert stats.entry_executed == 0

    @pytest.mark.asyncio
    async def test_entry_exception_recorded_continues(self) -> None:
        scheduler, _, _, _, entry_flow, _, _ = build_scheduler(
            entry_side_effect=RuntimeError("boom"),
        )
        stats = await scheduler.run_cycle_once()
        assert stats.entry_errors == 2
        assert stats.entry_executed == 0
        # 例外があっても全 watchlist x directions が試行される
        assert entry_flow.evaluate_and_enter.await_count == 2

    @pytest.mark.asyncio
    async def test_monitor_filled_propagated_to_stats(self) -> None:
        scheduler, _, _, _, _, _, _ = build_scheduler(
            monitor_result=make_monitor_result(
                filled=2, closed=1, forced=1, errors=("e1", "e2")
            ),
        )
        stats = await scheduler.run_cycle_once()
        assert stats.monitor_filled == 2
        assert stats.monitor_closed == 1
        assert stats.monitor_forced_closes == 1
        assert stats.monitor_errors == 2


# ─── サーキットブレーカー ──────────────


class TestCircuitBreaker:
    @pytest.mark.asyncio
    async def test_disabled_skips_check(self) -> None:
        scheduler, exchange, repo, _, _, _, _ = build_scheduler(
            config=make_config(circuit_breaker_enabled=False),
        )
        stats = await scheduler.run_cycle_once()
        assert stats.circuit_breaker_active is False
        # cb 無効なら balance/pnl 取得もしない
        exchange.get_account_balance_usd.assert_not_awaited()
        repo.get_consecutive_losses.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_active_skips_entry_flow(self) -> None:
        # 連敗 4 回で CONSECUTIVE_LOSS 発動
        scheduler, _, _, _, entry_flow, monitor, _ = build_scheduler(
            config=make_config(
                circuit_breaker_enabled=True, consecutive_loss_limit=3
            ),
            consecutive_losses=4,
        )
        stats = await scheduler.run_cycle_once()
        assert stats.circuit_breaker_active is True
        assert stats.circuit_breaker_reason == BreakReason.CONSECUTIVE_LOSS
        # Monitor は実行される
        monitor.run_cycle.assert_awaited_once()
        # Entry はスキップ
        entry_flow.evaluate_and_enter.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_state_change_to_active_sends_alert(self) -> None:
        scheduler, _, _, notifier, _, _, _ = build_scheduler(
            config=make_config(
                circuit_breaker_enabled=True, consecutive_loss_limit=3
            ),
            consecutive_losses=4,
        )
        await scheduler.run_cycle_once()
        notifier.send_alert.assert_awaited()
        msg = notifier.send_alert.await_args.args[0]
        assert "CONSECUTIVE_LOSS" in msg

    @pytest.mark.asyncio
    async def test_state_change_to_clear_sends_signal(self) -> None:
        scheduler, _, _, notifier, _, _, _ = build_scheduler(
            config=make_config(
                circuit_breaker_enabled=True, consecutive_loss_limit=3
            ),
            consecutive_losses=4,
        )
        # 1 サイクル目: 発動
        await scheduler.run_cycle_once()
        notifier.send_signal.reset_mock()
        # 2 サイクル目: 連敗を 0 に戻す → 解除
        scheduler.repo.get_consecutive_losses = AsyncMock(return_value=0)
        await scheduler.run_cycle_once()
        notifier.send_signal.assert_awaited()
        msg = notifier.send_signal.await_args.args[0]
        assert "cleared" in msg.lower()

    @pytest.mark.asyncio
    async def test_no_transition_no_notification(self) -> None:
        scheduler, _, _, notifier, _, _, _ = build_scheduler(
            config=make_config(
                circuit_breaker_enabled=True, consecutive_loss_limit=3
            ),
            consecutive_losses=4,
        )
        await scheduler.run_cycle_once()
        notifier.send_alert.reset_mock()
        # 同じ状態で 2 回目 → 通知なし
        await scheduler.run_cycle_once()
        notifier.send_alert.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_input_build_failure_treats_as_inactive(self) -> None:
        scheduler, _, _, _, entry_flow, _, _ = build_scheduler(
            config=make_config(circuit_breaker_enabled=True),
            balance_side_effect=RuntimeError("api down"),
        )
        stats = await scheduler.run_cycle_once()
        # fail-open: ブレーカー非active として扱う
        assert stats.circuit_breaker_active is False
        entry_flow.evaluate_and_enter.assert_awaited()

    @pytest.mark.asyncio
    async def test_zero_balance_skips_loss_pct_calc(self) -> None:
        # balance=0 でもブレーカー判定が壊れない（daily_loss_pct=0 で扱う）
        scheduler, _, _, _, _, _, _ = build_scheduler(
            config=make_config(circuit_breaker_enabled=True),
            balance=Decimal("0"),
            daily_pnl=Decimal("-10"),
        )
        stats = await scheduler.run_cycle_once()
        assert stats.circuit_breaker_active is False


# ─── 定期 reconciliation ───────────────


class TestPeriodicReconcile:
    @pytest.mark.asyncio
    async def test_first_call_initializes_only(self) -> None:
        # _started_at が None でも初回は last_periodic_reconcile_at をセットするだけ
        scheduler, _, _, _, _, _, reconciler = build_scheduler(
            config=make_config(reconcile_interval_seconds=0.01),
        )
        await scheduler.run_cycle_once()
        # 1 サイクル目: 走らない（初期化のみ）
        reconciler.run_periodic_check.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_runs_after_interval(self) -> None:
        scheduler, _, _, _, _, _, reconciler = build_scheduler(
            config=make_config(reconcile_interval_seconds=0.01),
        )
        await scheduler.run_cycle_once()  # 初期化
        await asyncio.sleep(0.02)  # interval 超
        await scheduler.run_cycle_once()
        reconciler.run_periodic_check.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skipped_within_interval(self) -> None:
        scheduler, _, _, _, _, _, reconciler = build_scheduler(
            config=make_config(reconcile_interval_seconds=300.0),
        )
        await scheduler.run_cycle_once()  # 初期化
        await scheduler.run_cycle_once()  # interval 内
        reconciler.run_periodic_check.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failure_does_not_break_cycle(self) -> None:
        scheduler, _, _, _, _, _, reconciler = build_scheduler(
            config=make_config(reconcile_interval_seconds=0.01),
            periodic_side_effect=RuntimeError("repo broken"),
        )
        await scheduler.run_cycle_once()
        await asyncio.sleep(0.02)
        # reconcile 失敗しても run_cycle_once 例外にならない
        stats = await scheduler.run_cycle_once()
        assert stats is not None
        reconciler.run_periodic_check.assert_awaited_once()


# ─── run / shutdown ───────────────────


class TestRunAndShutdown:
    @pytest.mark.asyncio
    async def test_run_calls_restore_on_startup(self) -> None:
        scheduler, _, _, _, _, _, reconciler = build_scheduler(
            config=make_config(cycle_interval_seconds=0.05),
        )

        async def shutdown_soon() -> None:
            await asyncio.sleep(0.05)
            scheduler.request_shutdown()

        await asyncio.gather(scheduler.run(), shutdown_soon())
        reconciler.restore_on_startup.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_run_loops_until_shutdown(self) -> None:
        scheduler, _, _, _, _, monitor, _ = build_scheduler(
            config=make_config(cycle_interval_seconds=0.05),
        )

        async def shutdown_soon() -> None:
            await asyncio.sleep(0.15)
            scheduler.request_shutdown()

        await asyncio.gather(scheduler.run(), shutdown_soon())
        # 少なくとも 2 サイクル走る
        assert monitor.run_cycle.await_count >= 2

    @pytest.mark.asyncio
    async def test_shutdown_signal_sent(self) -> None:
        scheduler, _, _, notifier, _, _, _ = build_scheduler(
            config=make_config(cycle_interval_seconds=0.05),
        )

        async def shutdown_soon() -> None:
            await asyncio.sleep(0.05)
            scheduler.request_shutdown()

        await asyncio.gather(scheduler.run(), shutdown_soon())
        signals = [c.args[0] for c in notifier.send_signal.await_args_list]
        # 起動 + 停止
        assert any("started" in s for s in signals)
        assert any("stopped" in s for s in signals)

    @pytest.mark.asyncio
    async def test_run_startup_reconcile_failure_continues(self) -> None:
        scheduler, _, _, notifier, _, _, _ = build_scheduler(
            config=make_config(cycle_interval_seconds=0.05),
            restore_side_effect=RuntimeError("api down"),
        )

        async def shutdown_soon() -> None:
            await asyncio.sleep(0.05)
            scheduler.request_shutdown()

        await asyncio.gather(scheduler.run(), shutdown_soon())
        # alert が送信される（startup_reconcile failed）
        alerts = [c.args[0] for c in notifier.send_alert.await_args_list]
        assert any("startup_reconcile" in a for a in alerts)

    @pytest.mark.asyncio
    async def test_run_cycle_exception_caught(self) -> None:
        scheduler, _, _, notifier, _, _, _ = build_scheduler(
            config=make_config(cycle_interval_seconds=0.05),
            monitor_side_effect=RuntimeError("monitor exploded"),
        )

        async def shutdown_soon() -> None:
            await asyncio.sleep(0.05)
            scheduler.request_shutdown()

        await asyncio.gather(scheduler.run(), shutdown_soon())
        alerts = [c.args[0] for c in notifier.send_alert.await_args_list]
        assert any("unexpected exception" in a for a in alerts)

    def test_request_shutdown_sets_flag(self) -> None:
        scheduler, _, _, _, _, _, _ = build_scheduler()
        assert scheduler._shutdown_requested is False
        scheduler.request_shutdown()
        assert scheduler._shutdown_requested is True

    @pytest.mark.asyncio
    async def test_wait_or_shutdown_zero_returns_immediately(self) -> None:
        scheduler, _, _, _, _, _, _ = build_scheduler()
        await scheduler._wait_or_shutdown(0)  # negative branch

    @pytest.mark.asyncio
    async def test_wait_or_shutdown_exits_on_shutdown(self) -> None:
        scheduler, _, _, _, _, _, _ = build_scheduler()

        async def trigger() -> None:
            await asyncio.sleep(0.05)
            scheduler.request_shutdown()

        # 大きい seconds でも shutdown フラグで早期終了する
        await asyncio.gather(
            scheduler._wait_or_shutdown(10.0),
            trigger(),
        )

    @pytest.mark.asyncio
    async def test_long_cycle_skips_sleep(self) -> None:
        # cycle が interval より長くかかった場合 sleep=0 で即次サイクル
        # (run を 1 サイクル強だけ回して終了)
        scheduler, _, _, _, _, _, _ = build_scheduler(
            config=make_config(cycle_interval_seconds=0.0),
        )

        async def shutdown_soon() -> None:
            await asyncio.sleep(0.05)
            scheduler.request_shutdown()

        await asyncio.gather(scheduler.run(), shutdown_soon())


# ─── 通知失敗 ──────────────────────────


class TestNotificationFailure:
    @pytest.mark.asyncio
    async def test_safe_notify_swallows_exception(self) -> None:
        scheduler, _, _, _, _, _, _ = build_scheduler(
            notifier_side_effect=RuntimeError("discord down"),
        )
        # 例外が伝播しないことを確認
        await scheduler._safe_notify("send_signal", "test message")


# ─── 結果オブジェクト ─────────────────


class TestStatsShape:
    def test_cycle_stats_is_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        from src.application.scheduler import CycleStats

        stats = CycleStats(
            timestamp=__import__(
                "datetime"
            ).datetime.now(__import__("datetime").UTC),
            monitor_filled=0,
            monitor_closed=0,
            monitor_forced_closes=0,
            monitor_errors=0,
            entry_attempts=0,
            entry_executed=0,
            entry_dryrun=0,
            entry_errors=0,
            circuit_breaker_active=False,
            circuit_breaker_reason=None,
            duration_seconds=0.0,
        )
        with pytest.raises(FrozenInstanceError):
            stats.entry_executed = 5  # type: ignore[misc]
