"""APPLICATION 層: reconciliation（章9.3・9.6・11.6）。

BOT 起動時の状態復元と、定期的なポジション突合を担う。

責務:
1. HL の真実の状態を取得（positions / open_orders / recent_fills）
2. DB の状態を取得（open trades）
3. CORE 層の reconcile_positions 純関数で突合
4. 結果のアクションを副作用として実行（DB 補正・通知）
5. 起動時のみ古い未約定注文を cleanup
6. 復元完了通知（起動時のみ）

CORE と ADAPTERS で型が異なる点に注意:
- CORE: HLPosition / DBTrade / HLFill（突合用最小データ）
- ADAPTERS: Position / Trade / Fill（運用用フルデータ）

このモジュールが両者の橋渡しをする。CLOSE_FROM_FILL では fill の
closed_pnl など CORE にない情報が必要なので、元の ADAPTERS Fill を
症候的にマッチングして取り戻している。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TypeVar, cast

from src.adapters.exchange import (
    ExchangeError,
    ExchangeProtocol,
    Fill,
    Order,
    Position,
)
from src.adapters.notifier import Notifier
from src.adapters.repository import Repository, Trade
from src.core.reconciliation import (
    ActionType,
    DBTrade,
    HLFill,
    HLPosition,
    ReconcileAction,
    ReconcileResult,
    reconcile_positions,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True)
class ReconciliationConfig:
    """reconciliation の動作設定。"""

    fills_lookback_hours: int  # 起動時に何時間分の fills を取得するか
    stale_order_cleanup_seconds: int  # この秒数より古い注文をキャンセル


@dataclass(frozen=True)
class ReconcileSummary:
    """reconciliation の実行結果。"""

    hl_position_count: int
    db_open_trade_count: int
    actions_executed: int
    stale_orders_cancelled: int
    errors: tuple[str, ...]


class StateReconciler:
    """起動時・定期実行の状態突合（章9.3・9.6）。"""

    def __init__(
        self,
        exchange: ExchangeProtocol,
        repo: Repository,
        notifier: Notifier,
        config: ReconciliationConfig,
    ) -> None:
        self.exchange = exchange
        self.repo = repo
        self.notifier = notifier
        self.config = config

    async def restore_on_startup(self) -> ReconcileSummary:
        """起動時の状態復元（章9.3）。stale order cleanup + 完了通知あり。"""
        return await self._reconcile(
            cleanup_enabled=True, notify_completion=True
        )

    async def run_periodic_check(self) -> ReconcileSummary:
        """定期実行の突合（章9.6）。cleanup なし、完了通知なし。"""
        return await self._reconcile(
            cleanup_enabled=False, notify_completion=False
        )

    async def _reconcile(
        self,
        *,
        cleanup_enabled: bool,
        notify_completion: bool,
    ) -> ReconcileSummary:
        """突合本体。例外は投げず errors に集約する。"""
        errors: list[str] = []

        hl_positions, hl_orders, hl_fills = await self._fetch_hl_state(errors)
        db_trades = await self._fetch_db_state(errors)
        result = self._run_core_reconcile(
            hl_positions, db_trades, hl_fills, errors
        )

        # PR B2: db_trade で id -> ADAPTERS Trade を引けるようにしておく
        # （_close_from_fill / _mark_manual_review が HL 注文 cleanup 用に
        # entry_order_id / tp_order_id / sl_order_id を必要とする）。
        db_trades_by_id = {t.id: t for t in db_trades}

        executed = 0
        for action in result.actions:
            try:
                await self._apply_action(
                    action, hl_fills, db_trades_by_id, errors
                )
                executed += 1
            except Exception as e:
                logger.exception("apply_action failed: %s", action.type)
                errors.append(f"apply_action({action.type}): {e}")

        cancelled = 0
        if cleanup_enabled:
            cancelled = await self._cleanup_stale_orders(hl_orders, errors)

        if notify_completion:
            await self._notify_completion(
                hl_position_count=len(hl_positions),
                actions_executed=executed,
                cancelled_count=cancelled,
                errors=errors,
            )

        return ReconcileSummary(
            hl_position_count=len(hl_positions),
            db_open_trade_count=len(db_trades),
            actions_executed=executed,
            stale_orders_cancelled=cancelled,
            errors=tuple(errors),
        )

    # ─── 状態取得 ──────────────────────────────

    async def _fetch_hl_state(
        self, errors: list[str]
    ) -> tuple[tuple[Position, ...], tuple[Order, ...], tuple[Fill, ...]]:
        """HL から positions / open_orders / recent_fills を取得。"""
        positions = await self._safe_call(
            self.exchange.get_positions,
            errors,
            "get_positions",
            default=cast(tuple[Position, ...], ()),
        )
        orders = await self._safe_call(
            self.exchange.get_open_orders,
            errors,
            "get_open_orders",
            default=cast(tuple[Order, ...], ()),
        )
        since_ms = int(
            (
                datetime.now(UTC)
                - timedelta(hours=self.config.fills_lookback_hours)
            ).timestamp()
            * 1000
        )
        fills = await self._safe_call(
            lambda: self.exchange.get_fills(since_ms=since_ms),
            errors,
            "get_fills",
            default=cast(tuple[Fill, ...], ()),
        )
        return positions, orders, fills

    async def _fetch_db_state(self, errors: list[str]) -> tuple[Trade, ...]:
        return await self._safe_call(
            self.repo.get_open_trades,
            errors,
            "get_open_trades",
            default=cast(tuple[Trade, ...], ()),
        )

    def _run_core_reconcile(
        self,
        hl_positions: tuple[Position, ...],
        db_trades: tuple[Trade, ...],
        hl_fills: tuple[Fill, ...],
        errors: list[str],
    ) -> ReconcileResult:
        """ADAPTERS 型 → CORE 型に変換して reconcile_positions を呼ぶ。

        PR A3 (#3 of 5): ``is_filled=0`` の trade（ALO 等で resting 中の
        未約定エントリー）は reconciliation の対象から除外する。
        これらは「約定すれば position_monitor が拾う」「約定しなければ
        資金は動いていない」状態であり、reconciler が "HL に position が
        無い" として CLOSE_FROM_FILL に流すと、関係ない過去 fill との
        誤マッチで DB が壊れる（2026-05-15 mainnet で ID 2-4 が ID 1 の
        TP fill で MANUAL クローズされた事例）。

        この絞り込みは entry order が長時間 resting し続けた場合の
        MANUAL_REVIEW 機会を失うが、その状態の検知は別経路
        （stale order cleanup, manual ops）で対応する想定。
        """
        filled_db_trades = tuple(t for t in db_trades if t.is_filled)
        try:
            return reconcile_positions(
                hl_positions=tuple(_to_hl_position(p) for p in hl_positions),
                db_trades=tuple(_to_db_trade(t) for t in filled_db_trades),
                hl_fills=tuple(_to_hl_fill(f) for f in hl_fills),
            )
        except Exception as e:
            logger.exception("reconcile_positions failed")
            errors.append(f"reconcile_positions: {e}")
            return ReconcileResult(
                actions=(),
                positions_resumed=0,
                external_detected=0,
                corrections_made=0,
                closed_from_fills=0,
                manual_review_needed=0,
            )

    # ─── アクション分岐 ────────────────────────

    async def _apply_action(
        self,
        action: ReconcileAction,
        adapter_fills: tuple[Fill, ...],
        db_trades_by_id: dict[int, Trade],
        errors: list[str],
    ) -> None:
        """ReconcileAction を副作用として実行（章9.3）。

        各 ActionType ごとに必須フィールドが決まっているので cast で型を
        narrow している（CORE 契約）。未知の type は warning ログのみ。
        """
        t = action.type
        if t == ActionType.REGISTER_EXTERNAL:
            await self._register_external(
                cast(HLPosition, action.hl_position)
            )
        elif t == ActionType.RESUME_MONITORING:
            await self._resume_monitoring(cast(DBTrade, action.db_trade))
        elif t == ActionType.CORRECT_DB:
            await self._correct_db(
                cast(DBTrade, action.db_trade),
                cast(HLPosition, action.hl_position),
            )
        elif t == ActionType.CLOSE_FROM_FILL:
            db_trade = cast(DBTrade, action.db_trade)
            await self._close_from_fill(
                db_trade,
                cast(HLFill, action.fill),
                adapter_fills,
                db_trades_by_id.get(db_trade.trade_id),
                errors,
            )
        elif t == ActionType.MANUAL_REVIEW:
            db_trade = cast(DBTrade, action.db_trade)
            await self._mark_manual_review(
                db_trade,
                db_trades_by_id.get(db_trade.trade_id),
                errors,
            )
        else:
            logger.warning("unknown action type: %s", t)

    async def _register_external(self, hl_pos: HLPosition) -> None:
        await self.repo.register_external_position(
            symbol=hl_pos.symbol,
            size=hl_pos.size,
            entry_price=hl_pos.entry_price,
        )
        await self.notifier.send_alert(
            f"external position detected: {hl_pos.symbol} "
            f"size={hl_pos.size} entry={hl_pos.entry_price}",
            dedup_key=f"external:{hl_pos.symbol}",
        )

    async def _resume_monitoring(self, db_trade: DBTrade) -> None:
        await self.repo.mark_resumed(db_trade.trade_id)

    async def _correct_db(
        self,
        db_trade: DBTrade,
        hl_pos: HLPosition,
    ) -> None:
        await self.repo.correct_position(
            trade_id=db_trade.trade_id,
            actual_size=hl_pos.size,
            actual_entry=hl_pos.entry_price,
        )
        await self.notifier.send_alert(
            f"position mismatch corrected: {hl_pos.symbol} "
            f"db_size={db_trade.size} hl_size={hl_pos.size}",
            dedup_key=f"correct:{hl_pos.symbol}",
        )

    async def _close_from_fill(
        self,
        db_trade: DBTrade,
        hl_fill: HLFill,
        adapter_fills: tuple[Fill, ...],
        adapter_trade: Trade | None,
        errors: list[str],
    ) -> None:
        """fill から決済記録。

        CORE の HLFill は closed_pnl / fee_usd を持たないため、
        元の ADAPTERS Fill を症候的にマッチングして取り戻す。

        PR B2 (#4 of 5): DB を closed にする前に、HL 側に残っている
        反対側の resting order（TP/SL のもう片方、entry ALO の残り）を
        cancel する。これを行わないと、決済済みに見える DB と裏腹に
        HL 側で SL/TP が宙ぶらりんになり、次サイクル以降に外部約定の
        ような形で意図しない約定が起きる。cancel 失敗（既約定 / 既
        cancel）はログのみで継続する（close 処理本体を止めない）。
        """
        adapter_fill = _find_adapter_fill(hl_fill, adapter_fills)
        if adapter_fill is None:
            logger.warning(
                "matching adapter fill not found for trade %d",
                db_trade.trade_id,
            )
            return
        await self._cancel_known_orders(
            db_trade.symbol, adapter_trade, errors, reason="close_from_fill"
        )
        await self.repo.close_trade_from_fill(
            trade_id=db_trade.trade_id, fill=adapter_fill
        )
        await self.notifier.send_signal(
            f"closed from fill: {db_trade.symbol} @ {adapter_fill.price} "
            f"pnl={adapter_fill.closed_pnl}",
            dedup_key=f"close_from_fill:{db_trade.trade_id}",
        )

    async def _mark_manual_review(
        self,
        db_trade: DBTrade,
        adapter_trade: Trade | None,
        errors: list[str],
    ) -> None:
        """MANUAL_REVIEW 印を立てる + HL 側 cleanup（PR B2 #4）。

        ``MANUAL_REVIEW`` は HL 側にも DB 側にもポジションが無い／
        曖昧な状態を運用者に知らせる経路だが、`is_filled=1` の trade に
        対応する resting TP/SL が宙ぶらりんなままだと将来の誤約定の元。
        そのため CLOSE_FROM_FILL と同じく HL の cleanup を併せて行う。
        """
        await self._cancel_known_orders(
            db_trade.symbol, adapter_trade, errors, reason="manual_review"
        )
        await self.repo.mark_manual_review(db_trade.trade_id)
        await self.notifier.send_alert(
            f"manual review needed: trade_id={db_trade.trade_id} "
            f"({db_trade.symbol})",
            dedup_key=f"manual:{db_trade.trade_id}",
        )

    async def _cancel_known_orders(
        self,
        symbol: str,
        adapter_trade: Trade | None,
        errors: list[str],
        *,
        reason: str,
    ) -> None:
        """DB が把握している entry/TP/SL HL 注文を best-effort で cancel。

        既約定・既 cancel・unknown oid いずれも ``ExchangeError`` で返って
        くるので、ログを残しつつ ``errors`` に積んで継続する。close 処理
        本体を止めないことが重要（DB と HL の状態乖離が拡大しないように）。
        """
        if adapter_trade is None:
            return
        candidates: tuple[tuple[str, int | None], ...] = (
            ("entry", adapter_trade.entry_order_id),
            ("tp", adapter_trade.tp_order_id),
            ("sl", adapter_trade.sl_order_id),
        )
        for kind, oid in candidates:
            if oid is None:
                continue
            try:
                await self.exchange.cancel_order(order_id=oid, symbol=symbol)
            except ExchangeError as e:
                logger.warning(
                    "%s cancel_order failed (oid=%d, kind=%s, trade=%d): %s",
                    reason,
                    oid,
                    kind,
                    adapter_trade.id,
                    e,
                )
                errors.append(
                    f"{reason}_cancel_{kind}_{adapter_trade.id}: {e}"
                )

    # ─── stale order cleanup ──────────────────

    async def _cleanup_stale_orders(
        self,
        hl_orders: tuple[Order, ...],
        errors: list[str],
    ) -> int:
        """この秒数より古い注文をキャンセル（章9.3 Step 5）。"""
        threshold_ms = int(
            (
                datetime.now(UTC)
                - timedelta(seconds=self.config.stale_order_cleanup_seconds)
            ).timestamp()
            * 1000
        )
        cancelled = 0
        for order in hl_orders:
            if order.timestamp_ms > threshold_ms:
                continue
            try:
                ok = await self.exchange.cancel_order(
                    order_id=order.order_id, symbol=order.symbol
                )
            except ExchangeError as e:
                logger.exception(
                    "cancel_order failed for stale order %d", order.order_id
                )
                errors.append(f"cancel_stale_{order.order_id}: {e}")
                continue
            if ok:
                cancelled += 1
        return cancelled

    # ─── 完了通知 ──────────────────────────────

    async def _notify_completion(
        self,
        *,
        hl_position_count: int,
        actions_executed: int,
        cancelled_count: int,
        errors: list[str],
    ) -> None:
        """復元完了通知。errors の有無で alert と signal を使い分け。"""
        message = (
            f"reconciliation done: {hl_position_count} positions, "
            f"{actions_executed} actions, "
            f"{cancelled_count} stale orders cancelled"
        )
        try:
            if errors:
                await self.notifier.send_alert(
                    f"{message} ({len(errors)} errors)"
                )
            else:
                await self.notifier.send_signal(message)
        except Exception:
            logger.exception("completion notification failed")

    # ─── ヘルパー ──────────────────────────────

    async def _safe_call(
        self,
        func: Callable[[], Awaitable[T]],
        errors: list[str],
        step_name: str,
        default: T,
    ) -> T:
        try:
            return await func()
        except Exception as e:
            logger.exception("%s failed", step_name)
            errors.append(f"{step_name}: {e}")
            return default


def _to_hl_position(p: Position) -> HLPosition:
    return HLPosition(
        symbol=p.symbol,
        size=p.size,
        entry_price=p.entry_price,
    )


def _to_db_trade(t: Trade) -> DBTrade:
    return DBTrade(
        trade_id=t.id,
        symbol=t.symbol,
        direction=t.direction,
        size=t.size_coins,
        entry_price=t.entry_price,
        entry_time_ms=int(t.entry_time.timestamp() * 1000),
    )


def _to_hl_fill(f: Fill) -> HLFill:
    return HLFill(
        symbol=f.symbol,
        side=f.side,
        size=f.size,
        price=f.price,
        timestamp=f.timestamp_ms,
    )


def _find_adapter_fill(
    hl_fill: HLFill,
    adapter_fills: tuple[Fill, ...],
) -> Fill | None:
    """CORE HLFill に対応する ADAPTERS Fill を症候的に探す。"""
    for f in adapter_fills:
        if (
            f.symbol == hl_fill.symbol
            and f.side == hl_fill.side
            and f.size == hl_fill.size
            and f.price == hl_fill.price
            and f.timestamp_ms == hl_fill.timestamp
        ):
            return f
    return None
