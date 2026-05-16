"""4層AND エントリー判定（章4・章11.4）。

設計上の重要原則:
- 純関数のみ（I/O・副作用なし）
- 引数は MarketSnapshot 1つ・戻り値は EntryDecision 1つ
- 閾値はまずハードコード。章23の設定管理回で config 引数化する予定
  （章11.13 マッピング表）

評価順序は dict 挿入順を使い、章4.4 の推奨順:
  ① MOMENTUM → ② FLOW → ④ REGIME → ③ SENTIMENT
としている。SENTIMENT を最後にするのは「有料APIを最後に呼ぶ」という
APPLICATION層側のコスト最適化と意味論を揃えるため。
"""

from __future__ import annotations

from src.core.models import EntryDecision, MarketSnapshot
from src.core.price_context import is_not_overheated_long, is_not_overheated_short

# ───────────────────────────────────────────────
# LONG 閾値（章4・章7）
# 章5の3重チェック過熱フィルターは price_context に分離。
# ───────────────────────────────────────────────
_LONG_VWAP_MAX_DISTANCE_PCT = 0.5
_LONG_MOMENTUM_5BAR_MIN_PCT = 0.3  # 5本前比+0.3%以上

_LONG_FLOW_BUY_SELL_RATIO_MIN = 1.5
_LONG_VOLUME_SURGE_MIN = 1.5

_LONG_SENTIMENT_SCORE_MIN = 0.6
_LONG_SENTIMENT_CONFIDENCE_MIN = 0.7

_LONG_FUNDING_RATE_MAX = 0.01  # 章4: < 0.01%/8h相当
_LONG_BTC_ATR_PCT_MAX = 5.0  # 章4: BTC ATR%が極端に高くない
_LONG_OI_CHANGE_MAX_PCT = 10.0  # 章4: OI 1h変化 ±10%以下

# ───────────────────────────────────────────────
# SHORT 閾値（章4・章7・LONGと対称）
# 章5の過熱フィルターは price_context.is_not_overheated_short に分離。
# ───────────────────────────────────────────────
_SHORT_VWAP_MIN_DISTANCE_PCT = -0.5
_SHORT_MOMENTUM_5BAR_MAX_PCT = -0.3

# 章4: 売り約定優勢。buy/sell 比が 1/1.5 以下なら sell/buy ≥ 1.5。
_SHORT_FLOW_BUY_SELL_RATIO_MAX = 1.0 / 1.5
_SHORT_VOLUME_SURGE_MIN = 1.5

_SHORT_SENTIMENT_SCORE_MAX = -0.3
_SHORT_SENTIMENT_CONFIDENCE_MIN = 0.7

# 章4: BTC下降 OR Funding > 0.03%/8h相当（買い過熱）
_SHORT_FUNDING_RATE_OVERHEATED = 0.03


# ───────────────────────────────────────────────
# 公開API
# ───────────────────────────────────────────────


def judge_long_entry(
    snap: MarketSnapshot,
    *,
    vwap_max_distance_pct: float = _LONG_VWAP_MAX_DISTANCE_PCT,
) -> EntryDecision:
    """LONG エントリー判定（純関数）。

    PR C1: ``vwap_max_distance_pct`` を kwarg で注入可能にした。
    省略時は従来値 ``_LONG_VWAP_MAX_DISTANCE_PCT`` (=0.5)。
    profile_phase2.yaml では 1.0 に緩和（4 層通過頻度を上げるため）。
    """
    layers = {
        "momentum": _check_momentum_long(snap, vwap_max_distance_pct),
        "flow": _check_flow_long(snap),
        "regime": _check_regime_long(snap),
        "sentiment": _check_sentiment_long(snap),
    }
    return _build_decision(layers, direction="LONG")


def judge_short_entry(
    snap: MarketSnapshot,
    *,
    vwap_min_distance_pct: float = _SHORT_VWAP_MIN_DISTANCE_PCT,
) -> EntryDecision:
    """SHORT エントリー判定（純関数）。

    PR C1: ``vwap_min_distance_pct`` を kwarg で注入可能にした。
    省略時は従来値 ``_SHORT_VWAP_MIN_DISTANCE_PCT`` (=-0.5)。
    profile_phase2.yaml では -1.0 に緩和。
    """
    layers = {
        "momentum": _check_momentum_short(snap, vwap_min_distance_pct),
        "flow": _check_flow_short(snap),
        "regime": _check_regime_short(snap),
        "sentiment": _check_sentiment_short(snap),
    }
    return _build_decision(layers, direction="SHORT")


# ───────────────────────────────────────────────
# 内部: 各層
# ───────────────────────────────────────────────


def _check_momentum_long(
    snap: MarketSnapshot, vwap_max_distance_pct: float
) -> bool:
    """章4 ① MOMENTUM + POSITION（LONG）。

    過熱フィルター（章5）は price_context.is_not_overheated_long に委譲。
    PR C1: ``vwap_max_distance_pct`` を呼び出し元から受け取る。
    """
    return (
        0 < snap.vwap_distance_pct < vwap_max_distance_pct
        and is_not_overheated_long(snap)
        and snap.momentum_5bar_pct > _LONG_MOMENTUM_5BAR_MIN_PCT
    )


def _check_flow_long(snap: MarketSnapshot) -> bool:
    """章4 ② FLOW（LONG）。"""
    return (
        snap.flow_buy_sell_ratio > _LONG_FLOW_BUY_SELL_RATIO_MIN
        and snap.flow_large_order_count > 0
        and snap.volume_surge_ratio > _LONG_VOLUME_SURGE_MIN
    )


def _check_regime_long(snap: MarketSnapshot) -> bool:
    """章4 ④ REGIME + LIQUIDATION（LONG・章13.5の代替指標）。"""
    return (
        snap.btc_ema_trend == "UPTREND"
        and snap.btc_atr_pct < _LONG_BTC_ATR_PCT_MAX
        and snap.funding_rate < _LONG_FUNDING_RATE_MAX
        and abs(snap.oi_change_1h_pct) < _LONG_OI_CHANGE_MAX_PCT
    )


def _check_sentiment_long(snap: MarketSnapshot) -> bool:
    """章4 ③・章7 SENTIMENT（LONG）。

    flags は dict で受け取り、未設定キーは False 扱い。
    """
    flags = snap.sentiment_flags
    return (
        snap.sentiment_score > _LONG_SENTIMENT_SCORE_MIN
        and snap.sentiment_confidence > _LONG_SENTIMENT_CONFIDENCE_MIN
        and not flags.get("has_hack", False)
        and not flags.get("has_regulation", False)
    )


def _check_momentum_short(
    snap: MarketSnapshot, vwap_min_distance_pct: float
) -> bool:
    """章4 ① MOMENTUM + POSITION（SHORT・LONGと対称）。

    過熱フィルター（章5）は price_context.is_not_overheated_short に委譲。
    PR C1: ``vwap_min_distance_pct`` を呼び出し元から受け取る。
    """
    return (
        vwap_min_distance_pct < snap.vwap_distance_pct < 0
        and is_not_overheated_short(snap)
        and snap.momentum_5bar_pct < _SHORT_MOMENTUM_5BAR_MAX_PCT
    )


def _check_flow_short(snap: MarketSnapshot) -> bool:
    """章4 ② FLOW（SHORT）。

    snapshot は買い/売り比のみ持つので、売り優勢は ratio < 1/1.5 で表現する。
    """
    return (
        snap.flow_buy_sell_ratio < _SHORT_FLOW_BUY_SELL_RATIO_MAX
        and snap.flow_large_order_count > 0
        and snap.volume_surge_ratio > _SHORT_VOLUME_SURGE_MIN
    )


def _check_regime_short(snap: MarketSnapshot) -> bool:
    """章4 ④ REGIME（SHORT）。BTC下降 OR Funding買い過熱、かつ OI 過熱なし。"""
    btc_or_funding_bearish = (
        snap.btc_ema_trend != "UPTREND" or snap.funding_rate > _SHORT_FUNDING_RATE_OVERHEATED
    )
    return btc_or_funding_bearish and abs(snap.oi_change_1h_pct) < _LONG_OI_CHANGE_MAX_PCT


def _check_sentiment_short(snap: MarketSnapshot) -> bool:
    """章4 ③・章7 SENTIMENT（SHORT）。"""
    return (
        snap.sentiment_score < _SHORT_SENTIMENT_SCORE_MAX
        and snap.sentiment_confidence > _SHORT_SENTIMENT_CONFIDENCE_MIN
    )


# ───────────────────────────────────────────────
# 内部: 結果組み立て
# ───────────────────────────────────────────────


def _build_decision(layers: dict[str, bool], direction: str) -> EntryDecision:
    if all(layers.values()):
        return EntryDecision(
            should_enter=True,
            direction=direction,
            rejection_reason=None,
            layer_results=layers,
        )
    # dict 挿入順で最初に False になった層を rejection の主因とする。
    failed = next(name for name, ok in layers.items() if not ok)
    return EntryDecision(
        should_enter=False,
        direction=None,
        rejection_reason=f"layer_{failed}_failed",
        layer_results=layers,
    )
