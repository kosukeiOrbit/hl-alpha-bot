"""APPLICATION 層 position_monitor のテスト。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from src.adapters.exchange import (
    ExchangeError,
    Fill,
    L2Book,
    L2BookLevel,
    Order,
    OrderResult,
    Position,
)
from src.adapters.repository import Trade
from src.application.position_monitor import (
    MonitorCycleResult,
    PositionMonitor,
    PositionMonitorConfig,
)


def make_config(**overrides: Any) -> PositionMonitorConfig:
    base: dict[str, Any] = {
        "funding_close_minutes_before": 5,
        "funding_close_enabled": True,
        "fills_lookback_seconds": 300,
        "force_close_slippage_tolerance_pct": Decimal("0.005"),
    }
    base.update(overrides)
    return PositionMonitorConfig(**base)


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
        "is_filled": False,
        "actual_entry_price": None,
        "tp_order_id": None,
        "sl_order_id": None,
        "fill_time": None,
    }
    base.update(overrides)
    return Trade(**base)


def make_fill(**overrides: Any) -> Fill:
    base: dict[str, Any] = {
        "order_id": 12345,
        "symbol": "BTC",
        "side": "buy",
        "size": Decimal("0.0002"),
        "price": Decimal("65010"),
        "fee_usd": Decimal("0.5"),
        "timestamp_ms": int(datetime.now(UTC).timestamp() * 1000),
        "closed_pnl": Decimal("0"),
    }
    base.update(overrides)
    return Fill(**base)


def make_order(**overrides: Any) -> Order:
    base: dict[str, Any] = {
        "order_id": 999,
        "client_order_id": None,
        "symbol": "BTC",
        "side": "sell",
        "size": Decimal("0.0002"),
        "price": Decimal("67100"),
        "tif": "Gtc",
        "timestamp_ms": 0,
    }
    base.update(overrides)
    return Order(**base)


def make_position(**overrides: Any) -> Position:
    base: dict[str, Any] = {
        "symbol": "BTC",
        "size": Decimal("0.0002"),
        "entry_price": Decimal("65000"),
        "unrealized_pnl": Decimal("2"),  # +$2 → current ≈ 75000?
        "leverage": 3,
        "liquidation_price": None,
    }
    base.update(overrides)
    return Position(**base)


def make_book(best_bid: Decimal, best_ask: Decimal) -> L2Book:
    return L2Book(
        symbol="BTC",
        bids=(L2BookLevel(price=best_bid, size=Decimal("1"), n_orders=1),),
        asks=(L2BookLevel(price=best_ask, size=Decimal("1"), n_orders=1),),
        timestamp_ms=0,
    )


def build_monitor(
    *,
    fills: tuple[Fill, ...] = (),
    open_trades: tuple[Trade, ...] = (),
    recent_trades: tuple[Trade, ...] = (),
    positions: tuple[Position, ...] = (),
    open_orders: tuple[Order, ...] = (),
    book: L2Book | None = None,
    place_order_result: OrderResult | None = None,
    config: PositionMonitorConfig | None = None,
    fills_side_effect: BaseException | None = None,
    positions_side_effect: BaseException | None = None,
    open_orders_side_effect: BaseException | None = None,
    place_order_side_effect: BaseException | None = None,
) -> tuple[PositionMonitor, Any, Any, Any]:
    exchange = AsyncMock()
    if fills_side_effect is not None:
        exchange.get_fills = AsyncMock(side_effect=fills_side_effect)
    else:
        exchange.get_fills = AsyncMock(return_value=fills)
    if positions_side_effect is not None:
        exchange.get_positions = AsyncMock(side_effect=positions_side_effect)
    else:
        exchange.get_positions = AsyncMock(return_value=positions)
    if open_orders_side_effect is not None:
        exchange.get_open_orders = AsyncMock(side_effect=open_orders_side_effect)
    else:
        exchange.get_open_orders = AsyncMock(return_value=open_orders)
    exchange.get_l2_book = AsyncMock(
        return_value=book or make_book(Decimal("64995"), Decimal("65005"))
    )
    if place_order_side_effect is not None:
        exchange.place_order = AsyncMock(side_effect=place_order_side_effect)
    else:
        exchange.place_order = AsyncMock(
            return_value=place_order_result
            or OrderResult(success=True, order_id=7777)
        )

    repo = AsyncMock()
    repo.get_open_trades = AsyncMock(return_value=open_trades)
    repo.get_recent_trades = AsyncMock(return_value=recent_trades)
    repo.mark_trade_filled = AsyncMock()
    repo.update_tp_sl_order_ids = AsyncMock()
    repo.update_mfe_mae = AsyncMock()
    repo.close_trade = AsyncMock()

    notifier = AsyncMock()

    monitor = PositionMonitor(
        exchange=exchange,
        repo=repo,
        notifier=notifier,
        config=config or make_config(),
    )
    return monitor, exchange, repo, notifier


# ─── 約定検知 ─────────────────────────────


class TestEntryFilledDetection:
    @pytest.mark.asyncio
    async def test_long_entry_marks_filled(self) -> None:
        trade = make_trade()
        fill = make_fill()
        monitor, _, repo, notifier = build_monitor(
            fills=(fill,),
            open_trades=(trade,),
        )
        result = await monitor.run_cycle()
        assert result.trades_filled == 1
        assert result.trades_closed == 0
        repo.mark_trade_filled.assert_awaited_once()
        repo.update_tp_sl_order_ids.assert_awaited_once()
        notifier.send_signal.assert_awaited()

    @pytest.mark.asyncio
    async def test_short_entry_uses_sell_side(self) -> None:
        trade = make_trade(direction="SHORT")
        fill = make_fill(side="sell")
        monitor, _, repo, _ = build_monitor(
            fills=(fill,),
            open_trades=(trade,),
        )
        result = await monitor.run_cycle()
        assert result.trades_filled == 1
        repo.mark_trade_filled.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unrelated_symbol_ignored(self) -> None:
        trade = make_trade(symbol="BTC")
        fill = make_fill(symbol="ETH")
        monitor, _, repo, _ = build_monitor(
            fills=(fill,),
            open_trades=(trade,),
        )
        result = await monitor.run_cycle()
        assert result.trades_filled == 0
        repo.mark_trade_filled.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_size_mismatch_ignored(self) -> None:
        # 部分約定は PR7.2 では未対応
        trade = make_trade(size_coins=Decimal("0.0002"))
        fill = make_fill(size=Decimal("0.0001"))
        monitor, _, repo, _ = build_monitor(
            fills=(fill,),
            open_trades=(trade,),
        )
        result = await monitor.run_cycle()
        assert result.trades_filled == 0
        repo.mark_trade_filled.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_already_filled_trade_skipped_for_entry(self) -> None:
        trade = make_trade(is_filled=True)
        fill = make_fill()
        monitor, _, _, _ = build_monitor(
            fills=(fill,),
            open_trades=(trade,),
        )
        result = await monitor.run_cycle()
        assert result.trades_filled == 0


# ─── PR B1 (#5): fill 冪等化（DB fill_time ベース dedup） ─


class TestFillIdempotency:
    """``_detect_fills`` の DB 由来 fill_time dedup（PR B1 #5）。

    2026-05-15 mainnet で ID 6 の entry fill (05:51:15.526) が次 cycle 以降の
    fills_lookback 内に残り続け、is_filled=0 だった ID 7 (同 size の隣接 ALO)
    を誤って ``is_filled=1`` にマークした（``actual_entry_price`` まで上書き）。
    本テスト群は: ① 既処理 timestamp は skip、② 未処理は正常に entry 反映、
    ③ 直近 trade 一覧の自動取得、を保証する。
    """

    @pytest.mark.asyncio
    async def test_fill_with_known_timestamp_is_skipped(self) -> None:
        ts = 1_715_750_175_526  # 2026-05-15 05:51:15.526 UTC 相当
        already_processed = make_trade(
            id=6,
            is_filled=True,
            fill_time=datetime.fromtimestamp(ts / 1000, tz=UTC),
        )
        pending = make_trade(id=7, is_filled=False)
        replay_fill = make_fill(timestamp_ms=ts)
        monitor, _, repo, _ = build_monitor(
            fills=(replay_fill,),
            open_trades=(pending,),
            recent_trades=(already_processed, pending),
        )
        result = await monitor.run_cycle()
        # ID 7 は誤って filled にならない
        assert result.trades_filled == 0
        repo.mark_trade_filled.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fill_with_new_timestamp_still_processed(self) -> None:
        old_ts = 1_715_750_175_526
        old_trade = make_trade(
            id=6,
            is_filled=True,
            fill_time=datetime.fromtimestamp(old_ts / 1000, tz=UTC),
        )
        pending = make_trade(id=7, is_filled=False)
        # 新しい timestamp（dedup set に入らない）の本物 entry fill
        fresh_fill = make_fill(timestamp_ms=old_ts + 60_000)
        monitor, _, repo, _ = build_monitor(
            fills=(fresh_fill,),
            open_trades=(pending,),
            recent_trades=(old_trade, pending),
        )
        result = await monitor.run_cycle()
        assert result.trades_filled == 1
        repo.mark_trade_filled.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_recent_trades_without_fill_time_do_not_block(self) -> None:
        # fill_time が None の旧レコードは dedup set に含まれない（影響なし）
        legacy = make_trade(id=5, is_filled=False, fill_time=None)
        pending = make_trade(id=7, is_filled=False)
        fill = make_fill()
        monitor, _, repo, _ = build_monitor(
            fills=(fill,),
            open_trades=(pending,),
            recent_trades=(legacy, pending),
        )
        result = await monitor.run_cycle()
        assert result.trades_filled == 1
        repo.mark_trade_filled.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_recent_trades_called_each_cycle(self) -> None:
        monitor, _, repo, _ = build_monitor()
        await monitor.run_cycle()
        repo.get_recent_trades.assert_awaited_once_with(limit=100)


# ─── TP/SL order_id 紐付け ────────────────


class TestTpSlOrderIdLinking:
    @pytest.mark.asyncio
    async def test_links_tp_sl_by_price_proximity(self) -> None:
        trade = make_trade(
            tp_price=Decimal("67000"),
            sl_price=Decimal("64000"),
        )
        fill = make_fill()
        tp_order = make_order(order_id=999, price=Decimal("67100"))
        sl_order = make_order(order_id=888, price=Decimal("63900"))
        monitor, _, repo, _ = build_monitor(
            fills=(fill,),
            open_trades=(trade,),
            open_orders=(tp_order, sl_order),
        )
        await monitor.run_cycle()
        repo.update_tp_sl_order_ids.assert_awaited_once_with(
            trade_id=trade.id,
            tp_order_id=999,
            sl_order_id=888,
        )

    @pytest.mark.asyncio
    async def test_only_tp_present(self) -> None:
        trade = make_trade(
            tp_price=Decimal("67000"),
            sl_price=Decimal("64000"),
        )
        fill = make_fill()
        tp_order = make_order(order_id=999, price=Decimal("67100"))
        monitor, _, repo, _ = build_monitor(
            fills=(fill,),
            open_trades=(trade,),
            open_orders=(tp_order,),
        )
        await monitor.run_cycle()
        repo.update_tp_sl_order_ids.assert_awaited_once_with(
            trade_id=trade.id,
            tp_order_id=999,
            sl_order_id=None,
        )

    @pytest.mark.asyncio
    async def test_unrelated_symbol_orders_skipped(self) -> None:
        trade = make_trade()
        fill = make_fill()
        eth_order = make_order(symbol="ETH", price=Decimal("3000"))
        monitor, _, repo, _ = build_monitor(
            fills=(fill,),
            open_trades=(trade,),
            open_orders=(eth_order,),
        )
        await monitor.run_cycle()
        repo.update_tp_sl_order_ids.assert_awaited_once_with(
            trade_id=trade.id,
            tp_order_id=None,
            sl_order_id=None,
        )

    @pytest.mark.asyncio
    async def test_duplicate_orders_first_wins(self) -> None:
        # 同じ TP 価格帯に 2 つあった場合、最初のものを採用
        trade = make_trade(
            tp_price=Decimal("67000"),
            sl_price=Decimal("64000"),
        )
        fill = make_fill()
        tp1 = make_order(order_id=111, price=Decimal("67100"))
        tp2 = make_order(order_id=222, price=Decimal("67050"))
        sl1 = make_order(order_id=333, price=Decimal("63950"))
        sl2 = make_order(order_id=444, price=Decimal("63900"))
        monitor, _, repo, _ = build_monitor(
            fills=(fill,),
            open_trades=(trade,),
            open_orders=(tp1, tp2, sl1, sl2),
        )
        await monitor.run_cycle()
        repo.update_tp_sl_order_ids.assert_awaited_once_with(
            trade_id=trade.id,
            tp_order_id=111,
            sl_order_id=333,
        )


# ─── 決済検知 ─────────────────────────────


class TestCloseDetection:
    @pytest.mark.asyncio
    async def test_tp_hit_closes_with_tp_reason(self) -> None:
        trade = make_trade(
            is_filled=True,
            actual_entry_price=Decimal("65000"),
            tp_price=Decimal("67000"),
            sl_price=Decimal("64000"),
            mfe_pct=Decimal("3.5"),
            mae_pct=Decimal("-1.0"),
        )
        close_fill = make_fill(
            side="sell",
            price=Decimal("67000"),
            closed_pnl=Decimal("4.0"),
        )
        monitor, _, repo, _ = build_monitor(
            fills=(close_fill,),
            open_trades=(trade,),
        )
        result = await monitor.run_cycle()
        assert result.trades_closed == 1
        req = repo.close_trade.await_args.args[0]
        assert req.exit_reason == "TP"
        assert req.pnl_usd == Decimal("4.0")
        assert req.mfe_pct == Decimal("3.5")
        assert req.mae_pct == Decimal("-1.0")

    @pytest.mark.asyncio
    async def test_sl_hit_closes_with_sl_reason(self) -> None:
        trade = make_trade(
            is_filled=True,
            tp_price=Decimal("67000"),
            sl_price=Decimal("64000"),
        )
        close_fill = make_fill(
            side="sell",
            price=Decimal("64000"),
            closed_pnl=Decimal("-2.0"),
        )
        monitor, _, repo, _ = build_monitor(
            fills=(close_fill,),
            open_trades=(trade,),
        )
        result = await monitor.run_cycle()
        assert result.trades_closed == 1
        req = repo.close_trade.await_args.args[0]
        assert req.exit_reason == "SL"

    @pytest.mark.asyncio
    async def test_short_close_uses_buy_side(self) -> None:
        trade = make_trade(
            direction="SHORT",
            is_filled=True,
            tp_price=Decimal("63000"),
            sl_price=Decimal("66000"),
        )
        close_fill = make_fill(
            side="buy",
            price=Decimal("63000"),
            closed_pnl=Decimal("4.0"),
        )
        monitor, _, repo, _ = build_monitor(
            fills=(close_fill,),
            open_trades=(trade,),
        )
        result = await monitor.run_cycle()
        assert result.trades_closed == 1
        req = repo.close_trade.await_args.args[0]
        assert req.exit_reason == "TP"

    @pytest.mark.asyncio
    async def test_close_without_filled_trade_ignored(self) -> None:
        trade = make_trade(is_filled=False)
        close_fill = make_fill(side="sell", closed_pnl=Decimal("4.0"))
        monitor, _, repo, _ = build_monitor(
            fills=(close_fill,),
            open_trades=(trade,),
        )
        result = await monitor.run_cycle()
        assert result.trades_closed == 0
        repo.close_trade.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_close_fill_with_unrelated_symbol_ignored(self) -> None:
        # ETH の close fill が来ても BTC trade にはマッチしない
        trade = make_trade(symbol="BTC", is_filled=True)
        close_fill = make_fill(
            symbol="ETH",
            side="sell",
            closed_pnl=Decimal("4.0"),
        )
        monitor, _, repo, _ = build_monitor(
            fills=(close_fill,),
            open_trades=(trade,),
        )
        result = await monitor.run_cycle()
        assert result.trades_closed == 0
        repo.close_trade.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_negative_pnl_notification(self) -> None:
        trade = make_trade(is_filled=True)
        close_fill = make_fill(
            side="sell",
            price=Decimal("64000"),
            closed_pnl=Decimal("-5"),
        )
        monitor, _, _, notifier = build_monitor(
            fills=(close_fill,),
            open_trades=(trade,),
        )
        await monitor.run_cycle()
        msg = notifier.send_signal.await_args.args[0]
        assert "PnL=-5" in msg


# ─── MFE/MAE 更新 ─────────────────────────


class TestMfeMaeUpdate:
    @pytest.mark.asyncio
    async def test_long_profit_updates_mfe(self) -> None:
        trade = make_trade(
            is_filled=True,
            entry_price=Decimal("65000"),
            mfe_pct=Decimal("0"),
            mae_pct=Decimal("0"),
        )
        # current ≈ 65000 + 2/0.0002 = 75000 → +15.4%
        position = make_position(
            entry_price=Decimal("65000"),
            size=Decimal("0.0002"),
            unrealized_pnl=Decimal("2"),
        )
        monitor, _, repo, _ = build_monitor(
            open_trades=(trade,),
            positions=(position,),
        )
        await monitor.run_cycle()
        repo.update_mfe_mae.assert_awaited_once()
        kwargs = repo.update_mfe_mae.await_args.kwargs
        assert kwargs["mfe_pct"] > Decimal("0")
        assert kwargs["mae_pct"] == Decimal("0")

    @pytest.mark.asyncio
    async def test_long_loss_updates_mae(self) -> None:
        trade = make_trade(
            is_filled=True,
            entry_price=Decimal("65000"),
            mfe_pct=Decimal("0"),
            mae_pct=Decimal("0"),
        )
        position = make_position(
            entry_price=Decimal("65000"),
            size=Decimal("0.0002"),
            unrealized_pnl=Decimal("-2"),
        )
        monitor, _, repo, _ = build_monitor(
            open_trades=(trade,),
            positions=(position,),
        )
        await monitor.run_cycle()
        kwargs = repo.update_mfe_mae.await_args.kwargs
        assert kwargs["mae_pct"] < Decimal("0")
        assert kwargs["mfe_pct"] == Decimal("0")

    @pytest.mark.asyncio
    async def test_short_profit_updates_mfe(self) -> None:
        trade = make_trade(
            direction="SHORT",
            is_filled=True,
            entry_price=Decimal("65000"),
            mfe_pct=Decimal("0"),
            mae_pct=Decimal("0"),
        )
        # SHORT: size is negative, profit when price drops
        position = make_position(
            entry_price=Decimal("65000"),
            size=Decimal("-0.0002"),
            unrealized_pnl=Decimal("2"),
        )
        monitor, _, repo, _ = build_monitor(
            open_trades=(trade,),
            positions=(position,),
        )
        await monitor.run_cycle()
        kwargs = repo.update_mfe_mae.await_args.kwargs
        assert kwargs["mfe_pct"] > Decimal("0")

    @pytest.mark.asyncio
    async def test_unfilled_trade_skipped(self) -> None:
        trade = make_trade(is_filled=False)
        position = make_position()
        monitor, _, repo, _ = build_monitor(
            open_trades=(trade,),
            positions=(position,),
        )
        await monitor.run_cycle()
        repo.update_mfe_mae.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_position_without_trade_skipped(self) -> None:
        # 外部ポジション（DB に該当 trade なし）
        position = make_position(symbol="ETH")
        monitor, _, repo, _ = build_monitor(
            open_trades=(),
            positions=(position,),
        )
        await monitor.run_cycle()
        repo.update_mfe_mae.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_positions_skips_open_trades_lookup(self) -> None:
        monitor, _, repo, _ = build_monitor(positions=())
        await monitor.run_cycle()
        repo.update_mfe_mae.assert_not_awaited()

    def test_estimate_current_price_zero_size_returns_entry(self) -> None:
        pos = make_position(size=Decimal("0"), entry_price=Decimal("65000"))
        assert PositionMonitor._estimate_current_price(pos) == Decimal("65000")

    def test_unrealized_pnl_pct_zero_entry_returns_zero(self) -> None:
        trade = make_trade(entry_price=Decimal("0"))
        assert (
            PositionMonitor._unrealized_pnl_pct(trade, Decimal("100"))
            == Decimal("0")
        )


# ─── Funding 強制決済 ────────────────────


_TARGET = "src.application.position_monitor.datetime"


class TestFundingForceClose:
    @pytest.mark.asyncio
    async def test_force_close_when_within_window(self) -> None:
        # 13:57:00 UTC — 3 minutes until next funding (14:00)
        fixed_now = datetime(2026, 4, 28, 13, 57, 0, tzinfo=UTC)
        position = make_position()
        monitor, exchange, _, notifier = build_monitor(positions=(position,))
        with patch(_TARGET) as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            result = await monitor.run_cycle()
        assert result.forced_closes == 1
        exchange.place_order.assert_awaited_once()
        notifier.send_signal.assert_awaited()

    @pytest.mark.asyncio
    async def test_no_force_close_when_far_from_funding(self) -> None:
        # 13:30:00 UTC — 30 minutes until funding
        fixed_now = datetime(2026, 4, 28, 13, 30, 0, tzinfo=UTC)
        position = make_position()
        monitor, exchange, _, _ = build_monitor(positions=(position,))
        with patch(_TARGET) as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            result = await monitor.run_cycle()
        assert result.forced_closes == 0
        exchange.place_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_disabled_funding_close_does_nothing(self) -> None:
        fixed_now = datetime(2026, 4, 28, 13, 57, 0, tzinfo=UTC)
        position = make_position()
        monitor, exchange, _, _ = build_monitor(
            positions=(position,),
            config=make_config(funding_close_enabled=False),
        )
        with patch(_TARGET) as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            result = await monitor.run_cycle()
        assert result.forced_closes == 0
        exchange.place_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_zero_size_position_skipped(self) -> None:
        fixed_now = datetime(2026, 4, 28, 13, 57, 0, tzinfo=UTC)
        position = make_position(size=Decimal("0"))
        monitor, exchange, _, _ = build_monitor(positions=(position,))
        with patch(_TARGET) as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            result = await monitor.run_cycle()
        assert result.forced_closes == 0
        exchange.place_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_short_force_close_uses_buy_side(self) -> None:
        fixed_now = datetime(2026, 4, 28, 13, 57, 0, tzinfo=UTC)
        position = make_position(size=Decimal("-0.0002"))
        monitor, exchange, _, _ = build_monitor(positions=(position,))
        with patch(_TARGET) as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            await monitor.run_cycle()
        req = exchange.place_order.await_args.args[0]
        assert req.side == "buy"
        assert req.size == Decimal("0.0002")
        # ask 65005 * (1 + 0.005) = 65330.025
        assert req.price > Decimal("65005")

    @pytest.mark.asyncio
    async def test_long_force_close_uses_sell_side(self) -> None:
        fixed_now = datetime(2026, 4, 28, 13, 57, 0, tzinfo=UTC)
        position = make_position(size=Decimal("0.0002"))
        monitor, exchange, _, _ = build_monitor(positions=(position,))
        with patch(_TARGET) as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            await monitor.run_cycle()
        req = exchange.place_order.await_args.args[0]
        assert req.side == "sell"
        # bid 64995 * (1 - 0.005) = 64670.025
        assert req.price < Decimal("64995")

    @pytest.mark.asyncio
    async def test_force_close_failure_alerts_but_continues(self) -> None:
        fixed_now = datetime(2026, 4, 28, 13, 57, 0, tzinfo=UTC)
        # 2 ポジションのうち最初の force close は失敗、2 つめは成功
        pos1 = make_position(symbol="BTC")
        pos2 = make_position(symbol="ETH", entry_price=Decimal("3000"))
        # place_order を 1 回目だけ失敗させる
        call_count = {"n": 0}

        def fail_then_succeed(*args: Any, **kwargs: Any) -> OrderResult:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise ExchangeError("boom")
            return OrderResult(success=True, order_id=1)

        monitor, exchange, _, notifier = build_monitor(positions=(pos1, pos2))
        exchange.place_order = AsyncMock(side_effect=fail_then_succeed)
        with patch(_TARGET) as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            result = await monitor.run_cycle()
        assert result.forced_closes == 1  # 2 つめだけ成功
        notifier.send_alert.assert_awaited()  # 失敗したものは alert

    @pytest.mark.asyncio
    async def test_force_close_unsuccessful_order_no_signal(self) -> None:
        fixed_now = datetime(2026, 4, 28, 13, 57, 0, tzinfo=UTC)
        position = make_position()
        monitor, _, _, notifier = build_monitor(
            positions=(position,),
            place_order_result=OrderResult(success=False, order_id=None),
        )
        with patch(_TARGET) as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            await monitor.run_cycle()
        # place_order は呼ばれるが send_signal は呼ばれない
        notifier.send_signal.assert_not_awaited()


# ─── エラーハンドリング ──────────────────


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_get_fills_error_continues_cycle(self) -> None:
        monitor, _, _, _ = build_monitor(
            fills_side_effect=ExchangeError("fills down"),
        )
        result = await monitor.run_cycle()
        assert result.trades_filled == 0
        assert any("detect_fills" in e for e in result.errors)

    @pytest.mark.asyncio
    async def test_get_positions_error_continues_cycle(self) -> None:
        monitor, _, _, _ = build_monitor(
            positions_side_effect=ExchangeError("positions down"),
        )
        result = await monitor.run_cycle()
        assert result.open_position_count == 0
        assert any("get_positions" in e for e in result.errors)

    @pytest.mark.asyncio
    async def test_open_orders_error_during_tp_sl_link(self) -> None:
        # entry fill 検知中に open_orders 取得が失敗 → cycle 全体が
        # detect_fills エラーになるが落ちない
        trade = make_trade()
        fill = make_fill()
        monitor, _, _, _ = build_monitor(
            fills=(fill,),
            open_trades=(trade,),
            open_orders_side_effect=ExchangeError("api down"),
        )
        result = await monitor.run_cycle()
        assert any("detect_fills" in e for e in result.errors)


# ─── dedup_key（PR7.5d-fix） ─────────────


class TestDedupKeys:
    """各通知に dedup_key kwarg が正しく付与されることを確認。"""

    @pytest.mark.asyncio
    async def test_fill_uses_trade_id_dedup_key(self) -> None:
        trade = make_trade()
        monitor, _, _, notifier = build_monitor(
            fills=(make_fill(),),
            open_trades=(trade,),
        )
        await monitor.run_cycle()
        # FILL の send_signal が dedup_key=fill:{trade.id}
        fill_call = next(
            c for c in notifier.send_signal.await_args_list
            if "FILL" in c.args[0]
        )
        assert fill_call.kwargs["dedup_key"] == f"fill:{trade.id}"

    @pytest.mark.asyncio
    async def test_close_uses_trade_id_dedup_key(self) -> None:
        # 既に約定済み LONG trade に対して反対 side(sell) + closed_pnl != 0
        # の fill が来たら _on_trade_closed が走る
        trade = make_trade(is_filled=True, tp_price=Decimal("65500"))
        close_fill = make_fill(
            side="sell",
            price=Decimal("65500"),
            closed_pnl=Decimal("50"),
        )
        monitor, _, _, notifier = build_monitor(
            fills=(close_fill,),
            open_trades=(trade,),
        )
        await monitor.run_cycle()
        close_call = next(
            c for c in notifier.send_signal.await_args_list
            if c.args[0].startswith("CLOSE ")
        )
        assert close_call.kwargs["dedup_key"] == f"close:{trade.id}"

    @pytest.mark.asyncio
    async def test_force_close_uses_symbol_reason_dedup_key(self) -> None:
        fixed_now = datetime(2026, 4, 28, 13, 57, 0, tzinfo=UTC)
        position = make_position()
        monitor, _, _, notifier = build_monitor(positions=(position,))
        with patch(_TARGET) as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            await monitor.run_cycle()
        signal_call = next(
            c for c in notifier.send_signal.await_args_list
            if "FORCE_CLOSE" in c.args[0]
        )
        assert signal_call.kwargs["dedup_key"] == (
            f"force_close:{position.symbol}:FUNDING"
        )

    @pytest.mark.asyncio
    async def test_force_close_failure_uses_symbol_dedup_key(self) -> None:
        fixed_now = datetime(2026, 4, 28, 13, 57, 0, tzinfo=UTC)
        pos = make_position()
        monitor, exchange, _, notifier = build_monitor(positions=(pos,))
        exchange.place_order = AsyncMock(side_effect=ExchangeError("boom"))
        with patch(_TARGET) as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            await monitor.run_cycle()
        notifier.send_alert.assert_awaited_once()
        assert notifier.send_alert.call_args.kwargs["dedup_key"] == (
            f"force_close_fail:{pos.symbol}"
        )


# ─── 結果オブジェクト ────────────────────


class TestMonitorCycleResult:
    @pytest.mark.asyncio
    async def test_empty_cycle_returns_zeros(self) -> None:
        monitor, _, _, _ = build_monitor()
        result = await monitor.run_cycle()
        assert result == MonitorCycleResult(
            trades_filled=0,
            trades_closed=0,
            open_position_count=0,
            forced_closes=0,
            errors=(),
        )

    def test_result_is_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        result = MonitorCycleResult(
            trades_filled=0,
            trades_closed=0,
            open_position_count=0,
            forced_closes=0,
            errors=(),
        )
        with pytest.raises(FrozenInstanceError):
            result.trades_filled = 5  # type: ignore[misc]
