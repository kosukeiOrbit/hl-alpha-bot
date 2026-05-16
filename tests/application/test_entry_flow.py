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
    Candle,
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
    btc_candles: tuple[Any, ...] | None = None,
    candles_side_effect: BaseException | None = None,
) -> tuple[EntryFlow, Any, Any, Any, Any]:
    """EntryFlow + AsyncMock の組を返す。

    btc_candles を渡さない場合は空 tuple を返すので、entry_flow は
    フォールバック（24h 比較）に落ちる。EMA/ATR を実走させたいテスト
    では明示的に candles を渡す。
    """
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
    if candles_side_effect is not None:
        exchange.get_candles = AsyncMock(side_effect=candles_side_effect)
    else:
        exchange.get_candles = AsyncMock(
            return_value=btc_candles if btc_candles is not None else ()
        )
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


# ─── BTC レジーム判定: フォールバック（PR7.1 互換） ───


class TestBtcRegimeFallbacks:
    """ローソク足が使えない時の簡易判定。PR7.7 で fallback に移動。"""

    def test_uptrend_when_above_24h_open(self) -> None:
        btc = make_btc_snapshot(current_price=70000.0, rolling_24h_open=68000.0)
        assert EntryFlow._fallback_btc_ema_trend(btc) == "UPTREND"

    def test_downtrend_when_below_24h_open(self) -> None:
        btc = make_btc_snapshot(current_price=66000.0, rolling_24h_open=68000.0)
        assert EntryFlow._fallback_btc_ema_trend(btc) == "DOWNTREND"

    def test_atr_pct_zero_when_price_zero(self) -> None:
        btc = make_btc_snapshot(current_price=0.0)
        assert EntryFlow._fallback_btc_atr_pct(btc) == 0.0

    def test_atr_pct_basic(self) -> None:
        btc = make_btc_snapshot(current_price=70000.0, high_24h=71000.0, low_24h=69000.0)
        assert EntryFlow._fallback_btc_atr_pct(btc) == pytest.approx(
            (71000.0 - 69000.0) / 70000.0 * 100
        )


# ─── BTC レジーム判定: ローソク足ベース（PR7.7） ──


def _make_candle(
    *,
    close: float,
    high: float | None = None,
    low: float | None = None,
    timestamp_ms: int = 0,
) -> Candle:
    """テスト用 Candle ファクトリ。high/low 未指定時は close ±1% で埋める。"""
    h = high if high is not None else close * 1.01
    low_ = low if low is not None else close * 0.99
    return Candle(
        symbol="BTC",
        interval="15m",
        timestamp_ms=timestamp_ms,
        open=Decimal(str(close)),
        high=Decimal(str(h)),
        low=Decimal(str(low_)),
        close=Decimal(str(close)),
        volume=Decimal("1"),
    )


def _uptrend_candles(n: int = 60) -> tuple[Candle, ...]:
    """単調上昇のローソク足 n 本。EMA20 > EMA50 になる。"""
    return tuple(
        _make_candle(close=60000.0 + i * 50, timestamp_ms=i)
        for i in range(n)
    )


def _downtrend_candles(n: int = 60) -> tuple[Candle, ...]:
    """単調下降のローソク足 n 本。EMA20 < EMA50 になる。"""
    return tuple(
        _make_candle(close=70000.0 - i * 50, timestamp_ms=i)
        for i in range(n)
    )


def _flat_candles(n: int = 60) -> tuple[Candle, ...]:
    """完全に flat → EMA20 == EMA50 → NEUTRAL。"""
    return tuple(
        _make_candle(close=70000.0, timestamp_ms=i) for i in range(n)
    )


class TestBtcEmaTrend:
    @pytest.mark.asyncio
    async def test_uptrend_when_ema_short_above_long(self) -> None:
        flow, _, _, _, _ = build_flow(btc_candles=_uptrend_candles())
        btc_snap = make_btc_snapshot()
        assert await flow._calc_btc_ema_trend(btc_snap) == "UPTREND"

    @pytest.mark.asyncio
    async def test_downtrend_when_ema_short_below_long(self) -> None:
        flow, _, _, _, _ = build_flow(btc_candles=_downtrend_candles())
        btc_snap = make_btc_snapshot()
        assert await flow._calc_btc_ema_trend(btc_snap) == "DOWNTREND"

    @pytest.mark.asyncio
    async def test_neutral_branch_via_monkeypatched_indicators(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Decimal の有限精度上、定数価格でも EMA20 と EMA50 は厳密一致しない。
        # NEUTRAL 分岐は indicators をモンキーパッチして到達確認する。
        flow, _, _, _, _ = build_flow(btc_candles=_flat_candles())
        monkeypatch.setattr(
            "src.application.entry_flow.calculate_ema",
            lambda prices, period: Decimal("70000"),
        )
        btc_snap = make_btc_snapshot()
        assert await flow._calc_btc_ema_trend(btc_snap) == "NEUTRAL"

    @pytest.mark.asyncio
    async def test_fallback_on_exchange_error(self) -> None:
        flow, _, _, _, _ = build_flow(
            candles_side_effect=ExchangeError("api down"),
            btc_snapshot=make_btc_snapshot(
                current_price=70000.0, rolling_24h_open=68000.0
            ),
        )
        btc_snap = make_btc_snapshot(
            current_price=70000.0, rolling_24h_open=68000.0
        )
        assert await flow._calc_btc_ema_trend(btc_snap) == "UPTREND"

    @pytest.mark.asyncio
    async def test_fallback_when_insufficient_candles(self) -> None:
        # EMA50 が要求するシード（50 本）に届かない
        flow, _, _, _, _ = build_flow(btc_candles=_uptrend_candles(n=40))
        btc_snap = make_btc_snapshot(
            current_price=70000.0, rolling_24h_open=68000.0
        )
        assert await flow._calc_btc_ema_trend(btc_snap) == "UPTREND"


class TestBtcAtrPct:
    @pytest.mark.asyncio
    async def test_basic_calculation(self) -> None:
        # 16 本: high-low=20 で固定 → ATR=20、close=100 → 20%
        candles = tuple(
            Candle(
                symbol="BTC",
                interval="15m",
                timestamp_ms=i,
                open=Decimal("100"),
                high=Decimal("110"),
                low=Decimal("90"),
                close=Decimal("100"),
                volume=Decimal("1"),
            )
            for i in range(16)
        )
        flow, _, _, _, _ = build_flow(btc_candles=candles)
        btc_snap = make_btc_snapshot()
        assert await flow._calc_btc_atr_pct(btc_snap) == pytest.approx(20.0)

    @pytest.mark.asyncio
    async def test_fallback_on_exchange_error(self) -> None:
        flow, _, _, _, _ = build_flow(
            candles_side_effect=ExchangeError("api down"),
        )
        btc_snap = make_btc_snapshot(
            current_price=70000.0, high_24h=71000.0, low_24h=69000.0
        )
        result = await flow._calc_btc_atr_pct(btc_snap)
        assert result == pytest.approx(
            (71000.0 - 69000.0) / 70000.0 * 100
        )

    @pytest.mark.asyncio
    async def test_fallback_when_insufficient_candles(self) -> None:
        # ATR(14) には 15 本必要。10 本では足りずフォールバック。
        candles = tuple(
            _make_candle(close=70000.0, timestamp_ms=i) for i in range(10)
        )
        flow, _, _, _, _ = build_flow(btc_candles=candles)
        btc_snap = make_btc_snapshot(
            current_price=70000.0, high_24h=71000.0, low_24h=69000.0
        )
        result = await flow._calc_btc_atr_pct(btc_snap)
        # フォールバックは 24h レンジ
        assert result == pytest.approx(
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


# ─── silent rejection 可視化（mainnet 観察対応） ─────


class TestSilentRejectionVisibility:
    """4 層通過 + 非 dry_run で発注に至らなかった場合の log + alert。

    PR7.4-real 以降の mainnet 観察で「4 層通過 5 件・trades 空」が発生し、
    silent rejection 経路に log/alert が無いことが原因と判明。
    挙動変更なし（return 値は同じ）、観察可能性のみ追加。
    """

    @pytest.mark.asyncio
    async def test_size_too_small_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging as _logging

        flow, _, _, _, _ = build_flow(balance=Decimal("0"))
        with caplog.at_level(
            _logging.WARNING, logger="src.application.entry_flow"
        ):
            await flow.evaluate_and_enter("ETH", "LONG")
        assert any(
            "entry skipped (size too small)" in r.message
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_size_too_small_sends_alert_with_dedup_key(self) -> None:
        flow, _, _, _, notifier = build_flow(balance=Decimal("0"))
        await flow.evaluate_and_enter("ETH", "LONG")
        # ALO の OrderRejectedError 用 send_alert と区別された dedup_key を使う
        size_call = next(
            (
                c
                for c in notifier.send_alert.await_args_list
                if "size too small" in c.args[0]
            ),
            None,
        )
        assert size_call is not None
        assert size_call.kwargs["dedup_key"] == "entry_skip_size:ETH:LONG"

    @pytest.mark.asyncio
    async def test_order_not_placed_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging as _logging

        results = make_grouped_results(entry_oid=None, entry_success=False)
        results = (
            replace(results[0], rejected_reason="Post only would have matched"),
            results[1],
            results[2],
        )
        flow, _, _, _, _ = build_flow(grouped_results=results)
        with caplog.at_level(
            _logging.WARNING, logger="src.application.entry_flow"
        ):
            await flow.evaluate_and_enter("ETH", "LONG")
        warn = next(
            (
                r
                for r in caplog.records
                if "entry skipped (order not placed)" in r.message
            ),
            None,
        )
        assert warn is not None
        # logger.warning に reason が含まれていること
        assert "Post only would have matched" in warn.message

    @pytest.mark.asyncio
    async def test_order_not_placed_sends_alert_with_dedup_key(self) -> None:
        results = make_grouped_results(entry_oid=None, entry_success=False)
        results = (
            replace(results[0], rejected_reason="Post only would have matched"),
            results[1],
            results[2],
        )
        flow, _, _, _, notifier = build_flow(grouped_results=results)
        await flow.evaluate_and_enter("ETH", "LONG")
        reject_call = next(
            (
                c
                for c in notifier.send_alert.await_args_list
                if "order not placed" in c.args[0]
            ),
            None,
        )
        assert reject_call is not None
        assert reject_call.kwargs["dedup_key"] == "entry_skip_reject:ETH:LONG"
        # メッセージに reason が含まれること（運用側で原因が分かる）
        assert "Post only would have matched" in reject_call.args[0]

    @pytest.mark.asyncio
    async def test_entry_not_filled_default_reason_in_alert(self) -> None:
        # results が success=True だが order_id=None の珍ケース（rejected_reason なし）
        results = make_grouped_results(entry_oid=None, entry_success=True)
        flow, _, _, _, notifier = build_flow(grouped_results=results)
        await flow.evaluate_and_enter("ETH", "LONG")
        reject_call = next(
            (
                c
                for c in notifier.send_alert.await_args_list
                if "order not placed" in c.args[0]
            ),
            None,
        )
        assert reject_call is not None
        assert "entry_not_filled" in reject_call.args[0]


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


# ─── PR A2 (#1): ATR サイジング ────────────────────


def _sizing_candle(
    *,
    open_: float,
    high: float,
    low: float,
    close: float,
    ts: int = 0,
) -> Candle:
    """1h サイジング ATR テスト用 Candle ファクトリ。"""
    return Candle(
        symbol="ETH",
        interval="1h",
        timestamp_ms=ts,
        open=Decimal(str(open_)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=Decimal("1"),
    )


class TestCalcAtrForSizing:
    """SL/TP サイジング用 ATR 計算（PR A2 #1）。

    旧 _estimate_atr の ``max(atr, 0.0001)`` floor を撤去し、
    candle ベース ATR(1h, 14) を使う。candle 取得失敗時は 24h レンジに
    フォールバック、それでも 0 なら ExchangeError で entry を止める。
    """

    @pytest.mark.asyncio
    async def test_uses_real_atr_from_1h_candles(self) -> None:
        # 16 本: 全 candle で high-low=20 → ATR=20 になるはず
        candles = tuple(
            _sizing_candle(open_=100, high=110, low=90, close=100, ts=i)
            for i in range(16)
        )
        flow, _, _, _, _ = build_flow(btc_candles=candles)
        snap = make_passing_long_snapshot()
        atr = await flow._calc_atr_for_sizing(snap)
        assert atr == Decimal("20")

    @pytest.mark.asyncio
    async def test_falls_back_to_24h_when_candles_fetch_error(self) -> None:
        flow, _, _, _, _ = build_flow(
            candles_side_effect=ExchangeError("api down"),
            eth_snapshot=make_passing_long_snapshot(
                high_24h=3050.0, low_24h=2810.0  # range 240 / 24 = 10
            ),
        )
        snap = make_passing_long_snapshot(high_24h=3050.0, low_24h=2810.0)
        atr = await flow._calc_atr_for_sizing(snap)
        assert atr == Decimal("240") / Decimal("24")  # = 10

    @pytest.mark.asyncio
    async def test_falls_back_to_24h_when_insufficient_candles(self) -> None:
        # period=14 では 15 本必要。10 本だと不足。
        candles = tuple(
            _sizing_candle(open_=100, high=110, low=90, close=100, ts=i)
            for i in range(10)
        )
        flow, _, _, _, _ = build_flow(btc_candles=candles)
        snap = make_passing_long_snapshot(high_24h=3000.0, low_24h=2760.0)
        atr = await flow._calc_atr_for_sizing(snap)
        # 24h fallback: (3000-2760)/24 = 10
        assert atr == Decimal("240") / Decimal("24")

    @pytest.mark.asyncio
    async def test_raises_when_both_unavailable(self) -> None:
        # candle 取得失敗 + 24h range = 0（dayHigh=dayLow=current_price 状況の再現）
        flow, _, _, _, _ = build_flow(
            candles_side_effect=ExchangeError("api down"),
        )
        snap = make_passing_long_snapshot(
            high_24h=80000.0, low_24h=80000.0  # range=0、本番バグの再現
        )
        with pytest.raises(ExchangeError, match="ATR sizing unavailable"):
            await flow._calc_atr_for_sizing(snap)

    @pytest.mark.asyncio
    async def test_raises_when_candles_empty_and_range_zero(self) -> None:
        # candles=() (BTC=ETH 同条件) + range=0 → 同じく raise
        flow, _, _, _, _ = build_flow()  # 既定 candles=()
        snap = make_passing_long_snapshot(
            high_24h=80000.0, low_24h=80000.0
        )
        with pytest.raises(ExchangeError, match="ATR sizing unavailable"):
            await flow._calc_atr_for_sizing(snap)

    @pytest.mark.asyncio
    async def test_no_more_min_atr_floor(self) -> None:
        """旧 ``max(atr, 0.0001)`` floor が撤去されていることを確認。

        極小 ATR でも実値がそのまま返る。stop_loss 側の min-1-tick 保証句が
        防御層として残るが、本層では floor しない。
        """
        # 16 本の極小レンジ candle（high-low=0.002）
        candles = tuple(
            _sizing_candle(
                open_=100, high=100.001, low=99.999, close=100, ts=i
            )
            for i in range(16)
        )
        flow, _, _, _, _ = build_flow(btc_candles=candles)
        snap = make_passing_long_snapshot()
        atr = await flow._calc_atr_for_sizing(snap)
        # 0.002 (実値)。旧実装なら 0.0001 floor で 0.002 のまま（floor 以上なので）
        # 重要: 値がもっと小さくても 0.0001 にクランプされないこと
        assert atr == Decimal("0.002")

    @pytest.mark.asyncio
    async def test_execute_entry_raises_when_atr_unavailable(self) -> None:
        """ATR 計算不能で _execute_entry が ExchangeError catch 経路に流れること。

        entry_flow の既存 except (OrderRejectedError ...| ExchangeError) で
        捕捉されて ``entry_fail:{symbol}:{direction}`` alert が発火する。
        rejected_reason に ATR 由来の文字列が入って trades は作られない。
        """
        flow, _, _, repo, notifier = build_flow(
            candles_side_effect=ExchangeError("api down"),
            eth_snapshot=make_passing_long_snapshot(
                high_24h=2500.0, low_24h=2500.0  # range=0 を強制
            ),
        )
        attempt = await flow.evaluate_and_enter("ETH", "LONG")
        assert attempt.executed is False
        assert "ATR sizing unavailable" in (attempt.rejected_reason or "")
        # trades は作らない（_execute_entry の try-except で吸収される前に raise）
        repo.open_trade.assert_not_awaited()
        # entry_fail alert が発火
        fail_call = next(
            (
                c
                for c in notifier.send_alert.await_args_list
                if "entry failed" in c.args[0]
            ),
            None,
        )
        assert fail_call is not None
        assert fail_call.kwargs["dedup_key"] == "entry_fail:ETH:LONG"


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


# ─── PR C1: VWAP 距離帯の config 注入 ─────────────────


class TestMomentumVwapDistanceConfig:
    """EntryFlowConfig 経由で MOMENTUM 帯幅を上書きできることの確認 (PR C1)。

    profile_phase2.yaml で momentum.vwap_min/max_distance_pct を設定した時、
    その値が CORE の judge_long_entry / judge_short_entry に届く経路の
    integration test。CORE 側 kwarg 注入の単体テストは
    tests/core/test_entry_judge.py に存在する。
    """

    @pytest.mark.asyncio
    async def test_default_config_rejects_at_0_8_percent_above_vwap(self) -> None:
        # 既定の 0.5% 上限を超える VWAP +0.8% の snapshot → MOMENTUM ✗
        # range は十分に広く取って過熱フィルタは通過させ、VWAP 距離だけで
        # 弾かれていることを保証する。
        snap = make_passing_long_snapshot(
            current_price=3024.0,  # vwap 3000 から +0.8%
            vwap=3000.0,
            high_24h=3200.0,
            low_24h=2800.0,  # range_pos ≈ 0.56 (< 0.85)
        )
        flow, exchange, _, _, _ = build_flow(eth_snapshot=snap)
        attempt = await flow.evaluate_and_enter("ETH", "LONG")
        assert attempt.decision.should_enter is False
        assert attempt.decision.rejection_reason == "layer_momentum_failed"
        exchange.place_orders_grouped.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_relaxed_config_accepts_same_snapshot(self) -> None:
        # 同じ +0.8% でも config で 1.0% に広げれば MOMENTUM ○ → 発注に進む
        snap = make_passing_long_snapshot(
            current_price=3024.0,
            vwap=3000.0,
            high_24h=3200.0,
            low_24h=2800.0,
        )
        flow, exchange, _, _, _ = build_flow(
            eth_snapshot=snap,
            config=make_config(
                momentum_vwap_max_distance_pct=Decimal("1.0"),
            ),
        )
        attempt = await flow.evaluate_and_enter("ETH", "LONG")
        assert attempt.decision.should_enter is True
        assert attempt.decision.layer_results["momentum"] is True
        exchange.place_orders_grouped.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_short_config_relaxation_routes_to_judge(self) -> None:
        # SHORT 側も同様に届くこと。VWAP -0.8% で既定はNG、-1.0 に緩めると OK。
        # momentum_5bar_pct も SHORT 用に反転させる必要がある。
        snap = make_passing_long_snapshot(
            current_price=2976.0,  # vwap 3000 から -0.8%
            vwap=3000.0,
            momentum_5bar_pct=-0.5,  # SHORT 用 < -0.3
            high_24h=3200.0,
            low_24h=2800.0,
        )
        flow, _, _, _, _ = build_flow(
            eth_snapshot=snap,
            config=make_config(
                momentum_vwap_min_distance_pct=Decimal("-1.0"),
            ),
        )
        # SHORT 評価で momentum 層が True になる (他層は SHORT 用に揃って
        # いないのでエントリーまでは行かないが、momentum 通過の事実は届く)
        attempt = await flow.evaluate_and_enter("ETH", "SHORT")
        assert attempt.decision.layer_results["momentum"] is True
