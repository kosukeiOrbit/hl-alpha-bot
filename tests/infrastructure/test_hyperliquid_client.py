"""HyperLiquidClient のテスト。

- 単体テスト（mock）: SDK の戻り値をモックして変換ロジック検証
- E2E テスト（@pytest.mark.e2e）: testnet で実接続。デフォルトでは skip
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.adapters.exchange import (
    DuplicateOrderError,
    ExchangeError,
    L2Book,
    OrderRejectedError,
    OrderRequest,
    RateLimitError,
    SymbolMeta,
    TriggerOrderRequest,
)
from src.infrastructure.hyperliquid_client import (
    HyperLiquidClient,
    _generate_cloid,
)

# ────────────────────────────────────────────────
# 初期化
# ────────────────────────────────────────────────


class TestInitialization:
    def test_default_network_is_testnet(self) -> None:
        client = HyperLiquidClient()
        assert client.network == "testnet"
        assert "testnet" in client._info_url

    def test_mainnet_network_url(self) -> None:
        client = HyperLiquidClient(network="mainnet")
        assert client._info_url == HyperLiquidClient.MAINNET_INFO_URL
        assert "testnet" not in client._info_url

    def test_invalid_network_raises(self) -> None:
        with pytest.raises(ValueError, match="network must be"):
            HyperLiquidClient(network="invalid")

    def test_address_optional_default_none(self) -> None:
        client = HyperLiquidClient()
        assert client.address is None

    def test_address_can_be_set(self) -> None:
        client = HyperLiquidClient(address="0xabc")
        assert client.address == "0xabc"

    def test_info_lazy_initialized(self) -> None:
        # 構築直後は _info=None。プロパティアクセスで初期化される。
        client = HyperLiquidClient(network="testnet")
        assert client._info is None
        info = client.info
        assert client._info is info  # 同じインスタンス
        # 2回目は同じ
        assert client.info is info

    def test_agent_private_key_default_none(self) -> None:
        client = HyperLiquidClient()
        assert client.agent_private_key is None
        assert client._exchange is None

    def test_agent_private_key_can_be_set(self) -> None:
        client = HyperLiquidClient(agent_private_key="0x" + "1" * 64)
        assert client.agent_private_key == "0x" + "1" * 64

    def test_mainnet_exchange_url(self) -> None:
        client = HyperLiquidClient(network="mainnet")
        assert client._exchange_url == HyperLiquidClient.MAINNET_EXCHANGE_URL

    def test_testnet_exchange_url(self) -> None:
        client = HyperLiquidClient(network="testnet")
        assert client._exchange_url == HyperLiquidClient.TESTNET_EXCHANGE_URL


# ────────────────────────────────────────────────
# tick_size 計算（純関数）
# ────────────────────────────────────────────────


class TestTickSizeCalculation:
    @pytest.mark.parametrize(
        "mark_price, sz_decimals, expected",
        [
            # 高価格は整数tick
            (Decimal("65432.1"), 5, Decimal("1")),
            (Decimal("12345"), 5, Decimal("1")),
            # 中価格レンジ
            (Decimal("3210"), 5, Decimal("0.1")),
            (Decimal("321"), 5, Decimal("0.01")),
            (Decimal("32.1"), 4, Decimal("0.001")),
            (Decimal("3.21"), 4, Decimal("0.0001")),
        ],
    )
    def test_calculates_for_various_prices(
        self, mark_price: Decimal, sz_decimals: int, expected: Decimal
    ) -> None:
        result = HyperLiquidClient._calculate_tick_size(mark_price, sz_decimals)
        assert result == expected

    def test_sub_dollar_price_uses_sz_decimals(self) -> None:
        # サブドル価格は szDecimals 由来。例: price=0.5, sz_decimals=4 → 6-4=2 → 0.01
        result = HyperLiquidClient._calculate_tick_size(Decimal("0.5"), 4)
        assert result == Decimal("0.01")

    def test_zero_or_negative_decimals_returns_one(self) -> None:
        # sz_decimals >= 6 → 整数 tick。
        result = HyperLiquidClient._calculate_tick_size(Decimal("100"), 6)
        assert result == Decimal("1")


# ────────────────────────────────────────────────
# get_symbols（mock）
# ────────────────────────────────────────────────


class TestGetSymbolsMocked:
    @pytest.mark.asyncio
    async def test_returns_symbols_from_universe(self) -> None:
        client = HyperLiquidClient(network="testnet")
        mock_response = [
            {
                "universe": [
                    {"name": "BTC", "szDecimals": 5, "maxLeverage": 50},
                    {"name": "ETH", "szDecimals": 4, "maxLeverage": 50},
                ]
            },
            [
                {"markPx": "65432.1", "openInterest": "1000"},
                {"markPx": "3210.5", "openInterest": "500"},
            ],
        ]
        client._info = MagicMock()
        client._info.meta_and_asset_ctxs = MagicMock(return_value=mock_response)

        symbols = await client.get_symbols()

        assert len(symbols) == 2
        assert symbols[0].symbol == "BTC"
        assert symbols[0].sz_decimals == 5
        assert symbols[0].max_leverage == 50
        assert symbols[1].symbol == "ETH"

    @pytest.mark.asyncio
    async def test_caches_symbols(self) -> None:
        client = HyperLiquidClient(network="testnet")
        mock_response = [
            {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 50}]},
            [{"markPx": "65000", "openInterest": "100"}],
        ]
        client._info = MagicMock()
        client._info.meta_and_asset_ctxs = MagicMock(return_value=mock_response)

        await client.get_symbols()
        await client.get_symbols()  # 2回目はキャッシュ

        assert client._info.meta_and_asset_ctxs.call_count == 1

    @pytest.mark.asyncio
    async def test_uses_default_max_leverage_when_missing(self) -> None:
        # maxLeverage キーがない場合は 50 にフォールバック。
        client = HyperLiquidClient(network="testnet")
        mock_response = [
            {"universe": [{"name": "BTC", "szDecimals": 5}]},
            [{"markPx": "65000"}],
        ]
        client._info = MagicMock()
        client._info.meta_and_asset_ctxs = MagicMock(return_value=mock_response)

        symbols = await client.get_symbols()
        assert symbols[0].max_leverage == 50

    @pytest.mark.asyncio
    async def test_raises_on_sdk_error(self) -> None:
        client = HyperLiquidClient(network="testnet")
        client._info = MagicMock()
        client._info.meta_and_asset_ctxs = MagicMock(
            side_effect=ConnectionError("network down")
        )
        with pytest.raises(ExchangeError, match="Failed to fetch symbols"):
            await client.get_symbols()


# ────────────────────────────────────────────────
# get_tick_size / get_sz_decimals
# ────────────────────────────────────────────────


class TestGetSymbolMetadata:
    @pytest.mark.asyncio
    async def test_get_tick_size(self) -> None:
        client = HyperLiquidClient(network="testnet")
        client._symbols_cache = (
            SymbolMeta("BTC", 5, 50, Decimal("1")),
            SymbolMeta("ETH", 4, 50, Decimal("0.1")),
        )

        assert await client.get_tick_size("BTC") == Decimal("1")
        assert await client.get_tick_size("ETH") == Decimal("0.1")

    @pytest.mark.asyncio
    async def test_get_sz_decimals(self) -> None:
        client = HyperLiquidClient(network="testnet")
        client._symbols_cache = (SymbolMeta("BTC", 5, 50, Decimal("1")),)

        assert await client.get_sz_decimals("BTC") == 5

    @pytest.mark.asyncio
    async def test_unknown_symbol_tick_size_raises(self) -> None:
        # キャッシュに別銘柄あり・対象銘柄なし → ループは回るが該当しない
        client = HyperLiquidClient(network="testnet")
        client._symbols_cache = (SymbolMeta("BTC", 5, 50, Decimal("1")),)
        with pytest.raises(ExchangeError, match="Symbol not found"):
            await client.get_tick_size("UNKNOWN")

    @pytest.mark.asyncio
    async def test_unknown_symbol_sz_decimals_raises(self) -> None:
        client = HyperLiquidClient(network="testnet")
        client._symbols_cache = (SymbolMeta("BTC", 5, 50, Decimal("1")),)
        with pytest.raises(ExchangeError, match="Symbol not found"):
            await client.get_sz_decimals("UNKNOWN")


# ────────────────────────────────────────────────
# get_l2_book（mock）
# ────────────────────────────────────────────────


class TestGetL2BookMocked:
    @pytest.mark.asyncio
    async def test_parses_l2_response(self) -> None:
        client = HyperLiquidClient(network="testnet")
        mock_response = {
            "levels": [
                # bids (高い順)
                [
                    {"px": "65000.0", "sz": "1.5", "n": 3},
                    {"px": "64999.0", "sz": "2.0", "n": 5},
                ],
                # asks (安い順)
                [
                    {"px": "65001.0", "sz": "1.0", "n": 2},
                    {"px": "65002.0", "sz": "1.5", "n": 4},
                ],
            ],
            "time": 1700000000000,
        }
        client._info = MagicMock()
        client._info.l2_snapshot = MagicMock(return_value=mock_response)

        book = await client.get_l2_book("BTC")

        assert isinstance(book, L2Book)
        assert book.symbol == "BTC"
        assert len(book.bids) == 2
        assert book.bids[0].price == Decimal("65000.0")
        assert book.bids[0].size == Decimal("1.5")
        assert book.bids[0].n_orders == 3
        assert book.asks[0].price == Decimal("65001.0")
        assert book.timestamp_ms == 1700000000000

    @pytest.mark.asyncio
    async def test_handles_missing_time_field(self) -> None:
        client = HyperLiquidClient(network="testnet")
        mock_response = {"levels": [[], []]}  # time なし
        client._info = MagicMock()
        client._info.l2_snapshot = MagicMock(return_value=mock_response)

        book = await client.get_l2_book("BTC")
        assert book.timestamp_ms == 0

    @pytest.mark.asyncio
    async def test_raises_on_sdk_error(self) -> None:
        client = HyperLiquidClient(network="testnet")
        client._info = MagicMock()
        client._info.l2_snapshot = MagicMock(side_effect=Exception("timeout"))
        with pytest.raises(ExchangeError, match="Failed to fetch l2_book"):
            await client.get_l2_book("BTC")


# ────────────────────────────────────────────────
# Funding rate / OI（mock）
# ────────────────────────────────────────────────


class TestFundingAndOI:
    @pytest.mark.asyncio
    async def test_funding_rate_converts_1h_to_8h(self) -> None:
        # SDK の funding は 1h 単位 → BOTでは 8h 相当に変換。
        client = HyperLiquidClient(network="testnet")
        mock_response = [
            {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 50}]},
            [{"markPx": "65000", "openInterest": "100", "funding": "0.0000125"}],
        ]
        # 1h funding 0.0000125 → 8h相当 0.0001 (0.01%)
        client._info = MagicMock()
        client._info.meta_and_asset_ctxs = MagicMock(return_value=mock_response)

        funding_8h = await client.get_funding_rate_8h("BTC")
        assert funding_8h == Decimal("0.0001")

    @pytest.mark.asyncio
    async def test_funding_missing_field_returns_zero(self) -> None:
        client = HyperLiquidClient(network="testnet")
        mock_response = [
            {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 50}]},
            [{"markPx": "65000", "openInterest": "100"}],  # funding なし
        ]
        client._info = MagicMock()
        client._info.meta_and_asset_ctxs = MagicMock(return_value=mock_response)

        funding_8h = await client.get_funding_rate_8h("BTC")
        assert funding_8h == Decimal("0")

    @pytest.mark.asyncio
    async def test_open_interest(self) -> None:
        client = HyperLiquidClient(network="testnet")
        mock_response = [
            {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 50}]},
            [{"markPx": "65000", "openInterest": "12345.67", "funding": "0"}],
        ]
        client._info = MagicMock()
        client._info.meta_and_asset_ctxs = MagicMock(return_value=mock_response)

        oi = await client.get_open_interest("BTC")
        assert oi == Decimal("12345.67")

    @pytest.mark.asyncio
    async def test_oi_missing_field_returns_zero(self) -> None:
        client = HyperLiquidClient(network="testnet")
        mock_response = [
            {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 50}]},
            [{"markPx": "65000", "funding": "0"}],  # openInterest なし
        ]
        client._info = MagicMock()
        client._info.meta_and_asset_ctxs = MagicMock(return_value=mock_response)

        oi = await client.get_open_interest("BTC")
        assert oi == Decimal("0")

    @pytest.mark.asyncio
    async def test_unknown_symbol_funding_raises(self) -> None:
        client = HyperLiquidClient(network="testnet")
        mock_response = [
            {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 50}]},
            [{"markPx": "65000", "openInterest": "100", "funding": "0"}],
        ]
        client._info = MagicMock()
        client._info.meta_and_asset_ctxs = MagicMock(return_value=mock_response)
        with pytest.raises(ExchangeError, match="Symbol not found"):
            await client.get_funding_rate_8h("UNKNOWN")

    @pytest.mark.asyncio
    async def test_unknown_symbol_oi_raises(self) -> None:
        client = HyperLiquidClient(network="testnet")
        mock_response = [
            {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 50}]},
            [{"markPx": "65000", "openInterest": "100", "funding": "0"}],
        ]
        client._info = MagicMock()
        client._info.meta_and_asset_ctxs = MagicMock(return_value=mock_response)
        with pytest.raises(ExchangeError, match="Symbol not found"):
            await client.get_open_interest("UNKNOWN")


# ────────────────────────────────────────────────
# _fetch_recent_candles
# ────────────────────────────────────────────────


class TestFetchRecentCandles:
    @pytest.mark.asyncio
    async def test_returns_candles_in_chronological_order(self) -> None:
        client = HyperLiquidClient(network="testnet")
        mock_candles = [
            {"t": 1700000000000, "o": "65000", "c": "65100", "v": "10"},
            {"t": 1700000300000, "o": "65100", "c": "65200", "v": "12"},
            {"t": 1700000600000, "o": "65200", "c": "65150", "v": "8"},
        ]
        client._info = MagicMock()
        client._info.candles_snapshot = MagicMock(return_value=mock_candles)
        result = await client._fetch_recent_candles("BTC", "5m", 3)
        assert len(result) == 3
        assert result[0]["t"] == 1700000000000
        assert result[-1]["t"] == 1700000600000

    @pytest.mark.asyncio
    async def test_unsupported_interval_raises(self) -> None:
        client = HyperLiquidClient(network="testnet")
        with pytest.raises(ExchangeError, match="Unsupported interval"):
            await client._fetch_recent_candles("BTC", "7m", 3)

    @pytest.mark.asyncio
    async def test_sdk_error_propagates(self) -> None:
        client = HyperLiquidClient(network="testnet")
        client._info = MagicMock()
        client._info.candles_snapshot = MagicMock(
            side_effect=ConnectionError("timeout")
        )
        with pytest.raises(ExchangeError, match="Failed to fetch candles"):
            await client._fetch_recent_candles("BTC", "5m", 3)


# ────────────────────────────────────────────────
# get_candles (公開 API: PR7.7)
# ────────────────────────────────────────────────


class TestGetCandles:
    @pytest.mark.asyncio
    async def test_returns_typed_candle_tuple(self) -> None:
        client = HyperLiquidClient(network="testnet")
        client._info = MagicMock()
        client._info.candles_snapshot = MagicMock(
            return_value=[
                {
                    "t": 1700000000000,
                    "o": "65000",
                    "h": "65200",
                    "l": "64900",
                    "c": "65100",
                    "v": "10",
                },
                {
                    "t": 1700000900000,
                    "o": "65100",
                    "h": "65300",
                    "l": "65000",
                    "c": "65250",
                    "v": "12",
                },
            ]
        )
        candles = await client.get_candles("BTC", "15m", 2)
        assert len(candles) == 2
        c0 = candles[0]
        assert c0.symbol == "BTC"
        assert c0.interval == "15m"
        assert c0.timestamp_ms == 1700000000000
        assert c0.open == Decimal("65000")
        assert c0.high == Decimal("65200")
        assert c0.low == Decimal("64900")
        assert c0.close == Decimal("65100")
        assert c0.volume == Decimal("10")

    @pytest.mark.asyncio
    async def test_propagates_unsupported_interval(self) -> None:
        client = HyperLiquidClient(network="testnet")
        with pytest.raises(ExchangeError, match="Unsupported interval"):
            await client.get_candles("BTC", "7m", 5)


# ────────────────────────────────────────────────
# _get_utc_day_open_price
# ────────────────────────────────────────────────


class TestUTCDayOpenPrice:
    @pytest.mark.asyncio
    async def test_returns_open_of_first_1h_candle(self) -> None:
        client = HyperLiquidClient(network="testnet")
        client._info = MagicMock()
        client._info.candles_snapshot = MagicMock(
            return_value=[{"t": 0, "o": "65432.1", "c": "65500", "v": "100"}]
        )
        result = await client._get_utc_day_open_price("BTC")
        assert result == Decimal("65432.1")

    @pytest.mark.asyncio
    async def test_no_candle_raises(self) -> None:
        client = HyperLiquidClient(network="testnet")
        client._info = MagicMock()
        client._info.candles_snapshot = MagicMock(return_value=[])
        with pytest.raises(ExchangeError, match="No UTC 00:00 candle"):
            await client._get_utc_day_open_price("BTC")

    @pytest.mark.asyncio
    async def test_sdk_error_propagates(self) -> None:
        client = HyperLiquidClient(network="testnet")
        client._info = MagicMock()
        client._info.candles_snapshot = MagicMock(
            side_effect=Exception("network error")
        )
        with pytest.raises(ExchangeError, match="Failed to fetch UTC open"):
            await client._get_utc_day_open_price("BTC")


# ────────────────────────────────────────────────
# _estimate_flow_from_book
# ────────────────────────────────────────────────


class TestEstimateFlowFromBook:
    @pytest.mark.asyncio
    async def test_calculates_top5_notional(self) -> None:
        client = HyperLiquidClient(network="testnet")
        client._info = MagicMock()
        client._info.l2_snapshot = MagicMock(
            return_value={
                "levels": [
                    [  # bids
                        {"px": "100", "sz": "1", "n": 1},
                        {"px": "99", "sz": "2", "n": 1},
                        {"px": "98", "sz": "1", "n": 1},
                        {"px": "97", "sz": "1", "n": 1},
                        {"px": "96", "sz": "1", "n": 1},
                        {"px": "95", "sz": "100", "n": 1},  # top5外
                    ],
                    [  # asks
                        {"px": "101", "sz": "2", "n": 1},
                        {"px": "102", "sz": "1", "n": 1},
                        {"px": "103", "sz": "1", "n": 1},
                        {"px": "104", "sz": "1", "n": 1},
                        {"px": "105", "sz": "1", "n": 1},
                        {"px": "106", "sz": "100", "n": 1},
                    ],
                ],
                "time": 0,
            }
        )
        buy_usd, sell_usd = await client._estimate_flow_from_book("BTC")
        # bids: 100 + 198 + 98 + 97 + 96 = 589
        # asks: 202 + 102 + 103 + 104 + 105 = 616
        assert buy_usd == Decimal("589")
        assert sell_usd == Decimal("616")

    @pytest.mark.asyncio
    async def test_book_error_returns_zeros(self) -> None:
        # 板取得失敗時は (0, 0) を返し snapshot 構築を継続。
        client = HyperLiquidClient(network="testnet")
        client._info = MagicMock()
        client._info.l2_snapshot = MagicMock(side_effect=Exception("error"))
        buy, sell = await client._estimate_flow_from_book("BTC")
        assert buy == Decimal("0")
        assert sell == Decimal("0")


# ────────────────────────────────────────────────
# get_market_snapshot 統合（mock）
# ────────────────────────────────────────────────


def _make_meta_response(symbol: str = "BTC", **ctx_overrides: object) -> list[object]:
    ctx = {
        "markPx": "65500",
        "dayHigh": "66000",
        "dayLow": "64000",
        "dayNtlVlm": "100000000",
        "dayBaseVlm": "1500",
        "prevDayPx": "65000",
        "funding": "0.00001",
        "openInterest": "12345",
    }
    ctx.update(ctx_overrides)  # type: ignore[arg-type]
    return [
        {"universe": [{"name": symbol, "szDecimals": 5, "maxLeverage": 50}]},
        [ctx],
    ]


def _make_5m_candles(count: int = 21) -> list[dict[str, object]]:
    return [
        {
            "t": i * 300_000,
            "o": "65000",
            "h": "65100",
            "l": "64900",
            "c": str(65000 + i * 10),
            "v": "10",
        }
        for i in range(count)
    ]


class TestGetMarketSnapshot:
    @pytest.mark.asyncio
    async def test_builds_complete_snapshot(self) -> None:
        client = HyperLiquidClient(network="testnet")
        client._info = MagicMock()
        client._info.meta_and_asset_ctxs = MagicMock(return_value=_make_meta_response())

        five_m = _make_5m_candles(21)
        utc_open_candles = [{"t": 0, "o": "65000", "c": "65100", "v": "100"}]

        def candles_side_effect(
            name: str, interval: str, start: int, end: int
        ) -> list[dict[str, object]]:
            if interval == "5m":
                return five_m
            if interval == "1h":
                return utc_open_candles
            return []

        client._info.candles_snapshot = MagicMock(side_effect=candles_side_effect)
        client._info.l2_snapshot = MagicMock(
            return_value={
                "levels": [
                    [{"px": "65499", "sz": "1", "n": 1}],
                    [{"px": "65501", "sz": "1", "n": 1}],
                ],
                "time": 0,
            }
        )

        snap = await client.get_market_snapshot("BTC")

        assert snap.symbol == "BTC"
        assert snap.current_price == 65500.0
        assert snap.high_24h == 66000.0
        assert snap.low_24h == 64000.0
        # VWAP = 100,000,000 / 1500 ≒ 66666.67
        assert snap.vwap == pytest.approx(66666.67, rel=0.01)
        assert snap.rolling_24h_open == 65000.0
        assert snap.utc_open_price == 65000.0
        # momentum_5bar: candle[-6].c=65150, candle[-1].c=65200 → ~0.0768%
        assert snap.momentum_5bar_pct == pytest.approx(0.0768, rel=0.1)
        # volume_surge_ratio = 直近10 / 平均10 = 1
        assert snap.volume_surge_ratio == pytest.approx(1.0, rel=0.01)
        # funding 1h=0.00001 → 8h=0.00008
        assert snap.funding_rate == pytest.approx(0.00008, rel=0.01)
        assert snap.open_interest == 12345.0
        # WS 未実装
        assert snap.flow_large_order_count == 0
        # flow_buy_sell_ratio = 65499/65501 ≒ 0.9999...
        assert snap.flow_buy_sell_ratio == pytest.approx(1.0, rel=0.01)
        # sentiment はデフォルト
        assert snap.sentiment_score == 0.0
        assert snap.sentiment_confidence == 0.0
        assert snap.btc_ema_trend == "UPTREND"

    @pytest.mark.asyncio
    async def test_unknown_symbol_raises(self) -> None:
        client = HyperLiquidClient(network="testnet")
        client._info = MagicMock()
        client._info.meta_and_asset_ctxs = MagicMock(return_value=_make_meta_response())
        with pytest.raises(ExchangeError, match="Symbol not found"):
            await client.get_market_snapshot("UNKNOWN")

    @pytest.mark.asyncio
    async def test_insufficient_candles_raises(self) -> None:
        client = HyperLiquidClient(network="testnet")
        client._info = MagicMock()
        client._info.meta_and_asset_ctxs = MagicMock(return_value=_make_meta_response())
        client._info.candles_snapshot = MagicMock(
            return_value=[
                {"t": i, "o": "65000", "h": "65000", "l": "65000", "c": "65000", "v": "10"}
                for i in range(5)
            ]
        )
        with pytest.raises(ExchangeError, match="Insufficient 5m candles"):
            await client.get_market_snapshot("BTC")

    @pytest.mark.asyncio
    async def test_zero_volume_avg_falls_back_to_one(self) -> None:
        # 直近20本平均が0 → volume_surge_ratio はゼロ除算回避で 1
        client = HyperLiquidClient(network="testnet")
        client._info = MagicMock()
        client._info.meta_and_asset_ctxs = MagicMock(return_value=_make_meta_response())
        candles = [
            {
                "t": i * 300_000,
                "o": "65000",
                "h": "65100",
                "l": "64900",
                "c": str(65000 + i * 10),
                "v": "0",  # 全部0
            }
            for i in range(21)
        ]
        utc = [{"t": 0, "o": "65000", "c": "65100", "v": "100"}]

        def side(name: str, interval: str, start: int, end: int) -> list[dict[str, object]]:
            return candles if interval == "5m" else utc

        client._info.candles_snapshot = MagicMock(side_effect=side)
        client._info.l2_snapshot = MagicMock(
            return_value={"levels": [[], []], "time": 0}
        )
        snap = await client.get_market_snapshot("BTC")
        assert snap.volume_surge_ratio == 1.0

    @pytest.mark.asyncio
    async def test_zero_five_bars_ago_close_falls_back_to_zero(self) -> None:
        # 5本前の close が 0 のとき momentum_5bar_pct はゼロ除算回避で 0
        client = HyperLiquidClient(network="testnet")
        client._info = MagicMock()
        client._info.meta_and_asset_ctxs = MagicMock(return_value=_make_meta_response())
        candles = [
            {"t": i * 300_000, "o": "0", "h": "0", "l": "0", "c": "0", "v": "10"}
            for i in range(21)
        ]
        utc = [{"t": 0, "o": "65000", "c": "65100", "v": "100"}]

        def side(name: str, interval: str, start: int, end: int) -> list[dict[str, object]]:
            return candles if interval == "5m" else utc

        client._info.candles_snapshot = MagicMock(side_effect=side)
        client._info.l2_snapshot = MagicMock(
            return_value={"levels": [[], []], "time": 0}
        )
        snap = await client.get_market_snapshot("BTC")
        assert snap.momentum_5bar_pct == 0.0

    @pytest.mark.asyncio
    async def test_zero_sell_flow_falls_back_to_one(self) -> None:
        # 板の ask 側が空 → flow_buy_sell_ratio はゼロ除算回避で 1
        client = HyperLiquidClient(network="testnet")
        client._info = MagicMock()
        client._info.meta_and_asset_ctxs = MagicMock(return_value=_make_meta_response())
        five_m = _make_5m_candles(21)
        utc = [{"t": 0, "o": "65000", "c": "65100", "v": "100"}]

        def side(name: str, interval: str, start: int, end: int) -> list[dict[str, object]]:
            return five_m if interval == "5m" else utc

        client._info.candles_snapshot = MagicMock(side_effect=side)
        client._info.l2_snapshot = MagicMock(
            return_value={
                "levels": [
                    [{"px": "65499", "sz": "1", "n": 1}],
                    [],  # ask 側空
                ],
                "time": 0,
            }
        )
        snap = await client.get_market_snapshot("BTC")
        assert snap.flow_buy_sell_ratio == 1.0


# ────────────────────────────────────────────────
# ユーザー状態系（PR6.3）
# ────────────────────────────────────────────────


_TEST_ADDR = "0x" + "1" * 40


class TestRequireAddress:
    def test_no_address_raises(self) -> None:
        client = HyperLiquidClient(network="testnet", address=None)
        with pytest.raises(ExchangeError, match="address is required"):
            client._require_address()

    def test_with_address_passes(self) -> None:
        client = HyperLiquidClient(network="testnet", address=_TEST_ADDR)
        client._require_address()  # 例外を投げない

    @pytest.mark.asyncio
    async def test_get_positions_without_address_raises(self) -> None:
        client = HyperLiquidClient(network="testnet", address=None)
        with pytest.raises(ExchangeError, match="address is required"):
            await client.get_positions()


class TestGetPositions:
    @pytest.mark.asyncio
    async def test_parses_long_position(self) -> None:
        client = HyperLiquidClient(network="testnet", address=_TEST_ADDR)
        client._info = MagicMock()
        client._info.user_state = MagicMock(
            return_value={
                "assetPositions": [
                    {
                        "position": {
                            "coin": "BTC",
                            "szi": "0.01",
                            "entryPx": "65000",
                            "unrealizedPnl": "5.5",
                            "leverage": {"value": 3},
                            "liquidationPx": "60000",
                        }
                    }
                ],
            }
        )
        positions = await client.get_positions()
        assert len(positions) == 1
        assert positions[0].symbol == "BTC"
        assert positions[0].size == Decimal("0.01")
        assert positions[0].entry_price == Decimal("65000")
        assert positions[0].leverage == 3
        assert positions[0].liquidation_price == Decimal("60000")

    @pytest.mark.asyncio
    async def test_parses_short_position(self) -> None:
        client = HyperLiquidClient(network="testnet", address=_TEST_ADDR)
        client._info = MagicMock()
        client._info.user_state = MagicMock(
            return_value={
                "assetPositions": [
                    {
                        "position": {
                            "coin": "ETH",
                            "szi": "-0.5",
                            "entryPx": "3200",
                            "unrealizedPnl": "10",
                            "leverage": {"value": 2},
                            "liquidationPx": None,
                        }
                    }
                ],
            }
        )
        positions = await client.get_positions()
        assert positions[0].size < 0
        assert positions[0].liquidation_price is None

    @pytest.mark.asyncio
    async def test_skips_zero_size(self) -> None:
        client = HyperLiquidClient(network="testnet", address=_TEST_ADDR)
        client._info = MagicMock()
        client._info.user_state = MagicMock(
            return_value={
                "assetPositions": [
                    {
                        "position": {
                            "coin": "BTC",
                            "szi": "0",
                            "entryPx": "0",
                            "unrealizedPnl": "0",
                            "leverage": {"value": 1},
                        }
                    }
                ],
            }
        )
        assert await client.get_positions() == ()

    @pytest.mark.asyncio
    async def test_empty_positions(self) -> None:
        client = HyperLiquidClient(network="testnet", address=_TEST_ADDR)
        client._info = MagicMock()
        client._info.user_state = MagicMock(return_value={"assetPositions": []})
        assert await client.get_positions() == ()

    @pytest.mark.asyncio
    async def test_no_assetPositions_key(self) -> None:
        # response が空 dict のとき。
        client = HyperLiquidClient(network="testnet", address=_TEST_ADDR)
        client._info = MagicMock()
        client._info.user_state = MagicMock(return_value={})
        assert await client.get_positions() == ()

    @pytest.mark.asyncio
    async def test_sdk_error_propagates(self) -> None:
        client = HyperLiquidClient(network="testnet", address=_TEST_ADDR)
        client._info = MagicMock()
        client._info.user_state = MagicMock(side_effect=ConnectionError("timeout"))
        with pytest.raises(ExchangeError, match="Failed to fetch positions"):
            await client.get_positions()


class TestGetOpenOrders:
    @pytest.mark.asyncio
    async def test_parses_orders(self) -> None:
        client = HyperLiquidClient(network="testnet", address=_TEST_ADDR)
        client._info = MagicMock()
        client._info.open_orders = MagicMock(
            return_value=[
                {
                    "oid": 12345,
                    "cloid": "test-cloid-1",
                    "coin": "BTC",
                    "side": "B",
                    "sz": "0.01",
                    "limitPx": "65000",
                    "orderType": "Alo",
                    "timestamp": 1700000000000,
                },
                {
                    "oid": 12346,
                    "cloid": None,
                    "coin": "ETH",
                    "side": "A",
                    "sz": "0.5",
                    "limitPx": "3200",
                    "orderType": "Gtc",
                    "timestamp": 1700000001000,
                },
            ]
        )
        orders = await client.get_open_orders()
        assert len(orders) == 2
        assert orders[0].order_id == 12345
        assert orders[0].client_order_id == "test-cloid-1"
        assert orders[0].side == "buy"
        assert orders[0].tif == "Alo"
        assert orders[1].side == "sell"
        assert orders[1].client_order_id is None

    @pytest.mark.asyncio
    async def test_unknown_tif_defaults_to_gtc(self) -> None:
        client = HyperLiquidClient(network="testnet", address=_TEST_ADDR)
        client._info = MagicMock()
        client._info.open_orders = MagicMock(
            return_value=[
                {
                    "oid": 1,
                    "cloid": None,
                    "coin": "BTC",
                    "side": "B",
                    "sz": "0.01",
                    "limitPx": "65000",
                    "orderType": "Unknown",
                    "timestamp": 0,
                }
            ]
        )
        orders = await client.get_open_orders()
        assert orders[0].tif == "Gtc"

    @pytest.mark.asyncio
    async def test_string_side_buy_sell(self) -> None:
        # SDK 経路によって "buy"/"sell" 直接の場合にも対応。
        client = HyperLiquidClient(network="testnet", address=_TEST_ADDR)
        client._info = MagicMock()
        client._info.open_orders = MagicMock(
            return_value=[
                {
                    "oid": 1,
                    "cloid": None,
                    "coin": "BTC",
                    "side": "buy",
                    "sz": "0.01",
                    "limitPx": "65000",
                    "orderType": "Gtc",
                    "timestamp": 0,
                }
            ]
        )
        orders = await client.get_open_orders()
        assert orders[0].side == "buy"

    @pytest.mark.asyncio
    async def test_sdk_error_propagates(self) -> None:
        client = HyperLiquidClient(network="testnet", address=_TEST_ADDR)
        client._info = MagicMock()
        client._info.open_orders = MagicMock(side_effect=Exception("err"))
        with pytest.raises(ExchangeError, match="Failed to fetch open orders"):
            await client.get_open_orders()


class TestGetFills:
    @pytest.mark.asyncio
    async def test_parses_fills(self) -> None:
        client = HyperLiquidClient(network="testnet", address=_TEST_ADDR)
        client._info = MagicMock()
        client._info.user_fills_by_time = MagicMock(
            return_value=[
                {
                    "oid": 12345,
                    "coin": "BTC",
                    "side": "B",
                    "sz": "0.01",
                    "px": "65000",
                    "fee": "0.5",
                    "time": 1700000000000,
                    "closedPnl": "0",
                },
                {
                    "oid": 12346,
                    "coin": "BTC",
                    "side": "A",
                    "sz": "0.01",
                    "px": "65500",
                    "fee": "0.5",
                    "time": 1700001000000,
                    "closedPnl": "5.0",
                },
            ]
        )
        fills = await client.get_fills(since_ms=1700000000000)
        assert len(fills) == 2
        assert fills[0].side == "buy"
        assert fills[0].closed_pnl == Decimal("0")
        assert fills[1].side == "sell"
        assert fills[1].closed_pnl == Decimal("5.0")

    @pytest.mark.asyncio
    async def test_empty_fills(self) -> None:
        client = HyperLiquidClient(network="testnet", address=_TEST_ADDR)
        client._info = MagicMock()
        client._info.user_fills_by_time = MagicMock(return_value=[])
        assert await client.get_fills(since_ms=0) == ()

    @pytest.mark.asyncio
    async def test_sdk_error_propagates(self) -> None:
        client = HyperLiquidClient(network="testnet", address=_TEST_ADDR)
        client._info = MagicMock()
        client._info.user_fills_by_time = MagicMock(
            side_effect=ConnectionError("network")
        )
        with pytest.raises(ExchangeError, match="Failed to fetch fills"):
            await client.get_fills(since_ms=0)


class TestGetFundingPayments:
    @pytest.mark.asyncio
    async def test_converts_1h_rate_to_8h(self) -> None:
        # SDK の delta.fundingRate は 1h → BOT は 8h 相当。
        client = HyperLiquidClient(network="testnet", address=_TEST_ADDR)
        client._info = MagicMock()
        client._info.user_funding_history = MagicMock(
            return_value=[
                {
                    "time": 1700000000000,
                    "delta": {
                        "coin": "BTC",
                        "fundingRate": "0.0000125",
                        "usdc": "-0.5",
                    },
                }
            ]
        )
        payments = await client.get_funding_payments(since_ms=0)
        assert len(payments) == 1
        # 1h 0.0000125 → 8h 0.0001
        assert payments[0].funding_rate_8h == Decimal("0.0001")
        assert payments[0].payment_usd == Decimal("-0.5")
        assert payments[0].symbol == "BTC"

    @pytest.mark.asyncio
    async def test_sdk_error_propagates(self) -> None:
        client = HyperLiquidClient(network="testnet", address=_TEST_ADDR)
        client._info = MagicMock()
        client._info.user_funding_history = MagicMock(side_effect=Exception("err"))
        with pytest.raises(ExchangeError, match="Failed to fetch funding payments"):
            await client.get_funding_payments(since_ms=0)


class TestGetAccountBalance:
    @pytest.mark.asyncio
    async def test_returns_account_value(self) -> None:
        client = HyperLiquidClient(network="testnet", address=_TEST_ADDR)
        client._info = MagicMock()
        client._info.user_state = MagicMock(
            return_value={"marginSummary": {"accountValue": "1234.56"}}
        )
        assert await client.get_account_balance_usd() == Decimal("1234.56")

    @pytest.mark.asyncio
    async def test_returns_zero_when_empty(self) -> None:
        client = HyperLiquidClient(network="testnet", address=_TEST_ADDR)
        client._info = MagicMock()
        client._info.user_state = MagicMock(return_value={})
        assert await client.get_account_balance_usd() == Decimal("0")

    @pytest.mark.asyncio
    async def test_sdk_error_propagates(self) -> None:
        client = HyperLiquidClient(network="testnet", address=_TEST_ADDR)
        client._info = MagicMock()
        client._info.user_state = MagicMock(side_effect=Exception("err"))
        with pytest.raises(ExchangeError, match="Failed to fetch account balance"):
            await client.get_account_balance_usd()


class TestGetOrderStatus:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "hl_status, expected",
        [
            ("open", "pending"),
            ("triggered", "pending"),
            ("filled", "filled"),
            ("canceled", "cancelled"),
            ("cancelled", "cancelled"),
            ("rejected", "rejected"),
            ("unknown_status", "pending"),  # フォールバック
        ],
    )
    async def test_status_mapping(self, hl_status: str, expected: str) -> None:
        client = HyperLiquidClient(network="testnet", address=_TEST_ADDR)
        client._info = MagicMock()
        client._info.query_order_by_oid = MagicMock(
            return_value={"status": "order", "order": {"status": hl_status}}
        )
        result = await client.get_order_status(12345)
        assert result == expected

    @pytest.mark.asyncio
    async def test_sdk_error_propagates(self) -> None:
        client = HyperLiquidClient(network="testnet", address=_TEST_ADDR)
        client._info = MagicMock()
        client._info.query_order_by_oid = MagicMock(side_effect=Exception("err"))
        with pytest.raises(ExchangeError, match="Failed to fetch order status"):
            await client.get_order_status(12345)


class TestGetOrderByClientId:
    @pytest.mark.asyncio
    async def test_finds_matching_order(self) -> None:
        client = HyperLiquidClient(network="testnet", address=_TEST_ADDR)
        client._info = MagicMock()
        client._info.open_orders = MagicMock(
            return_value=[
                {
                    "oid": 100,
                    "cloid": "other-cloid",
                    "coin": "BTC",
                    "side": "B",
                    "sz": "0.01",
                    "limitPx": "65000",
                    "orderType": "Alo",
                    "timestamp": 0,
                },
                {
                    "oid": 200,
                    "cloid": "target-cloid",
                    "coin": "ETH",
                    "side": "B",
                    "sz": "0.5",
                    "limitPx": "3200",
                    "orderType": "Alo",
                    "timestamp": 0,
                },
            ]
        )
        order = await client.get_order_by_client_id("target-cloid")
        assert order is not None
        assert order.order_id == 200
        assert order.client_order_id == "target-cloid"

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self) -> None:
        client = HyperLiquidClient(network="testnet", address=_TEST_ADDR)
        client._info = MagicMock()
        client._info.open_orders = MagicMock(return_value=[])
        assert await client.get_order_by_client_id("missing") is None


# ────────────────────────────────────────────────
# Exchange プロパティ（PR6.4.1）
# ────────────────────────────────────────────────


_TEST_PRIV = "0x" + "1" * 64


class TestExchangeProperty:
    def test_requires_private_key(self) -> None:
        client = HyperLiquidClient(network="testnet", address=_TEST_ADDR)
        with pytest.raises(ExchangeError, match="agent_private_key is required"):
            _ = client.exchange

    def test_requires_address(self) -> None:
        client = HyperLiquidClient(network="testnet", agent_private_key=_TEST_PRIV)
        with pytest.raises(ExchangeError, match=r"address.*required"):
            _ = client.exchange

    def test_lazy_init_calls_sdk_with_correct_args(self) -> None:
        client = HyperLiquidClient(
            network="testnet",
            address=_TEST_ADDR,
            agent_private_key=_TEST_PRIV,
        )
        assert client._exchange is None

        with (
            patch("src.infrastructure.hyperliquid_client.Exchange") as mock_exchange,
            patch("src.infrastructure.hyperliquid_client.Account") as mock_account,
        ):
            mock_account.from_key = MagicMock(return_value="signer-account")
            mock_exchange.return_value = MagicMock()
            ex = client.exchange

            mock_account.from_key.assert_called_once_with(_TEST_PRIV)
            mock_exchange.assert_called_once_with(
                wallet="signer-account",
                base_url=HyperLiquidClient.TESTNET_EXCHANGE_URL,
                account_address=_TEST_ADDR,
            )
            # 2回目は同じインスタンスを返す（再初期化しない）
            assert client.exchange is ex
            mock_exchange.assert_called_once()


# ────────────────────────────────────────────────
# cancel_order（PR6.4.1・章22.4）
# ────────────────────────────────────────────────


class TestCancelOrder:
    def _client(self) -> HyperLiquidClient:
        c = HyperLiquidClient(
            network="testnet",
            address=_TEST_ADDR,
            agent_private_key=_TEST_PRIV,
        )
        c._exchange = MagicMock()
        return c

    @pytest.mark.asyncio
    async def test_cancel_success(self) -> None:
        client = self._client()
        assert client._exchange is not None
        client._exchange.cancel = MagicMock(
            return_value={
                "status": "ok",
                "response": {
                    "type": "cancel",
                    "data": {"statuses": ["success"]},
                },
            }
        )
        assert await client.cancel_order(order_id=12345, symbol="BTC") is True
        client._exchange.cancel.assert_called_once_with("BTC", 12345)

    @pytest.mark.asyncio
    async def test_cancel_returns_false_on_inner_error(self) -> None:
        client = self._client()
        assert client._exchange is not None
        client._exchange.cancel = MagicMock(
            return_value={
                "status": "ok",
                "response": {
                    "type": "cancel",
                    "data": {
                        "statuses": [{"error": "Order was already canceled"}],
                    },
                },
            }
        )
        assert await client.cancel_order(order_id=999, symbol="BTC") is False

    @pytest.mark.asyncio
    async def test_cancel_returns_false_on_status_err(self) -> None:
        client = self._client()
        assert client._exchange is not None
        client._exchange.cancel = MagicMock(
            return_value={"status": "err", "response": "Too many requests"}
        )
        assert await client.cancel_order(order_id=12345, symbol="BTC") is False

    @pytest.mark.asyncio
    async def test_cancel_returns_false_on_empty_statuses(self) -> None:
        client = self._client()
        assert client._exchange is not None
        client._exchange.cancel = MagicMock(
            return_value={
                "status": "ok",
                "response": {"type": "cancel", "data": {"statuses": []}},
            }
        )
        assert await client.cancel_order(order_id=12345, symbol="BTC") is False

    @pytest.mark.asyncio
    async def test_cancel_sdk_exception_raises_exchange_error(self) -> None:
        client = self._client()
        assert client._exchange is not None
        client._exchange.cancel = MagicMock(side_effect=ConnectionError("timeout"))
        with pytest.raises(ExchangeError, match="Failed to cancel order"):
            await client.cancel_order(order_id=12345, symbol="BTC")

    @pytest.mark.asyncio
    async def test_cancel_without_address_raises(self) -> None:
        client = HyperLiquidClient(
            network="testnet", address=None, agent_private_key=_TEST_PRIV
        )
        with pytest.raises(ExchangeError, match="address is required"):
            await client.cancel_order(order_id=12345, symbol="BTC")

    @pytest.mark.asyncio
    async def test_cancel_without_private_key_raises(self) -> None:
        client = HyperLiquidClient(
            network="testnet", address=_TEST_ADDR, agent_private_key=None
        )
        with pytest.raises(ExchangeError, match="agent_private_key is required"):
            await client.cancel_order(order_id=12345, symbol="BTC")


# ────────────────────────────────────────────────
# place_order ヘルパー（PR6.4.2）
# ────────────────────────────────────────────────


class TestBuildOrderType:
    @pytest.mark.parametrize(
        "tif, expected",
        [
            ("Alo", {"limit": {"tif": "Alo"}}),
            ("Ioc", {"limit": {"tif": "Ioc"}}),
            ("Gtc", {"limit": {"tif": "Gtc"}}),
        ],
    )
    def test_valid_tif(self, tif: str, expected: dict[str, dict[str, str]]) -> None:
        assert HyperLiquidClient._build_order_type(tif) == expected

    def test_invalid_tif_raises(self) -> None:
        with pytest.raises(ExchangeError, match="Unsupported tif"):
            HyperLiquidClient._build_order_type("FOK")


class TestGenerateCloid:
    def test_format(self) -> None:
        cloid = _generate_cloid()
        assert cloid.startswith("0x")
        assert len(cloid) == 34
        # hex 文字のみ
        int(cloid, 16)

    def test_uniqueness(self) -> None:
        cloids = {_generate_cloid() for _ in range(100)}
        assert len(cloids) == 100


# ────────────────────────────────────────────────
# place_order（PR6.4.2・章22.4）
# ────────────────────────────────────────────────


def _make_request(**overrides: Any) -> OrderRequest:
    base = {
        "symbol": "BTC",
        "side": "buy",
        "size": Decimal("0.01"),
        "price": Decimal("60000"),
        "tif": "Alo",
        "reduce_only": False,
        "client_order_id": None,
    }
    base.update(overrides)
    return OrderRequest(**base)  # type: ignore[arg-type]


def _writeable_client() -> HyperLiquidClient:
    c = HyperLiquidClient(
        network="testnet",
        address=_TEST_ADDR,
        agent_private_key=_TEST_PRIV,
    )
    c._exchange = MagicMock()
    return c


class TestPlaceOrderResting:
    @pytest.mark.asyncio
    async def test_alo_buy_rests_on_book(self) -> None:
        client = _writeable_client()
        assert client._exchange is not None
        client._exchange.order = MagicMock(
            return_value={
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {"statuses": [{"resting": {"oid": 12345}}]},
                },
            }
        )
        cloid = "0x" + "f" * 32
        result = await client.place_order(
            _make_request(client_order_id=cloid, price=Decimal("60000"))
        )
        assert result.success is True
        assert result.order_id == 12345

        call_args = client._exchange.order.call_args
        assert call_args[0][0] == "BTC"
        assert call_args[0][1] is True
        assert call_args[0][2] == 0.01
        assert call_args[0][3] == 60000.0
        assert call_args[0][4] == {"limit": {"tif": "Alo"}}
        assert call_args[0][5] is False
        # cloid は SDK の Cloid 型でラップされる
        assert str(call_args[0][6]) == cloid

    @pytest.mark.asyncio
    async def test_sell_passes_is_buy_false(self) -> None:
        client = _writeable_client()
        assert client._exchange is not None
        client._exchange.order = MagicMock(
            return_value={
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {"statuses": [{"resting": {"oid": 1}}]},
                },
            }
        )
        await client.place_order(_make_request(side="sell"))
        assert client._exchange.order.call_args[0][1] is False


class TestPlaceOrderFilled:
    @pytest.mark.asyncio
    async def test_ioc_immediate_fill_returns_oid(self) -> None:
        client = _writeable_client()
        assert client._exchange is not None
        client._exchange.order = MagicMock(
            return_value={
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {
                        "statuses": [
                            {
                                "filled": {
                                    "totalSz": "0.01",
                                    "avgPx": "65000",
                                    "oid": 99999,
                                }
                            }
                        ]
                    },
                },
            }
        )
        result = await client.place_order(
            _make_request(tif="Ioc", price=Decimal("70000"))
        )
        assert result.success is True
        assert result.order_id == 99999


class TestPlaceOrderALORejection:
    @pytest.mark.asyncio
    async def test_alo_would_match_raises_rejected(self) -> None:
        client = _writeable_client()
        assert client._exchange is not None
        client._exchange.order = MagicMock(
            return_value={
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {
                        "statuses": [
                            {
                                "error": (
                                    "Order would have matched immediately. "
                                    "ALO rejected."
                                )
                            }
                        ]
                    },
                },
            }
        )
        with pytest.raises(OrderRejectedError) as exc_info:
            await client.place_order(_make_request())
        assert exc_info.value.code == "ALO_REJECT"

    @pytest.mark.asyncio
    async def test_alo_real_testnet_message_raises_rejected(self) -> None:
        # HL testnet が実際に返すメッセージ（PR6.4.2 検証で判明）。
        client = _writeable_client()
        assert client._exchange is not None
        client._exchange.order = MagicMock(
            return_value={
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {
                        "statuses": [
                            {
                                "error": (
                                    "Post only order would have immediately "
                                    "matched, bbo was 76317@76332. asset=3"
                                )
                            }
                        ]
                    },
                },
            }
        )
        with pytest.raises(OrderRejectedError) as exc_info:
            await client.place_order(_make_request())
        assert exc_info.value.code == "ALO_REJECT"


class TestPlaceOrderDuplicate:
    @pytest.mark.asyncio
    async def test_duplicate_cloid_raises(self) -> None:
        client = _writeable_client()
        assert client._exchange is not None
        client._exchange.order = MagicMock(
            return_value={
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {
                        "statuses": [
                            {"error": "duplicate cloid: order already placed"}
                        ]
                    },
                },
            }
        )
        with pytest.raises(DuplicateOrderError):
            await client.place_order(
                _make_request(client_order_id="0x" + "f" * 32)
            )


class TestPlaceOrderRateLimit:
    @pytest.mark.asyncio
    async def test_top_level_err_too_many_requests(self) -> None:
        client = _writeable_client()
        assert client._exchange is not None
        client._exchange.order = MagicMock(
            return_value={"status": "err", "response": "Too many requests"}
        )
        with pytest.raises(RateLimitError):
            await client.place_order(_make_request())

    @pytest.mark.asyncio
    async def test_top_level_err_rate_limited(self) -> None:
        client = _writeable_client()
        assert client._exchange is not None
        client._exchange.order = MagicMock(
            return_value={"status": "err", "response": "rate limit exceeded"}
        )
        with pytest.raises(RateLimitError):
            await client.place_order(_make_request())

    @pytest.mark.asyncio
    async def test_top_level_err_other_raises_rejected(self) -> None:
        client = _writeable_client()
        assert client._exchange is not None
        client._exchange.order = MagicMock(
            return_value={"status": "err", "response": "insufficient balance"}
        )
        with pytest.raises(OrderRejectedError):
            await client.place_order(_make_request())


class TestPlaceOrderErrors:
    @pytest.mark.asyncio
    async def test_no_address_raises(self) -> None:
        client = HyperLiquidClient(
            network="testnet", address=None, agent_private_key=_TEST_PRIV
        )
        with pytest.raises(ExchangeError, match="address is required"):
            await client.place_order(_make_request())

    @pytest.mark.asyncio
    async def test_no_private_key_raises(self) -> None:
        client = HyperLiquidClient(
            network="testnet", address=_TEST_ADDR, agent_private_key=None
        )
        with pytest.raises(ExchangeError, match="agent_private_key is required"):
            await client.place_order(_make_request())

    @pytest.mark.asyncio
    async def test_sdk_exception_propagates_as_exchange_error(self) -> None:
        client = _writeable_client()
        assert client._exchange is not None
        client._exchange.order = MagicMock(
            side_effect=ConnectionError("network down")
        )
        with pytest.raises(ExchangeError, match="Failed to place order"):
            await client.place_order(_make_request())

    @pytest.mark.asyncio
    async def test_no_statuses_raises(self) -> None:
        client = _writeable_client()
        assert client._exchange is not None
        client._exchange.order = MagicMock(
            return_value={
                "status": "ok",
                "response": {"type": "order", "data": {"statuses": []}},
            }
        )
        with pytest.raises(ExchangeError, match="No statuses"):
            await client.place_order(_make_request())

    @pytest.mark.asyncio
    async def test_unknown_top_status_raises(self) -> None:
        client = _writeable_client()
        assert client._exchange is not None
        client._exchange.order = MagicMock(return_value={"status": "wat"})
        with pytest.raises(ExchangeError, match="Unknown status"):
            await client.place_order(_make_request())

    @pytest.mark.asyncio
    async def test_unexpected_status_format_raises(self) -> None:
        client = _writeable_client()
        assert client._exchange is not None
        client._exchange.order = MagicMock(
            return_value={
                "status": "ok",
                "response": {"type": "order", "data": {"statuses": [123]}},
            }
        )
        with pytest.raises(ExchangeError, match="Unexpected status format"):
            await client.place_order(_make_request())

    @pytest.mark.asyncio
    async def test_unrecognized_dict_raises(self) -> None:
        client = _writeable_client()
        assert client._exchange is not None
        client._exchange.order = MagicMock(
            return_value={
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {"statuses": [{"unknown_kind": {}}]},
                },
            }
        )
        with pytest.raises(ExchangeError, match="Unrecognized status"):
            await client.place_order(_make_request())

    @pytest.mark.asyncio
    async def test_string_status_returns_success_without_oid(self) -> None:
        # 古い SDK 互換: statuses[0] が "success" のような文字列。
        client = _writeable_client()
        assert client._exchange is not None
        client._exchange.order = MagicMock(
            return_value={
                "status": "ok",
                "response": {"type": "order", "data": {"statuses": ["success"]}},
            }
        )
        result = await client.place_order(_make_request())
        assert result.success is True
        assert result.order_id is None

    @pytest.mark.asyncio
    async def test_generic_inner_error_raises_rejected(self) -> None:
        client = _writeable_client()
        assert client._exchange is not None
        client._exchange.order = MagicMock(
            return_value={
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {"statuses": [{"error": "minimum size violated"}]},
                },
            }
        )
        with pytest.raises(OrderRejectedError):
            await client.place_order(_make_request())


class TestPlaceOrderAutoGenerateCloid:
    @pytest.mark.asyncio
    async def test_cloid_auto_generated_when_none(self) -> None:
        client = _writeable_client()
        assert client._exchange is not None
        client._exchange.order = MagicMock(
            return_value={
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {"statuses": [{"resting": {"oid": 1}}]},
                },
            }
        )
        await client.place_order(_make_request(client_order_id=None))
        cloid_passed = str(client._exchange.order.call_args[0][6])
        assert cloid_passed.startswith("0x")
        assert len(cloid_passed) == 34


# ────────────────────────────────────────────────
# place_trigger_order / place_orders_grouped（PR6.4.3）
# ────────────────────────────────────────────────


def _trigger_request(**overrides: Any) -> TriggerOrderRequest:
    base: dict[str, Any] = {
        "symbol": "BTC",
        "side": "sell",
        "size": Decimal("0.0002"),
        "trigger_price": Decimal("70000"),
        "is_market": True,
        "limit_price": None,
        "tpsl": "sl",
        "reduce_only": True,
    }
    base.update(overrides)
    return TriggerOrderRequest(**base)


class TestBuildTriggerOrderType:
    def test_market_sl(self) -> None:
        req = _trigger_request()
        assert HyperLiquidClient._build_trigger_order_type(req) == {
            "trigger": {
                "isMarket": True,
                "triggerPx": 70000.0,
                "tpsl": "sl",
            }
        }

    def test_limit_tp(self) -> None:
        req = _trigger_request(
            tpsl="tp",
            is_market=False,
            limit_price=Decimal("80100"),
            trigger_price=Decimal("80000"),
        )
        assert HyperLiquidClient._build_trigger_order_type(req) == {
            "trigger": {
                "isMarket": False,
                "triggerPx": 80000.0,
                "tpsl": "tp",
            }
        }

    def test_invalid_tpsl_raises(self) -> None:
        # frozen dataclass の Literal 制約は静的検査だけなので、
        # 実行時のガードを object.__setattr__ で偽装入力させて確認。
        req = _trigger_request()
        object.__setattr__(req, "tpsl", "invalid")
        with pytest.raises(ExchangeError, match="Invalid tpsl"):
            HyperLiquidClient._build_trigger_order_type(req)


class TestPlaceTriggerOrder:
    @pytest.mark.asyncio
    async def test_market_sl_uses_trigger_as_limit_px(self) -> None:
        client = _writeable_client()
        assert client._exchange is not None
        client._exchange.order = MagicMock(
            return_value={
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {"statuses": [{"resting": {"oid": 9001}}]},
                },
            }
        )
        result = await client.place_trigger_order(_trigger_request())
        assert result.success is True
        assert result.order_id == 9001

        call_args = client._exchange.order.call_args
        assert call_args[0][0] == "BTC"
        assert call_args[0][1] is False  # sell → is_buy=False
        assert call_args[0][2] == 0.0002
        assert call_args[0][3] == 70000.0  # market → trigger_price 流用
        assert call_args[0][4] == {
            "trigger": {
                "isMarket": True,
                "triggerPx": 70000.0,
                "tpsl": "sl",
            }
        }
        assert call_args[0][5] is True  # reduce_only

    @pytest.mark.asyncio
    async def test_limit_tp_uses_limit_price(self) -> None:
        client = _writeable_client()
        assert client._exchange is not None
        client._exchange.order = MagicMock(
            return_value={
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {"statuses": [{"resting": {"oid": 1}}]},
                },
            }
        )
        await client.place_trigger_order(
            _trigger_request(
                tpsl="tp",
                is_market=False,
                limit_price=Decimal("80100"),
                trigger_price=Decimal("80000"),
            )
        )
        call_args = client._exchange.order.call_args
        assert call_args[0][3] == 80100.0  # limit → limit_price

    @pytest.mark.asyncio
    async def test_buy_side_passes_is_buy_true(self) -> None:
        # SHORT 側の TP は side=buy。
        client = _writeable_client()
        assert client._exchange is not None
        client._exchange.order = MagicMock(
            return_value={
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {"statuses": [{"resting": {"oid": 1}}]},
                },
            }
        )
        await client.place_trigger_order(_trigger_request(side="buy"))
        assert client._exchange.order.call_args[0][1] is True

    @pytest.mark.asyncio
    async def test_limit_without_limit_price_raises(self) -> None:
        client = _writeable_client()
        with pytest.raises(ExchangeError, match="limit_price is required"):
            await client.place_trigger_order(
                _trigger_request(is_market=False, limit_price=None)
            )

    @pytest.mark.asyncio
    async def test_no_address_raises(self) -> None:
        client = HyperLiquidClient(
            network="testnet", address=None, agent_private_key=_TEST_PRIV
        )
        with pytest.raises(ExchangeError, match="address is required"):
            await client.place_trigger_order(_trigger_request())

    @pytest.mark.asyncio
    async def test_no_private_key_raises(self) -> None:
        client = HyperLiquidClient(
            network="testnet", address=_TEST_ADDR, agent_private_key=None
        )
        with pytest.raises(ExchangeError, match="agent_private_key is required"):
            await client.place_trigger_order(_trigger_request())

    @pytest.mark.asyncio
    async def test_sdk_exception_propagates(self) -> None:
        client = _writeable_client()
        assert client._exchange is not None
        client._exchange.order = MagicMock(
            side_effect=ConnectionError("network down")
        )
        with pytest.raises(ExchangeError, match="Failed to place trigger order"):
            await client.place_trigger_order(_trigger_request())


class TestPlaceOrdersGrouped:
    @pytest.mark.asyncio
    async def test_entry_with_tp_and_sl(self) -> None:
        client = _writeable_client()
        assert client._exchange is not None
        client._exchange.bulk_orders = MagicMock(
            return_value={
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {
                        "statuses": [
                            {"resting": {"oid": 100}},
                            {"resting": {"oid": 101}},
                            {"resting": {"oid": 102}},
                        ]
                    },
                },
            }
        )
        entry = _make_request(price=Decimal("65000"), size=Decimal("0.0002"))
        tp = _trigger_request(
            tpsl="tp",
            is_market=False,
            limit_price=Decimal("67100"),
            trigger_price=Decimal("67000"),
        )
        sl = _trigger_request(trigger_price=Decimal("63000"))

        results = await client.place_orders_grouped(entry, tp, sl)
        assert tuple(r.order_id for r in results) == (100, 101, 102)
        assert all(r.success for r in results)

        sdk_orders = client._exchange.bulk_orders.call_args[0][0]
        assert len(sdk_orders) == 3
        assert client._exchange.bulk_orders.call_args[1]["grouping"] == "normalTpsl"
        # entry の dict 構造
        assert sdk_orders[0]["coin"] == "BTC"
        assert sdk_orders[0]["is_buy"] is True
        assert sdk_orders[0]["order_type"] == {"limit": {"tif": "Alo"}}
        # tp は trigger.tpsl="tp"
        assert sdk_orders[1]["order_type"]["trigger"]["tpsl"] == "tp"
        assert sdk_orders[1]["limit_px"] == 67100.0
        # sl は trigger.tpsl="sl"・market は trigger_price 流用
        assert sdk_orders[2]["order_type"]["trigger"]["tpsl"] == "sl"
        assert sdk_orders[2]["limit_px"] == 63000.0

    @pytest.mark.asyncio
    async def test_entry_with_sl_only(self) -> None:
        client = _writeable_client()
        assert client._exchange is not None
        client._exchange.bulk_orders = MagicMock(
            return_value={
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {
                        "statuses": [
                            {"resting": {"oid": 100}},
                            {"resting": {"oid": 102}},
                        ]
                    },
                },
            }
        )
        entry = _make_request(price=Decimal("65000"), size=Decimal("0.0002"))
        sl = _trigger_request(trigger_price=Decimal("63000"))

        results = await client.place_orders_grouped(entry, None, sl)
        assert len(results) == 2
        assert client._exchange.bulk_orders.call_args[0][0].__len__() == 2

    @pytest.mark.asyncio
    async def test_entry_with_tp_only(self) -> None:
        # tp 単独でも分岐を踏ませる。
        client = _writeable_client()
        assert client._exchange is not None
        client._exchange.bulk_orders = MagicMock(
            return_value={
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {
                        "statuses": [
                            {"resting": {"oid": 100}},
                            {"resting": {"oid": 101}},
                        ]
                    },
                },
            }
        )
        entry = _make_request(price=Decimal("65000"), size=Decimal("0.0002"))
        tp = _trigger_request(
            tpsl="tp",
            is_market=False,
            limit_price=Decimal("67100"),
            trigger_price=Decimal("67000"),
        )
        results = await client.place_orders_grouped(entry, tp, None)
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_no_tp_no_sl_raises(self) -> None:
        client = _writeable_client()
        entry = _make_request()
        with pytest.raises(ExchangeError, match="At least one of tp or sl"):
            await client.place_orders_grouped(entry, None, None)

    @pytest.mark.asyncio
    async def test_partial_failure_returns_mixed_results(self) -> None:
        client = _writeable_client()
        assert client._exchange is not None
        client._exchange.bulk_orders = MagicMock(
            return_value={
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {
                        "statuses": [
                            {"resting": {"oid": 100}},
                            {"error": "Insufficient margin"},
                            {"resting": {"oid": 102}},
                        ]
                    },
                },
            }
        )
        entry = _make_request(price=Decimal("65000"), size=Decimal("0.0002"))
        tp = _trigger_request(
            tpsl="tp",
            is_market=False,
            limit_price=Decimal("67100"),
            trigger_price=Decimal("67000"),
        )
        sl = _trigger_request(trigger_price=Decimal("63000"))

        results = await client.place_orders_grouped(entry, tp, sl)
        assert results[0].success is True
        assert results[1].success is False
        assert "Insufficient margin" in (results[1].rejected_reason or "")
        assert results[2].success is True

    @pytest.mark.asyncio
    async def test_filled_status_returns_success(self) -> None:
        client = _writeable_client()
        assert client._exchange is not None
        client._exchange.bulk_orders = MagicMock(
            return_value={
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {
                        "statuses": [
                            {"filled": {"totalSz": "0.0002", "avgPx": "65000", "oid": 100}},
                            {"resting": {"oid": 102}},
                        ]
                    },
                },
            }
        )
        entry = _make_request(price=Decimal("65000"), size=Decimal("0.0002"))
        sl = _trigger_request(trigger_price=Decimal("63000"))
        results = await client.place_orders_grouped(entry, None, sl)
        assert results[0].order_id == 100
        assert results[0].success is True

    @pytest.mark.asyncio
    async def test_string_status_returns_success_no_oid(self) -> None:
        client = _writeable_client()
        assert client._exchange is not None
        client._exchange.bulk_orders = MagicMock(
            return_value={
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {"statuses": ["success", {"resting": {"oid": 102}}]},
                },
            }
        )
        entry = _make_request(price=Decimal("65000"), size=Decimal("0.0002"))
        sl = _trigger_request(trigger_price=Decimal("63000"))
        results = await client.place_orders_grouped(entry, None, sl)
        assert results[0].success is True
        assert results[0].order_id is None

    @pytest.mark.asyncio
    async def test_top_level_err_rate_limit_raises(self) -> None:
        client = _writeable_client()
        assert client._exchange is not None
        client._exchange.bulk_orders = MagicMock(
            return_value={"status": "err", "response": "Too many requests"}
        )
        entry = _make_request()
        sl = _trigger_request(trigger_price=Decimal("63000"))
        with pytest.raises(RateLimitError):
            await client.place_orders_grouped(entry, None, sl)

    @pytest.mark.asyncio
    async def test_unknown_top_status_raises(self) -> None:
        client = _writeable_client()
        assert client._exchange is not None
        client._exchange.bulk_orders = MagicMock(return_value={"status": "wat"})
        entry = _make_request()
        sl = _trigger_request(trigger_price=Decimal("63000"))
        with pytest.raises(ExchangeError, match="Unknown status"):
            await client.place_orders_grouped(entry, None, sl)

    @pytest.mark.asyncio
    async def test_no_statuses_raises(self) -> None:
        client = _writeable_client()
        assert client._exchange is not None
        client._exchange.bulk_orders = MagicMock(
            return_value={
                "status": "ok",
                "response": {"type": "order", "data": {"statuses": []}},
            }
        )
        entry = _make_request()
        sl = _trigger_request(trigger_price=Decimal("63000"))
        with pytest.raises(ExchangeError, match="No statuses"):
            await client.place_orders_grouped(entry, None, sl)

    @pytest.mark.asyncio
    async def test_unexpected_status_format_raises(self) -> None:
        client = _writeable_client()
        assert client._exchange is not None
        client._exchange.bulk_orders = MagicMock(
            return_value={
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {"statuses": [123, {"resting": {"oid": 1}}]},
                },
            }
        )
        entry = _make_request()
        sl = _trigger_request(trigger_price=Decimal("63000"))
        with pytest.raises(ExchangeError, match="Unexpected status"):
            await client.place_orders_grouped(entry, None, sl)

    @pytest.mark.asyncio
    async def test_unrecognized_dict_status_raises(self) -> None:
        client = _writeable_client()
        assert client._exchange is not None
        client._exchange.bulk_orders = MagicMock(
            return_value={
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {"statuses": [{"weird": {}}, {"resting": {"oid": 1}}]},
                },
            }
        )
        entry = _make_request()
        sl = _trigger_request(trigger_price=Decimal("63000"))
        with pytest.raises(ExchangeError, match="Unrecognized status"):
            await client.place_orders_grouped(entry, None, sl)

    @pytest.mark.asyncio
    async def test_no_address_raises(self) -> None:
        client = HyperLiquidClient(
            network="testnet", address=None, agent_private_key=_TEST_PRIV
        )
        entry = _make_request()
        sl = _trigger_request(trigger_price=Decimal("63000"))
        with pytest.raises(ExchangeError, match="address is required"):
            await client.place_orders_grouped(entry, None, sl)

    @pytest.mark.asyncio
    async def test_no_private_key_raises(self) -> None:
        client = HyperLiquidClient(
            network="testnet", address=_TEST_ADDR, agent_private_key=None
        )
        entry = _make_request()
        sl = _trigger_request(trigger_price=Decimal("63000"))
        with pytest.raises(ExchangeError, match="agent_private_key is required"):
            await client.place_orders_grouped(entry, None, sl)

    @pytest.mark.asyncio
    async def test_sdk_exception_propagates(self) -> None:
        client = _writeable_client()
        assert client._exchange is not None
        client._exchange.bulk_orders = MagicMock(
            side_effect=ConnectionError("net down")
        )
        entry = _make_request()
        sl = _trigger_request(trigger_price=Decimal("63000"))
        with pytest.raises(ExchangeError, match="Failed to place grouped"):
            await client.place_orders_grouped(entry, None, sl)


# ────────────────────────────────────────────────
# E2E（testnet 実接続・デフォルト skip）
# ────────────────────────────────────────────────


@pytest.mark.e2e
class TestE2ETestnet:
    """testnet で実接続するテスト。実行: pytest -m e2e"""

    @pytest.mark.asyncio
    async def test_connects_and_gets_btc_meta(self) -> None:
        client = HyperLiquidClient(network="testnet")
        symbols = await client.get_symbols()
        assert len(symbols) > 0
        btc = next((s for s in symbols if s.symbol == "BTC"), None)
        assert btc is not None
        assert btc.sz_decimals == 5  # 章22.5: BTC は 5

    @pytest.mark.asyncio
    async def test_l2_book_btc(self) -> None:
        client = HyperLiquidClient(network="testnet")
        book = await client.get_l2_book("BTC")
        assert book.symbol == "BTC"
        assert len(book.bids) > 0
        assert len(book.asks) > 0
        # bids は降順、asks は昇順
        assert book.bids[0].price > book.bids[-1].price
        assert book.asks[0].price < book.asks[-1].price
        # bid_top < ask_top
        assert book.bids[0].price < book.asks[0].price

    @pytest.mark.asyncio
    async def test_funding_rate_btc(self) -> None:
        client = HyperLiquidClient(network="testnet")
        funding = await client.get_funding_rate_8h("BTC")
        # 通常は -5% 〜 +5% の範囲（極端でなければ）
        assert -Decimal("0.05") < funding < Decimal("0.05")

    @pytest.mark.asyncio
    async def test_open_interest_btc(self) -> None:
        client = HyperLiquidClient(network="testnet")
        oi = await client.get_open_interest("BTC")
        assert oi > 0

    @pytest.mark.asyncio
    async def test_market_snapshot_btc(self) -> None:
        client = HyperLiquidClient(network="testnet")
        snap = await client.get_market_snapshot("BTC")
        assert snap.symbol == "BTC"
        assert snap.current_price > 0
        assert snap.vwap > 0
        assert snap.high_24h >= snap.low_24h
        assert snap.utc_open_price > 0
        assert snap.rolling_24h_open > 0
        assert snap.open_interest > 0
        # APPLICATION 層で上書きされる前提のデフォルト
        assert snap.sentiment_score == 0.0
        assert snap.btc_ema_trend == "UPTREND"


# ────────────────────────────────────────────────
# E2E ユーザー状態系（PR6.3・testnet）
# ────────────────────────────────────────────────


@pytest.mark.e2e
class TestE2EUserState:
    """testnet で自分のアドレスに対して user 状態を取得するテスト。

    HL_TESTNET_ADDRESS 環境変数が無ければ skip。
    """

    @pytest.fixture
    def address(self) -> str:
        import os

        addr = os.getenv("HL_TESTNET_ADDRESS")
        if not addr:
            pytest.skip("HL_TESTNET_ADDRESS not set")
        return addr

    @pytest.mark.asyncio
    async def test_get_account_balance(self, address: str) -> None:
        client = HyperLiquidClient(network="testnet", address=address)
        balance = await client.get_account_balance_usd()
        assert balance >= Decimal("0")

    @pytest.mark.asyncio
    async def test_get_positions(self, address: str) -> None:
        client = HyperLiquidClient(network="testnet", address=address)
        positions = await client.get_positions()
        assert isinstance(positions, tuple)
        for pos in positions:
            assert pos.symbol
            assert pos.size != 0
            assert pos.entry_price > 0

    @pytest.mark.asyncio
    async def test_get_open_orders(self, address: str) -> None:
        client = HyperLiquidClient(network="testnet", address=address)
        orders = await client.get_open_orders()
        assert isinstance(orders, tuple)

    @pytest.mark.asyncio
    async def test_get_fills_recent(self, address: str) -> None:
        client = HyperLiquidClient(network="testnet", address=address)
        since_ms = int((datetime.now(UTC).timestamp() - 86400) * 1000)
        fills = await client.get_fills(since_ms=since_ms)
        assert isinstance(fills, tuple)

    @pytest.mark.asyncio
    async def test_get_funding_payments_recent(self, address: str) -> None:
        client = HyperLiquidClient(network="testnet", address=address)
        since_ms = int((datetime.now(UTC).timestamp() - 86400) * 1000)
        payments = await client.get_funding_payments(since_ms=since_ms)
        assert isinstance(payments, tuple)


# ────────────────────────────────────────────────
# E2E cancel_order（PR6.4.1・testnet 実署名）
# ────────────────────────────────────────────────


@pytest.mark.e2e
class TestE2ECancel:
    """testnet で Agent Wallet 署名 → 存在しない order_id を cancel → False。

    実行条件:
        HL_TESTNET_ADDRESS （Master Wallet）
        HL_TESTNET_AGENT_KEY （Agent Wallet 秘密鍵）
    どちらか欠けていれば skip。
    """

    @pytest.fixture
    def client(self) -> HyperLiquidClient:
        import os

        addr = os.getenv("HL_TESTNET_ADDRESS")
        key = os.getenv("HL_TESTNET_AGENT_KEY")
        if not addr or not key:
            pytest.skip("HL_TESTNET_ADDRESS or HL_TESTNET_AGENT_KEY not set")
        return HyperLiquidClient(
            network="testnet",
            address=addr,
            agent_private_key=key,
        )

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_order_returns_false(
        self, client: HyperLiquidClient
    ) -> None:
        result = await client.cancel_order(order_id=999999999, symbol="BTC")
        assert result is False


# ────────────────────────────────────────────────
# E2E place_order（PR6.4.2・testnet 実発注）
# ────────────────────────────────────────────────


@pytest.mark.e2e
class TestE2EPlaceOrder:
    """testnet で実発注するテスト。実行: pytest -m e2e

    実行条件:
        HL_TESTNET_ADDRESS, HL_TESTNET_AGENT_KEY
        testnet USDC 残高（最低 50 USDC 推奨）
    """

    @pytest.fixture
    def client(self) -> HyperLiquidClient:
        import os

        addr = os.getenv("HL_TESTNET_ADDRESS")
        key = os.getenv("HL_TESTNET_AGENT_KEY")
        if not addr or not key:
            pytest.skip("HL_TESTNET_ADDRESS or HL_TESTNET_AGENT_KEY not set")
        return HyperLiquidClient(
            network="testnet",
            address=addr,
            agent_private_key=key,
        )

    @pytest.mark.asyncio
    async def test_alo_rest_then_cancel(self, client: HyperLiquidClient) -> None:
        """ALO 注文を板に置いてからキャンセル。"""
        book = await client.get_l2_book("BTC")
        best_bid = book.bids[0].price
        tick = await client.get_tick_size("BTC")
        target_price = best_bid - tick * Decimal("10")

        request = OrderRequest(
            symbol="BTC",
            side="buy",
            size=Decimal("0.0002"),
            price=target_price,
            tif="Alo",
        )

        result = await client.place_order(request)
        try:
            assert result.success is True
            assert result.order_id is not None
            orders = await client.get_open_orders()
            our_order = next(
                (o for o in orders if o.order_id == result.order_id), None
            )
            assert our_order is not None
            assert our_order.symbol == "BTC"
        finally:
            if result.order_id is not None:
                await client.cancel_order(result.order_id, "BTC")

    @pytest.mark.asyncio
    async def test_alo_immediate_match_raises_rejected(
        self, client: HyperLiquidClient
    ) -> None:
        """ALO で best_ask 以上に買い指値 → 即マッチして ALO 拒否。"""
        book = await client.get_l2_book("BTC")
        best_ask = book.asks[0].price
        tick = await client.get_tick_size("BTC")
        target_price = best_ask + tick * Decimal("10")

        request = OrderRequest(
            symbol="BTC",
            side="buy",
            size=Decimal("0.0002"),
            price=target_price,
            tif="Alo",
        )
        with pytest.raises(OrderRejectedError):
            await client.place_order(request)


# ────────────────────────────────────────────────
# E2E place_trigger_order（PR6.4.3）
# ────────────────────────────────────────────────


@pytest.mark.e2e
class TestE2ETriggerOrder:
    """testnet で place_trigger_order の入口バリデーションを確認するテスト。

    実発注は無し（既存ポジションが無いと reduce_only 違反になりやすい）。
    リクエスト構造のバリデーションが効くことだけ検証する。
    """

    @pytest.fixture
    def client(self) -> HyperLiquidClient:
        import os

        addr = os.getenv("HL_TESTNET_ADDRESS")
        key = os.getenv("HL_TESTNET_AGENT_KEY")
        if not addr or not key:
            pytest.skip("HL_TESTNET_ADDRESS or HL_TESTNET_AGENT_KEY not set")
        return HyperLiquidClient(
            network="testnet",
            address=addr,
            agent_private_key=key,
        )

    @pytest.mark.asyncio
    async def test_trigger_order_invalid_request_raises(
        self, client: HyperLiquidClient
    ) -> None:
        bad = TriggerOrderRequest(
            symbol="BTC",
            side="sell",
            size=Decimal("0.0002"),
            trigger_price=Decimal("70000"),
            is_market=False,
            limit_price=None,
            tpsl="sl",
            reduce_only=True,
        )
        with pytest.raises(ExchangeError, match="limit_price is required"):
            await client.place_trigger_order(bad)
