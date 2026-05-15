"""状態突合の純関数（章9.3）。

HL側のポジション・約定履歴と DB側の取引記録を突合し、
どのアクションが必要かを判定する純関数を提供する。

副作用は伴わない。アクションリストを返すだけ。
APPLICATION層（reconciliation.py）が結果を受けて実際にDB操作する。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class ActionType(StrEnum):
    """突合アクションの種類。"""

    REGISTER_EXTERNAL = "REGISTER_EXTERNAL"  # HLにあるがDBにない（外部由来）
    RESUME_MONITORING = "RESUME_MONITORING"  # 一致 → 監視再開
    CORRECT_DB = "CORRECT_DB"  # 不一致 → HLを真として補正
    CLOSE_FROM_FILL = "CLOSE_FROM_FILL"  # DBにあるがHLにない（決済済）
    MANUAL_REVIEW = "MANUAL_REVIEW"  # 手動確認必要


@dataclass(frozen=True)
class HLPosition:
    """HL側のポジション情報（突合用最小データ）。"""

    symbol: str
    size: Decimal  # 正=LONG / 負=SHORT
    entry_price: Decimal


@dataclass(frozen=True)
class DBTrade:
    """DB側の取引記録（突合用最小データ）。

    PR A3 (#3 of 5 mainnet first-trades bugs): ``entry_time_ms`` を追加。
    ``_find_matching_fill`` で ``fill.timestamp >= entry_time_ms`` を強制し、
    エントリー前の古い fill が誤マッチする事故（2026-05-15 ID 1 の TP fill が
    ID 2-7 に伝播）を防ぐ。
    """

    trade_id: int
    symbol: str
    direction: str  # 'LONG' or 'SHORT'
    size: Decimal
    entry_price: Decimal
    entry_time_ms: int


@dataclass(frozen=True)
class HLFill:
    """HL側の約定履歴（突合用最小データ）。"""

    symbol: str
    side: str  # 'buy' or 'sell'
    size: Decimal
    price: Decimal
    timestamp: int  # ms


@dataclass(frozen=True)
class ReconcileAction:
    """突合の結果アクション。"""

    type: ActionType
    hl_position: HLPosition | None = None
    db_trade: DBTrade | None = None
    fill: HLFill | None = None


@dataclass(frozen=True)
class ReconcileResult:
    """全体の突合結果サマリー。"""

    actions: tuple[ReconcileAction, ...]
    positions_resumed: int
    external_detected: int
    corrections_made: int
    closed_from_fills: int
    manual_review_needed: int


def reconcile_positions(
    hl_positions: tuple[HLPosition, ...],
    db_trades: tuple[DBTrade, ...],
    hl_fills: tuple[HLFill, ...],
    size_tolerance: Decimal = Decimal("0.0001"),
) -> ReconcileResult:
    """ポジション突合の判定ロジック（純関数・章9.3）。

    入出力が決定的なのでテスト容易。副作用なし。

    Args:
        hl_positions: HL側の現在ポジション
        db_trades: DBにある未決済取引
        hl_fills: HLの直近約定履歴（決済の補完用）
        size_tolerance: サイズ一致と判定する許容差
    """
    actions: list[ReconcileAction] = []

    db_by_symbol = {t.symbol: t for t in db_trades}
    hl_symbols = {p.symbol for p in hl_positions}

    # HL側を起点にチェック
    for hl_pos in hl_positions:
        db_match = db_by_symbol.get(hl_pos.symbol)
        if db_match is None:
            actions.append(
                ReconcileAction(
                    type=ActionType.REGISTER_EXTERNAL,
                    hl_position=hl_pos,
                )
            )
        elif _positions_match(db_match, hl_pos, size_tolerance):
            actions.append(
                ReconcileAction(
                    type=ActionType.RESUME_MONITORING,
                    hl_position=hl_pos,
                    db_trade=db_match,
                )
            )
        else:
            actions.append(
                ReconcileAction(
                    type=ActionType.CORRECT_DB,
                    hl_position=hl_pos,
                    db_trade=db_match,
                )
            )

    # DB にあるが HL にないポジション → 決済済 or 手動確認
    for db_trade in db_trades:
        if db_trade.symbol in hl_symbols:
            continue

        fill = _find_matching_fill(hl_fills, db_trade)
        if fill is not None:
            actions.append(
                ReconcileAction(
                    type=ActionType.CLOSE_FROM_FILL,
                    db_trade=db_trade,
                    fill=fill,
                )
            )
        else:
            actions.append(
                ReconcileAction(
                    type=ActionType.MANUAL_REVIEW,
                    db_trade=db_trade,
                )
            )

    return ReconcileResult(
        actions=tuple(actions),
        positions_resumed=sum(1 for a in actions if a.type == ActionType.RESUME_MONITORING),
        external_detected=sum(1 for a in actions if a.type == ActionType.REGISTER_EXTERNAL),
        corrections_made=sum(1 for a in actions if a.type == ActionType.CORRECT_DB),
        closed_from_fills=sum(1 for a in actions if a.type == ActionType.CLOSE_FROM_FILL),
        manual_review_needed=sum(1 for a in actions if a.type == ActionType.MANUAL_REVIEW),
    )


def _positions_match(
    db_trade: DBTrade,
    hl_pos: HLPosition,
    tolerance: Decimal,
) -> bool:
    """DB と HL のポジションが一致するか判定（純関数）。

    呼び出し元 reconcile_positions が symbol で対応付けた後に呼ぶため、
    シンボル一致は前提（ここでは方向とサイズだけ見る）。
    """
    expected_long = db_trade.direction == "LONG"
    actual_long = hl_pos.size > 0
    if expected_long != actual_long:
        return False

    db_size = abs(db_trade.size)
    hl_size = abs(hl_pos.size)
    return abs(db_size - hl_size) <= tolerance


def _find_matching_fill(
    hl_fills: tuple[HLFill, ...],
    db_trade: DBTrade,
) -> HLFill | None:
    """DB取引に対応する決済 fill を探す（純関数）。

    決済 fill の条件:
    - シンボル一致
    - DB の direction と逆方向 side（LONG決済 → sell, SHORT決済 → buy）
    - サイズが概ね一致（tolerance 0.0001）
    - **fill.timestamp >= db_trade.entry_time_ms**（PR A3 #3）

    最後の条件が無いと、エントリー前に発生した過去の決済 fill が
    （symbol/side/size が偶然一致するだけで）誤マッチして DB を上書きする。
    2026-05-15 mainnet で ID 1 の TP fill が ID 2-7 にすべてマッチした
    実例を再現したテストを ``tests/core/test_reconciliation.py`` に追加。

    上限（max_age）は意図的に設けていない。理由:
    - is_filled=1 フィルタ（``StateReconciler._run_core_reconcile``）で
      reconciler が見る trade は実約定済みに限定される
    - PR A1 の max_position_count=1 ゲートにより同時刻に同 symbol/side/
      size の trade が複数 open することは構造的に発生しない
    - 上限を設けると低ボラ局面での長保有 trade の正規 fill を
      取りこぼし、誤って MANUAL_REVIEW に流れる
    """
    expected_side = "sell" if db_trade.direction == "LONG" else "buy"
    for fill in hl_fills:
        if fill.symbol != db_trade.symbol:
            continue
        if fill.side != expected_side:
            continue
        if fill.timestamp < db_trade.entry_time_ms:
            continue
        if abs(fill.size - abs(db_trade.size)) <= Decimal("0.0001"):
            return fill
    return None
