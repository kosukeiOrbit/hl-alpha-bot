"""HyperLiquidClient（章11.6・章22）。

公式 hyperliquid-python-sdk をラップして ExchangeProtocol を実装する。

実装済み:
- 接続管理 / lazy Info init (PR6.1)
- get_symbols / get_l2_book / get_funding_rate_8h / get_open_interest (PR6.1)
- get_tick_size / get_sz_decimals (PR6.1)
- get_market_snapshot + helpers (PR6.2)
- ユーザー状態取得 (PR6.3: get_positions / get_open_orders / get_fills /
  get_funding_payments / get_account_balance_usd / get_order_status /
  get_order_by_client_id)
- cancel_order / Exchange 初期化 (PR6.4.1)
- place_order (PR6.4.2)
- place_trigger_order / place_orders_grouped (PR6.4.3)

ExchangeProtocol は本実装で 100% カバー。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar, Literal

from eth_account import Account
from hyperliquid.exchange import Exchange  # type: ignore[import-untyped]
from hyperliquid.info import Info  # type: ignore[import-untyped]
from hyperliquid.utils.types import Cloid  # type: ignore[import-untyped]

from src.adapters.exchange import (
    Candle,
    DuplicateOrderError,
    ExchangeError,
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
    MAINNET_EXCHANGE_URL = "https://api.hyperliquid.xyz"
    TESTNET_EXCHANGE_URL = "https://api.hyperliquid-testnet.xyz"

    # PR D1: meta_and_asset_ctxs キャッシュの TTL（秒）。明示 invalidate
    # の取りこぼし時の safety net。cycle_interval=30s に対して十分小さい。
    _META_CACHE_TTL_SECONDS: ClassVar[float] = 1.0

    def __init__(
        self,
        network: str = "testnet",
        address: str | None = None,
        agent_private_key: str | None = None,
    ) -> None:
        if network not in ("mainnet", "testnet"):
            raise ValueError(f"network must be 'mainnet' or 'testnet', got {network!r}")
        self.network = network
        self.address = address
        self.agent_private_key = agent_private_key
        self._info_url = (
            self.MAINNET_INFO_URL if network == "mainnet" else self.TESTNET_INFO_URL
        )
        self._exchange_url = (
            self.MAINNET_EXCHANGE_URL
            if network == "mainnet"
            else self.TESTNET_EXCHANGE_URL
        )
        self._info: Info | None = None
        self._exchange: Exchange | None = None
        self._symbols_cache: tuple[SymbolMeta, ...] | None = None
        # PR7.4-fix: unified account（cross-margin）か legacy split かを
        # 初回 get_account_balance_usd 時に検出してキャッシュ。
        # アカウント設定は BOT 稼働中に変わらないので一度で十分。
        self._abstraction_state: str | None = None
        # PR D1: meta_and_asset_ctxs を 1 cycle 内で 1 回だけ叩くキャッシュ。
        # tuple は (meta, asset_ctxs, monotonic_fetched_at)。
        # scheduler が cycle 開始時に invalidate_meta_cache を呼んでクリア
        # する設計（明示クリア基本・TTL は異常時の safety net）。
        self._meta_cache: (
            tuple[dict[str, Any], list[dict[str, Any]], float] | None
        ) = None

    @property
    def info(self) -> Info:
        """公式SDK Info オブジェクト（lazy 初期化）。"""
        if self._info is None:
            self._info = Info(base_url=self._info_url, skip_ws=True)
        return self._info

    @property
    def exchange(self) -> Exchange:
        """公式SDK Exchange オブジェクト（lazy 初期化・署名用）。

        Agent Wallet 秘密鍵で署名し、取引は Master Wallet（address）の
        口座で行う（章10.4 Agent Wallet モデル）。
        """
        if self._exchange is None:
            if self.agent_private_key is None:
                raise ExchangeError(
                    "agent_private_key is required for write operations. "
                    "Pass agent_private_key to HyperLiquidClient."
                )
            if self.address is None:
                raise ExchangeError(
                    "address (master) is required for write operations."
                )
            account = Account.from_key(self.agent_private_key)
            self._exchange = Exchange(
                wallet=account,
                base_url=self._exchange_url,
                account_address=self.address,
            )
        return self._exchange

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
        """SDK の meta_and_asset_ctxs を呼び (meta, asset_ctxs) を返す。

        PR D1: 1 cycle 内で複数経路（snapshot / funding / OI / symbols）が
        同じデータを必要とするため、cycle 単位でキャッシュする。
        scheduler が ``invalidate_meta_cache`` を cycle 開始時に呼んで
        明示クリアする。``_META_CACHE_TTL_SECONDS`` は invalidate が
        呼ばれなかった場合の safety net（cycle_interval よりはるかに短い）。
        """
        if self._meta_cache is not None:
            meta, asset_ctxs, fetched_at = self._meta_cache
            if time.monotonic() - fetched_at < self._META_CACHE_TTL_SECONDS:
                return meta, asset_ctxs
        try:
            response = await asyncio.to_thread(self.info.meta_and_asset_ctxs)
        except Exception as e:
            raise ExchangeError(f"Failed to fetch symbols: {e}") from e
        meta = response[0]
        asset_ctxs = response[1]
        self._meta_cache = (meta, asset_ctxs, time.monotonic())
        return meta, asset_ctxs

    async def invalidate_meta_cache(self) -> None:
        """meta_and_asset_ctxs キャッシュをクリア（PR D1）。

        scheduler が各 cycle 開始時に呼ぶ。次の ``_fetch_meta_and_ctxs``
        は確実に HL API を叩いて最新値を取得する。
        """
        self._meta_cache = None

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

        utc_open_price = await self._get_utc_day_open_price(
            symbol, fallback_price=current_price
        )

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

    async def get_candles(
        self, symbol: str, interval: str, limit: int = 100
    ) -> tuple[Candle, ...]:
        """直近 limit 本のローソク足を Candle dataclass で返す（古い→新しい順）。

        指標計算（EMA / ATR 等）から使う公開 API。内部的には
        ``_fetch_recent_candles`` を呼んで dict → Candle に変換する。
        """
        raw = await self._fetch_recent_candles(symbol, interval, limit)
        return tuple(
            Candle(
                symbol=symbol,
                interval=interval,
                timestamp_ms=int(c["t"]),
                open=Decimal(str(c["o"])),
                high=Decimal(str(c["h"])),
                low=Decimal(str(c["l"])),
                close=Decimal(str(c["c"])),
                volume=Decimal(str(c["v"])),
            )
            for c in raw
        )

    async def _get_utc_day_open_price(
        self, symbol: str, fallback_price: Decimal
    ) -> Decimal:
        """当日 UTC 00:00 の始値を取得（1h 足の最初のローソクの open）。

        PR7.x-fix: ローソク足取得失敗 / 空応答時は ExchangeError を投げず
        ``fallback_price`` を返してメインループを止めない（章19 の継続稼働
        ポリシー）。fallback_price は呼び出し側 (`get_market_snapshot`) で
        既に取得済みの current_price (= markPx) を渡す。

        本来の utc_open_price は MOMENTUM 補助フィルタの参照点で、現値で
        代用しても LONG 判定が常に False 側に振れるだけ（安全側）。

        2 日で 6 回観察された ``No UTC 00:00 candle found`` ERROR は HL 側で
        00:00 の 1h 足が一時的に未配信になるケース（連続 4 cycle 続くことが
        ある）。発生頻度 0.035% でも cycle 全停止は本番運用で許容不能。
        """
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
            logger.warning(
                "Failed to fetch UTC 00:00 candle for %s: %s, "
                "falling back to current_price %s",
                symbol,
                e,
                fallback_price,
            )
            return fallback_price
        if not response:
            logger.warning(
                "No UTC 00:00 candle for %s (start_ms=%d), "
                "falling back to current_price %s",
                symbol,
                start_ms,
                fallback_price,
            )
            return fallback_price
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

    # ─── ユーザー状態取得（PR6.3・章22.7） ───

    def _require_address(self) -> None:
        """address が設定されているか確認。なければ ExchangeError を即時raise。"""
        if not self.address:
            raise ExchangeError(
                "address is required for user state queries. "
                "Pass address to HyperLiquidClient(address=...)"
            )

    async def get_positions(self) -> tuple[Position, ...]:
        """現在のポジション一覧（章22.7 clearinghouseState / userState）。

        size=0 のエントリは「ポジションなし」と見なしてスキップする。
        """
        self._require_address()
        try:
            response = await asyncio.to_thread(self.info.user_state, self.address)
        except Exception as e:
            raise ExchangeError(f"Failed to fetch positions: {e}") from e

        positions: list[Position] = []
        for asset_pos in response.get("assetPositions", []):
            pos_data = asset_pos.get("position", {})
            size_raw = Decimal(str(pos_data.get("szi", "0")))
            if size_raw == 0:
                continue

            entry_px = Decimal(str(pos_data.get("entryPx", "0")))
            unrealized_pnl = Decimal(str(pos_data.get("unrealizedPnl", "0")))
            leverage = pos_data.get("leverage", {})
            leverage_value = int(leverage.get("value", 1))
            liquidation_px_raw = pos_data.get("liquidationPx")
            liquidation_px = (
                Decimal(str(liquidation_px_raw))
                if liquidation_px_raw is not None
                else None
            )

            positions.append(
                Position(
                    symbol=pos_data["coin"],
                    size=size_raw,
                    entry_price=entry_px,
                    unrealized_pnl=unrealized_pnl,
                    leverage=leverage_value,
                    liquidation_price=liquidation_px,
                )
            )
        return tuple(positions)

    async def get_open_orders(self) -> tuple[Order, ...]:
        """未約定注文一覧（章22.7 openOrders）。"""
        self._require_address()
        try:
            response = await asyncio.to_thread(self.info.open_orders, self.address)
        except Exception as e:
            raise ExchangeError(f"Failed to fetch open orders: {e}") from e
        return tuple(self._parse_order(order_data) for order_data in response)

    @staticmethod
    def _parse_order(order_data: dict[str, Any]) -> Order:
        """SDK レスポンス1件を Order に変換。

        HL の side は "B"（buy）/ "A"（ask=sell）。SDK 経路によっては
        "buy"/"sell" 文字列の場合もあるため両対応。
        tif は Alo/Ioc/Gtc 以外なら Gtc にフォールバック。
        """
        side_raw = order_data.get("side", "")
        side: Literal["buy", "sell"] = "buy" if side_raw in ("B", "buy") else "sell"

        tif_raw = order_data.get("orderType", "Gtc")
        tif: Literal["Alo", "Ioc", "Gtc"] = (
            tif_raw if tif_raw in ("Alo", "Ioc", "Gtc") else "Gtc"
        )

        return Order(
            order_id=int(order_data.get("oid", 0)),
            client_order_id=order_data.get("cloid"),
            symbol=order_data.get("coin", ""),
            side=side,
            size=Decimal(str(order_data.get("sz", "0"))),
            price=Decimal(str(order_data.get("limitPx", "0"))),
            tif=tif,
            timestamp_ms=int(order_data.get("timestamp", 0)),
        )

    async def get_fills(self, since_ms: int) -> tuple[Fill, ...]:
        """約定履歴（章22.7 userFills・since_ms 以降）。"""
        self._require_address()
        try:
            response = await asyncio.to_thread(
                self.info.user_fills_by_time, self.address, since_ms
            )
        except Exception as e:
            raise ExchangeError(f"Failed to fetch fills: {e}") from e

        fills: list[Fill] = []
        for fill_data in response:
            side_raw = fill_data.get("side", "")
            side: Literal["buy", "sell"] = (
                "buy" if side_raw in ("B", "buy") else "sell"
            )
            fills.append(
                Fill(
                    order_id=int(fill_data.get("oid", 0)),
                    symbol=fill_data.get("coin", ""),
                    side=side,
                    size=Decimal(str(fill_data.get("sz", "0"))),
                    price=Decimal(str(fill_data.get("px", "0"))),
                    fee_usd=Decimal(str(fill_data.get("fee", "0"))),
                    timestamp_ms=int(fill_data.get("time", 0)),
                    closed_pnl=Decimal(str(fill_data.get("closedPnl", "0"))),
                )
            )
        return tuple(fills)

    async def get_funding_payments(self, since_ms: int) -> tuple[FundingPayment, ...]:
        """Funding精算履歴（章22.6・章22.7 userFundingHistory）。

        SDK の delta.fundingRate は 1h レート。Protocol は 8h 相当値なので 8 倍する。
        """
        self._require_address()
        try:
            response = await asyncio.to_thread(
                self.info.user_funding_history, self.address, since_ms
            )
        except Exception as e:
            raise ExchangeError(f"Failed to fetch funding payments: {e}") from e

        payments: list[FundingPayment] = []
        for entry in response:
            delta = entry.get("delta", {})
            funding_1h = Decimal(str(delta.get("fundingRate", "0")))
            payments.append(
                FundingPayment(
                    symbol=delta.get("coin", ""),
                    funding_rate_8h=funding_1h * Decimal("8"),
                    payment_usd=Decimal(str(delta.get("usdc", "0"))),
                    timestamp_ms=int(entry.get("time", 0)),
                )
            )
        return tuple(payments)

    async def get_account_balance_usd(self) -> Decimal:
        """口座残高 USDC（章22.7）。

        HL のアカウント抽象状態（`query_user_abstraction_state`）に応じて
        参照先を切り替える:

        - **unifiedAccount**: spot USDC が perp 担保として使われる cross-margin
          モード。``marginSummary.accountValue`` は perp 直接保有のみで spot 分が
          反映されないため、``spot_user_state`` から USDC 残高を読む。
        - **それ以外（legacy split margin）**: 従来通り
          ``marginSummary.accountValue``。

        2026-05-13 mainnet 観察で「spot=$295 / perp=$0 で BOT が balance=$0 と
        見る」事象が発生し、`query_user_abstraction_state == "unifiedAccount"`
        と判明（章9.x で詳述）。

        抽象状態の判定は初回呼び出し時のみ。アカウント設定は BOT 稼働中に
        変わらないので結果をインスタンスにキャッシュする。
        """
        self._require_address()
        if self._abstraction_state is None:
            self._abstraction_state = await self._fetch_abstraction_state()
        if self._abstraction_state == "unifiedAccount":
            return await self._get_unified_balance_usd()
        return await self._get_split_balance_usd()

    async def _fetch_abstraction_state(self) -> str:
        """``query_user_abstraction_state`` 結果を文字列で返す。

        取得失敗時は安全側として ``"splitAccount"``（legacy）扱いに倒す。
        ここで unified を誤判定すると spot 残高ベースで size 計算してしまい、
        split account ユーザーで実発注時に margin 不足拒否を踏むため。
        """
        try:
            result = await asyncio.to_thread(
                self.info.query_user_abstraction_state, self.address
            )
        except Exception as e:
            logger.warning(
                "query_user_abstraction_state failed: %s, "
                "assuming splitAccount (legacy)",
                e,
            )
            return "splitAccount"
        state = str(result) if result is not None else "splitAccount"
        logger.info("HL account abstraction state: %s", state)
        return state

    async def _get_unified_balance_usd(self) -> Decimal:
        """unified account: spot USDC を perp 担保として読む。

        ``tokenToAvailableAfterMaintenance`` がメンテナンス margin 控除後の
        実利用可能額。これが最も精確。
        無ければ ``balances`` 配列の USDC.total にフォールバック。
        """
        try:
            spot = await asyncio.to_thread(
                self.info.spot_user_state, self.address
            )
        except Exception as e:
            raise ExchangeError(
                f"Failed to fetch spot state for balance: {e}"
            ) from e
        for entry in spot.get("tokenToAvailableAfterMaintenance", []):
            # entry の形式: [token_id, "amount"]。USDC は token_id=0。
            if len(entry) >= 2 and int(entry[0]) == 0:
                return Decimal(str(entry[1]))
        for b in spot.get("balances", []):
            if b.get("coin") == "USDC":
                return Decimal(str(b.get("total", "0")))
        return Decimal("0")

    async def _get_split_balance_usd(self) -> Decimal:
        """legacy split margin: 従来通り marginSummary.accountValue。"""
        try:
            response = await asyncio.to_thread(self.info.user_state, self.address)
        except Exception as e:
            raise ExchangeError(f"Failed to fetch account balance: {e}") from e
        margin_summary = response.get("marginSummary", {})
        return Decimal(str(margin_summary.get("accountValue", "0")))

    async def get_order_status(
        self, order_id: int
    ) -> Literal["pending", "filled", "cancelled", "rejected"]:
        """注文ステータス取得（章22.7 orderStatus）。

        HL のステータス文字列を Protocol の Literal に正規化する。
        未知のステータスは "pending" にフォールバック（保守的）。
        """
        self._require_address()
        try:
            response = await asyncio.to_thread(
                self.info.query_order_by_oid, self.address, order_id
            )
        except Exception as e:
            raise ExchangeError(
                f"Failed to fetch order status for {order_id}: {e}"
            ) from e

        order_data = response.get("order", {})
        order_status_raw = order_data.get("status", "")
        if order_status_raw in ("open", "triggered"):
            return "pending"
        if order_status_raw == "filled":
            return "filled"
        if order_status_raw in ("canceled", "cancelled"):
            return "cancelled"
        if order_status_raw == "rejected":
            return "rejected"
        return "pending"

    async def get_order_by_client_id(self, client_order_id: str) -> Order | None:
        """client_order_id で注文取得（章9.5 冪等性チェック）。

        SDK に専用APIがないため、open_orders から線形検索。
        """
        orders = await self.get_open_orders()
        for order in orders:
            if order.client_order_id == client_order_id:
                return order
        return None

    # ─── 注文操作（PR6.4・章22.4） ───

    async def cancel_order(self, order_id: int, symbol: str) -> bool:
        """注文キャンセル（章22.4）。

        Agent Wallet 秘密鍵で署名し、Master Wallet の口座で発注をキャンセル。

        Args:
            order_id: HL の oid。
            symbol: 銘柄名（例: "BTC"）。SDK は coin 名を期待する。

        Returns:
            True: キャンセル成功。
            False: キャンセル失敗（既に約定済み・キャンセル済み・存在しない等）。

        Raises:
            ExchangeError: address / agent_private_key 未設定、SDK 呼び出し失敗。
        """
        self._require_address()
        if self.agent_private_key is None:
            raise ExchangeError("agent_private_key is required for cancel_order")

        try:
            response = await asyncio.to_thread(self.exchange.cancel, symbol, order_id)
        except Exception as e:
            raise ExchangeError(f"Failed to cancel order {order_id}: {e}") from e

        # SDK レスポンス例（章22.4）:
        #   成功: {"status":"ok","response":{"type":"cancel",
        #           "data":{"statuses":["success"]}}}
        #   失敗: {"status":"ok","response":{"type":"cancel",
        #           "data":{"statuses":[{"error":"..."}]}}}
        #   API err: {"status":"err","response":"..."}
        if response.get("status") != "ok":
            return False

        statuses = response.get("response", {}).get("data", {}).get("statuses", [])
        if not statuses:
            return False

        first = statuses[0]
        return not (isinstance(first, dict) and "error" in first)

    async def place_order(self, request: OrderRequest) -> OrderResult:
        """通常注文を発注（章22.4・PR6.4.2）。

        Agent Wallet 秘密鍵で署名し、Master Wallet の口座で発注。
        client_order_id を渡すと冪等性が確保される（章9.5）。
        指定が無ければ UUID4 ベースの cloid を自動生成。

        Args:
            request: OrderRequest。

        Returns:
            OrderResult: 成功時は order_id 含む。

        Raises:
            OrderRejectedError: ALO拒否・通常拒否（残高不足等）。
            DuplicateOrderError: 同一 client_order_id で発注済み。
            RateLimitError: レート制限到達（章22.2）。
            ExchangeError: address / agent_private_key 未設定、SDK 通信エラー等。
        """
        self._require_address()
        if self.agent_private_key is None:
            raise ExchangeError("agent_private_key is required for place_order")

        is_buy = request.side == "buy"
        order_type = self._build_order_type(request.tif)
        cloid_str = request.client_order_id or _generate_cloid()
        cloid_obj = Cloid.from_str(cloid_str)

        try:
            response = await asyncio.to_thread(
                self.exchange.order,
                request.symbol,
                is_buy,
                float(request.size),
                float(request.price),
                order_type,
                request.reduce_only,
                cloid_obj,
            )
        except Exception as e:
            raise ExchangeError(
                f"Failed to place order for {request.symbol}: {e}"
            ) from e

        return self._parse_order_response(response)

    @staticmethod
    def _build_order_type(tif: str) -> dict[str, dict[str, str]]:
        """tif 文字列を SDK の order_type dict 形式に変換（章22.4）。

        SDK 形式:
        - {"limit": {"tif": "Alo"}}  → Post-Only
        - {"limit": {"tif": "Ioc"}}  → Immediate-Or-Cancel
        - {"limit": {"tif": "Gtc"}}  → Good-Till-Cancel
        """
        if tif not in ("Alo", "Ioc", "Gtc"):
            raise ExchangeError(f"Unsupported tif: {tif}")
        return {"limit": {"tif": tif}}

    @staticmethod
    def _parse_order_response(response: dict[str, Any]) -> OrderResult:
        """SDK レスポンスを OrderResult に変換し、エラーを例外にマッピング。

        想定レスポンス形式（章22.4）:
        - 成功 (resting): {"status":"ok","response":{"type":"order",
            "data":{"statuses":[{"resting":{"oid":12345}}]}}}
        - 即約定 (filled): 同上 with {"filled":{"totalSz":..,"avgPx":..,"oid":..}}
        - ALO拒否: 同上 with {"error":"Order would have matched..."}
        - レート制限: {"status":"err","response":"Too many requests"}
        """
        top_status = response.get("status")
        if top_status == "err":
            HyperLiquidClient._raise_top_level_err(response)
        if top_status != "ok":
            raise ExchangeError(f"Unknown status: {top_status}")

        statuses = response.get("response", {}).get("data", {}).get("statuses", [])
        if not statuses:
            raise ExchangeError(f"No statuses in response: {response}")

        first = statuses[0]
        # 古い SDK 互換: statuses[0] が "success" などの文字列。
        if isinstance(first, str):
            return OrderResult(success=True, order_id=None)
        if not isinstance(first, dict):
            raise ExchangeError(f"Unexpected status format: {first!r}")

        if "error" in first:
            HyperLiquidClient._raise_inner_error(str(first["error"]))
        if "resting" in first:
            return OrderResult(success=True, order_id=int(first["resting"]["oid"]))
        if "filled" in first:
            return OrderResult(success=True, order_id=int(first["filled"]["oid"]))
        raise ExchangeError(f"Unrecognized status: {first!r}")

    @staticmethod
    def _raise_top_level_err(response: dict[str, Any]) -> None:
        msg = str(response.get("response", "unknown"))
        if "Too many requests" in msg or "rate" in msg.lower():
            raise RateLimitError(msg)
        raise OrderRejectedError(msg)

    # ALO 拒否メッセージ判定。HL testnet 実機例:
    #   "Post only order would have immediately matched, bbo was 76317@76332. asset=3"
    # 将来の表記揺れに備えて複数パターンを許容。
    _ALO_REJECT_MARKERS: ClassVar[tuple[str, ...]] = (
        "post only",
        "alo",
        "would have matched",
        "would have immediately matched",
    )

    @staticmethod
    def _raise_inner_error(error_msg: str) -> None:
        lowered = error_msg.lower()
        if any(m in lowered for m in HyperLiquidClient._ALO_REJECT_MARKERS):
            raise OrderRejectedError(error_msg, code="ALO_REJECT")
        if "duplicate" in lowered or "already" in lowered:
            raise DuplicateOrderError(error_msg)
        raise OrderRejectedError(error_msg)

    # ─── Trigger order / grouped order（PR6.4.3・章14.5/14.6・章22.4） ───

    async def place_trigger_order(
        self, request: TriggerOrderRequest
    ) -> OrderResult:
        """TP/SL 単発の trigger order を発注（章22.4・章14.5）。

        Raises:
            OrderRejectedError: 拒否（ALO 含む）。
            DuplicateOrderError: cloid 重複。
            RateLimitError: レート制限。
            ExchangeError: address / agent_private_key 未設定、limit_price
                指定漏れ、SDK 通信エラー等。
        """
        self._require_address()
        if self.agent_private_key is None:
            raise ExchangeError(
                "agent_private_key is required for place_trigger_order"
            )
        if not request.is_market and request.limit_price is None:
            raise ExchangeError("limit_price is required when is_market=False")

        order_type = self._build_trigger_order_type(request)
        is_buy = request.side == "buy"
        # is_market=True でも HL は limit_px を要求するので trigger_price で代用。
        limit_px_decimal = (
            request.limit_price
            if request.limit_price is not None
            else request.trigger_price
        )
        cloid_obj = Cloid.from_str(_generate_cloid())

        try:
            response = await asyncio.to_thread(
                self.exchange.order,
                request.symbol,
                is_buy,
                float(request.size),
                float(limit_px_decimal),
                order_type,
                request.reduce_only,
                cloid_obj,
            )
        except Exception as e:
            raise ExchangeError(
                f"Failed to place trigger order for {request.symbol}: {e}"
            ) from e

        return self._parse_order_response(response)

    @staticmethod
    def _build_trigger_order_type(
        request: TriggerOrderRequest,
    ) -> dict[str, dict[str, Any]]:
        """TriggerOrderRequest を SDK の order_type dict に変換（章22.4）。

        SDK 形式:
        {"trigger": {"isMarket": bool, "triggerPx": float, "tpsl": "tp"|"sl"}}

        triggerPx は float で渡す。SDK 内部の float_to_wire が
        f"{x:.8f}" で wire 文字列化するので、str を渡すと
        ValueError: Unknown format code 'f' for str になる。
        """
        if request.tpsl not in ("tp", "sl"):
            raise ExchangeError(f"Invalid tpsl: {request.tpsl}")
        return {
            "trigger": {
                "isMarket": request.is_market,
                "triggerPx": float(request.trigger_price),
                "tpsl": request.tpsl,
            }
        }

    async def place_orders_grouped(
        self,
        entry: OrderRequest,
        tp: TriggerOrderRequest | None,
        sl: TriggerOrderRequest | None,
    ) -> tuple[OrderResult, ...]:
        """エントリー + TP + SL を normalTpsl で連結発注（章14.6）。

        エントリーが約定すると HL 側で自動的に TP/SL が active 化する。
        部分成功あり: 個別失敗は OrderResult(success=False, rejected_reason=...)
        として返し、全体を例外にはしない。トップレベル err のみ例外化。
        """
        self._require_address()
        if self.agent_private_key is None:
            raise ExchangeError(
                "agent_private_key is required for place_orders_grouped"
            )
        if tp is None and sl is None:
            raise ExchangeError(
                "At least one of tp or sl must be provided. "
                "Use place_order() if you don't need TP/SL grouping."
            )

        sdk_orders = self._build_grouped_orders(entry, tp, sl)
        try:
            response = await asyncio.to_thread(
                self.exchange.bulk_orders,
                sdk_orders,
                grouping="normalTpsl",
            )
        except Exception as e:
            raise ExchangeError(f"Failed to place grouped orders: {e}") from e

        return self._parse_grouped_response(response)

    def _build_grouped_orders(
        self,
        entry: OrderRequest,
        tp: TriggerOrderRequest | None,
        sl: TriggerOrderRequest | None,
    ) -> list[dict[str, Any]]:
        """bulk_orders に渡す SDK OrderRequest dict のリストを構築。"""
        orders: list[dict[str, Any]] = [self._entry_to_sdk_dict(entry)]
        if tp is not None:
            orders.append(self._trigger_to_sdk_dict(tp))
        if sl is not None:
            orders.append(self._trigger_to_sdk_dict(sl))
        return orders

    def _entry_to_sdk_dict(self, entry: OrderRequest) -> dict[str, Any]:
        cloid_str = entry.client_order_id or _generate_cloid()
        return {
            "coin": entry.symbol,
            "is_buy": entry.side == "buy",
            "sz": float(entry.size),
            "limit_px": float(entry.price),
            "order_type": self._build_order_type(entry.tif),
            "reduce_only": entry.reduce_only,
            "cloid": Cloid.from_str(cloid_str),
        }

    def _trigger_to_sdk_dict(
        self, trigger: TriggerOrderRequest
    ) -> dict[str, Any]:
        limit_px_decimal = (
            trigger.limit_price
            if trigger.limit_price is not None
            else trigger.trigger_price
        )
        return {
            "coin": trigger.symbol,
            "is_buy": trigger.side == "buy",
            "sz": float(trigger.size),
            "limit_px": float(limit_px_decimal),
            "order_type": self._build_trigger_order_type(trigger),
            "reduce_only": trigger.reduce_only,
            "cloid": Cloid.from_str(_generate_cloid()),
        }

    @staticmethod
    def _parse_grouped_response(
        response: dict[str, Any],
    ) -> tuple[OrderResult, ...]:
        """bulk_orders レスポンスをパース。個別失敗は OrderResult として保持。"""
        top_status = response.get("status")
        if top_status == "err":
            HyperLiquidClient._raise_top_level_err(response)
        if top_status != "ok":
            raise ExchangeError(f"Unknown status: {top_status}")

        statuses = response.get("response", {}).get("data", {}).get("statuses", [])
        if not statuses:
            raise ExchangeError(f"No statuses in grouped response: {response}")

        return tuple(
            HyperLiquidClient._grouped_status_to_result(i, s)
            for i, s in enumerate(statuses)
        )

    @staticmethod
    def _grouped_status_to_result(idx: int, status: Any) -> OrderResult:
        """grouped bulk_orders 個別 status を OrderResult / 例外に変換。

        PR Level 3: idx==0（entry slot）の inner error は単発 place_order と
        同じく ``_raise_inner_error`` 経由で ``OrderRejectedError`` を raise する。
        これにより entry_flow の既存 try/except (OrderRejectedError ...) が
        grouped path でも自動的に効いて logger.exception + send_alert
        (entry_fail:{symbol}:{direction}) が走る。

        idx>=1（TP/SL slot）の inner error は **raise しない**。entry が
        statuses[0] で resting/filled として成功している可能性があり、ここで
        raise すると tuple そのものが失われて呼び出し側で「entry は約定したが
        TP/SL が未付与」という危険な状態を検知できなくなる。partial success
        は OrderResult(success=False) で表現し、entry_flow で個別に判定する。

        2026-05-14 mainnet 観察で entry slot の ALO 拒否が silent return
        されていたことを修正（章9.x で詳述）。
        """
        if isinstance(status, str):
            return OrderResult(success=True, order_id=None)
        if not isinstance(status, dict):
            raise ExchangeError(f"Unexpected status[{idx}] format: {status!r}")
        if "error" in status:
            error_msg = str(status["error"])
            if idx == 0:
                HyperLiquidClient._raise_inner_error(error_msg)
            return OrderResult(
                success=False,
                order_id=None,
                rejected_reason=error_msg,
            )
        if "resting" in status:
            return OrderResult(
                success=True, order_id=int(status["resting"]["oid"])
            )
        if "filled" in status:
            return OrderResult(
                success=True, order_id=int(status["filled"]["oid"])
            )
        raise ExchangeError(f"Unrecognized status[{idx}]: {status!r}")


def _generate_cloid() -> str:
    """client_order_id を自動生成（章9.5）。

    HL の cloid 仕様: 0x プレフィックス + 32 hex chars（16 バイト）。
    UUID4.hex は 32 hex chars なので 0x を前置するだけで適合。
    """
    return "0x" + uuid.uuid4().hex
