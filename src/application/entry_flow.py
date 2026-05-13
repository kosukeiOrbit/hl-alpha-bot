"""APPLICATION 層: entry_flow（章11.6）。

4層 AND 判定 → grouped 発注（entry + TP + SL）までの一連のフロー。
CORE 層の純関数と INFRASTRUCTURE 層の Protocol 実装を組み合わせる。

責務:
1. snapshot 構築（Exchange + Sentiment + Repository(OI履歴) を統合）
2. 4層 AND 判定（CORE 層の純関数を呼ぶ）
3. 各層判定結果のロギング（Repository）
4. 通過時はサイズ・SL/TP 計算 + grouped 発注
5. ドライランモード対応（is_dry_run=True なら発注スキップ・signals だけ記録）

PR6.4.3 の testnet 検証で判明した挙動への対応:
- grouped 発注の results[1]/results[2]（tp/sl）は entry 約定までは
  order_id=None で返ることがあるが success=True なら正常系として扱う。
  必須なのは results[0]（entry）の order_id。
- Repository.open_trade は entry 発注成功時点で trade_id を確定させる
  （実際の TP/SL の order_id 紐付けは PR7.2 position_monitor で約定検知後）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from src.adapters.exchange import (
    DuplicateOrderError,
    ExchangeError,
    ExchangeProtocol,
    OrderRejectedError,
    OrderRequest,
    RateLimitError,
    TriggerOrderRequest,
)
from src.adapters.notifier import Notifier
from src.adapters.repository import Repository, SignalLog, TradeOpenRequest
from src.adapters.sentiment import SentimentProvider
from src.core.entry_judge import judge_long_entry, judge_short_entry
from src.core.indicators import calculate_atr_pct, calculate_ema
from src.core.models import EntryDecision, MarketSnapshot
from src.core.position_sizer import SizingInput, calculate_position_size
from src.core.stop_loss import StopLossInput, calculate_sl_tp

# BTC レジーム判定用ローソク足設定（章4 ④ REGIME）
_BTC_REGIME_INTERVAL = "15m"
_BTC_EMA_LIMIT = 60  # EMA50 のシード(50) + マージン
_BTC_EMA_SHORT_PERIOD = 20
_BTC_EMA_LONG_PERIOD = 50
_BTC_ATR_LIMIT = 30  # ATR(14) は 15 本以上必要、マージン込み
_BTC_ATR_PERIOD = 14

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EntryFlowConfig:
    """entry_flow 動作設定（章23 settings.yaml から渡される）。"""

    is_dry_run: bool
    leverage: int
    flow_layer_enabled: bool  # False で FLOW 層を bypass（章11.6.3 Phase A）
    position_size_pct: Decimal  # SizingInput.position_size_pct
    sl_atr_mult: Decimal
    tp_atr_mult: Decimal
    oi_lookup_tolerance_minutes: int


@dataclass(frozen=True)
class EntryAttempt:
    """entry_flow の1回の試行結果。"""

    symbol: str
    direction: Literal["LONG", "SHORT"]
    decision: EntryDecision
    executed: bool  # True なら exchange に entry を投げた
    is_dry_run: bool
    trade_id: int | None
    rejected_reason: str | None
    snapshot: MarketSnapshot


_LAYER_NAMES: tuple[Literal["MOMENTUM", "FLOW", "SENTIMENT", "REGIME"], ...] = (
    "MOMENTUM",
    "FLOW",
    "SENTIMENT",
    "REGIME",
)


class EntryFlow:
    """4層 AND 判定 → grouped 発注のオーケストレータ。"""

    def __init__(
        self,
        exchange: ExchangeProtocol,
        sentiment: SentimentProvider,
        repo: Repository,
        notifier: Notifier,
        config: EntryFlowConfig,
    ) -> None:
        self.exchange = exchange
        self.sentiment = sentiment
        self.repo = repo
        self.notifier = notifier
        self.config = config

    async def evaluate_and_enter(
        self, symbol: str, direction: Literal["LONG", "SHORT"]
    ) -> EntryAttempt:
        """1 銘柄 1 方向の評価 + 発注を行う。"""
        snapshot = await self._build_snapshot(symbol, direction)
        decision = self._judge(snapshot, direction)
        await self._log_signals(snapshot, direction, decision)

        if not decision.should_enter:
            return EntryAttempt(
                symbol=symbol,
                direction=direction,
                decision=decision,
                executed=False,
                is_dry_run=self.config.is_dry_run,
                trade_id=None,
                rejected_reason=decision.rejection_reason,
                snapshot=snapshot,
            )

        if self.config.is_dry_run:
            await self.notifier.send_signal(
                f"[DRYRUN] {direction} {symbol} would enter at "
                f"{snapshot.current_price}",
                dedup_key=f"dryrun:{symbol}:{direction}",
            )
            return EntryAttempt(
                symbol=symbol,
                direction=direction,
                decision=decision,
                executed=False,
                is_dry_run=True,
                trade_id=None,
                rejected_reason=None,
                snapshot=snapshot,
            )

        return await self._execute_entry(snapshot, direction, decision)

    # ─── snapshot 構築 ───────────────────────────

    async def _build_snapshot(
        self, symbol: str, direction: Literal["LONG", "SHORT"]
    ) -> MarketSnapshot:
        market = await self.exchange.get_market_snapshot(symbol)
        sentiment = await self.sentiment.judge_cached_or_fresh(
            symbol=symbol, direction=direction
        )

        # BTC レジーム情報はローソク足から計算（章4 ④ REGIME）。
        # 取得失敗時は 24h スナップショットからの簡易判定にフォールバック。
        btc_snap = (
            market if symbol == "BTC" else await self.exchange.get_market_snapshot("BTC")
        )
        btc_ema_trend = await self._calc_btc_ema_trend(btc_snap)
        btc_atr_pct = await self._calc_btc_atr_pct(btc_snap)

        # OI 履歴
        oi_now = await self.exchange.get_open_interest(symbol)
        now_utc = datetime.now(UTC)
        oi_1h_ago = await self.repo.get_oi_at(
            symbol,
            now_utc - timedelta(hours=1),
            tolerance_minutes=self.config.oi_lookup_tolerance_minutes,
        )
        await self.repo.record_oi(symbol, now_utc, oi_now)
        oi_1h_ago_value = oi_1h_ago if oi_1h_ago is not None else oi_now

        return replace(
            market,
            sentiment_score=float(sentiment.score),
            sentiment_confidence=float(sentiment.confidence),
            btc_ema_trend=btc_ema_trend,
            btc_atr_pct=btc_atr_pct,
            open_interest=float(oi_now),
            open_interest_1h_ago=float(oi_1h_ago_value),
        )

    async def _calc_btc_ema_trend(self, btc_snap: MarketSnapshot) -> str:
        """BTC 15m EMA20 vs EMA50 でトレンド判定（章4 ④ REGIME）。

        Returns:
            "UPTREND" / "DOWNTREND" / "NEUTRAL"
            ローソク足取得失敗・本数不足時は 24h 比較にフォールバック。
        """
        try:
            candles = await self.exchange.get_candles(
                symbol="BTC",
                interval=_BTC_REGIME_INTERVAL,
                limit=_BTC_EMA_LIMIT,
            )
        except ExchangeError as e:
            logger.warning(
                "BTC candles fetch failed for EMA: %s, falling back", e
            )
            return self._fallback_btc_ema_trend(btc_snap)

        if len(candles) < _BTC_EMA_LONG_PERIOD:
            logger.warning(
                "Not enough BTC candles for EMA50: got %d", len(candles)
            )
            return self._fallback_btc_ema_trend(btc_snap)

        closes = [c.close for c in candles]
        ema_short = calculate_ema(closes, period=_BTC_EMA_SHORT_PERIOD)
        ema_long = calculate_ema(closes, period=_BTC_EMA_LONG_PERIOD)
        if ema_short > ema_long:
            return "UPTREND"
        if ema_short < ema_long:
            return "DOWNTREND"
        return "NEUTRAL"

    async def _calc_btc_atr_pct(self, btc_snap: MarketSnapshot) -> float:
        """BTC 15m ATR(14) を最新 close で割った %（章4 ④ REGIME）。

        ローソク足取得失敗・本数不足時は 24h レンジ幅で代用。
        """
        try:
            candles = await self.exchange.get_candles(
                symbol="BTC",
                interval=_BTC_REGIME_INTERVAL,
                limit=_BTC_ATR_LIMIT,
            )
        except ExchangeError as e:
            logger.warning(
                "BTC candles fetch failed for ATR: %s, falling back", e
            )
            return self._fallback_btc_atr_pct(btc_snap)

        if len(candles) < _BTC_ATR_PERIOD + 1:
            logger.warning(
                "Not enough BTC candles for ATR(%d): got %d",
                _BTC_ATR_PERIOD,
                len(candles),
            )
            return self._fallback_btc_atr_pct(btc_snap)

        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        closes = [c.close for c in candles]
        atr_pct = calculate_atr_pct(highs, lows, closes, period=_BTC_ATR_PERIOD)
        return float(atr_pct)

    @staticmethod
    def _fallback_btc_ema_trend(btc_snap: MarketSnapshot) -> str:
        """ローソク足が使えない時の簡易判定（PR7.1 互換）。"""
        if btc_snap.current_price > btc_snap.rolling_24h_open:
            return "UPTREND"
        return "DOWNTREND"

    @staticmethod
    def _fallback_btc_atr_pct(btc_snap: MarketSnapshot) -> float:
        """ローソク足が使えない時の簡易計算（PR7.1 互換）。"""
        if btc_snap.current_price == 0:
            return 0.0
        return (btc_snap.high_24h - btc_snap.low_24h) / btc_snap.current_price * 100

    # ─── 判定 ────────────────────────────────────

    def _judge(
        self,
        snapshot: MarketSnapshot,
        direction: Literal["LONG", "SHORT"],
    ) -> EntryDecision:
        decision = (
            judge_long_entry(snapshot)
            if direction == "LONG"
            else judge_short_entry(snapshot)
        )
        if not self.config.flow_layer_enabled:
            decision = self._bypass_flow(decision, direction)
        return decision

    @staticmethod
    def _bypass_flow(
        decision: EntryDecision, direction: Literal["LONG", "SHORT"]
    ) -> EntryDecision:
        """FLOW 層を強制 True に上書き（章11.6.3 Phase A）。

        他3層 (momentum, sentiment, regime) が全て True なら should_enter=True、
        どれか1つでも False なら should_enter=False のまま、その層を
        rejection_reason に反映する。
        """
        new_layer_results = dict(decision.layer_results)
        new_layer_results["flow"] = True

        all_pass = all(new_layer_results.values())
        if all_pass:
            return replace(
                decision,
                should_enter=True,
                direction=direction,
                rejection_reason=None,
                layer_results=new_layer_results,
            )

        # FLOW 以外のいずれかの層が落ちている。dict 挿入順から最初の False を採用。
        failed = next(name for name, ok in new_layer_results.items() if not ok)
        return replace(
            decision,
            should_enter=False,
            direction=None,
            rejection_reason=f"layer_{failed}_failed",
            layer_results=new_layer_results,
        )

    # ─── ロギング ────────────────────────────────

    async def _log_signals(
        self,
        snapshot: MarketSnapshot,
        direction: Literal["LONG", "SHORT"],
        decision: EntryDecision,
    ) -> None:
        snapshot_excerpt = json.dumps(
            {
                "current_price": snapshot.current_price,
                "vwap": snapshot.vwap,
                "momentum_5bar_pct": snapshot.momentum_5bar_pct,
                "sentiment_score": snapshot.sentiment_score,
                "btc_ema_trend": snapshot.btc_ema_trend,
            }
        )
        timestamp = datetime.now(UTC)
        for layer in _LAYER_NAMES:
            layer_lower = layer.lower()
            passed = decision.layer_results.get(layer_lower, False)
            rejection_reason = (
                decision.rejection_reason
                if (
                    not passed
                    and decision.rejection_reason
                    and layer_lower in decision.rejection_reason.lower()
                )
                else None
            )
            await self.repo.log_signal(
                SignalLog(
                    timestamp=timestamp,
                    symbol=snapshot.symbol,
                    direction=direction,
                    layer=layer,
                    passed=passed,
                    rejection_reason=rejection_reason,
                    snapshot_excerpt=snapshot_excerpt,
                )
            )

    # ─── 実発注 ──────────────────────────────────

    async def _execute_entry(
        self,
        snapshot: MarketSnapshot,
        direction: Literal["LONG", "SHORT"],
        decision: EntryDecision,
    ) -> EntryAttempt:
        balance = await self.exchange.get_account_balance_usd()
        consecutive_losses = await self.repo.get_consecutive_losses()
        sz_decimals = await self.exchange.get_sz_decimals(snapshot.symbol)
        tick_size = await self.exchange.get_tick_size(snapshot.symbol)
        atr_estimate = self._estimate_atr(snapshot)
        entry_price = Decimal(str(snapshot.current_price))

        sl_tp = calculate_sl_tp(
            StopLossInput(
                direction=direction,
                entry_price=entry_price,
                atr_value=atr_estimate,
                sl_multiplier=self.config.sl_atr_mult,
                tp_multiplier=self.config.tp_atr_mult,
                tick_size=tick_size,
            )
        )

        sizing = calculate_position_size(
            SizingInput(
                account_balance_usd=balance,
                entry_price=entry_price,
                sl_price=sl_tp.sl_price,
                leverage=self.config.leverage,
                position_size_pct=self.config.position_size_pct,
                sz_decimals=sz_decimals,
                consecutive_losses=consecutive_losses,
            )
        )
        if sizing.size_coins <= 0:
            reason = sizing.rejected_reason or "size_too_small"
            logger.warning(
                "entry skipped (size too small): %s %s reason=%s "
                "balance=%s notional=%s sz_decimals=%s",
                snapshot.symbol,
                direction,
                reason,
                balance,
                sizing.notional_usd,
                sz_decimals,
            )
            await self.notifier.send_alert(
                f"entry skipped (size too small): {snapshot.symbol} "
                f"{direction} reason={reason}",
                dedup_key=f"entry_skip_size:{snapshot.symbol}:{direction}",
            )
            return EntryAttempt(
                symbol=snapshot.symbol,
                direction=direction,
                decision=decision,
                executed=False,
                is_dry_run=False,
                trade_id=None,
                rejected_reason=reason,
                snapshot=snapshot,
            )

        side: Literal["buy", "sell"] = "buy" if direction == "LONG" else "sell"
        exit_side: Literal["buy", "sell"] = "sell" if direction == "LONG" else "buy"
        entry_request = OrderRequest(
            symbol=snapshot.symbol,
            side=side,
            size=sizing.size_coins,
            price=entry_price,
            tif="Alo",
            reduce_only=False,
        )
        tp_request = TriggerOrderRequest(
            symbol=snapshot.symbol,
            side=exit_side,
            size=sizing.size_coins,
            trigger_price=sl_tp.tp_price,
            is_market=False,
            limit_price=sl_tp.tp_price,
            tpsl="tp",
            reduce_only=True,
        )
        sl_request = TriggerOrderRequest(
            symbol=snapshot.symbol,
            side=exit_side,
            size=sizing.size_coins,
            trigger_price=sl_tp.sl_price,
            is_market=True,
            limit_price=None,
            tpsl="sl",
            reduce_only=True,
        )

        try:
            results = await self.exchange.place_orders_grouped(
                entry_request, tp_request, sl_request
            )
        except (
            OrderRejectedError,
            DuplicateOrderError,
            RateLimitError,
            ExchangeError,
        ) as e:
            logger.exception("place_orders_grouped failed")
            await self.notifier.send_alert(
                f"entry failed for {snapshot.symbol} {direction}: {e}",
                dedup_key=f"entry_fail:{snapshot.symbol}:{direction}",
            )
            return EntryAttempt(
                symbol=snapshot.symbol,
                direction=direction,
                decision=decision,
                executed=False,
                is_dry_run=False,
                trade_id=None,
                rejected_reason=str(e),
                snapshot=snapshot,
            )

        # PR6.4.3 検証: results[0] (entry) の order_id だけ必須。
        # results[1]/results[2] (tp/sl) は entry 約定まで order_id=None で
        # 返ることがある。success=True なら正常系として扱う。
        if not results or not results[0].success or results[0].order_id is None:
            entry_reason = (
                results[0].rejected_reason
                if results and results[0].rejected_reason
                else "entry_not_filled"
            )
            # PR7.4-real 後の運用観察で判明: place_orders_grouped は HL から
            # statuses[0].error が来ても例外を投げず success=False で return
            # する（hyperliquid_client._grouped_status_to_result）。単発
            # place_order の _raise_inner_error 経路と非対称。silent rejection
            # で 4 層通過しても trades にも incidents にも何も残らない問題が
            # mainnet で実観測された (5/13 ETH SHORT 5 件)。ここで最低限の
            # 可視化を行う（構造的修正は別 PR）。
            logger.warning(
                "entry skipped (order not placed): %s %s reason=%s",
                snapshot.symbol,
                direction,
                entry_reason,
            )
            await self.notifier.send_alert(
                f"entry skipped (order not placed): {snapshot.symbol} "
                f"{direction} reason={entry_reason}",
                dedup_key=f"entry_skip_reject:{snapshot.symbol}:{direction}",
            )
            return EntryAttempt(
                symbol=snapshot.symbol,
                direction=direction,
                decision=decision,
                executed=False,
                is_dry_run=False,
                trade_id=None,
                rejected_reason=entry_reason,
                snapshot=snapshot,
            )

        trade_id = await self.repo.open_trade(
            TradeOpenRequest(
                symbol=snapshot.symbol,
                direction=direction,
                entry_price=entry_price,
                size_coins=sizing.size_coins,
                sl_price=sl_tp.sl_price,
                tp_price=sl_tp.tp_price,
                leverage=self.config.leverage,
                is_dry_run=False,
                decision=decision,
            )
        )

        await self.notifier.send_signal(
            f"{direction} {snapshot.symbol} @{entry_price} "
            f"size={sizing.size_coins} sl={sl_tp.sl_price} "
            f"tp={sl_tp.tp_price} (trade_id={trade_id})",
            dedup_key=f"entry:{trade_id}",
        )
        return EntryAttempt(
            symbol=snapshot.symbol,
            direction=direction,
            decision=decision,
            executed=True,
            is_dry_run=False,
            trade_id=trade_id,
            rejected_reason=None,
            snapshot=snapshot,
        )

    @staticmethod
    def _estimate_atr(snapshot: MarketSnapshot) -> Decimal:
        """24h レンジを 24 で割って 1h ATR の代わりに使う（章11.6 簡易実装）。"""
        atr = (snapshot.high_24h - snapshot.low_24h) / 24
        return Decimal(str(max(atr, 0.0001)))
