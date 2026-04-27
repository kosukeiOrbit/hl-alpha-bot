"""HyperLiquidClient（章11.6・章22）。

公式 hyperliquid-python-sdk をラップして ExchangeProtocol を実装する。

実装済み:
- 接続管理 / lazy Info init (PR6.1)
- get_symbols / get_l2_book / get_funding_rate_8h / get_open_interest (PR6.1)
- get_tick_size / get_sz_decimals (PR6.1)
- get_market_snapshot + helpers (PR6.2)

未実装（後続PR）:
- ユーザー状態取得（PR6.3: get_positions / get_open_orders / get_fills 等）
- 注文操作（PR6.4: place_order / cancel_order 等）
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar

from hyperliquid.info import Info  # type: ignore[import-untyped]

from src.adapters.exchange import (
    ExchangeError,
    L2Book,
    L2BookLevel,
    SymbolMeta,
)
from src.core.models import MarketSnapshot
from src.core.vwap import calculate_vwap_from_volume

logger = logging.getLogger(__name__)


class HyperLiquidClient:
    """HyperLiquid 接続クライアント。

    SDK が同期 API なので、ブロッキング呼び出しは asyncio.to_thread で
    非同期化する（章22.2 のレート制限は SDK が内部でハンドル）。
    """

    MAINNET_INFO_URL = "https://api.hyperliquid.xyz"
    TESTNET_INFO_URL = "https://api.hyperliquid-testnet.xyz"

    def __init__(
        self,
        network: str = "testnet",
        address: str | None = None,
    ) -> None:
        if network not in ("mainnet", "testnet"):
            raise ValueError(f"network must be 'mainnet' or 'testnet', got {network!r}")
        self.network = network
        self.address = address
        self._info_url = (
            self.MAINNET_INFO_URL if network == "mainnet" else self.TESTNET_INFO_URL
        )
        self._info: Info | None = None
        self._symbols_cache: tuple[SymbolMeta, ...] | None = None

    @property
    def info(self) -> Info:
        """公式SDK Info オブジェクト（lazy 初期化）。"""
        if self._info is None:
            self._info = Info(base_url=self._info_url, skip_ws=True)
        return self._info

    # ─── 銘柄メタ情報 ───

    async def get_symbols(self) -> tuple[SymbolMeta, ...]:
        """全銘柄のメタ情報取得（章22.7 metaAndAssetCtxs）。"""
        if self._symbols_cache is not None:
            return self._symbols_cache

        meta, asset_ctxs = await self._fetch_meta_and_ctxs()

        universe = meta["universe"]
        symbols: list[SymbolMeta] = []
        for i, asset_info in enumerate(universe):
            ctx = asset_ctxs[i]
            symbol_name = asset_info["name"]
            sz_decimals = int(asset_info["szDecimals"])
            max_leverage = int(asset_info.get("maxLeverage", 50))
            tick_size = self._calculate_tick_size(
                Decimal(str(ctx["markPx"])), sz_decimals
            )
            symbols.append(
                SymbolMeta(
                    symbol=symbol_name,
                    sz_decimals=sz_decimals,
                    max_leverage=max_leverage,
                    tick_size=tick_size,
                )
            )

        self._symbols_cache = tuple(symbols)
        return self._symbols_cache

    @staticmethod
    def _calculate_tick_size(mark_price: Decimal, sz_decimals: int) -> Decimal:
        """tick_size を計算（章22.5 簡易版）。

        HL PERP の価格精度ルール:
        - 最大 5 significant figures
        - 小数点以下は max(0, 6 - sz_decimals) 桁

        本実装は 5SF 制約から逆算する簡易版。実運用で誤差が出れば
        実際の市場 tick と突合してチューニングする想定。
        """
        max_decimals_from_sz = 6 - sz_decimals
        if max_decimals_from_sz <= 0:
            return Decimal("1")

        if mark_price >= Decimal("10000"):
            return Decimal("1")
        if mark_price >= Decimal("1000"):
            return Decimal("0.1")
        if mark_price >= Decimal("100"):
            return Decimal("0.01")
        if mark_price >= Decimal("10"):
            return Decimal("0.001")
        if mark_price >= Decimal("1"):
            return Decimal("0.0001")
        # サブドル価格 → szDecimals 由来
        return Decimal("1") / (Decimal("10") ** max_decimals_from_sz)

    async def get_tick_size(self, symbol: str) -> Decimal:
        """tick_size 取得（symbols cache から）。"""
        symbols = await self.get_symbols()
        for s in symbols:
            if s.symbol == symbol:
                return s.tick_size
        raise ExchangeError(f"Symbol not found: {symbol}")

    async def get_sz_decimals(self, symbol: str) -> int:
        """szDecimals 取得（symbols cache から）。"""
        symbols = await self.get_symbols()
        for s in symbols:
            if s.symbol == symbol:
                return s.sz_decimals
        raise ExchangeError(f"Symbol not found: {symbol}")

    # ─── 板情報 ───

    async def get_l2_book(self, symbol: str) -> L2Book:
        """板情報取得（章22.7 l2Book・weight=2）。"""
        try:
            response = await asyncio.to_thread(self.info.l2_snapshot, symbol)
        except Exception as e:
            raise ExchangeError(f"Failed to fetch l2_book for {symbol}: {e}") from e

        levels = response["levels"]
        bids_raw = levels[0]
        asks_raw = levels[1]
        timestamp_ms = int(response.get("time", 0))

        bids = tuple(
            L2BookLevel(
                price=Decimal(str(level["px"])),
                size=Decimal(str(level["sz"])),
                n_orders=int(level["n"]),
            )
            for level in bids_raw
        )
        asks = tuple(
            L2BookLevel(
                price=Decimal(str(level["px"])),
                size=Decimal(str(level["sz"])),
                n_orders=int(level["n"]),
            )
            for level in asks_raw
        )

        return L2Book(
            symbol=symbol,
            bids=bids,
            asks=asks,
            timestamp_ms=timestamp_ms,
        )

    # ─── Funding rate / OI ───

    async def get_funding_rate_8h(self, symbol: str) -> Decimal:
        """8時間相当 Funding rate（章22.6）。

        SDK の funding は 1h ごとの精算値なので、表示用に 8 倍して返す。
        """
        meta, asset_ctxs = await self._fetch_meta_and_ctxs()
        for i, asset_info in enumerate(meta["universe"]):
            if asset_info["name"] == symbol:
                funding_1h = Decimal(str(asset_ctxs[i].get("funding", "0")))
                return funding_1h * Decimal("8")
        raise ExchangeError(f"Symbol not found: {symbol}")

    async def get_open_interest(self, symbol: str) -> Decimal:
        """OI 取得（章13.5 regime 判定用）。"""
        meta, asset_ctxs = await self._fetch_meta_and_ctxs()
        for i, asset_info in enumerate(meta["universe"]):
            if asset_info["name"] == symbol:
                return Decimal(str(asset_ctxs[i].get("openInterest", "0")))
        raise ExchangeError(f"Symbol not found: {symbol}")

    # ─── 内部ヘルパー ───

    async def _fetch_meta_and_ctxs(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """SDK の meta_and_asset_ctxs を呼び (meta, asset_ctxs) を返す。"""
        try:
            response = await asyncio.to_thread(self.info.meta_and_asset_ctxs)
        except Exception as e:
            raise ExchangeError(f"Failed to fetch symbols: {e}") from e
        meta = response[0]
        asset_ctxs = response[1]
        return meta, asset_ctxs

    # ─── MarketSnapshot 構築（章4 4層AND判定の入力） ───

    # サポートする candle interval
    _INTERVAL_MS: ClassVar[dict[str, int]] = {
        "1m": 60_000,
        "5m": 300_000,
        "15m": 900_000,
        "1h": 3_600_000,
        "1d": 86_400_000,
    }

    # 5分足を何本取得するか（5本前比モメンタム + 20本平均出来高 + 安全マージン）
    _RECENT_5M_BARS = 21

    async def get_market_snapshot(self, symbol: str) -> MarketSnapshot:
        """4層AND判定用の MarketSnapshot 構築（章4・章11.6）。

        Exchange 由来の値だけを埋める。sentiment_*・btc_ema_trend・btc_atr_pct
        などは APPLICATION 層で別アダプタの値で上書きされる前提のデフォルト。

        flow_large_order_count は WS trades が必要（後続PR）なので 0。
        flow_buy_sell_ratio は L2 板の上位5レベル比で暫定実装。
        """
        meta, asset_ctxs = await self._fetch_meta_and_ctxs()
        ctx: dict[str, Any] | None = None
        for i, asset_info in enumerate(meta["universe"]):
            if asset_info["name"] == symbol:
                ctx = asset_ctxs[i]
                break
        if ctx is None:
            raise ExchangeError(f"Symbol not found: {symbol}")

        current_price = Decimal(str(ctx["markPx"]))
        day_high = Decimal(str(ctx.get("dayHigh", current_price)))
        day_low = Decimal(str(ctx.get("dayLow", current_price)))
        day_volume_usd = Decimal(str(ctx.get("dayNtlVlm", "0")))
        day_volume_base = Decimal(str(ctx.get("dayBaseVlm", "0")))
        prev_day_px = Decimal(str(ctx.get("prevDayPx", current_price)))
        funding_1h = Decimal(str(ctx.get("funding", "0")))
        open_interest = Decimal(str(ctx.get("openInterest", "0")))

        vwap = calculate_vwap_from_volume(
            day_volume_usd=day_volume_usd,
            day_volume_base=day_volume_base,
            fallback_price=current_price,
        )

        candles_5m = await self._fetch_recent_candles(symbol, "5m", self._RECENT_5M_BARS)
        if len(candles_5m) < 6:
            raise ExchangeError(
                f"Insufficient 5m candles for {symbol}: got {len(candles_5m)}"
            )

        # candles は時系列昇順（古い→新しい）想定
        latest_close = Decimal(str(candles_5m[-1]["c"]))
        five_bars_ago_close = Decimal(str(candles_5m[-6]["c"]))
        if five_bars_ago_close == 0:
            momentum_5bar_pct = Decimal("0")
        else:
            momentum_5bar_pct = (
                (latest_close - five_bars_ago_close) / five_bars_ago_close * Decimal("100")
            )

        volume_5min_recent = Decimal(str(candles_5m[-1]["v"]))
        # 直近20本（最新を除く）の平均。candles_5m は最低6本保証なので非空。
        prior_volumes = [Decimal(str(c["v"])) for c in candles_5m[-21:-1]]
        avg_volume = sum(prior_volumes, Decimal("0")) / Decimal(len(prior_volumes))
        volume_surge_ratio = (
            Decimal("1") if avg_volume == 0 else volume_5min_recent / avg_volume
        )

        utc_open_price = await self._get_utc_day_open_price(symbol)

        flow_buy_usd, flow_sell_usd = await self._estimate_flow_from_book(symbol)
        flow_buy_sell_ratio = (
            Decimal("1") if flow_sell_usd == 0 else flow_buy_usd / flow_sell_usd
        )

        return MarketSnapshot(
            symbol=symbol,
            current_price=float(current_price),
            vwap=float(vwap),
            momentum_5bar_pct=float(momentum_5bar_pct),
            utc_open_price=float(utc_open_price),
            rolling_24h_open=float(prev_day_px),
            high_24h=float(day_high),
            low_24h=float(day_low),
            flow_buy_sell_ratio=float(flow_buy_sell_ratio),
            # WS trades 未実装のため 0 固定（後続PR）
            flow_large_order_count=0,
            volume_surge_ratio=float(volume_surge_ratio),
            # sentiment は APPLICATION 層で SentimentProvider から上書きされる
            sentiment_score=0.0,
            sentiment_confidence=0.0,
            sentiment_flags={},
            # btc_* は APPLICATION 層で別途計算して上書き
            btc_ema_trend="UPTREND",
            btc_atr_pct=0.0,
            # funding は 1h を 8h 相当に変換
            funding_rate=float(funding_1h * Decimal("8")),
            open_interest=float(open_interest),
            # 1h前のOIは別途計測が必要（PR6.3 以降で履歴管理）。暫定で同値。
            open_interest_1h_ago=float(open_interest),
        )

    async def _fetch_recent_candles(
        self, symbol: str, interval: str, count: int
    ) -> list[dict[str, Any]]:
        """直近 count 本のローソクを取得（章22.7 candleSnapshot）。

        SDK の candles_snapshot は (name, interval, startTime, endTime) を取る。
        戻り値は古い→新しい順想定の dict のリスト。
        """
        interval_ms = self._INTERVAL_MS.get(interval)
        if interval_ms is None:
            raise ExchangeError(f"Unsupported interval: {interval}")

        end_ms = int(datetime.now(UTC).timestamp() * 1000)
        start_ms = end_ms - (interval_ms * count)
        try:
            response = await asyncio.to_thread(
                self.info.candles_snapshot, symbol, interval, start_ms, end_ms
            )
        except Exception as e:
            raise ExchangeError(f"Failed to fetch candles for {symbol}: {e}") from e
        return list(response)

    async def _get_utc_day_open_price(self, symbol: str) -> Decimal:
        """当日 UTC 00:00 の始値を取得（1h 足の最初のローソクの open）。"""
        now_utc = datetime.now(UTC)
        utc_midnight = datetime(
            now_utc.year, now_utc.month, now_utc.day, tzinfo=UTC
        )
        start_ms = int(utc_midnight.timestamp() * 1000)
        end_ms = start_ms + 3_600_000
        try:
            response = await asyncio.to_thread(
                self.info.candles_snapshot, symbol, "1h", start_ms, end_ms
            )
        except Exception as e:
            raise ExchangeError(
                f"Failed to fetch UTC open candle for {symbol}: {e}"
            ) from e
        if not response:
            raise ExchangeError(f"No UTC 00:00 candle found for {symbol}")
        return Decimal(str(response[0]["o"]))

    async def _estimate_flow_from_book(self, symbol: str) -> tuple[Decimal, Decimal]:
        """L2 板の上位5レベル notional から flow を暫定推定。

        正式実装は WS trades チャンネル（後続PR）。
        板取得失敗時は (0, 0) を返す（snapshot 構築は継続）。
        """
        try:
            book = await self.get_l2_book(symbol)
        except ExchangeError:
            return Decimal("0"), Decimal("0")

        top5_bids = book.bids[:5]
        top5_asks = book.asks[:5]
        buy_usd = sum(
            (level.price * level.size for level in top5_bids), Decimal("0")
        )
        sell_usd = sum(
            (level.price * level.size for level in top5_asks), Decimal("0")
        )
        return buy_usd, sell_usd
