"""テクニカル指標の純関数（章4・章13）。

すべて純関数:
- 引数のリスト（高値・安値・終値）から計算
- 副作用なし
- ローソク足取得は呼び出し側の責務（INFRASTRUCTURE / APPLICATION 層）

使用箇所:
- entry_flow._calc_btc_ema_trend (BTC レジーム判定)
- entry_flow._calc_btc_atr_pct (BTC ボラティリティ判定)
- 将来: dynamic_stop での SL/TP 算出
"""

from __future__ import annotations

from decimal import Decimal


def calculate_ema(prices: list[Decimal], period: int) -> Decimal:
    """指数移動平均（EMA）を計算。

    seed = 最初の period 件の SMA、以降は alpha=2/(period+1) で平滑化:
        EMA(t) = price(t) * alpha + EMA(t-1) * (1 - alpha)

    Args:
        prices: 価格のリスト（古い順）。最低 ``period`` 件必要
        period: 期間（例: 20, 50）

    Returns:
        最新の EMA 値（最後の要素時点）

    Raises:
        ValueError: prices が period 未満、または period < 1
    """
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")
    if len(prices) < period:
        raise ValueError(
            f"need at least {period} prices, got {len(prices)}"
        )

    seed = sum(prices[:period], Decimal(0)) / Decimal(period)
    ema = seed
    alpha = Decimal(2) / (Decimal(period) + Decimal(1))

    for price in prices[period:]:
        ema = price * alpha + ema * (Decimal(1) - alpha)

    return ema


def calculate_atr(
    highs: list[Decimal],
    lows: list[Decimal],
    closes: list[Decimal],
    period: int = 14,
) -> Decimal:
    """Average True Range（ATR）を計算。

    True Range (TR) = max(
        high - low,
        |high - prev_close|,
        |low - prev_close|,
    )
    最初の ATR は TR[:period] の単純平均、以降は Wilder's smoothing:
        ATR(t) = (ATR(t-1) * (period - 1) + TR(t)) / period

    Args:
        highs / lows / closes: それぞれ古い順のリスト。長さ一致必須
        period: 期間（既定 14）

    Returns:
        最新の ATR 値

    Raises:
        ValueError: 長さ不一致、行数 < period+1、または period < 1

    Note:
        最初の TR を計算するのに前足の close が必要なので、
        period+1 本以上の足が必要。
    """
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError(
            f"highs/lows/closes length mismatch: "
            f"{len(highs)}/{len(lows)}/{len(closes)}"
        )
    if len(closes) < period + 1:
        raise ValueError(
            f"need at least {period + 1} bars for ATR(period={period}), "
            f"got {len(closes)}"
        )

    trs: list[Decimal] = []
    for i in range(1, len(closes)):
        h = highs[i]
        low = lows[i]
        prev_close = closes[i - 1]
        tr = max(
            h - low,
            abs(h - prev_close),
            abs(low - prev_close),
        )
        trs.append(tr)

    atr = sum(trs[:period], Decimal(0)) / Decimal(period)
    for tr in trs[period:]:
        atr = (atr * (Decimal(period) - Decimal(1)) + tr) / Decimal(period)
    return atr


def calculate_atr_pct(
    highs: list[Decimal],
    lows: list[Decimal],
    closes: list[Decimal],
    period: int = 14,
) -> Decimal:
    """ATR を最新 close で割って % で返す（章4 で使う）。

    Returns:
        ATR / latest_close * 100（latest_close=0 なら 0 を返す）
    """
    atr = calculate_atr(highs, lows, closes, period)
    latest_close = closes[-1]
    if latest_close == 0:
        return Decimal(0)
    return atr / latest_close * Decimal(100)
