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
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from src.adapters.exchange import ExchangeError, ExchangeProtocol
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

# CircuitBreaker 用ローソク足取得設定（章9.7 Layer 4/5）
_LAYER4_INTERVAL = "1m"
_LAYER4_BARS = 2  # 直近 2 本で 1 本ぶんの変化率を取る
_LAYER5_SYMBOL = "BTC"
_LAYER5_INTERVAL = "5m"
_LAYER5_BARS = 2
# api_error_rate の rolling window（秒）と最小サンプル数
_API_TRACKER_WINDOW_SECONDS = 300
_API_TRACKER_MIN_SAMPLES = 10


@dataclass
class ApiCallTracker:
    """cycle 単位の成功/失敗を rolling window で記録（章9.7 Layer 6）。

    BOT のメインループ 1 cycle ぶんが「1 API call の単位」。
    cycle 内のどこかで API が落ちれば失敗、最後まで完走すれば成功。
    粒度は粗いが、API 全体の不調を観測するには十分。

    再起動でリセットされる（in-memory のみ）。
    """

    _records: list[tuple[float, bool]] = field(default_factory=list)
    window_seconds: int = _API_TRACKER_WINDOW_SECONDS
    min_samples: int = _API_TRACKER_MIN_SAMPLES

    def record_success(self) -> None:
        self._records.append((time.time(), True))
        self._prune()

    def record_failure(self) -> None:
        self._records.append((time.time(), False))
        self._prune()

    def _prune(self) -> None:
        cutoff = time.time() - self.window_seconds
        self._records = [r for r in self._records if r[0] >= cutoff]

    def error_rate(self) -> Decimal:
        """ ``min_samples`` 未満なら 0 を返す（サンプル不足を発火扱いしない）。"""
        self._prune()
        if len(self._records) < self.min_samples:
            return Decimal("0")
        failures = sum(1 for _, ok in self._records if not ok)
        return Decimal(failures) / Decimal(len(self._records))


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
        self._api_tracker = ApiCallTracker()

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
                self._api_tracker.record_success()
            except Exception:
                logger.exception("cycle failed")
                self._api_tracker.record_failure()
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
        """BreakerInput を実値で組む（章9.7 全 7 Layer 対応）。

        PR7.4-real で placeholder（weekly_loss_pct=0 / 1m/5m=0 / api_err=0）を
        すべて実値に置き換え。各 _compute_* は個別に try/except し、エラー時は
        安全側のデフォルト（0 / 空 tuple）を返すので、1 つの指標取得失敗で
        他の Layer の判定が無効化されることはない。
        """
        balance = await self.exchange.get_account_balance_usd()
        now_utc = datetime.now(UTC)
        today_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        week_ago = now_utc - timedelta(days=7)

        # 独立した取得は並行化（cycle 時間短縮）
        (
            positions,
            consecutive,
            daily_pnl,
            weekly_pnl,
            symbol_changes,
            btc_5min_change,
        ) = await asyncio.gather(
            self.exchange.get_positions(),
            self.repo.get_consecutive_losses(),
            self.repo.get_daily_pnl_usd(today_utc),
            self.repo.get_pnl_since(week_ago),
            self._compute_symbol_1min_changes_pct(self.config.watchlist),
            self._compute_btc_5min_change_pct(),
        )

        daily_loss_pct = (
            (daily_pnl / balance) * Decimal("100")
            if balance > 0
            else Decimal("0")
        )
        weekly_loss_pct = (
            (weekly_pnl / balance) * Decimal("100")
            if balance > 0
            else Decimal("0")
        )

        return BreakerInput(
            daily_loss_pct=daily_loss_pct,
            weekly_loss_pct=weekly_loss_pct,
            consecutive_losses=consecutive,
            symbol_1min_changes_pct=symbol_changes,
            btc_5min_change_pct=btc_5min_change,
            api_error_rate_5min=self._api_tracker.error_rate(),
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

    async def _compute_symbol_1min_changes_pct(
        self, symbols: tuple[str, ...]
    ) -> tuple[tuple[str, Decimal], ...]:
        """各銘柄の直近 1m 足の close 変化率（章9.7 Layer 4 FLASH_CRASH）。

        2 本のローソク足を取得し ``(curr - prev) / prev * 100`` を計算。
        symbol 単位で try/except し、失敗銘柄は結果から除外する。
        """
        results: list[tuple[str, Decimal]] = []
        for symbol in symbols:
            try:
                candles = await self.exchange.get_candles(
                    symbol, _LAYER4_INTERVAL, _LAYER4_BARS
                )
            except ExchangeError as e:
                logger.warning(
                    "1m change fetch failed for %s: %s, skipping", symbol, e
                )
                continue
            if len(candles) < 2:
                continue
            prev = candles[-2].close
            curr = candles[-1].close
            if prev == 0:
                continue
            results.append((symbol, (curr - prev) / prev * Decimal("100")))
        return tuple(results)

    async def _compute_btc_5min_change_pct(self) -> Decimal:
        """BTC 直近 5m 足の close 変化率（章9.7 Layer 5 BTC_ANOMALY）。

        取得失敗時は 0 を返す（安全側: ブレーカーは発動しない）。
        """
        try:
            candles = await self.exchange.get_candles(
                _LAYER5_SYMBOL, _LAYER5_INTERVAL, _LAYER5_BARS
            )
        except ExchangeError as e:
            logger.warning("BTC 5m change fetch failed: %s, using 0", e)
            return Decimal("0")
        if len(candles) < 2:
            return Decimal("0")
        prev = candles[-2].close
        curr = candles[-1].close
        if prev == 0:
            return Decimal("0")
        return (curr - prev) / prev * Decimal("100")

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
