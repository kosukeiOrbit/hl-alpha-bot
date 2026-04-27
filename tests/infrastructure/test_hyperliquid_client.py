"""HyperLiquidClient のテスト。

- 単体テスト（mock）: SDK の戻り値をモックして変換ロジック検証
- E2E テスト（@pytest.mark.e2e）: testnet で実接続。デフォルトでは skip
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from src.adapters.exchange import ExchangeError, L2Book, SymbolMeta
from src.infrastructure.hyperliquid_client import HyperLiquidClient

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
