"""hl-alpha-bot エントリーポイント（章11・19）。

依存組み立て + scheduler 起動。Phase 0 観察モードで動かすための
最小限の構成。設定値はすべて config/settings.yaml から。

build_scheduler / setup_logging / install_signal_handlers は単体で
テスト可能にしている（main / async_main は実機駆動なので no-cover）。
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

# プロジェクトルートを sys.path に（config/ パッケージ参照のため）
# 直接実行時のみ働く分岐。pytest 経由ではすでに root が入っている。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:  # pragma: no cover
    sys.path.insert(0, str(_PROJECT_ROOT))

from config.schema import AppSettings  # noqa: E402
from src.adapters.exchange import ExchangeProtocol  # noqa: E402
from src.adapters.notifier import Notifier  # noqa: E402
from src.adapters.sentiment import SentimentProvider  # noqa: E402
from src.application.entry_flow import (  # noqa: E402
    EntryFlow,
    EntryFlowConfig,
)
from src.application.position_monitor import (  # noqa: E402
    PositionMonitor,
    PositionMonitorConfig,
)
from src.application.reconciliation import (  # noqa: E402
    ReconciliationConfig,
    StateReconciler,
)
from src.application.scheduler import (  # noqa: E402
    Scheduler,
    SchedulerConfig,
)
from src.core.config_loader import load_settings  # noqa: E402
from src.infrastructure.console_notifier import ConsoleNotifier  # noqa: E402
from src.infrastructure.discord_notifier import (  # noqa: E402
    DiscordNotifier,
    DiscordNotifierConfig,
)
from src.infrastructure.fixed_sentiment_provider import (  # noqa: E402
    FixedSentimentProvider,
)
from src.infrastructure.funding_rate_sentiment_provider import (  # noqa: E402
    FundingRateSentimentConfig,
    FundingRateSentimentProvider,
)
from src.infrastructure.hyperliquid_client import (  # noqa: E402
    HyperLiquidClient,
)
from src.infrastructure.secrets_loader import load_secrets  # noqa: E402
from src.infrastructure.sqlite_repository import SQLiteRepository  # noqa: E402

logger = logging.getLogger(__name__)


def setup_logging(settings: AppSettings) -> None:
    """ロギング設定（章26）。

    既存ハンドラを除去してから TimedRotatingFileHandler + stdout を
    付け直す。複数回呼んでも安全（テストや再起動シナリオ用）。
    """
    log_path = Path(settings.logging.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    formatter = logging.Formatter(fmt)

    file_handler = TimedRotatingFileHandler(
        filename=str(log_path),
        when=settings.logging.rotation_when,
        backupCount=settings.logging.rotation_backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    root_logger.setLevel(settings.logging.level)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(stream_handler)


def _build_notifier(secrets: Any) -> Notifier:
    """secrets.discord が完備なら DiscordNotifier、そうでなければ ConsoleNotifier。

    Discord 設定無しでも起動できるよう後方互換を保つ。
    """
    discord = getattr(secrets, "discord", None)
    if discord is not None and discord.is_configured:
        logger.info("using DiscordNotifier")
        return DiscordNotifier(
            DiscordNotifierConfig(
                webhook_signal=discord.webhook_signal,
                webhook_alert=discord.webhook_alert,
                webhook_summary=discord.webhook_summary,
                webhook_error=discord.webhook_error,
            )
        )
    logger.info("using ConsoleNotifier (no Discord webhooks configured)")
    return ConsoleNotifier(use_logging=True)


def _build_sentiment_provider(
    settings: AppSettings, exchange: ExchangeProtocol
) -> SentimentProvider:
    """settings.sentiment.provider に応じて SentimentProvider を構築。

    "funding_rate" の場合は HL Funding Rate ベース、それ以外（"fixed" 既定）は
    FixedSentimentProvider。Phase 0 は fixed、Phase 1 以降で funding_rate に
    切替（profile_phase1.yaml で設定）。
    """
    if settings.sentiment.provider == "funding_rate":
        logger.info("using FundingRateSentimentProvider")
        return FundingRateSentimentProvider(
            exchange,
            FundingRateSentimentConfig(
                scale_factor=settings.sentiment.funding_scale_factor,
                cache_window_seconds=(
                    settings.sentiment.funding_cache_window_seconds
                ),
                confidence=settings.sentiment.funding_confidence,
            ),
        )
    logger.info("using FixedSentimentProvider")
    return FixedSentimentProvider(
        score=settings.sentiment.fixed_score,
        confidence=settings.sentiment.fixed_confidence,
        reasoning=settings.sentiment.reasoning,
    )


def build_scheduler(
    settings: AppSettings, secrets: Any
) -> tuple[Scheduler, SQLiteRepository]:
    """全コンポーネントを組み立てる。

    Returns:
        (scheduler, repo): repo は呼び出し側で initialize / close する。
    """
    exchange = HyperLiquidClient(
        network=settings.exchange.network,
        address=secrets.master_address,
        agent_private_key=secrets.agent_private_key,
    )
    repo = SQLiteRepository(settings.storage.db_path)
    notifier = _build_notifier(secrets)
    sentiment = _build_sentiment_provider(settings, exchange)

    entry_flow = EntryFlow(
        exchange=exchange,
        sentiment=sentiment,
        repo=repo,
        notifier=notifier,
        config=EntryFlowConfig(
            is_dry_run=settings.trading.is_dry_run,
            leverage=settings.trading.leverage,
            flow_layer_enabled=settings.trading.flow_layer_enabled,
            position_size_pct=settings.trading.position_size_pct,
            sl_atr_mult=settings.trading.sl_atr_mult,
            tp_atr_mult=settings.trading.tp_atr_mult,
            oi_lookup_tolerance_minutes=(
                settings.entry_flow.oi_lookup_tolerance_minutes
            ),
            momentum_vwap_min_distance_pct=(
                settings.momentum.vwap_min_distance_pct
            ),
            momentum_vwap_max_distance_pct=(
                settings.momentum.vwap_max_distance_pct
            ),
        ),
    )

    position_monitor = PositionMonitor(
        exchange=exchange,
        repo=repo,
        notifier=notifier,
        config=PositionMonitorConfig(
            funding_close_minutes_before=(
                settings.position_monitor.funding_close_minutes_before
            ),
            funding_close_enabled=(
                settings.position_monitor.funding_close_enabled
            ),
            fills_lookback_seconds=(
                settings.position_monitor.fills_lookback_seconds
            ),
            force_close_slippage_tolerance_pct=(
                settings.position_monitor.force_close_slippage_tolerance_pct
            ),
        ),
    )

    reconciler = StateReconciler(
        exchange=exchange,
        repo=repo,
        notifier=notifier,
        config=ReconciliationConfig(
            fills_lookback_hours=settings.reconciliation.fills_lookback_hours,
            stale_order_cleanup_seconds=(
                settings.reconciliation.stale_order_cleanup_seconds
            ),
        ),
    )

    scheduler = Scheduler(
        exchange=exchange,
        repo=repo,
        notifier=notifier,
        entry_flow=entry_flow,
        position_monitor=position_monitor,
        reconciler=reconciler,
        config=SchedulerConfig(
            watchlist=settings.watchlist.fixed,
            directions=settings.watchlist.directions,
            cycle_interval_seconds=(
                settings.scheduler.cycle_interval_seconds
            ),
            reconcile_interval_seconds=(
                settings.scheduler.reconcile_interval_seconds
            ),
            circuit_breaker_enabled=(
                settings.scheduler.circuit_breaker_enabled
            ),
            max_position_count=settings.scheduler.max_position_count,
            daily_loss_limit_pct=settings.scheduler.daily_loss_limit_pct,
            weekly_loss_limit_pct=(
                settings.scheduler.weekly_loss_limit_pct
            ),
            consecutive_loss_limit=(
                settings.scheduler.consecutive_loss_limit
            ),
            flash_crash_threshold_pct=(
                settings.scheduler.flash_crash_threshold_pct
            ),
            btc_anomaly_threshold_pct=(
                settings.scheduler.btc_anomaly_threshold_pct
            ),
            api_error_rate_max=settings.scheduler.api_error_rate_max,
            position_overflow_multiplier=(
                settings.scheduler.position_overflow_multiplier
            ),
        ),
    )
    return scheduler, repo


def install_signal_handlers(scheduler: Scheduler) -> None:
    """SIGTERM / SIGINT で graceful shutdown を要求するハンドラを登録。

    Windows では SIGTERM が無いので SIGINT のみ。
    """

    def _handler(signum: int, _frame: object) -> None:
        sig_name = signal.Signals(signum).name
        logger.info("received %s, requesting shutdown", sig_name)
        scheduler.request_shutdown()

    signal.signal(signal.SIGINT, _handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handler)


async def async_main(  # pragma: no cover
    settings_path: str = "config/settings.yaml",
    profile_path: str | None = None,
) -> None:
    """非同期メイン（実機駆動・本体テスト対象外）。"""
    settings = load_settings(settings_path, profile_path)
    setup_logging(settings)
    logger.info("hl-alpha-bot starting (phase=%s)", settings.phase)
    secrets = load_secrets()
    scheduler, repo = build_scheduler(settings, secrets)
    await repo.initialize()
    install_signal_handlers(scheduler)
    try:
        await scheduler.run()
    finally:
        await repo.close()
        # DiscordNotifier 等 close を持つ notifier は閉じる（duck typing）
        notifier = scheduler.notifier
        if hasattr(notifier, "close"):
            await notifier.close()
        logger.info("hl-alpha-bot stopped")


def main() -> None:  # pragma: no cover
    """同期エントリーポイント（python -m src.main で呼ばれる）。"""
    import argparse

    parser = argparse.ArgumentParser(description="hl-alpha-bot")
    parser.add_argument("--settings", default="config/settings.yaml")
    parser.add_argument(
        "--profile", default=None, help="profile_*.yaml path"
    )
    args = parser.parse_args()
    try:
        asyncio.run(
            async_main(
                settings_path=args.settings,
                profile_path=args.profile,
            )
        )
    except KeyboardInterrupt:
        logger.info("interrupted by user")


if __name__ == "__main__":  # pragma: no cover
    main()
