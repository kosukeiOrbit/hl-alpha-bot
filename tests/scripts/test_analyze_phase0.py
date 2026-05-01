"""scripts/analyze_phase0.py のユニットテスト。

read-only スクリプトなので :memory: DB に手動でテストデータを INSERT して
集計関数を直接呼ぶ形で検証する。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.analyze_phase0 import (
    LAYERS,
    AnalysisReport,
    LayerStats,
    PassedSignal,
    TradeSummary,
    build_report,
    compute_by_hour,
    compute_time_filter,
    fetch_layer_stats,
    fetch_passed_signals,
    fetch_trade_summary,
    fetch_uptime,
    main,
    parse_window,
    render_human,
    render_json,
)

# ─── テスト用 schema ──────────────────────

_SCHEMA_SQL = """
CREATE TABLE signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    direction       TEXT NOT NULL,
    layer           TEXT NOT NULL,
    passed          INTEGER NOT NULL,
    rejection_reason TEXT,
    snapshot_excerpt TEXT
);
CREATE TABLE trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    direction       TEXT NOT NULL,
    leverage_used   INTEGER NOT NULL DEFAULT 0,
    tp_order_id     TEXT,
    sl_order_id     TEXT,
    size_coins      REAL NOT NULL,
    entry_price     REAL NOT NULL,
    actual_entry_price REAL,
    sl_price        REAL NOT NULL,
    tp_price        REAL NOT NULL,
    exit_price      REAL,
    is_filled       INTEGER NOT NULL DEFAULT 0,
    is_dry_run      INTEGER NOT NULL DEFAULT 1,
    is_manual_review INTEGER NOT NULL DEFAULT 0,
    is_external     INTEGER NOT NULL DEFAULT 0,
    resumed_at      TEXT,
    entry_time      TEXT NOT NULL,
    exit_time       TEXT,
    fill_time       TEXT,
    closed_at       TEXT,
    pnl_usd         REAL,
    fee_usd_total   REAL,
    funding_paid_usd REAL,
    mfe_pct         REAL,
    mae_pct         REAL,
    exit_reason     TEXT,
    vwap_metrics    TEXT
);
"""


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA_SQL)
    return conn


def _insert_signal(
    conn: sqlite3.Connection,
    *,
    ts: str,
    symbol: str,
    direction: str,
    layer: str,
    passed: int,
) -> None:
    conn.execute(
        """
        INSERT INTO signals (timestamp, symbol, direction, layer, passed)
        VALUES (?, ?, ?, ?, ?)
        """,
        (ts, symbol, direction, layer, passed),
    )


def _insert_trade(
    conn: sqlite3.Connection,
    *,
    symbol: str = "BTC",
    direction: str = "LONG",
    is_dry_run: int = 1,
    entry_time: str = "2026-04-29T00:00:00+00:00",
    exit_time: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO trades (
            symbol, direction, size_coins, entry_price, sl_price, tp_price,
            is_dry_run, entry_time, exit_time
        ) VALUES (?, ?, 1.0, 100.0, 90.0, 110.0, ?, ?, ?)
        """,
        (symbol, direction, is_dry_run, entry_time, exit_time),
    )


@pytest.fixture
def populated_conn() -> sqlite3.Connection:
    """10 サイクル × 2 銘柄 × 1 方向 × 4 層 = 80 行 + trades 数件。

    MOMENTUM はサイクル 5 のみ pass、他層は常に pass。SOL は混入用に追加 1 行。
    """
    conn = _make_conn()
    base = datetime(2026, 4, 29, 0, 0, 0, tzinfo=UTC)
    for cycle in range(10):
        ts = (base + timedelta(minutes=cycle)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        for sym in ("BTC", "ETH"):
            for layer in LAYERS:
                passed = (
                    (1 if cycle == 5 else 0) if layer == "MOMENTUM" else 1
                )
                _insert_signal(
                    conn,
                    ts=ts,
                    symbol=sym,
                    direction="LONG",
                    layer=layer,
                    passed=passed,
                )
    # 異なる時刻のサンプル（時間帯ヒストグラム用）
    _insert_signal(
        conn,
        ts="2026-04-29T13:30:00+00:00",
        symbol="SOL",
        direction="SHORT",
        layer="MOMENTUM",
        passed=1,
    )
    # trades 数件
    _insert_trade(conn, symbol="BTC", is_dry_run=1)
    _insert_trade(conn, symbol="BTC", is_dry_run=1, exit_time="2026-04-29T01:00:00+00:00")
    _insert_trade(conn, symbol="ETH", is_dry_run=0)
    conn.commit()
    return conn


# ─── parse_window ────────────────────────


class TestParseWindow:
    def test_24h(self) -> None:
        assert parse_window("24h") == timedelta(hours=24)

    def test_7d(self) -> None:
        assert parse_window("7d") == timedelta(days=7)

    def test_30m(self) -> None:
        assert parse_window("30m") == timedelta(minutes=30)

    def test_all_returns_none(self) -> None:
        assert parse_window("all") is None

    def test_zero_minutes(self) -> None:
        assert parse_window("0m") == timedelta(0)

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid window"):
            parse_window("")

    def test_too_short_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid window"):
            parse_window("h")

    def test_non_numeric_value_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid window value"):
            parse_window("abh")

    def test_negative_value_raises(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            parse_window("-1h")

    def test_unknown_unit_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown window unit"):
            parse_window("10y")


# ─── compute_time_filter ─────────────────


class TestComputeTimeFilter:
    def test_none_returns_none(self) -> None:
        assert compute_time_filter(None) is None

    def test_with_explicit_now(self) -> None:
        now = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)
        result = compute_time_filter(timedelta(hours=2), now=now)
        assert result == "2026-04-29T10:00:00+00:00"

    def test_default_now(self) -> None:
        # now 省略で例外なく ISO8601 文字列が返る
        result = compute_time_filter(timedelta(hours=1))
        assert result is not None
        assert "T" in result
        assert "+00:00" in result


# ─── fetch_layer_stats ───────────────────


class TestFetchLayerStats:
    def test_returns_all_layers_even_when_empty(self) -> None:
        conn = _make_conn()
        stats = fetch_layer_stats(conn, since=None, symbol=None)
        assert tuple(s.layer for s in stats) == LAYERS
        assert all(s.total == 0 for s in stats)
        assert all(s.passed == 0 for s in stats)
        assert all(s.pass_rate == 0.0 for s in stats)

    def test_aggregates_pass_and_fail(
        self, populated_conn: sqlite3.Connection
    ) -> None:
        stats = fetch_layer_stats(populated_conn, since=None, symbol=None)
        by_layer = {s.layer: s for s in stats}
        # MOMENTUM: BTC+ETH × 10 cycles + SOL 1 = 21 total
        # passed: cycle 5 だけ通過 (BTC+ETH=2) + SOL の SHORT=1 = 3
        assert by_layer["MOMENTUM"].total == 21
        assert by_layer["MOMENTUM"].passed == 3
        # FLOW/SENTIMENT/REGIME: BTC+ETH × 10 cycles = 20、すべて通過
        for layer in ("FLOW", "SENTIMENT", "REGIME"):
            assert by_layer[layer].total == 20
            assert by_layer[layer].passed == 20

    def test_symbol_filter(
        self, populated_conn: sqlite3.Connection
    ) -> None:
        stats = fetch_layer_stats(populated_conn, since=None, symbol="BTC")
        by_layer = {s.layer: s for s in stats}
        # BTC のみ: MOMENTUM 10 行（うち 1 pass）、他層 10 / 10
        assert by_layer["MOMENTUM"].total == 10
        assert by_layer["MOMENTUM"].passed == 1
        assert by_layer["FLOW"].total == 10
        assert by_layer["FLOW"].passed == 10

    def test_since_filter(
        self, populated_conn: sqlite3.Connection
    ) -> None:
        # cycle 5 (00:05) 以降に絞る
        stats = fetch_layer_stats(
            populated_conn,
            since="2026-04-29T00:05:00+00:00",
            symbol=None,
        )
        by_layer = {s.layer: s for s in stats}
        # cycle 5..9 の BTC+ETH = 10 + SOL の MOMENTUM 1 = 11
        assert by_layer["MOMENTUM"].total == 11
        # MOMENTUM passed: cycle5 BTC+ETH (2) + SOL (1) = 3
        assert by_layer["MOMENTUM"].passed == 3

    def test_layer_stats_pass_rate(self) -> None:
        s = LayerStats(layer="MOMENTUM", total=10, passed=2)
        assert s.pass_rate == 0.2
        s_zero = LayerStats(layer="FLOW", total=0, passed=0)
        assert s_zero.pass_rate == 0.0


# ─── fetch_passed_signals ────────────────


class TestFetchPassedSignals:
    def test_only_full_pass_groups(
        self, populated_conn: sqlite3.Connection
    ) -> None:
        # cycle 5 だけ MOMENTUM pass、他層は常時 pass → BTC + ETH の 2 件のみ
        # SOL は MOMENTUM 1 行しかないので 4 層揃わず除外される
        results = fetch_passed_signals(
            populated_conn, since=None, symbol=None
        )
        symbols = {r.symbol for r in results}
        assert symbols == {"BTC", "ETH"}
        assert len(results) == 2
        assert all(r.direction == "LONG" for r in results)

    def test_symbol_filter(
        self, populated_conn: sqlite3.Connection
    ) -> None:
        results = fetch_passed_signals(
            populated_conn, since=None, symbol="BTC"
        )
        assert len(results) == 1
        assert results[0].symbol == "BTC"

    def test_limit(self, populated_conn: sqlite3.Connection) -> None:
        results = fetch_passed_signals(
            populated_conn, since=None, symbol=None, limit=1
        )
        assert len(results) == 1


# ─── fetch_uptime ────────────────────────


class TestFetchUptime:
    def test_empty_db_returns_none(self) -> None:
        conn = _make_conn()
        assert fetch_uptime(conn, since=None, symbol=None) == (None, None)

    def test_returns_min_max(
        self, populated_conn: sqlite3.Connection
    ) -> None:
        earliest, latest = fetch_uptime(
            populated_conn, since=None, symbol=None
        )
        assert earliest == "2026-04-29T00:00:00+00:00"
        # SOL の 13:30 が最新
        assert latest == "2026-04-29T13:30:00+00:00"

    def test_since_excludes_old(
        self, populated_conn: sqlite3.Connection
    ) -> None:
        earliest, _ = fetch_uptime(
            populated_conn,
            since="2026-04-29T00:05:00+00:00",
            symbol=None,
        )
        assert earliest == "2026-04-29T00:05:00+00:00"


# ─── fetch_trade_summary ─────────────────


class TestFetchTradeSummary:
    def test_empty(self) -> None:
        conn = _make_conn()
        ts = fetch_trade_summary(conn, since=None, symbol=None)
        assert ts == TradeSummary(total=0, dry_run=0, real=0, closed=0)

    def test_counts(self, populated_conn: sqlite3.Connection) -> None:
        ts = fetch_trade_summary(populated_conn, since=None, symbol=None)
        assert ts.total == 3
        assert ts.dry_run == 2
        assert ts.real == 1
        assert ts.closed == 1

    def test_symbol_filter(
        self, populated_conn: sqlite3.Connection
    ) -> None:
        ts = fetch_trade_summary(
            populated_conn, since=None, symbol="ETH"
        )
        assert ts.total == 1
        assert ts.real == 1
        assert ts.dry_run == 0

    def test_since_filter(
        self, populated_conn: sqlite3.Connection
    ) -> None:
        ts = fetch_trade_summary(
            populated_conn,
            since="2027-01-01T00:00:00+00:00",
            symbol=None,
        )
        assert ts.total == 0


# ─── compute_by_hour ─────────────────────


class TestComputeByHour:
    def test_empty(self) -> None:
        conn = _make_conn()
        assert compute_by_hour(conn, since=None, symbol=None) == {}

    def test_buckets(self, populated_conn: sqlite3.Connection) -> None:
        result = compute_by_hour(populated_conn, since=None, symbol=None)
        # 0 時台: 80 行 (10 cycle × 2 sym × 4 layer)
        assert result[0] == 80
        # 13 時台: SOL 1 行
        assert result[13] == 1

    def test_handles_invalid_timestamp(self) -> None:
        conn = _make_conn()
        _insert_signal(
            conn,
            ts="not-a-timestamp",
            symbol="BTC",
            direction="LONG",
            layer="MOMENTUM",
            passed=0,
        )
        _insert_signal(
            conn,
            ts="",
            symbol="BTC",
            direction="LONG",
            layer="MOMENTUM",
            passed=0,
        )
        _insert_signal(
            conn,
            ts="2026-04-29T07:00:00+00:00",
            symbol="BTC",
            direction="LONG",
            layer="MOMENTUM",
            passed=0,
        )
        conn.commit()
        result = compute_by_hour(conn, since=None, symbol=None)
        # 空文字 / 不正時刻はスキップされ、有効な 1 件だけ集計
        assert result == {7: 1}

    def test_z_suffix_normalized(self) -> None:
        conn = _make_conn()
        _insert_signal(
            conn,
            ts="2026-04-29T09:00:00Z",
            symbol="BTC",
            direction="LONG",
            layer="MOMENTUM",
            passed=0,
        )
        conn.commit()
        result = compute_by_hour(conn, since=None, symbol=None)
        assert result == {9: 1}


# ─── build_report (file based, read-only) ─


class TestBuildReport:
    def test_read_only_open(self, tmp_path: Path) -> None:
        db_path = tmp_path / "bot.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(_SCHEMA_SQL)
        _insert_signal(
            conn,
            ts="2026-04-29T05:00:00+00:00",
            symbol="BTC",
            direction="LONG",
            layer="MOMENTUM",
            passed=1,
        )
        conn.commit()
        conn.close()

        now = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)
        report = build_report(
            db_path=db_path, window="24h", symbol=None, now=now
        )
        assert isinstance(report, AnalysisReport)
        assert report.window == "24h"
        assert report.estimated_cycles == 1
        assert report.earliest_signal == "2026-04-29T05:00:00+00:00"

    def test_window_all_includes_old(self, tmp_path: Path) -> None:
        db_path = tmp_path / "bot.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(_SCHEMA_SQL)
        _insert_signal(
            conn,
            ts="2020-01-01T00:00:00+00:00",
            symbol="BTC",
            direction="LONG",
            layer="MOMENTUM",
            passed=0,
        )
        conn.commit()
        conn.close()

        report = build_report(db_path=db_path, window="all")
        assert report.estimated_cycles == 1


# ─── render ──────────────────────────────


def _empty_report() -> AnalysisReport:
    return AnalysisReport(
        window="24h",
        symbol=None,
        db_path="x.db",
        earliest_signal=None,
        latest_signal=None,
        estimated_cycles=0,
        layer_stats=tuple(
            LayerStats(layer=layer, total=0, passed=0) for layer in LAYERS
        ),
        passed_signals=(),
        trade_summary=TradeSummary(total=0, dry_run=0, real=0, closed=0),
        by_hour={},
    )


def _populated_report() -> AnalysisReport:
    return AnalysisReport(
        window="7d",
        symbol="BTC",
        db_path="x.db",
        earliest_signal="2026-04-29T00:00:00+00:00",
        latest_signal="2026-04-29T13:30:00+00:00",
        estimated_cycles=21,
        layer_stats=(
            LayerStats(layer="MOMENTUM", total=21, passed=3),
            LayerStats(layer="FLOW", total=20, passed=20),
            LayerStats(layer="SENTIMENT", total=20, passed=20),
            LayerStats(layer="REGIME", total=20, passed=20),
        ),
        passed_signals=(
            PassedSignal(
                timestamp="2026-04-29T00:05:00+00:00",
                symbol="BTC",
                direction="LONG",
            ),
        ),
        trade_summary=TradeSummary(total=3, dry_run=2, real=1, closed=1),
        by_hour={0: 80, 13: 1},
    )


class TestRenderHuman:
    def test_empty_report(self) -> None:
        out = render_human(_empty_report())
        assert "Phase 0 観察データ分析" in out
        assert "(none)" in out
        assert "(該当なし)" in out

    def test_populated_report(self) -> None:
        out = render_human(_populated_report())
        assert "MOMENTUM" in out
        assert "BTC" in out
        assert "LONG" in out
        # pass_rate のフォーマット (3/21 ≒ 14.3%)
        assert "14.3%" in out
        # bar 文字が出る（max_count=80 → 13:00 行は 1*40/80=0 文字）
        assert "00:00" in out
        assert "13:00" in out


class TestRenderJson:
    def test_round_trip(self) -> None:
        out = render_json(_populated_report())
        data = json.loads(out)
        assert data["window"] == "7d"
        assert data["symbol"] == "BTC"
        assert data["estimated_cycles"] == 21
        assert len(data["layer_stats"]) == 4
        assert data["layer_stats"][0]["layer"] == "MOMENTUM"
        assert data["layer_stats"][0]["pass_rate"] == pytest.approx(
            3 / 21, abs=1e-6
        )
        assert data["passed_signals"][0]["symbol"] == "BTC"
        assert data["trade_summary"]["total"] == 3
        # by_hour のキーは文字列化されている
        assert data["by_hour"] == {"0": 80, "13": 1}

    def test_empty_round_trip(self) -> None:
        out = render_json(_empty_report())
        data = json.loads(out)
        assert data["earliest_signal"] is None
        assert data["passed_signals"] == []
        assert data["by_hour"] == {}


# ─── main (CLI) ──────────────────────────


@pytest.fixture
def real_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "bot.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA_SQL)
    _insert_signal(
        conn,
        ts="2026-04-29T08:00:00+00:00",
        symbol="BTC",
        direction="LONG",
        layer="MOMENTUM",
        passed=1,
    )
    conn.commit()
    conn.close()
    return db_path


class TestMain:
    def test_human_output(
        self,
        real_db: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = main(["--db", str(real_db), "--window", "all"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "Phase 0 観察データ分析" in out

    def test_json_output(
        self,
        real_db: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = main(["--db", str(real_db), "--window", "all", "--json"])
        out = capsys.readouterr().out
        assert rc == 0
        data = json.loads(out)
        assert data["window"] == "all"

    def test_symbol_filter(
        self,
        real_db: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = main(
            [
                "--db",
                str(real_db),
                "--window",
                "all",
                "--symbol",
                "BTC",
                "--json",
            ]
        )
        out = capsys.readouterr().out
        assert rc == 0
        data = json.loads(out)
        assert data["symbol"] == "BTC"

    def test_missing_db_returns_1(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = main(["--db", str(tmp_path / "nope.db")])
        err = capsys.readouterr().err
        assert rc == 1
        assert "DB not found" in err

    def test_invalid_window_returns_2(
        self,
        real_db: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = main(["--db", str(real_db), "--window", "10y"])
        err = capsys.readouterr().err
        assert rc == 2
        assert "ERROR" in err
