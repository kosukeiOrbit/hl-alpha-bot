"""APPLICATION 層 entry_flow のテスト。

Protocol 実装はすべて AsyncMock で差し替え、INFRASTRUCTURE には触らない。
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any, Literal
from unittest.mock import AsyncMock

import pytest

from src.adapters.exchange import (
    ExchangeError,
    OrderRejectedError,
    OrderResult,
)
from src.adapters.sentiment import SentimentResult
from src.application.entry_flow import (
    EntryAttempt,
    EntryFlow,
    EntryFlowConfig,
)
from src.core.models import EntryDecision, MarketSnapshot

# ─── 共通ファクトリ ─────────────────────────────────


def make_config(**overrides: Any) -> EntryFlowConfig:
    base = {
        "is_dry_run": False,
        "leverage": 3,
        "flow_layer_enabled": True,
        "position_size_pct": Decimal("0.05"),
        "sl_atr_mult": Decimal("1.5"),
        "tp_atr_mult": Decimal("3.0"),
        "oi_lookup_tolerance_minutes": 5,
    }
    base.update(overrides)
    return EntryFlowConfig(**base)  # type: ignore[arg-type]


def make_passing_long_snapshot(**overrides: Any) -> MarketSnapshot:
    """LONG の 4 層 AND を全部通す MarketSnapshot。"""
    base: dict[str, Any] = {
        "symbol": "ETH",
        "current_price": 3000.0,
        # vwap_distance_pct = (3000-2994)/2994*100 ≈ 0.20% ∈ (0, 0.5)
        "vwap": 2994.0,
        # momentum > 0.3
        "momentum_5bar_pct": 0.5,
        # utc_day_change_pct = (3000-2980)/2980 ≈ +0.67% < 5%
        "utc_open_price": 2980.0,
        # rolling_24h_change_pct = (3000-2900)/2900 ≈ +3.4% < 10%
        "rolling_24h_open": 2900.0,
        # position_in_24h_range = (3000-2800)/(3050-2800) = 0.8 < 0.85
        "high_24h": 3050.0,
        "low_24h": 2800.0,
        # FLOW: ratio>1.5, large_order>0, surge>1.5
        "flow_buy_sell_ratio": 2.0,
        "flow_large_order_count": 3,
        "volume_surge_ratio": 2.0,
        # SENTIMENT
        "sentiment_score": 0.8,
        "sentiment_confidence": 0.9,
        "sentiment_flags": {"has_hack": False, "has_regulation": False},
        # REGIME
        "btc_ema_trend": "UPTREND",
        "btc_atr_pct": 2.0,
        "funding_rate": 0.005,
        # OI 変化 ±10% 未満
        "open_interest": 100.0,
        "open_interest_1h_ago": 100.0,
    }
    base.update(overrides)
    return MarketSnapshot(**base)


def make_btc_snapshot(**overrides: Any) -> MarketSnapshot:
    """BTC レジーム参照用の snapshot（UPTREND・低 ATR）。"""
    base: dict[str, Any] = {
        "symbol": "BTC",
        "current_price": 70000.0,
        "vwap": 69500.0,
        "momentum_5bar_pct": 0.0,
        "utc_open_price": 69000.0,
        "rolling_24h_open": 68000.0,
        "high_24h": 71000.0,
        "low_24h": 69000.0,
        "flow_buy_sell_ratio": 1.0,
        "flow_large_order_count": 0,
        "volume_surge_ratio": 1.0,
        "sentiment_score": 0.0,
        "sentiment_confidence": 0.0,
        "sentiment_flags": {},
        "btc_ema_trend": "UPTREND",
        "btc_atr_pct": 0.0,
        "funding_rate": 0.0,
        "open_interest": 0.0,
        "open_interest_1h_ago": 0.0,
    }
    base.update(overrides)
    return MarketSnapshot(**base)


def make_sentiment(score: float = 0.8, confidence: float = 0.9) -> SentimentResult:
    return SentimentResult(
        score=Decimal(str(score)),
        confidence=Decimal(str(confidence)),
        direction="bullish" if score > 0 else "bearish" if score < 0 else "neutral",
        reasoning="test",
        source_count=3,
        cached=False,
    )


def make_grouped_results(
    entry_oid: int | None = 1001,
    tp_oid: int | None = None,
    sl_oid: int | None = None,
    entry_success: bool = True,
) -> tuple[OrderResult, ...]:
    """grouped 発注のレスポンスを模倣。tp/sl は order_id=None でも OK。"""
    return (
        OrderResult(success=entry_success, order_id=entry_oid),
        OrderResult(success=True, order_id=tp_oid),
        OrderResult(success=True, order_id=sl_oid),
    )


def build_flow(
    *,
    eth_snapshot: MarketSnapshot | None = None,
    btc_snapshot: MarketSnapshot | None = None,
    sentiment_result: SentimentResult | None = None,
    config: EntryFlowConfig | None = None,
    balance: Decimal = Decimal("1000"),
    sz_decimals: int = 4,
    tick_size: Decimal = Decimal("0.1"),
    consecutive_losses: int = 0,
    grouped_results: tuple[OrderResult, ...] | None = None,
    grouped_side_effect: BaseException | None = None,
    oi_at_value: Decimal | None = Decimal("100"),
) -> tuple[EntryFlow, Any, Any, Any, Any]:
    """EntryFlow + AsyncMock の組を返す。"""
    eth_snap = eth_snapshot or make_passing_long_snapshot()
    btc_snap = btc_snapshot or make_btc_snapshot()
    sentiment_res = sentiment_result or make_sentiment()

    exchange = AsyncMock()

    async def get_market_snapshot(sym: str) -> MarketSnapshot:
        return btc_snap if sym == "BTC" else eth_snap

    exchange.get_market_snapshot.side_effect = get_market_snapshot
    exchange.get_open_interest = AsyncMock(return_value=Decimal("100"))
    exchange.get_account_balance_usd = AsyncMock(return_value=balance)
    exchange.get_sz_decimals = AsyncMock(return_value=sz_decimals)
    exchange.get_tick_size = AsyncMock(return_value=tick_size)
    if grouped_side_effect is not None:
        exchange.place_orders_grouped = AsyncMock(side_effect=grouped_side_effect)
    else:
        exchange.place_orders_grouped = AsyncMock(
            return_value=grouped_results or make_grouped_results()
        )

    sentiment = AsyncMock()
    sentiment.judge_cached_or_fresh = AsyncMock(return_value=sentiment_res)

    repo = AsyncMock()
    repo.get_oi_at = AsyncMock(return_value=oi_at_value)
    repo.record_oi = AsyncMock()
    repo.get_consecutive_losses = AsyncMock(return_value=consecutive_losses)
    repo.log_signal = AsyncMock()
    repo.open_trade = AsyncMock(return_value=42)

    notifier = AsyncMock()

    flow = EntryFlow(
        exchange=exchange,
        sentiment=sentiment,
        repo=repo,
        notifier=notifier,
        config=config or make_config(),
    )
    return flow, exchange, sentiment, repo, notifier


# ─── snapshot 構築 ──────────────────────────────────


class TestBuildSnapshot:
    @pytest.mark.asyncio
    async def test_records_current_oi(self) -> None:
        flow, _, _, repo, _ = build_flow()
        await flow.evaluate_and_enter("ETH", "LONG")
        repo.record_oi.assert_awaited_once()
        symbol_arg, _, oi_arg = repo.record_oi.call_args[0]
        assert symbol_arg == "ETH"
        assert oi_arg == Decimal("100")

    @pytest.mark.asyncio
    async def test_uses_current_oi_when_history_missing(self) -> None:
        flow, _, _, _, _ = build_flow(oi_at_value=None)
        attempt = await flow.evaluate_and_enter("ETH", "LONG")
        # 履歴 None → oi_now を流用 → oi_change_1h_pct=0 のままで通る
        assert attempt.snapshot.open_interest == 100.0
        assert attempt.snapshot.open_interest_1h_ago == 100.0

    @pytest.mark.asyncio
    async def test_btc_symbol_reuses_market_snapshot(self) -> None:
        # BTC を評価する場合、BTC 用 snapshot を 2 回取らない。
        flow, exchange, _, _, _ = build_flow(
            eth_snapshot=make_passing_long_snapshot(symbol="BTC"),
        )
        await flow.evaluate_and_enter("BTC", "LONG")
        assert exchange.get_market_snapshot.await_count == 1


# ─── BTC レジーム簡易計算 ────────────────────────────


class TestBtcRegimeHelpers:
    def test_uptrend_when_above_24h_open(self) -> None:
        btc = make_btc_snapshot(current_price=70000.0, rolling_24h_open=68000.0)
        assert EntryFlow._calc_btc_ema_trend(btc) == "UPTREND"

    def test_downtrend_when_below_24h_open(self) -> None:
        btc = make_btc_snapshot(current_price=66000.0, rolling_24h_open=68000.0)
        assert EntryFlow._calc_btc_ema_trend(btc) == "DOWNTREND"

    def test_atr_pct_zero_when_price_zero(self) -> None:
        btc = make_btc_snapshot(current_price=0.0)
        assert EntryFlow._calc_btc_atr_pct(btc) == 0.0

    def test_atr_pct_basic(self) -> None:
        btc = make_btc_snapshot(current_price=70000.0, high_24h=71000.0, low_24h=69000.0)
        assert EntryFlow._calc_btc_atr_pct(btc) == pytest.approx(
            (71000.0 - 69000.0) / 70000.0 * 100
        )


# ─── ドライラン ─────────────────────────────────────


class TestDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_does_not_place_order(self) -> None:
        flow, exchange, _, repo, notifier = build_flow(
            config=make_config(is_dry_run=True),
        )
        attempt = await flow.evaluate_and_enter("ETH", "LONG")
        assert attempt.executed is False
        assert attempt.is_dry_run is True
        exchange.place_orders_grouped.assert_not_awaited()
        repo.open_trade.assert_not_awaited()
        notifier.send_signal.assert_awaited_once()
        assert "DRYRUN" in notifier.send_signal.call_args[0][0]

    @pytest.mark.asyncio
    async def test_dry_run_logs_four_signals(self) -> None:
        flow, _, _, repo, _ = build_flow(config=make_config(is_dry_run=True))
        await flow.evaluate_and_enter("ETH", "LONG")
        layers = [c.args[0].layer for c in repo.log_signal.await_args_list]
        assert layers == ["MOMENTUM", "FLOW", "SENTIMENT", "REGIME"]


# ─── FLOW bypass ────────────────────────────────────


class TestFlowBypass:
    @pytest.mark.asyncio
    async def test_bypass_promotes_should_enter_when_other_layers_pass(self) -> None:
        # FLOW を確実に落とす snapshot
        snap = make_passing_long_snapshot(flow_buy_sell_ratio=1.0)
        flow, exchange, _, repo, _ = build_flow(
            eth_snapshot=snap,
            config=make_config(flow_layer_enabled=False),
        )
        attempt = await flow.evaluate_and_enter("ETH", "LONG")
        assert attempt.decision.should_enter is True
        assert attempt.decision.layer_results["flow"] is True
        exchange.place_orders_grouped.assert_awaited_once()
        repo.open_trade.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_bypass_keeps_other_failures(self) -> None:
        # FLOW + MOMENTUM 両方落とす（momentum_5bar_pct=0）
        snap = make_passing_long_snapshot(
            flow_buy_sell_ratio=1.0, momentum_5bar_pct=0.0
        )
        flow, exchange, _, _, _ = build_flow(
            eth_snapshot=snap,
            config=make_config(flow_layer_enabled=False),
        )
        attempt = await flow.evaluate_and_enter("ETH", "LONG")
        assert attempt.decision.should_enter is False
        assert attempt.decision.layer_results["flow"] is True
        assert attempt.decision.layer_results["momentum"] is False
        assert attempt.decision.rejection_reason == "layer_momentum_failed"
        exchange.place_orders_grouped.assert_not_awaited()


# ─── 実発注 ─────────────────────────────────────────


class TestRealEntry:
    @pytest.mark.asyncio
    async def test_grouped_order_placed_on_pass(self) -> None:
        flow, exchange, _, repo, notifier = build_flow()
        attempt = await flow.evaluate_and_enter("ETH", "LONG")
        assert attempt.executed is True
        assert attempt.trade_id == 42
        assert attempt.rejected_reason is None
        exchange.place_orders_grouped.assert_awaited_once()
        repo.open_trade.assert_awaited_once()
        notifier.send_signal.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_short_uses_sell_side(self) -> None:
        # SHORT が通る snapshot を別途用意するのはコストが高いので、
        # LONG 通過 snapshot を流用しつつ direction="SHORT" を発注したい
        # → judge は通らないので、_execute_entry を直接呼ぶ。
        flow, exchange, _, _, _ = build_flow()
        snap = make_passing_long_snapshot()
        decision = EntryDecision(
            should_enter=True,
            direction="SHORT",
            rejection_reason=None,
            layer_results={
                "momentum": True,
                "flow": True,
                "regime": True,
                "sentiment": True,
            },
        )
        await flow._execute_entry(snap, "SHORT", decision)
        entry_arg = exchange.place_orders_grouped.call_args[0][0]
        assert entry_arg.side == "sell"
        tp_arg = exchange.place_orders_grouped.call_args[0][1]
        sl_arg = exchange.place_orders_grouped.call_args[0][2]
        assert tp_arg.side == "buy"
        assert sl_arg.side == "buy"

    @pytest.mark.asyncio
    async def test_zero_size_returns_rejected(self) -> None:
        # 残高ゼロでサイザーが size_too_small_after_rounding を返す
        flow, exchange, _, repo, _ = build_flow(balance=Decimal("0"))
        attempt = await flow.evaluate_and_enter("ETH", "LONG")
        assert attempt.executed is False
        assert attempt.rejected_reason == "size_too_small_after_rounding"
        exchange.place_orders_grouped.assert_not_awaited()
        repo.open_trade.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tp_sl_can_have_no_oid(self) -> None:
        # PR6.4.3 検証: tp/sl は entry 約定まで order_id=None で返ることがある
        results = make_grouped_results(entry_oid=999, tp_oid=None, sl_oid=None)
        flow, _, _, repo, _ = build_flow(grouped_results=results)
        attempt = await flow.evaluate_and_enter("ETH", "LONG")
        assert attempt.executed is True
        assert attempt.trade_id == 42
        repo.open_trade.assert_awaited_once()


# ─── エラーハンドリング ─────────────────────────────


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_alo_rejection_caught_and_returned(self) -> None:
        flow, _, _, _, notifier = build_flow(
            grouped_side_effect=OrderRejectedError("ALO rejected", code="ALO_REJECT")
        )
        attempt = await flow.evaluate_and_enter("ETH", "LONG")
        assert attempt.executed is False
        assert attempt.rejected_reason is not None
        assert "ALO rejected" in attempt.rejected_reason
        notifier.send_alert.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_exchange_error_caught(self) -> None:
        flow, _, _, _, _ = build_flow(
            grouped_side_effect=ExchangeError("network down"),
        )
        attempt = await flow.evaluate_and_enter("ETH", "LONG")
        assert attempt.executed is False
        assert "network down" in (attempt.rejected_reason or "")

    @pytest.mark.asyncio
    async def test_entry_no_oid_returns_rejected(self) -> None:
        # 発注は通ったが results[0] が success=False
        results = make_grouped_results(entry_oid=None, entry_success=False)
        results = (
            replace(results[0], rejected_reason="margin"),
            results[1],
            results[2],
        )
        flow, _, _, repo, _ = build_flow(grouped_results=results)
        attempt = await flow.evaluate_and_enter("ETH", "LONG")
        assert attempt.executed is False
        assert attempt.rejected_reason == "margin"
        repo.open_trade.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_entry_success_but_oid_none_returns_rejected(self) -> None:
        # success=True だが order_id が無い珍ケース → entry_not_filled
        results = make_grouped_results(entry_oid=None, entry_success=True)
        flow, _, _, repo, _ = build_flow(grouped_results=results)
        attempt = await flow.evaluate_and_enter("ETH", "LONG")
        assert attempt.executed is False
        assert attempt.rejected_reason == "entry_not_filled"
        repo.open_trade.assert_not_awaited()


# ─── 判定で落ちるケース ─────────────────────────────


class TestJudgmentFailure:
    @pytest.mark.asyncio
    async def test_failed_decision_returns_rejection_reason(self) -> None:
        # MOMENTUM だけ落とす
        snap = make_passing_long_snapshot(momentum_5bar_pct=0.0)
        flow, exchange, _, repo, _ = build_flow(eth_snapshot=snap)
        attempt = await flow.evaluate_and_enter("ETH", "LONG")
        assert attempt.executed is False
        assert attempt.rejected_reason == "layer_momentum_failed"
        exchange.place_orders_grouped.assert_not_awaited()
        repo.open_trade.assert_not_awaited()
        # signals は記録される
        assert repo.log_signal.await_count == 4

    @pytest.mark.asyncio
    async def test_log_signals_records_reason_only_for_failed_layer(self) -> None:
        snap = make_passing_long_snapshot(momentum_5bar_pct=0.0)
        flow, _, _, repo, _ = build_flow(eth_snapshot=snap)
        await flow.evaluate_and_enter("ETH", "LONG")
        signals = [c.args[0] for c in repo.log_signal.await_args_list]
        momentum_log = next(s for s in signals if s.layer == "MOMENTUM")
        flow_log = next(s for s in signals if s.layer == "FLOW")
        assert momentum_log.passed is False
        assert momentum_log.rejection_reason == "layer_momentum_failed"
        # FLOW は通っているので reason は付かない
        assert flow_log.passed is True
        assert flow_log.rejection_reason is None


# ─── EntryAttempt ─────────────────────────────────


class TestEntryAttemptShape:
    def test_is_frozen(self) -> None:
        snap = make_passing_long_snapshot()
        decision = EntryDecision(
            should_enter=False,
            direction=None,
            rejection_reason="x",
            layer_results={"momentum": False},
        )
        attempt = EntryAttempt(
            symbol="ETH",
            direction="LONG",
            decision=decision,
            executed=False,
            is_dry_run=False,
            trade_id=None,
            rejected_reason="x",
            snapshot=snap,
        )
        from dataclasses import FrozenInstanceError

        with pytest.raises(FrozenInstanceError):
            attempt.executed = True  # type: ignore[misc]


# ─── dedup_key（PR7.5d-fix） ─────────────────────


class TestDedupKeys:
    """各通知に dedup_key kwarg が正しく付与されることを確認。"""

    @pytest.mark.asyncio
    async def test_dryrun_uses_symbol_direction_dedup_key(self) -> None:
        flow, _, _, _, notifier = build_flow(
            config=make_config(is_dry_run=True),
        )
        await flow.evaluate_and_enter("ETH", "LONG")
        notifier.send_signal.assert_awaited_once()
        assert notifier.send_signal.call_args.kwargs["dedup_key"] == (
            "dryrun:ETH:LONG"
        )

    @pytest.mark.asyncio
    async def test_real_entry_uses_trade_id_dedup_key(self) -> None:
        flow, _, _, _, notifier = build_flow()
        attempt = await flow.evaluate_and_enter("ETH", "LONG")
        assert attempt.executed is True
        notifier.send_signal.assert_awaited_once()
        assert notifier.send_signal.call_args.kwargs["dedup_key"] == (
            f"entry:{attempt.trade_id}"
        )

    @pytest.mark.asyncio
    async def test_entry_failure_uses_symbol_direction_dedup_key(self) -> None:
        flow, _, _, _, notifier = build_flow(
            grouped_side_effect=OrderRejectedError(
                "ALO rejected", code="ALO_REJECT"
            )
        )
        await flow.evaluate_and_enter("ETH", "LONG")
        notifier.send_alert.assert_awaited_once()
        assert notifier.send_alert.call_args.kwargs["dedup_key"] == (
            "entry_fail:ETH:LONG"
        )


# ─── direction Literal 型 ─────────────────────────


class TestDirectionLiteral:
    @pytest.mark.parametrize("direction", ["LONG", "SHORT"])
    @pytest.mark.asyncio
    async def test_evaluate_runs_for_both_directions(
        self, direction: Literal["LONG", "SHORT"]
    ) -> None:
        # SHORT は判定が通らない snapshot だが落ちない経路の確認だけ。
        flow, _, _, _, _ = build_flow()
        attempt = await flow.evaluate_and_enter("ETH", direction)
        assert attempt.symbol == "ETH"
        assert attempt.direction == direction
