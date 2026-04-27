"""ADAPTERS Protocol の構造テスト。

Protocol 自体には実装がないため、以下を検証する:
1. データクラスの不変性
2. 例外階層
3. モック実装が Protocol を満たす（型チェック相当）
"""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from datetime import datetime
from decimal import Decimal
from typing import Literal

import pytest

from src.adapters.exchange import (
    DuplicateOrderError,
    ExchangeError,
    ExchangeProtocol,
    Fill,
    FundingPayment,
    L2Book,
    L2BookLevel,
    Order,
    OrderRejectedError,
    OrderRequest,
    OrderResult,
    Position,
    RateLimitError,
    SymbolMeta,
    TriggerOrderRequest,
)
from src.adapters.notifier import Notifier
from src.adapters.repository import (
    Repository,
    SignalLog,
    Trade,
    TradeCloseRequest,
    TradeOpenRequest,
)
from src.adapters.sentiment import (
    SentimentError,
    SentimentProvider,
    SentimentResult,
    SentimentSource,
    SentimentTimeoutError,
)
from src.core.models import EntryDecision, MarketSnapshot

# ────────────────────────────────────────────────
# A. データクラス境界
# ────────────────────────────────────────────────


class TestExchangeDataClasses:
    def test_l2_book_level_immutable(self) -> None:
        level = L2BookLevel(price=Decimal("100"), size=Decimal("1"), n_orders=3)
        with pytest.raises(FrozenInstanceError):
            level.price = Decimal("999")  # type: ignore[misc]

    def test_l2_book_with_levels(self) -> None:
        book = L2Book(
            symbol="BTC",
            bids=(L2BookLevel(Decimal("99"), Decimal("1"), 1),),
            asks=(L2BookLevel(Decimal("100"), Decimal("1"), 1),),
            timestamp_ms=1700000000000,
        )
        assert book.symbol == "BTC"
        assert len(book.bids) == 1
        assert book.asks[0].price == Decimal("100")

    def test_order_request_default_reduce_only_false(self) -> None:
        req = OrderRequest(
            symbol="BTC",
            side="buy",
            size=Decimal("0.01"),
            price=Decimal("65000"),
            tif="Alo",
        )
        assert req.reduce_only is False
        assert req.client_order_id is None

    def test_trigger_order_default_reduce_only_true(self) -> None:
        # TP/SL は reduce_only がデフォルトで True（誤って増ポジションしない）。
        req = TriggerOrderRequest(
            symbol="BTC",
            side="sell",
            size=Decimal("0.01"),
            trigger_price=Decimal("64000"),
            is_market=True,
            limit_price=None,
            tpsl="sl",
        )
        assert req.reduce_only is True

    def test_position_can_be_short(self) -> None:
        pos = Position(
            symbol="BTC",
            size=Decimal("-0.01"),
            entry_price=Decimal("65000"),
            unrealized_pnl=Decimal("0"),
            leverage=3,
            liquidation_price=Decimal("70000"),
        )
        assert pos.size < 0

    def test_symbol_meta_has_required_fields(self) -> None:
        meta = SymbolMeta(
            symbol="BTC",
            sz_decimals=5,
            max_leverage=50,
            tick_size=Decimal("0.1"),
        )
        assert meta.tick_size == Decimal("0.1")
        assert meta.sz_decimals == 5


class TestSentimentDataClasses:
    def test_sentiment_result_score_in_range(self) -> None:
        result = SentimentResult(
            score=Decimal("0.7"),
            confidence=Decimal("0.85"),
            direction="bullish",
            reasoning="positive news",
            source_count=5,
            cached=False,
        )
        assert -1 <= result.score <= 1
        assert 0 <= result.confidence <= 1

    def test_sentiment_source_immutable(self) -> None:
        src = SentimentSource(
            source="coindesk",
            title="BTC up",
            body="Bitcoin reached new high",
            published_at_ms=1700000000000,
            url="https://example.com/news/1",
        )
        with pytest.raises(FrozenInstanceError):
            src.title = "modified"  # type: ignore[misc]


class TestRepositoryDataClasses:
    def test_trade_open_request_holds_decision(self) -> None:
        decision = EntryDecision(
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
        req = TradeOpenRequest(
            symbol="BTC",
            direction="LONG",
            entry_price=Decimal("65000"),
            size_coins=Decimal("0.01"),
            sl_price=Decimal("64000"),
            tp_price=Decimal("67000"),
            leverage=3,
            is_dry_run=False,
            decision=decision,
        )
        assert req.symbol == "BTC"
        assert req.decision.should_enter is True

    def test_trade_close_request_immutable(self) -> None:
        req = TradeCloseRequest(
            trade_id=1,
            exit_price=Decimal("66000"),
            exit_reason="TP",
            pnl_usd=Decimal("100"),
            fee_usd_total=Decimal("0.5"),
            funding_paid_usd=Decimal("0.1"),
            mfe_pct=Decimal("1.5"),
            mae_pct=Decimal("-0.3"),
        )
        with pytest.raises(FrozenInstanceError):
            req.trade_id = 999  # type: ignore[misc]

    def test_signal_log_with_rejection(self) -> None:
        log = SignalLog(
            timestamp=datetime(2026, 4, 27, 12, 0, 0),
            symbol="BTC",
            direction="LONG",
            layer="MOMENTUM",
            passed=False,
            rejection_reason="vwap_too_far",
            snapshot_excerpt='{"vwap":65000}',
        )
        assert log.passed is False
        assert log.rejection_reason == "vwap_too_far"


# ────────────────────────────────────────────────
# B. 例外階層
# ────────────────────────────────────────────────


class TestExchangeExceptions:
    def test_order_rejected_error_with_code(self) -> None:
        err = OrderRejectedError("ALO would match", code="ALO_REJECT")
        assert err.code == "ALO_REJECT"
        assert "ALO" in str(err)

    def test_order_rejected_error_without_code(self) -> None:
        err = OrderRejectedError("rejected")
        assert err.code is None

    def test_rate_limit_inherits_exchange_error(self) -> None:
        assert issubclass(RateLimitError, ExchangeError)

    def test_duplicate_order_inherits_exchange_error(self) -> None:
        assert issubclass(DuplicateOrderError, ExchangeError)

    def test_order_rejected_inherits_exchange_error(self) -> None:
        assert issubclass(OrderRejectedError, ExchangeError)


class TestSentimentExceptions:
    def test_timeout_inherits_sentiment_error(self) -> None:
        assert issubclass(SentimentTimeoutError, SentimentError)


# ────────────────────────────────────────────────
# C. Protocol 互換性（モック実装が Protocol を満たす）
# ────────────────────────────────────────────────


class FakeExchange:
    """ExchangeProtocol を満たす最小モック。"""

    async def get_symbols(self) -> tuple[SymbolMeta, ...]:
        return ()

    async def get_l2_book(self, symbol: str) -> L2Book:
        return L2Book(symbol=symbol, bids=(), asks=(), timestamp_ms=0)

    async def get_market_snapshot(self, symbol: str) -> MarketSnapshot:
        return MarketSnapshot(
            symbol=symbol,
            current_price=100.0,
            vwap=100.0,
            momentum_5bar_pct=0.0,
            utc_open_price=100.0,
            rolling_24h_open=100.0,
            high_24h=100.0,
            low_24h=100.0,
            flow_buy_sell_ratio=1.0,
            flow_large_order_count=0,
            volume_surge_ratio=1.0,
            sentiment_score=0.0,
            sentiment_confidence=0.0,
        )

    async def get_funding_rate_8h(self, symbol: str) -> Decimal:
        return Decimal("0")

    async def get_open_interest(self, symbol: str) -> Decimal:
        return Decimal("0")

    async def get_positions(self) -> tuple[Position, ...]:
        return ()

    async def get_open_orders(self) -> tuple[Order, ...]:
        return ()

    async def get_fills(self, since_ms: int) -> tuple[Fill, ...]:
        return ()

    async def get_funding_payments(self, since_ms: int) -> tuple[FundingPayment, ...]:
        return ()

    async def get_account_balance_usd(self) -> Decimal:
        return Decimal("0")

    async def place_order(self, request: OrderRequest) -> OrderResult:
        return OrderResult(success=True, order_id=1)

    async def place_trigger_order(self, request: TriggerOrderRequest) -> OrderResult:
        return OrderResult(success=True, order_id=1)

    async def place_orders_grouped(
        self,
        entry: OrderRequest,
        tp: TriggerOrderRequest | None,
        sl: TriggerOrderRequest | None,
    ) -> tuple[OrderResult, ...]:
        return (OrderResult(success=True, order_id=1),)

    async def cancel_order(self, order_id: int) -> bool:
        return True

    async def get_order_by_client_id(self, client_order_id: str) -> Order | None:
        return None

    async def get_order_status(
        self, order_id: int
    ) -> Literal["pending", "filled", "cancelled", "rejected"]:
        return "filled"

    async def get_tick_size(self, symbol: str) -> Decimal:
        return Decimal("0.1")

    async def get_sz_decimals(self, symbol: str) -> int:
        return 5


class FakeNotifier:
    """Notifier を満たす最小モック。"""

    async def send_signal(self, message: str, dedup_key: str | None = None) -> None:
        return None

    async def send_alert(self, message: str, dedup_key: str | None = None) -> None:
        return None

    async def send_summary(self, message: str) -> None:
        return None

    async def send_error(self, message: str, exception: Exception | None = None) -> None:
        return None


class TestProtocolCompatibility:
    def test_fake_exchange_satisfies_protocol(self) -> None:
        # 構造的サブタイピングで型チェック相当の確認（mypy が変数代入で検出する）。
        fake: ExchangeProtocol = FakeExchange()
        assert hasattr(fake, "get_symbols")
        assert hasattr(fake, "place_order")
        assert hasattr(fake, "place_orders_grouped")

    def test_fake_notifier_satisfies_protocol(self) -> None:
        notifier: Notifier = FakeNotifier()
        assert hasattr(notifier, "send_signal")
        assert hasattr(notifier, "send_alert")
        assert hasattr(notifier, "send_summary")
        assert hasattr(notifier, "send_error")

    def test_protocol_methods_are_awaitable(self) -> None:
        fake: ExchangeProtocol = FakeExchange()

        async def run() -> None:
            symbols = await fake.get_symbols()
            assert isinstance(symbols, tuple)
            balance = await fake.get_account_balance_usd()
            assert isinstance(balance, Decimal)
            result = await fake.place_order(
                OrderRequest(
                    symbol="BTC",
                    side="buy",
                    size=Decimal("0.01"),
                    price=Decimal("65000"),
                    tif="Alo",
                )
            )
            assert result.success is True

        asyncio.run(run())

    def test_notifier_methods_are_awaitable(self) -> None:
        notifier: Notifier = FakeNotifier()

        async def run() -> None:
            await notifier.send_signal("test")
            await notifier.send_alert("test", dedup_key="key1")
            await notifier.send_summary("test")
            await notifier.send_error("test", exception=ValueError("boom"))

        asyncio.run(run())


# ────────────────────────────────────────────────
# D. import smoke: 主要な Protocol / 型が import 可能であることを確認
# ────────────────────────────────────────────────


class TestImportSmoke:
    def test_all_exchange_symbols_importable(self) -> None:
        for cls in (
            L2Book,
            L2BookLevel,
            Position,
            Order,
            Fill,
            FundingPayment,
            OrderRequest,
            TriggerOrderRequest,
            OrderResult,
            SymbolMeta,
            ExchangeError,
            ExchangeProtocol,
        ):
            assert cls is not None

    def test_all_repository_symbols_importable(self) -> None:
        for cls in (
            Trade,
            TradeOpenRequest,
            TradeCloseRequest,
            SignalLog,
            Repository,
        ):
            assert cls is not None

    def test_all_sentiment_symbols_importable(self) -> None:
        for cls in (
            SentimentSource,
            SentimentResult,
            SentimentError,
            SentimentTimeoutError,
            SentimentProvider,
        ):
            assert cls is not None
