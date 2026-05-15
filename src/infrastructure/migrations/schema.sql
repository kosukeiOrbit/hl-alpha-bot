-- hl-alpha-bot SQLite schema (PR7.5a)
-- Phase 0 minimum: trades / signals / incidents / oi_history / balance_history /
-- blacklist / schema_version
-- Phase 1+ で sentiment_logs, funding_payments, deposits_withdrawals 等を追加

-- ===========================================================
-- 1. trades: 確定した取引記録（決済済み・未決済両方）
-- ===========================================================
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    direction       TEXT NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
    leverage_used   INTEGER NOT NULL DEFAULT 0,

    -- HL 注文 ID（HL の oid は数値だが、表記の安定のため TEXT 保存）
    -- entry_order_id は PR B2 で追加（既存 DB には ALTER TABLE で後付け）。
    entry_order_id  TEXT,
    tp_order_id     TEXT,
    sl_order_id     TEXT,

    -- サイズ・価格
    size_coins      REAL NOT NULL,
    entry_price     REAL NOT NULL,
    actual_entry_price REAL,
    sl_price        REAL NOT NULL,
    tp_price        REAL NOT NULL,
    exit_price      REAL,

    -- 状態フラグ
    is_filled       INTEGER NOT NULL DEFAULT 0,
    is_dry_run      INTEGER NOT NULL DEFAULT 1,
    is_manual_review INTEGER NOT NULL DEFAULT 0,
    is_external     INTEGER NOT NULL DEFAULT 0,
    resumed_at      TEXT,

    -- タイムスタンプ（ISO8601 UTC）
    entry_time      TEXT NOT NULL,
    exit_time       TEXT,
    fill_time       TEXT,
    closed_at       TEXT,

    -- 損益
    pnl_usd         REAL,
    fee_usd_total   REAL,
    funding_paid_usd REAL,

    -- MFE/MAE
    mfe_pct         REAL,
    mae_pct         REAL,

    -- 決済理由
    exit_reason     TEXT CHECK (exit_reason IN
        ('TP', 'SL', 'FUNDING', 'MANUAL', 'TIMEOUT') OR exit_reason IS NULL),

    -- VWAP メトリクス（章6・JSON）
    vwap_metrics    TEXT
);

CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time);
CREATE INDEX IF NOT EXISTS idx_trades_active ON trades(exit_time) WHERE exit_time IS NULL;

-- ===========================================================
-- 2. signals: 4層AND評価のロギング
-- ===========================================================
CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    direction       TEXT NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
    layer           TEXT NOT NULL CHECK (layer IN
        ('MOMENTUM', 'FLOW', 'SENTIMENT', 'REGIME')),
    passed          INTEGER NOT NULL CHECK (passed IN (0, 1)),
    rejection_reason TEXT,
    snapshot_excerpt TEXT
);

CREATE INDEX IF NOT EXISTS idx_signals_timestamp ON signals(timestamp);
CREATE INDEX IF NOT EXISTS idx_signals_symbol_dir ON signals(symbol, direction);

-- ===========================================================
-- 3. incidents: 障害ログ（章8.6）
-- ===========================================================
CREATE TABLE IF NOT EXISTS incidents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    severity        TEXT NOT NULL CHECK (severity IN
        ('INFO', 'WARNING', 'ERROR', 'CRITICAL')),
    event           TEXT NOT NULL,
    details         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_incidents_timestamp ON incidents(timestamp);

-- ===========================================================
-- 4. oi_history: 章13.5 OI 急変検出用
-- ===========================================================
CREATE TABLE IF NOT EXISTS oi_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    oi_value        REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_oi_history_symbol_time
    ON oi_history(symbol, timestamp);

-- ===========================================================
-- 5. balance_history: 残高スナップショット（日次サマリー用）
-- ===========================================================
CREATE TABLE IF NOT EXISTS balance_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    balance_usd     REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_balance_history_time
    ON balance_history(timestamp);

-- ===========================================================
-- 6. blacklist: 銘柄ブラックリスト（章8.11）
-- ===========================================================
CREATE TABLE IF NOT EXISTS blacklist (
    symbol          TEXT PRIMARY KEY,
    reason          TEXT,
    added_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ===========================================================
-- マイグレーション完了マーク
-- ===========================================================
CREATE TABLE IF NOT EXISTS schema_version (
    version         INTEGER PRIMARY KEY,
    applied_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT OR IGNORE INTO schema_version (version) VALUES (1);
