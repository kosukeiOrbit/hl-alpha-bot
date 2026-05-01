"""Phase 0 観察データ分析スクリプト（read-only）。

config/profile_phase0.yaml で動かしている BOT が記録した signals / trades を
集計して標準出力に表示する。SQLite を read-only モードで開くので動作中の BOT
には影響しない。

表示セクション:
1. 稼動状況: 最古 / 最新シグナル時刻、推定サイクル数
2. 4 層通過状況: layer × passed の集計
3. 各層の通過率: layer ごとの passed/total
4. 4 層全通過シグナル: 直近の (timestamp, symbol, direction)
5. エントリー試行統計: trades テーブル要約
6. 時間帯別分布: signals の hour-of-day ヒストグラム

使用例::

    python scripts/analyze_phase0.py
    python scripts/analyze_phase0.py --window 7d
    python scripts/analyze_phase0.py --window 30m --symbol BTC
    python scripts/analyze_phase0.py --json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

LAYERS: tuple[str, ...] = ("MOMENTUM", "FLOW", "SENTIMENT", "REGIME")
DEFAULT_DB_PATH = "data/hl_bot.db"
DEFAULT_WINDOW = "24h"
PASSED_SIGNALS_LIMIT = 50


@dataclass(frozen=True)
class LayerStats:
    """1 層の通過状況。"""

    layer: str
    total: int
    passed: int

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total > 0 else 0.0


@dataclass(frozen=True)
class PassedSignal:
    """4 層全部 passed=1 の (timestamp, symbol, direction)。"""

    timestamp: str
    symbol: str
    direction: str


@dataclass(frozen=True)
class TradeSummary:
    """trades テーブルの要約。"""

    total: int
    dry_run: int
    real: int
    closed: int


@dataclass(frozen=True)
class AnalysisReport:
    """分析レポート全体。"""

    window: str
    symbol: str | None
    db_path: str
    earliest_signal: str | None
    latest_signal: str | None
    estimated_cycles: int
    layer_stats: tuple[LayerStats, ...]
    passed_signals: tuple[PassedSignal, ...]
    trade_summary: TradeSummary
    by_hour: dict[int, int]


# ─── 純粋関数（テスト容易性のため I/O から分離） ─────────


def parse_window(window: str) -> timedelta | None:
    """`24h` / `7d` / `30m` / `all` → timedelta（all は None）。"""
    if window == "all":
        return None
    if not window or len(window) < 2:
        raise ValueError(f"invalid window: {window!r}")
    unit = window[-1]
    try:
        value = int(window[:-1])
    except ValueError as exc:
        raise ValueError(f"invalid window value: {window!r}") from exc
    if value < 0:
        raise ValueError(f"window must be non-negative: {window!r}")
    if unit == "h":
        return timedelta(hours=value)
    if unit == "d":
        return timedelta(days=value)
    if unit == "m":
        return timedelta(minutes=value)
    raise ValueError(f"unknown window unit: {unit!r}")


def compute_time_filter(
    delta: timedelta | None,
    now: datetime | None = None,
) -> str | None:
    """delta が None なら None、それ以外は ISO8601 UTC 文字列。"""
    if delta is None:
        return None
    base = now if now is not None else datetime.now(UTC)
    return (base - delta).strftime("%Y-%m-%dT%H:%M:%S+00:00")


# ─── SQL クエリ層 ─────────────────────────────────────


def fetch_layer_stats(
    conn: sqlite3.Connection,
    since: str | None,
    symbol: str | None,
) -> tuple[LayerStats, ...]:
    """signals を layer × passed で集計。LAYERS 全件を必ず返す（0件含む）。"""
    sql = "SELECT layer, passed, COUNT(*) FROM signals WHERE 1=1"
    params: list[Any] = []
    if since is not None:
        sql += " AND timestamp >= ?"
        params.append(since)
    if symbol is not None:
        sql += " AND symbol = ?"
        params.append(symbol)
    sql += " GROUP BY layer, passed"

    counts: dict[str, dict[int, int]] = {}
    for row in conn.execute(sql, params).fetchall():
        layer, passed_flag, cnt = row[0], int(row[1]), int(row[2])
        counts.setdefault(layer, {})[passed_flag] = cnt

    result = []
    for layer in LAYERS:
        layer_counts = counts.get(layer, {})
        total = sum(layer_counts.values())
        passed = layer_counts.get(1, 0)
        result.append(LayerStats(layer=layer, total=total, passed=passed))
    return tuple(result)


def fetch_passed_signals(
    conn: sqlite3.Connection,
    since: str | None,
    symbol: str | None,
    limit: int = PASSED_SIGNALS_LIMIT,
) -> tuple[PassedSignal, ...]:
    """4 層全部 passed=1 の (timestamp, symbol, direction) を抽出。

    entry_flow._log_signals は同一サイクルの 4 行を同じ timestamp で書く
    （src/application/entry_flow.py:258）。よって timestamp+symbol+direction
    でグループ化して passed=1 が 4 つ揃ったものが「全層通過」になる。
    """
    sql = """
        SELECT timestamp, symbol, direction
        FROM signals
        WHERE passed = 1
    """
    params: list[Any] = []
    if since is not None:
        sql += " AND timestamp >= ?"
        params.append(since)
    if symbol is not None:
        sql += " AND symbol = ?"
        params.append(symbol)
    sql += """
        GROUP BY timestamp, symbol, direction
        HAVING COUNT(DISTINCT layer) = ?
        ORDER BY timestamp DESC
        LIMIT ?
    """
    params.append(len(LAYERS))
    params.append(limit)
    return tuple(
        PassedSignal(timestamp=row[0], symbol=row[1], direction=row[2])
        for row in conn.execute(sql, params).fetchall()
    )


def fetch_uptime(
    conn: sqlite3.Connection,
    since: str | None,
    symbol: str | None,
) -> tuple[str | None, str | None]:
    """signals の最古 / 最新 timestamp を返す（行が無ければ (None, None)）。"""
    sql = "SELECT MIN(timestamp), MAX(timestamp) FROM signals WHERE 1=1"
    params: list[Any] = []
    if since is not None:
        sql += " AND timestamp >= ?"
        params.append(since)
    if symbol is not None:
        sql += " AND symbol = ?"
        params.append(symbol)
    row = conn.execute(sql, params).fetchone()
    return (row[0], row[1])


def fetch_trade_summary(
    conn: sqlite3.Connection,
    since: str | None,
    symbol: str | None,
) -> TradeSummary:
    sql = """
        SELECT
            COUNT(*),
            COALESCE(SUM(CASE WHEN is_dry_run=1 THEN 1 ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN is_dry_run=0 THEN 1 ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN exit_time IS NOT NULL THEN 1 ELSE 0 END), 0)
        FROM trades
        WHERE 1=1
    """
    params: list[Any] = []
    if since is not None:
        sql += " AND entry_time >= ?"
        params.append(since)
    if symbol is not None:
        sql += " AND symbol = ?"
        params.append(symbol)
    row = conn.execute(sql, params).fetchone()
    return TradeSummary(
        total=int(row[0]),
        dry_run=int(row[1]),
        real=int(row[2]),
        closed=int(row[3]),
    )


def compute_by_hour(
    conn: sqlite3.Connection,
    since: str | None,
    symbol: str | None,
) -> dict[int, int]:
    """signals.timestamp の hour-of-day（UTC）→ 件数。"""
    sql = "SELECT timestamp FROM signals WHERE 1=1"
    params: list[Any] = []
    if since is not None:
        sql += " AND timestamp >= ?"
        params.append(since)
    if symbol is not None:
        sql += " AND symbol = ?"
        params.append(symbol)
    counts: Counter[int] = Counter()
    for (ts,) in conn.execute(sql, params).fetchall():
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        counts[dt.hour] += 1
    return dict(counts)


# ─── レポート組み立て ────────────────────────────────


def build_report(
    db_path: str | Path,
    window: str = DEFAULT_WINDOW,
    symbol: str | None = None,
    now: datetime | None = None,
) -> AnalysisReport:
    """SQLite を read-only で開いて AnalysisReport を返す。"""
    delta = parse_window(window)
    since = compute_time_filter(delta, now=now)

    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        return _build_report_from_conn(
            conn, db_path=str(db_path), window=window, symbol=symbol, since=since
        )
    finally:
        conn.close()


def _build_report_from_conn(
    conn: sqlite3.Connection,
    db_path: str,
    window: str,
    symbol: str | None,
    since: str | None,
) -> AnalysisReport:
    """conn から AnalysisReport を組み立て（テスト容易化のための分離）。"""
    layer_stats = fetch_layer_stats(conn, since, symbol)
    passed = fetch_passed_signals(conn, since, symbol)
    earliest, latest = fetch_uptime(conn, since, symbol)
    trade = fetch_trade_summary(conn, since, symbol)
    by_hour = compute_by_hour(conn, since, symbol)

    # 推定サイクル数: signals は 1 サイクルにつき layer 数 × symbol 数 ×
    # direction 数だけ書かれる。symbol/direction フィルタが無いケースでは
    # MOMENTUM 層の総件数 ÷ (symbol × direction の組み合わせ) でだいたい
    # サイクル数になる。フィルタがある場合は MOMENTUM 件数そのまま。
    momentum_total = next(
        (s.total for s in layer_stats if s.layer == "MOMENTUM"), 0
    )
    estimated_cycles = momentum_total

    return AnalysisReport(
        window=window,
        symbol=symbol,
        db_path=db_path,
        earliest_signal=earliest,
        latest_signal=latest,
        estimated_cycles=estimated_cycles,
        layer_stats=layer_stats,
        passed_signals=passed,
        trade_summary=trade,
        by_hour=by_hour,
    )


# ─── レンダリング ────────────────────────────────────


def render_human(report: AnalysisReport) -> str:
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append("  Phase 0 観察データ分析")
    lines.append("=" * 60)
    lines.append(f"DB:     {report.db_path}")
    lines.append(f"Window: {report.window}")
    lines.append(f"Symbol: {report.symbol or '(all)'}")
    lines.append("")

    lines.append("─── 稼動状況 ───")
    lines.append(f"earliest signal:  {report.earliest_signal or '(none)'}")
    lines.append(f"latest signal:    {report.latest_signal or '(none)'}")
    lines.append(
        f"estimated cycles: {report.estimated_cycles} "
        "(MOMENTUM 層シグナル数を採用)"
    )
    lines.append("")

    lines.append("─── 4 層通過状況 ───")
    lines.append(f"{'Layer':12} {'Passed':>8} {'Total':>8} {'Rate':>8}")
    for s in report.layer_stats:
        lines.append(
            f"{s.layer:12} {s.passed:>8} {s.total:>8} {s.pass_rate:>7.1%}"
        )
    lines.append("")

    lines.append(
        f"─── 4 層全通過シグナル（最新 {PASSED_SIGNALS_LIMIT} 件まで） ───"
    )
    if not report.passed_signals:
        lines.append("(該当なし)")
    else:
        for ps in report.passed_signals:
            lines.append(f"  {ps.timestamp}  {ps.symbol:8} {ps.direction}")
    lines.append("")

    lines.append("─── エントリー試行統計（trades） ───")
    t = report.trade_summary
    lines.append(f"total:    {t.total}")
    lines.append(f"dry_run:  {t.dry_run}")
    lines.append(f"real:     {t.real}")
    lines.append(f"closed:   {t.closed}")
    lines.append("")

    lines.append("─── 時間帯別シグナル分布（UTC hour） ───")
    if not report.by_hour:
        lines.append("(該当なし)")
    else:
        max_count = max(report.by_hour.values())
        for h in range(24):
            count = report.by_hour.get(h, 0)
            bar_len = (
                int(40 * count / max_count) if max_count > 0 else 0
            )
            bar = "#" * bar_len
            lines.append(f"  {h:02d}:00  {count:>5}  {bar}")
    lines.append("")
    return "\n".join(lines)


def render_json(report: AnalysisReport) -> str:
    payload: dict[str, Any] = {
        "window": report.window,
        "symbol": report.symbol,
        "db_path": report.db_path,
        "earliest_signal": report.earliest_signal,
        "latest_signal": report.latest_signal,
        "estimated_cycles": report.estimated_cycles,
        "layer_stats": [
            {
                "layer": s.layer,
                "total": s.total,
                "passed": s.passed,
                "pass_rate": round(s.pass_rate, 6),
            }
            for s in report.layer_stats
        ],
        "passed_signals": [asdict(ps) for ps in report.passed_signals],
        "trade_summary": asdict(report.trade_summary),
        "by_hour": {str(k): v for k, v in sorted(report.by_hour.items())},
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


# ─── CLI ─────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Phase 0 観察データ分析（read-only）"
    )
    parser.add_argument(
        "--db", default=DEFAULT_DB_PATH, help="SQLite DB path"
    )
    parser.add_argument(
        "--window",
        default=DEFAULT_WINDOW,
        help="集計対象期間: 24h / 7d / 30m / all",
    )
    parser.add_argument(
        "--symbol", default=None, help="銘柄フィルタ（例: BTC）"
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="JSON で出力",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: DB not found: {db_path}", file=sys.stderr)
        return 1

    try:
        report = build_report(
            db_path=str(db_path),
            window=args.window,
            symbol=args.symbol,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.as_json:
        print(render_json(report))
    else:
        print(render_human(report))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
