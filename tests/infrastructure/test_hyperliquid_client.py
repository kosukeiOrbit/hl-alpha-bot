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
# 未実装メソッドの NotImplementedError
# ────────────────────────────────────────────────


class TestUnimplementedMethods:
    @pytest.mark.asyncio
    async def test_get_market_snapshot_raises_not_implemented(self) -> None:
        client = HyperLiquidClient(network="testnet")
        with pytest.raises(NotImplementedError, match=r"PR6\.2"):
            await client.get_market_snapshot("BTC")


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
