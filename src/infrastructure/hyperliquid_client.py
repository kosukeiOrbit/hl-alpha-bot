"""HyperLiquidClient（章11.6・章22）。

公式 hyperliquid-python-sdk をラップして ExchangeProtocol を実装する。

PR6.1 のスコープ（read-only のみ）:
- 接続管理
- 全銘柄メタ情報取得（get_symbols）
- 板情報取得（get_l2_book）
- Funding rate / OI 取得
- tick_size / sz_decimals 取得

未実装（後続PR）:
- get_market_snapshot（PR6.2）
- ユーザー状態取得（PR6.3）
- 注文操作（PR6.4）
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from hyperliquid.info import Info  # type: ignore[import-untyped]

from src.adapters.exchange import (
    ExchangeError,
    L2Book,
    L2BookLevel,
    SymbolMeta,
)

if TYPE_CHECKING:
    from src.core.models import MarketSnapshot

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

    # ─── 未実装（後続PR） ───

    async def get_market_snapshot(self, symbol: str) -> MarketSnapshot:
        """PR6.2 で実装予定。"""
        raise NotImplementedError("Implemented in PR6.2")
