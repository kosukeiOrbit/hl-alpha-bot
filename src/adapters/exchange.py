"""ExchangeProtocol（章11.5）。

HyperLiquid との通信を抽象化し、INFRASTRUCTURE層で実装される。
章22 の API 仕様をベースに、CORE層が必要とする最小限の操作を定義。
全メソッド async（HL は HTTP / WebSocket）。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol

from src.core.models import MarketSnapshot

# ───────────────────────────────────────────────
# データ構造
# ───────────────────────────────────────────────


@dataclass(frozen=True)
class L2BookLevel:
    """板の1レベル（章22.4）。"""

    price: Decimal
    size: Decimal
    n_orders: int


@dataclass(frozen=True)
class L2Book:
    """板情報（買い・売り）。bids は高値順、asks は安値順。"""

    symbol: str
    bids: tuple[L2BookLevel, ...]
    asks: tuple[L2BookLevel, ...]
    timestamp_ms: int


@dataclass(frozen=True)
class Position:
    """ポジション情報（章22.7 clearinghouseState）。size は正=LONG / 負=SHORT。"""

    symbol: str
    size: Decimal
    entry_price: Decimal
    unrealized_pnl: Decimal
    leverage: int
    liquidation_price: Decimal | None


@dataclass(frozen=True)
class Order:
    """未約定注文。"""

    order_id: int
    client_order_id: str | None
    symbol: str
    side: Literal["buy", "sell"]
    size: Decimal
    price: Decimal
    tif: Literal["Alo", "Ioc", "Gtc"]
    timestamp_ms: int


@dataclass(frozen=True)
class Fill:
    """約定履歴。closed_pnl はエントリー時0、決済時に確定値。"""

    order_id: int
    symbol: str
    side: Literal["buy", "sell"]
    size: Decimal
    price: Decimal
    fee_usd: Decimal
    timestamp_ms: int
    closed_pnl: Decimal


@dataclass(frozen=True)
class FundingPayment:
    """Funding精算履歴（章22.6）。payment_usd は正=受取 / 負=支払。"""

    symbol: str
    funding_rate_8h: Decimal
    payment_usd: Decimal
    timestamp_ms: int


@dataclass(frozen=True)
class OrderRequest:
    """注文発注リクエスト（章22.4）。"""

    symbol: str
    side: Literal["buy", "sell"]
    size: Decimal
    price: Decimal
    tif: Literal["Alo", "Ioc", "Gtc"]
    reduce_only: bool = False
    client_order_id: str | None = None


@dataclass(frozen=True)
class TriggerOrderRequest:
    """TP/SL 注文（章22.4）。reduce_only はデフォルト True。"""

    symbol: str
    side: Literal["buy", "sell"]
    size: Decimal
    trigger_price: Decimal
    is_market: bool
    limit_price: Decimal | None
    tpsl: Literal["tp", "sl"]
    reduce_only: bool = True


@dataclass(frozen=True)
class OrderResult:
    """注文発注結果。"""

    success: bool
    order_id: int | None
    rejected_reason: str | None = None


@dataclass(frozen=True)
class SymbolMeta:
    """銘柄メタ情報（章22.5）。"""

    symbol: str
    sz_decimals: int
    max_leverage: int
    tick_size: Decimal


# ───────────────────────────────────────────────
# 例外階層
# ───────────────────────────────────────────────


class ExchangeError(Exception):
    """Exchange操作の基礎例外。"""


class OrderRejectedError(ExchangeError):
    """注文拒否（ALO拒否含む）。"""

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class RateLimitError(ExchangeError):
    """レート制限到達（章22.2）。"""


class DuplicateOrderError(ExchangeError):
    """同じ client_order_id で二重発注（章9.5）。"""


# ───────────────────────────────────────────────
# Protocol
# ───────────────────────────────────────────────


class ExchangeProtocol(Protocol):
    """HyperLiquid 取引所との通信インターフェース。

    INFRASTRUCTURE層がこの Protocol を実装する。
    """

    # ─── マーケットデータ取得 ───
    async def get_symbols(self) -> tuple[SymbolMeta, ...]: ...

    async def get_l2_book(self, symbol: str) -> L2Book: ...

    async def get_market_snapshot(self, symbol: str) -> MarketSnapshot: ...

    async def get_funding_rate_8h(self, symbol: str) -> Decimal: ...

    async def get_open_interest(self, symbol: str) -> Decimal: ...

    # ─── ユーザー状態取得 ───
    async def get_positions(self) -> tuple[Position, ...]: ...

    async def get_open_orders(self) -> tuple[Order, ...]: ...

    async def get_fills(self, since_ms: int) -> tuple[Fill, ...]: ...

    async def get_funding_payments(self, since_ms: int) -> tuple[FundingPayment, ...]: ...

    async def get_account_balance_usd(self) -> Decimal: ...

    # ─── 注文操作 ───
    async def place_order(self, request: OrderRequest) -> OrderResult: ...

    async def place_trigger_order(self, request: TriggerOrderRequest) -> OrderResult: ...

    async def place_orders_grouped(
        self,
        entry: OrderRequest,
        tp: TriggerOrderRequest | None,
        sl: TriggerOrderRequest | None,
    ) -> tuple[OrderResult, ...]: ...

    async def cancel_order(self, order_id: int, symbol: str) -> bool: ...

    async def get_order_by_client_id(self, client_order_id: str) -> Order | None: ...

    async def get_order_status(
        self, order_id: int
    ) -> Literal["pending", "filled", "cancelled", "rejected"]: ...

    # ─── 銘柄メタ ───
    async def get_tick_size(self, symbol: str) -> Decimal: ...

    async def get_sz_decimals(self, symbol: str) -> int: ...
