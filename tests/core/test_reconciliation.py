"""core/reconciliation のテスト（章9.3・章11.7-11.8）。"""

from __future__ import annotations

from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from src.core.reconciliation import (
    ActionType,
    DBTrade,
    HLFill,
    HLPosition,
    reconcile_positions,
)


def make_hl_pos(
    symbol: str = "BTC",
    size: str = "0.01",
    entry_price: str = "65000",
) -> HLPosition:
    return HLPosition(
        symbol=symbol,
        size=Decimal(size),
        entry_price=Decimal(entry_price),
    )


def make_db_trade(
    trade_id: int = 1,
    symbol: str = "BTC",
    direction: str = "LONG",
    size: str = "0.01",
    entry_price: str = "65000",
) -> DBTrade:
    return DBTrade(
        trade_id=trade_id,
        symbol=symbol,
        direction=direction,
        size=Decimal(size),
        entry_price=Decimal(entry_price),
    )


def make_fill(
    symbol: str = "BTC",
    side: str = "sell",
    size: str = "0.01",
    price: str = "66000",
    timestamp: int = 1700000000000,
) -> HLFill:
    return HLFill(
        symbol=symbol,
        side=side,
        size=Decimal(size),
        price=Decimal(price),
        timestamp=timestamp,
    )


# ────────────────────────────────────────────────
# A. 単一ケース
# ────────────────────────────────────────────────


class TestSingleCases:
    def test_empty_inputs_returns_empty_result(self) -> None:
        result = reconcile_positions((), (), ())
        assert result.actions == ()
        assert result.positions_resumed == 0
        assert result.external_detected == 0
        assert result.corrections_made == 0
        assert result.closed_from_fills == 0
        assert result.manual_review_needed == 0

    def test_position_match_resumes_monitoring(self) -> None:
        result = reconcile_positions((make_hl_pos(),), (make_db_trade(),), ())
        assert len(result.actions) == 1
        assert result.actions[0].type == ActionType.RESUME_MONITORING
        assert result.positions_resumed == 1

    def test_external_position_detected(self) -> None:
        # HLにあるがDBにない → REGISTER_EXTERNAL
        result = reconcile_positions((make_hl_pos(),), (), ())
        assert result.actions[0].type == ActionType.REGISTER_EXTERNAL
        assert result.actions[0].hl_position is not None
        assert result.external_detected == 1

    def test_position_size_mismatch_corrects_db(self) -> None:
        result = reconcile_positions(
            (make_hl_pos(size="0.02"),),
            (make_db_trade(size="0.01"),),
            (),
        )
        assert result.actions[0].type == ActionType.CORRECT_DB
        assert result.corrections_made == 1

    def test_direction_mismatch_corrects_db(self) -> None:
        # LONG vs SHORT 不一致
        result = reconcile_positions(
            (make_hl_pos(size="-0.01"),),  # SHORT
            (make_db_trade(direction="LONG"),),
            (),
        )
        assert result.actions[0].type == ActionType.CORRECT_DB

    def test_db_only_with_matching_fill_closes(self) -> None:
        # DBにあるがHLにない・対応fillあり → CLOSE_FROM_FILL
        result = reconcile_positions(
            (),
            (make_db_trade(),),
            (make_fill(),),  # LONG → sell
        )
        assert result.actions[0].type == ActionType.CLOSE_FROM_FILL
        assert result.actions[0].fill is not None
        assert result.closed_from_fills == 1

    def test_db_only_without_fill_marks_manual_review(self) -> None:
        result = reconcile_positions((), (make_db_trade(),), ())
        assert result.actions[0].type == ActionType.MANUAL_REVIEW
        assert result.manual_review_needed == 1

    def test_short_position_match(self) -> None:
        result = reconcile_positions(
            (make_hl_pos(size="-0.01"),),
            (make_db_trade(direction="SHORT", size="0.01"),),
            (),
        )
        assert result.actions[0].type == ActionType.RESUME_MONITORING

    def test_size_within_tolerance_matches(self) -> None:
        result = reconcile_positions(
            (make_hl_pos(size="0.01000001"),),
            (make_db_trade(size="0.01"),),
            (),
        )
        assert result.actions[0].type == ActionType.RESUME_MONITORING

    def test_size_outside_tolerance_corrects(self) -> None:
        result = reconcile_positions(
            (make_hl_pos(size="0.011"),),
            (make_db_trade(size="0.01"),),
            (),
        )
        assert result.actions[0].type == ActionType.CORRECT_DB


# ────────────────────────────────────────────────
# B. 複合ケース
# ────────────────────────────────────────────────


class TestCombinedCases:
    def test_mixed_actions(self) -> None:
        # 一致 / 外部 / DBのみで HL から消失（fillなし）の3パターン同時
        hl_positions = (
            make_hl_pos(symbol="BTC"),
            make_hl_pos(symbol="ETH", size="0.5"),
        )
        db_trades = (
            make_db_trade(trade_id=1, symbol="BTC"),
            make_db_trade(trade_id=2, symbol="SOL", size="10"),
        )
        result = reconcile_positions(hl_positions, db_trades, ())

        assert result.positions_resumed == 1
        assert result.external_detected == 1
        assert result.manual_review_needed == 1
        assert len(result.actions) == 3

    def test_fill_matches_short_close(self) -> None:
        # SHORT決済 fill (buy) が一致
        result = reconcile_positions(
            (),
            (make_db_trade(direction="SHORT", size="0.01"),),
            (make_fill(side="buy", size="0.01"),),
        )
        assert result.actions[0].type == ActionType.CLOSE_FROM_FILL

    def test_fill_wrong_side_treated_as_no_fill(self) -> None:
        # LONG決済を期待しているのに buy fill しかない → MANUAL
        result = reconcile_positions(
            (),
            (make_db_trade(direction="LONG", size="0.01"),),
            (make_fill(side="buy", size="0.01"),),
        )
        assert result.actions[0].type == ActionType.MANUAL_REVIEW

    def test_fill_wrong_symbol_treated_as_no_fill(self) -> None:
        result = reconcile_positions(
            (),
            (make_db_trade(symbol="BTC"),),
            (make_fill(symbol="ETH"),),
        )
        assert result.actions[0].type == ActionType.MANUAL_REVIEW

    def test_fill_size_outside_tolerance_treated_as_no_fill(self) -> None:
        result = reconcile_positions(
            (),
            (make_db_trade(size="0.01"),),
            (make_fill(size="0.02"),),  # サイズ違いすぎ
        )
        assert result.actions[0].type == ActionType.MANUAL_REVIEW


# ────────────────────────────────────────────────
# C. property-based
# ────────────────────────────────────────────────


class TestPropertyBased:
    @given(n_hl=st.integers(min_value=0, max_value=20))
    def test_count_invariant_hl_only(self, n_hl: int) -> None:
        # 全 HL ポジションが REGISTER_EXTERNAL になる（DB空・fill空）。
        hls = tuple(make_hl_pos(symbol=f"COIN{i}") for i in range(n_hl))
        result = reconcile_positions(hls, (), ())
        assert len(result.actions) == n_hl
        assert result.external_detected == n_hl

    @given(
        n_only_hl=st.integers(min_value=0, max_value=10),
        n_only_db=st.integers(min_value=0, max_value=10),
    )
    def test_action_count_equals_total(self, n_only_hl: int, n_only_db: int) -> None:
        # アクション数 = HL ポジション数 + (DBにのみある trade) 数。
        hls = tuple(make_hl_pos(symbol=f"HL{i}") for i in range(n_only_hl))
        dbs = tuple(
            make_db_trade(trade_id=i, symbol=f"DB{i}") for i in range(n_only_db)
        )
        result = reconcile_positions(hls, dbs, ())
        assert len(result.actions) == n_only_hl + n_only_db
