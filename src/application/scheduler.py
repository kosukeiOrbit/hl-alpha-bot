"""APPLICATION 層: scheduler（章11.6・章19）。

entry_flow / position_monitor / reconciliation を時間軸で組み合わせる
メインループ。APPLICATION 層の最後のピース。

責務:
1. 起動時 reconciliation
2. メインループ（cycle_interval ごと）
   - サーキットブレーカーチェック（CORE 層 check_circuit_breaker）
   - PositionMonitor.run_cycle（ブレーカー時も実行・既存ポジ管理は止めない）
   - EntryFlow.evaluate_and_enter（ブレーカー時はスキップ）
   - 定期 reconciliation（5分ごと）
3. グレースフルシャットダウン（request_shutdown フラグ）

CircuitBreakerInput への入力で Phase 0 では取れない指標
(weekly_loss_pct, 1min/5min 価格変動, API error rate) は 0/空で埋める。
将来 PR で WS 監視や履歴トラッカーから供給する予定。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from src.adapters.exchange import ExchangeProtocol
from src.adapters.notifier import Notifier
from src.adapters.repository import Repository
from src.application.entry_flow import EntryFlow
from src.application.position_monitor import PositionMonitor
from src.application.reconciliation import StateReconciler
from src.core.circuit_breaker import (
    BreakerInput,
    BreakerResult,
    check_circuit_breaker,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SchedulerConfig:
    """scheduler 動作設定。"""

    watchlist: tuple[str, ...]
    directions: tuple[Literal["LONG", "SHORT"], ...]
    cycle_interval_seconds: float  # メインループ間隔（テストで小さくできるよう float）
    reconcile_interval_seconds: float  # 定期 reconciliation 間隔
    circuit_breaker_enabled: bool
    max_position_count: int
    # サーキットブレーカー閾値（章23 settings.yaml から）
    daily_loss_limit_pct: Decimal
    weekly_loss_limit_pct: Decimal
    consecutive_loss_limit: int
    flash_crash_threshold_pct: Decimal
    btc_anomaly_threshold_pct: Decimal
    api_error_rate_max: Decimal
    position_overflow_multiplier: Decimal


@dataclass(frozen=True)
class CycleStats:
    """1 サイクルの統計。"""

    timestamp: datetime
    monitor_filled: int
    monitor_closed: int
    monitor_forced_closes: int
    monitor_errors: int
    entry_attempts: int
    entry_executed: int
    entry_dryrun: int
    entry_errors: int
    circuit_breaker_active: bool
    circuit_breaker_reason: str | None
    duration_seconds: float


class Scheduler:
    """メインループ（章19）。

    run() は本番駆動。テストは run_cycle_once() で 1 サイクルだけ実行可能。
    """

    def __init__(
        self,
        exchange: ExchangeProtocol,
        repo: Repository,
        notifier: Notifier,
        entry_flow: EntryFlow,
        position_monitor: PositionMonitor,
        reconciler: StateReconciler,
        config: SchedulerConfig,
    ) -> None:
        self.exchange = exchange
        self.repo = repo
        self.notifier = notifier
        self.entry_flow = entry_flow
        self.position_monitor = position_monitor
        self.reconciler = reconciler
        self.config = config

        self._shutdown_requested = False
        self._started_at: datetime | None = None
        self._last_periodic_reconcile_at: datetime | None = None
        self._last_breaker_active = False

    # ─── 起動・停止 ─────────────────────────

    async def run(self) -> None:
        """メインループ実行（章19 起動シーケンス）。"""
        self._started_at = datetime.now(UTC)
        self._shutdown_requested = False

        await self._safe_call(
            self.reconciler.restore_on_startup,
            "startup_reconcile",
        )
        await self._safe_notify(
            "send_signal",
            f"BOT started: watchlist={list(self.config.watchlist)} "
            f"cycle={self.config.cycle_interval_seconds}s",
        )

        while not self._shutdown_requested:
            cycle_start = datetime.now(UTC)
            try:
                stats = await self.run_cycle_once()
                logger.info(
                    "cycle done filled=%d closed=%d "
                    "attempts=%d executed=%d dryrun=%d errors=%d "
                    "cb=%s duration=%.2fs",
                    stats.monitor_filled,
                    stats.monitor_closed,
                    stats.entry_attempts,
                    stats.entry_executed,
                    stats.entry_dryrun,
                    stats.entry_errors,
                    "active" if stats.circuit_breaker_active else "off",
                    stats.duration_seconds,
                )
            except Exception:
                logger.exception("cycle failed")
                await self._safe_notify(
                    "send_alert",
                    "unexpected exception in cycle (continuing next cycle)",
                    dedup_key="cycle_error",
                )

            elapsed = (datetime.now(UTC) - cycle_start).total_seconds()
            sleep_seconds = max(
                0.0, self.config.cycle_interval_seconds - elapsed
            )
            await self._wait_or_shutdown(sleep_seconds)

        await self._on_shutdown()

    def request_shutdown(self) -> None:
        """シャットダウン要求（外部の SIGTERM ハンドラから呼ばれる）。"""
        logger.info("shutdown requested")
        self._shutdown_requested = True

    async def _on_shutdown(self) -> None:
        logger.info("scheduler shutting down")
        await self._safe_notify("send_signal", "BOT stopped")

    async def _wait_or_shutdown(self, seconds: float) -> None:
        """seconds 秒待つ。shutdown フラグが立ったら早期終了。

        seconds=0 でも必ず一度 await asyncio.sleep(0) で制御を返す。
        これがないとタイトループになり shutdown 要求が入る隙間がない。
        """
        await asyncio.sleep(0)
        if seconds <= 0:
            return
        end = asyncio.get_event_loop().time() + seconds
        while True:
            if self._shutdown_requested:
                return
            remaining = end - asyncio.get_event_loop().time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(0.1, remaining))

    # ─── 1 サイクル ─────────────────────────

    async def run_cycle_once(self) -> CycleStats:
        """1 サイクル実行（テスト時にも呼べる単位）。"""
        cycle_start = datetime.now(UTC)

        breaker_result = await self._check_circuit_breaker()
        await self._notify_breaker_transition(breaker_result)

        # PositionMonitor はブレーカー有無に関係なく実行（既存ポジは管理続行）
        monitor_result = await self.position_monitor.run_cycle()

        entry_attempts = 0
        entry_executed = 0
        entry_dryrun = 0
        entry_errors = 0
        if not breaker_result.triggered:
            (
                entry_attempts,
                entry_executed,
                entry_dryrun,
                entry_errors,
            ) = await self._run_entry_flow_pass()

        await self._maybe_run_periodic_reconcile()

        duration = (datetime.now(UTC) - cycle_start).total_seconds()
        return CycleStats(
            timestamp=cycle_start,
            monitor_filled=monitor_result.trades_filled,
            monitor_closed=monitor_result.trades_closed,
            monitor_forced_closes=monitor_result.forced_closes,
            monitor_errors=len(monitor_result.errors),
            entry_attempts=entry_attempts,
            entry_executed=entry_executed,
            entry_dryrun=entry_dryrun,
            entry_errors=entry_errors,
            circuit_breaker_active=breaker_result.triggered,
            circuit_breaker_reason=(
                breaker_result.reason.value if breaker_result.reason else None
            ),
            duration_seconds=duration,
        )

    async def _run_entry_flow_pass(self) -> tuple[int, int, int, int]:
        """watchlist x directions の組み合わせで entry_flow を呼ぶ。

        Returns:
            (attempts, executed, dryrun, errors)
        """
        attempts = 0
        executed = 0
        dryrun = 0
        errors = 0
        for symbol in self.config.watchlist:
            for direction in self.config.directions:
                attempts += 1
                try:
                    attempt = await self.entry_flow.evaluate_and_enter(
                        symbol=symbol, direction=direction
                    )
                except Exception:
                    logger.exception(
                        "evaluate_and_enter failed: %s %s", symbol, direction
                    )
                    errors += 1
                    continue
                if attempt.executed:
                    executed += 1
                elif attempt.is_dry_run and attempt.decision.should_enter:
                    dryrun += 1
        return attempts, executed, dryrun, errors

    # ─── サーキットブレーカー（章9.7） ──

    async def _check_circuit_breaker(self) -> BreakerResult:
        """サーキットブレーカー判定（CORE check_circuit_breaker 経由）。

        入力構築失敗時は fail-open（triggered=False）として扱う。
        Phase 0 で取れない指標は 0/空で埋める。
        """
        if not self.config.circuit_breaker_enabled:
            return BreakerResult(triggered=False)
        try:
            inputs = await self._build_breaker_input()
        except Exception:
            logger.exception("circuit breaker input build failed")
            return BreakerResult(triggered=False)
        return check_circuit_breaker(inputs)

    async def _build_breaker_input(self) -> BreakerInput:
        """BreakerInput を組む。

        取れる指標:
        - balance: exchange.get_account_balance_usd
        - daily_pnl: repo.get_daily_pnl_usd
        - consecutive_losses: repo.get_consecutive_losses
        - position_count: exchange.get_positions
        Phase 0 で取れない指標は 0/空で埋める（後続 PR で WS 等から）。
        """
        balance = await self.exchange.get_account_balance_usd()
        positions = await self.exchange.get_positions()
        consecutive = await self.repo.get_consecutive_losses()
        today_utc = datetime.now(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        daily_pnl = await self.repo.get_daily_pnl_usd(today_utc)

        daily_loss_pct = (
            (daily_pnl / balance) * Decimal("100")
            if balance > 0
            else Decimal("0")
        )

        return BreakerInput(
            daily_loss_pct=daily_loss_pct,
            weekly_loss_pct=Decimal("0"),  # Phase 0 未対応
            consecutive_losses=consecutive,
            symbol_1min_changes_pct=(),  # Phase 0 未対応（WS trades 待ち）
            btc_5min_change_pct=Decimal("0"),  # 同上
            api_error_rate_5min=Decimal("0"),  # 同上（エラー追跡なし）
            position_count=len(positions),
            max_position_count=self.config.max_position_count,
            daily_loss_limit_pct=self.config.daily_loss_limit_pct,
            weekly_loss_limit_pct=self.config.weekly_loss_limit_pct,
            consecutive_loss_limit=self.config.consecutive_loss_limit,
            flash_crash_threshold_pct=self.config.flash_crash_threshold_pct,
            btc_anomaly_threshold_pct=self.config.btc_anomaly_threshold_pct,
            api_error_rate_max=self.config.api_error_rate_max,
            position_overflow_multiplier=(
                self.config.position_overflow_multiplier
            ),
        )

    async def _notify_breaker_transition(self, result: BreakerResult) -> None:
        """ブレーカー状態の遷移時のみ通知。"""
        if result.triggered == self._last_breaker_active:
            return
        if result.triggered:
            reason = result.reason.value if result.reason else "unknown"
            await self._safe_notify(
                "send_alert",
                f"circuit breaker activated: {reason}",
                dedup_key=f"cb_active:{reason}",
            )
        else:
            await self._safe_notify(
                "send_signal",
                "circuit breaker cleared",
                dedup_key="cb_clear",
            )
        self._last_breaker_active = result.triggered

    # ─── 定期 reconciliation（章9.6） ──

    async def _maybe_run_periodic_reconcile(self) -> None:
        """5 分ごとに定期 reconcile。最初は started_at から計測。"""
        now_utc = datetime.now(UTC)
        if self._last_periodic_reconcile_at is None:
            self._last_periodic_reconcile_at = self._started_at or now_utc
            return
        elapsed = (
            now_utc - self._last_periodic_reconcile_at
        ).total_seconds()
        if elapsed < self.config.reconcile_interval_seconds:
            return
        try:
            await self.reconciler.run_periodic_check()
        except Exception:
            logger.exception("periodic reconciliation failed")
        self._last_periodic_reconcile_at = now_utc

    # ─── ヘルパー ──────────────────────────

    async def _safe_call(
        self,
        func: Callable[[], Awaitable[object]],
        step_name: str,
    ) -> None:
        try:
            await func()
        except Exception:
            logger.exception("%s failed", step_name)
            await self._safe_notify(
                "send_alert",
                f"{step_name} failed (continuing)",
                dedup_key=f"step_fail:{step_name}",
            )

    async def _safe_notify(
        self,
        method_name: str,
        message: str,
        *,
        dedup_key: str | None = None,
        exception: Exception | None = None,
    ) -> None:
        """通知失敗で全体を落とさない。

        method_name に応じて受け付ける kwarg だけを通すよう振り分ける
        （Notifier Protocol で send_summary は dedup_key を持たず、
        send_error だけが exception を持つため）。
        """
        try:
            method = getattr(self.notifier, method_name)
            kwargs: dict[str, object] = {}
            if method_name in ("send_signal", "send_alert"):
                kwargs["dedup_key"] = dedup_key
            elif method_name == "send_error":
                kwargs["exception"] = exception
            await method(message, **kwargs)
        except Exception:
            logger.exception("notification failed: %s", method_name)
