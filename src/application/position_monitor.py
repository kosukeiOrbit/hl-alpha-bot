"""APPLICATION 層: position_monitor（章11.6）。

保持ポジションの監視・約定検知・決済管理。

責務:
1. user_fills polling で entry / 決済両方の約定を検知
2. entry 約定時に Repository を更新 + grouped 発注された TP/SL の
   order_id を約定後に紐付け（章14.6 PR6.4.x の実機検証で確定した手順）
3. 保持ポジションの MFE/MAE を毎サイクル更新（章8.2）
4. TP/SL 約定時の決済処理 + Repository.close_trade
5. Funding 精算前の強制決済（章13.4・章22.6）

設計上の重要原則:
- run_cycle は絶対に例外を投げない。各サブ処理の失敗は errors に
  記録して呼び出し側に返す（メインループが落ちないようにするため）。
- 部分約定は PR7.2 では未対応（size 完全一致のみマッチ）。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal, TypeVar

from src.adapters.exchange import (
    ExchangeError,
    ExchangeProtocol,
    Fill,
    OrderRequest,
    Position,
)
from src.adapters.notifier import Notifier
from src.adapters.repository import Repository, Trade, TradeCloseRequest

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True)
class PositionMonitorConfig:
    """position_monitor の動作設定。"""

    funding_close_minutes_before: int  # 何分前に決済するか（HL は 1h 精算）
    funding_close_enabled: bool
    fills_lookback_seconds: int  # 直近何秒の fills を取得するか
    force_close_slippage_tolerance_pct: Decimal  # IOC limit_px のクッション


@dataclass(frozen=True)
class MonitorCycleResult:
    """1 サイクルの監視結果。"""

    trades_filled: int
    trades_closed: int
    open_position_count: int
    forced_closes: int
    errors: tuple[str, ...]


class PositionMonitor:
    """保持ポジションの監視・決済管理。"""

    def __init__(
        self,
        exchange: ExchangeProtocol,
        repo: Repository,
        notifier: Notifier,
        config: PositionMonitorConfig,
    ) -> None:
        self.exchange = exchange
        self.repo = repo
        self.notifier = notifier
        self.config = config

    async def run_cycle(self) -> MonitorCycleResult:
        """1 サイクル分の監視を実行（例外は投げない）。"""
        errors: list[str] = []

        fills_result = await self._safe_call(
            self._detect_fills, errors, "detect_fills"
        )
        trades_filled, trades_closed = fills_result or (0, 0)

        positions = await self._safe_get_positions(errors)

        await self._safe_call(
            lambda: self._update_mfe_mae(positions),
            errors,
            "update_mfe_mae",
        )

        forced = 0
        if self.config.funding_close_enabled:
            forced_result = await self._safe_call(
                lambda: self._check_funding_close(positions),
                errors,
                "funding_close",
            )
            forced = forced_result or 0

        return MonitorCycleResult(
            trades_filled=trades_filled,
            trades_closed=trades_closed,
            open_position_count=len(positions),
            forced_closes=forced,
            errors=tuple(errors),
        )

    # ─── 約定検知 ───────────────────────────────

    async def _detect_fills(self) -> tuple[int, int]:
        """直近の fills を取得して entry / 決済をマッチング。"""
        since_ms = int(
            (
                datetime.now(UTC)
                - timedelta(seconds=self.config.fills_lookback_seconds)
            ).timestamp()
            * 1000
        )
        fills = await self.exchange.get_fills(since_ms=since_ms)
        open_trades = await self.repo.get_open_trades()

        entry_filled = 0
        closed = 0
        for fill in fills:
            handled = await self._dispatch_fill(fill, open_trades)
            if handled == "entry":
                entry_filled += 1
            elif handled == "close":
                closed += 1
        return entry_filled, closed

    async def _dispatch_fill(
        self, fill: Fill, open_trades: tuple[Trade, ...]
    ) -> Literal["entry", "close", "ignored"]:
        """単一の fill を該当する trade に振り分ける。"""
        if fill.closed_pnl == 0:
            for trade in open_trades:
                if not trade.is_filled and self._fill_matches_entry(trade, fill):
                    await self._on_entry_filled(trade, fill)
                    return "entry"
            return "ignored"

        for trade in open_trades:
            if trade.is_filled and self._fill_matches_close(trade, fill):
                await self._on_trade_closed(trade, fill)
                return "close"
        return "ignored"

    @staticmethod
    def _fill_matches_entry(trade: Trade, fill: Fill) -> bool:
        if trade.symbol != fill.symbol:
            return False
        expected_side: Literal["buy", "sell"] = (
            "buy" if trade.direction == "LONG" else "sell"
        )
        return fill.side == expected_side and fill.size == trade.size_coins

    @staticmethod
    def _fill_matches_close(trade: Trade, fill: Fill) -> bool:
        if trade.symbol != fill.symbol:
            return False
        expected_side: Literal["buy", "sell"] = (
            "sell" if trade.direction == "LONG" else "buy"
        )
        return fill.side == expected_side and fill.size == trade.size_coins

    async def _on_entry_filled(self, trade: Trade, fill: Fill) -> None:
        """entry 約定時の処理（章14.6）。"""
        fill_time = datetime.fromtimestamp(fill.timestamp_ms / 1000, tz=UTC)
        await self.repo.mark_trade_filled(
            trade_id=trade.id,
            fill_price=fill.price,
            fill_time=fill_time,
        )
        tp_oid, sl_oid = await self._find_tp_sl_order_ids(trade)
        await self.repo.update_tp_sl_order_ids(
            trade_id=trade.id,
            tp_order_id=tp_oid,
            sl_order_id=sl_oid,
        )
        await self.notifier.send_signal(
            f"FILL {trade.direction} {trade.symbol} @ {fill.price} "
            f"(trade_id={trade.id}, tp_oid={tp_oid}, sl_oid={sl_oid})",
            dedup_key=f"fill:{trade.id}",
        )

    async def _find_tp_sl_order_ids(
        self, trade: Trade
    ) -> tuple[int | None, int | None]:
        """grouped 発注された TP/SL を価格で識別して order_id を回収。

        entry 約定後、HL が自動で TP/SL を発注する（章14.6・PR6.4.3 検証済）。
        open_orders にそれらが現れるので、価格が tp_price / sl_price の
        どちらに近いかで紐付ける。
        """
        open_orders = await self.exchange.get_open_orders()
        tp_oid: int | None = None
        sl_oid: int | None = None
        for order in open_orders:
            if order.symbol != trade.symbol:
                continue
            diff_to_tp = abs(order.price - trade.tp_price)
            diff_to_sl = abs(order.price - trade.sl_price)
            if diff_to_tp < diff_to_sl:
                if tp_oid is None:
                    tp_oid = order.order_id
            elif sl_oid is None:
                sl_oid = order.order_id
        return tp_oid, sl_oid

    async def _on_trade_closed(self, trade: Trade, fill: Fill) -> None:
        """TP/SL 約定時の決済処理。"""
        diff_to_tp = abs(fill.price - trade.tp_price)
        diff_to_sl = abs(fill.price - trade.sl_price)
        exit_reason: Literal["TP", "SL", "FUNDING", "MANUAL", "TIMEOUT"] = (
            "TP" if diff_to_tp < diff_to_sl else "SL"
        )

        await self.repo.close_trade(
            TradeCloseRequest(
                trade_id=trade.id,
                exit_price=fill.price,
                exit_reason=exit_reason,
                pnl_usd=fill.closed_pnl,
                fee_usd_total=fill.fee_usd,
                funding_paid_usd=Decimal("0"),
                mfe_pct=trade.mfe_pct or Decimal("0"),
                mae_pct=trade.mae_pct or Decimal("0"),
            )
        )
        sign = "+" if fill.closed_pnl >= 0 else ""
        await self.notifier.send_signal(
            f"CLOSE {trade.direction} {trade.symbol} by {exit_reason} @ "
            f"{fill.price} PnL={sign}{fill.closed_pnl} (trade_id={trade.id})",
            dedup_key=f"close:{trade.id}",
        )

    # ─── MFE/MAE 更新 ──────────────────────────

    async def _update_mfe_mae(self, positions: tuple[Position, ...]) -> None:
        """保持ポジションの MFE/MAE を更新（章8.2）。"""
        if not positions:
            return
        open_trades = await self.repo.get_open_trades()
        trade_by_symbol = {
            t.symbol: t for t in open_trades if t.is_filled
        }
        for pos in positions:
            trade = trade_by_symbol.get(pos.symbol)
            if trade is None:
                continue
            current_price = self._estimate_current_price(pos)
            pnl_pct = self._unrealized_pnl_pct(trade, current_price)
            new_mfe = max(trade.mfe_pct or Decimal("0"), pnl_pct)
            new_mae = min(trade.mae_pct or Decimal("0"), pnl_pct)
            await self.repo.update_mfe_mae(
                trade_id=trade.id,
                mfe_pct=new_mfe,
                mae_pct=new_mae,
            )

    @staticmethod
    def _estimate_current_price(pos: Position) -> Decimal:
        """unrealized_pnl と size から現在価格を逆算（章11.6 簡易実装）。

        後続 PR で l2_book.bids[0] ベースの正確版に置き換え予定。
        """
        if pos.size == 0:
            return pos.entry_price
        return pos.entry_price + pos.unrealized_pnl / pos.size

    @staticmethod
    def _unrealized_pnl_pct(trade: Trade, current_price: Decimal) -> Decimal:
        if trade.entry_price == 0:
            return Decimal("0")
        if trade.direction == "LONG":
            return (current_price - trade.entry_price) / trade.entry_price * 100
        return (trade.entry_price - current_price) / trade.entry_price * 100

    # ─── Funding 強制決済 ──────────────────────

    async def _check_funding_close(
        self, positions: tuple[Position, ...]
    ) -> int:
        """Funding 精算前の強制決済（章13.4・章22.6）。

        HL は毎時 00 分（UTC）に 1h 分の Funding を精算するので、
        funding_close_minutes_before 分前まで近づいたら全ポジションを
        reduce_only IOC で決済する。
        """
        now_utc = datetime.now(UTC)
        next_funding = (
            now_utc.replace(minute=0, second=0, microsecond=0)
            + timedelta(hours=1)
        )
        minutes_until = (next_funding - now_utc).total_seconds() / 60
        if minutes_until > self.config.funding_close_minutes_before:
            return 0

        forced = 0
        for pos in positions:
            if pos.size == 0:
                continue
            try:
                await self._force_close(pos, reason="FUNDING")
                forced += 1
            except ExchangeError as e:
                logger.exception("force close failed: %s", pos.symbol)
                await self.notifier.send_alert(
                    f"force close failed for {pos.symbol}: {e}",
                    dedup_key=f"force_close_fail:{pos.symbol}",
                )
        return forced

    async def _force_close(
        self,
        pos: Position,
        reason: Literal["FUNDING", "MANUAL", "TIMEOUT"],
    ) -> None:
        """reduce_only IOC で強制決済。"""
        side: Literal["buy", "sell"] = "sell" if pos.size > 0 else "buy"
        size = abs(pos.size)
        book = await self.exchange.get_l2_book(pos.symbol)
        tolerance = self.config.force_close_slippage_tolerance_pct
        if side == "buy":
            best = book.asks[0].price
            limit_price = best * (Decimal("1") + tolerance)
        else:
            best = book.bids[0].price
            limit_price = best * (Decimal("1") - tolerance)

        result = await self.exchange.place_order(
            OrderRequest(
                symbol=pos.symbol,
                side=side,
                size=size,
                price=limit_price,
                tif="Ioc",
                reduce_only=True,
            )
        )
        if result.success:
            await self.notifier.send_signal(
                f"FORCE_CLOSE {pos.symbol} reason={reason} size={size} "
                f"(order_id={result.order_id})",
                dedup_key=f"force_close:{pos.symbol}:{reason}",
            )

    # ─── ヘルパー ──────────────────────────────

    async def _safe_call(
        self,
        func: Callable[[], Awaitable[T]],
        errors: list[str],
        step_name: str,
    ) -> T | None:
        try:
            return await func()
        except Exception as e:
            logger.exception("%s failed", step_name)
            errors.append(f"{step_name}: {e}")
            return None

    async def _safe_get_positions(
        self, errors: list[str]
    ) -> tuple[Position, ...]:
        try:
            return await self.exchange.get_positions()
        except Exception as e:
            logger.exception("get_positions failed")
            errors.append(f"get_positions: {e}")
            return ()
