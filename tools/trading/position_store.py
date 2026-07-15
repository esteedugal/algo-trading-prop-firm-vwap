"""
Position Store
----------------
SQLite-backed state for the VWAP pullback/reversion strategy. Single-
writer, single-process (1-minute cron ticks), stdlib sqlite3 — same
discipline as the sibling ORB/EMA projects' position stores.

Seven tables. Five are byte-for-byte identical in shape and CRUD
behavior to the sibling projects' position_store.py (account_state,
daily_state, positions, breaches, eod_reports) — none of them ever
referenced strategy-specific concepts.

The two that differ replace the ORB sibling's single write-once
`candidates` table (same pattern as the EMA sibling's trend_state/
trend_setups, renamed for this strategy):
  vwap_state  - one row per (trading_date, ticker), CONTINUOUSLY MUTATED
                as new 1-min bars arrive — tracks which phase a ticker is
                in: no_bias -> clean_bias -> watching_resumption ->
                position_open -> back to clean_bias/no_bias.
  vwap_setups - one row per DISTINCT pullback->resumption cycle. A ticker
                can generate multiple vwap_setups rows in one day —
                deliberately no UNIQUE(trading_date, ticker) constraint.
                The positions table's own partial unique index (below)
                already prevents two CONCURRENT open positions on the
                same ticker; it does not (and should not) prevent
                sequential same-day re-entry.
"""

import sqlite3
from datetime import datetime
from typing import Optional

DB_PATH = "data/vwap_trend.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS account_state (
    id                         INTEGER PRIMARY KEY CHECK (id = 1),
    initial_balance            REAL    NOT NULL,
    daily_loss_limit_dollars   REAL    NOT NULL,
    max_drawdown_dollars       REAL    NOT NULL,
    drawdown_ratchet_multiple  REAL    NOT NULL DEFAULT 3.0,
    cumulative_realized_pnl    REAL    NOT NULL DEFAULT 0,
    is_ratcheted                INTEGER NOT NULL DEFAULT 0,
    ratcheted_at                 TEXT,
    terminated                    INTEGER NOT NULL DEFAULT 0,
    terminated_at                  TEXT,
    terminated_reason               TEXT,
    evaluation_start_date            TEXT    NOT NULL,
    evaluation_period_days            INTEGER,
    created_at                         TEXT    NOT NULL,
    updated_at                          TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_state (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    trading_date                TEXT    NOT NULL UNIQUE,
    start_of_day_equity          REAL    NOT NULL,
    daily_loss_limit_snapshot     REAL    NOT NULL,
    realized_pnl_running            REAL    NOT NULL DEFAULT 0,
    trading_halted                   INTEGER NOT NULL DEFAULT 0,
    halt_reason                       TEXT,
    halted_at                          TEXT,
    created_at                          TEXT    NOT NULL,
    updated_at                           TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS vwap_state (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    trading_date                TEXT    NOT NULL,
    ticker                       TEXT    NOT NULL,
    phase                         TEXT    NOT NULL DEFAULT 'no_bias',
        -- no_bias -> clean_bias -> watching_resumption -> position_open -> (clean_bias/no_bias)
    direction                      TEXT,
    vwap                            REAL,
    last_bar_ts                      TEXT,  -- idempotency guard against reprocessing the same 1-min bar
    bias_started_at                   TEXT, -- bar ts of the last genuine side-of-VWAP flip -- bounds swing lookback
    clean_since_bar_ts                 TEXT,
    pullback_bar_high                   REAL,
    pullback_bar_low                     REAL,
    pullback_bar_ts                       TEXT,
    active_setup_id                        INTEGER REFERENCES vwap_setups(id),
    cycles_today                            INTEGER NOT NULL DEFAULT 0,
    created_at                               TEXT    NOT NULL,
    updated_at                                TEXT    NOT NULL,
    UNIQUE(trading_date, ticker)
);

CREATE TABLE IF NOT EXISTS vwap_setups (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    trading_date                TEXT    NOT NULL,
    ticker                       TEXT    NOT NULL,
    cycle_number                  INTEGER NOT NULL,
    direction                      TEXT    NOT NULL,
    pullback_bar_high               REAL,
    pullback_bar_low                 REAL,
    pullback_bar_ts                   TEXT,
    swing_ref_price                    REAL,  -- swing point used for the stop
    swing_ref_ts                        TEXT,
    day_extreme_price                    REAL,  -- day high/low used for the target, if used
    day_extreme_ts                        TEXT,
    entry_trigger                          REAL,
    stop_price                              REAL,
    target_price                             REAL,
    risk_per_share                            REAL,
    stop_basis                                 TEXT,  -- 'vwap' | 'swing' -- whichever was tighter
    target_basis                                TEXT, -- 'day_extreme' | 'fixed_r'
    status                                       TEXT    NOT NULL DEFAULT 'watching',
        -- watching -> triggered / invalidated_halt_proxy / skipped_* / expired_eod
        --           / abandoned_bias_flip / abandoned_bias_failure
    created_at                                    TEXT    NOT NULL,
    updated_at                                     TEXT    NOT NULL
    -- deliberately NO UNIQUE(trading_date, ticker) -- multiple setups/day, by design
);

CREATE TABLE IF NOT EXISTS positions (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id                 INTEGER NOT NULL REFERENCES vwap_setups(id),
    trading_date                  TEXT    NOT NULL,
    ticker                          TEXT    NOT NULL,
    direction                        TEXT    NOT NULL,
    status                             TEXT    NOT NULL DEFAULT 'pending',
    qty                                 REAL    NOT NULL,
    stop_price                           REAL    NOT NULL,
    target_price                          REAL    NOT NULL,
    risk_per_share                         REAL    NOT NULL,
    risk_dollars                            REAL    NOT NULL,
    fill_price                               REAL,
    filled_at                                 TEXT,
    client_order_id                            TEXT    UNIQUE,
    alpaca_order_id                             TEXT,
    alpaca_order_status                          TEXT,
    exit_reason                                   TEXT,
    exit_price                                     REAL,
    exit_time                                       TEXT,
    pnl_dollar                                       REAL,
    held_seconds                                      REAL,
    is_valid_trade                                     INTEGER,
    profit_share_of_total                               REAL,
    rationale                                            TEXT,
    close_client_order_id                                 TEXT,
    close_alpaca_order_id                                  TEXT,
    created_at                                              TEXT    NOT NULL,
    updated_at                                               TEXT    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_open_ticker
    ON positions(ticker) WHERE status IN ('pending', 'open');

CREATE TABLE IF NOT EXISTS breaches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trading_date  TEXT    NOT NULL,
    occurred_at   TEXT    NOT NULL,
    rule          TEXT    NOT NULL,
    detail        TEXT    NOT NULL,
    action_taken  TEXT    NOT NULL,
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS eod_reports (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    trading_date           TEXT    NOT NULL UNIQUE,
    trade_table_markdown   TEXT    NOT NULL,
    reflection_text        TEXT,
    model_used             TEXT,
    tokens_in              INTEGER,
    tokens_out             INTEGER,
    generated_at           TEXT    NOT NULL
);
"""


def init_db(db_path: str = DB_PATH) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def _connect(db_path: str) -> sqlite3.Connection:
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ── account_state ────────────────────────────────────────────────────────

def get_account_state(db_path: str = DB_PATH) -> Optional[dict]:
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT * FROM account_state WHERE id = 1").fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def init_account_state(
    initial_balance: float,
    daily_loss_limit_dollars: float,
    max_drawdown_dollars: float,
    drawdown_ratchet_multiple: float,
    evaluation_start_date: str,
    db_path: str = DB_PATH,
) -> dict:
    """Idempotent: if the singleton row already exists, returns it unchanged."""
    existing = get_account_state(db_path)
    if existing:
        return existing

    conn = _connect(db_path)
    now = datetime.now().isoformat()
    try:
        conn.execute(
            """
            INSERT INTO account_state (
                id, initial_balance, daily_loss_limit_dollars, max_drawdown_dollars,
                drawdown_ratchet_multiple, evaluation_start_date, created_at, updated_at
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
            """,
            (initial_balance, daily_loss_limit_dollars, max_drawdown_dollars,
             drawdown_ratchet_multiple, evaluation_start_date, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return get_account_state(db_path)


def update_account_state(db_path: str = DB_PATH, **fields) -> None:
    if not fields:
        return
    conn = _connect(db_path)
    try:
        set_clause = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [datetime.now().isoformat()]
        conn.execute(f"UPDATE account_state SET {set_clause}, updated_at=? WHERE id=1", values)
        conn.commit()
    finally:
        conn.close()


# ── daily_state ──────────────────────────────────────────────────────────

def get_daily_state(trading_date: str, db_path: str = DB_PATH) -> Optional[dict]:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM daily_state WHERE trading_date = ?", (trading_date,)
        ).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def create_daily_state(
    trading_date: str,
    start_of_day_equity: float,
    daily_loss_limit_snapshot: float,
    db_path: str = DB_PATH,
) -> dict:
    conn = _connect(db_path)
    now = datetime.now().isoformat()
    try:
        conn.execute(
            """
            INSERT INTO daily_state (
                trading_date, start_of_day_equity, daily_loss_limit_snapshot, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (trading_date, start_of_day_equity, daily_loss_limit_snapshot, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return get_daily_state(trading_date, db_path)


def update_daily_state(trading_date: str, db_path: str = DB_PATH, **fields) -> None:
    if not fields:
        return
    conn = _connect(db_path)
    try:
        set_clause = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [datetime.now().isoformat(), trading_date]
        conn.execute(
            f"UPDATE daily_state SET {set_clause}, updated_at=? WHERE trading_date=?", values
        )
        conn.commit()
    finally:
        conn.close()


# ── vwap_state ───────────────────────────────────────────────────────────

def get_vwap_state(trading_date: str, ticker: str, db_path: str = DB_PATH) -> Optional[dict]:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM vwap_state WHERE trading_date = ? AND ticker = ?",
            (trading_date, ticker),
        ).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def create_vwap_state(trading_date: str, ticker: str, db_path: str = DB_PATH) -> dict:
    conn = _connect(db_path)
    now = datetime.now().isoformat()
    try:
        conn.execute(
            "INSERT INTO vwap_state (trading_date, ticker, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (trading_date, ticker, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return get_vwap_state(trading_date, ticker, db_path)


def get_or_create_vwap_state(trading_date: str, ticker: str, db_path: str = DB_PATH) -> dict:
    existing = get_vwap_state(trading_date, ticker, db_path)
    return existing if existing else create_vwap_state(trading_date, ticker, db_path)


def update_vwap_state(trading_date: str, ticker: str, db_path: str = DB_PATH, **fields) -> None:
    if not fields:
        return
    conn = _connect(db_path)
    try:
        set_clause = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [datetime.now().isoformat(), trading_date, ticker]
        conn.execute(
            f"UPDATE vwap_state SET {set_clause}, updated_at=? WHERE trading_date=? AND ticker=?", values
        )
        conn.commit()
    finally:
        conn.close()


def list_vwap_states_for_date(trading_date: str, db_path: str = DB_PATH) -> list:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM vwap_state WHERE trading_date = ?", (trading_date,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


# ── vwap_setups ──────────────────────────────────────────────────────────

def create_setup(
    trading_date: str,
    ticker: str,
    cycle_number: int,
    direction: str,
    db_path: str = DB_PATH,
) -> int:
    conn = _connect(db_path)
    now = datetime.now().isoformat()
    try:
        cur = conn.execute(
            """
            INSERT INTO vwap_setups (trading_date, ticker, cycle_number, direction, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (trading_date, ticker, cycle_number, direction, now, now),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_setup(setup_id: int, db_path: str = DB_PATH) -> Optional[dict]:
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT * FROM vwap_setups WHERE id = ?", (setup_id,)).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def update_setup(setup_id: int, db_path: str = DB_PATH, **fields) -> None:
    if not fields:
        return
    conn = _connect(db_path)
    try:
        set_clause = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [datetime.now().isoformat(), setup_id]
        conn.execute(f"UPDATE vwap_setups SET {set_clause}, updated_at=? WHERE id=?", values)
        conn.commit()
    finally:
        conn.close()


def list_setups_for_date(trading_date: str, db_path: str = DB_PATH) -> list:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM vwap_setups WHERE trading_date = ? ORDER BY created_at",
            (trading_date,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def list_watching_setups(trading_date: str, db_path: str = DB_PATH) -> list:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM vwap_setups WHERE trading_date = ? AND status = 'watching' ORDER BY created_at",
            (trading_date,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


# ── positions ────────────────────────────────────────────────────────────

def get_open_position_db(ticker: str, db_path: str = DB_PATH) -> Optional[dict]:
    """DB-recorded pending/open position for this ticker."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM positions WHERE ticker = ? AND status IN ('pending', 'open') LIMIT 1",
            (ticker,),
        ).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def get_position_by_client_order_id(client_order_id: str, db_path: str = DB_PATH) -> Optional[dict]:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM positions WHERE client_order_id = ? OR close_client_order_id = ?",
            (client_order_id, client_order_id),
        ).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def create_pending_position(
    candidate_id: int,
    trading_date: str,
    ticker: str,
    direction: str,
    qty: float,
    stop_price: float,
    target_price: float,
    risk_per_share: float,
    risk_dollars: float,
    client_order_id: str,
    db_path: str = DB_PATH,
) -> int:
    """
    Raises sqlite3.IntegrityError if a pending/open row already exists for
    this ticker, or client_order_id was already used. candidate_id here
    is a vwap_setups.id.
    """
    conn = _connect(db_path)
    now = datetime.now().isoformat()
    try:
        cur = conn.execute(
            """
            INSERT INTO positions (
                candidate_id, trading_date, ticker, direction, status, qty,
                stop_price, target_price, risk_per_share, risk_dollars,
                client_order_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (candidate_id, trading_date, ticker, direction, qty,
             stop_price, target_price, risk_per_share, risk_dollars,
             client_order_id, now, now),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def mark_position_open(
    position_id: int,
    alpaca_order_id: str,
    alpaca_order_status: str,
    fill_price: float,
    filled_qty: float,
    filled_at: str,
    db_path: str = DB_PATH,
) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            UPDATE positions SET status='open', alpaca_order_id=?, alpaca_order_status=?,
                fill_price=?, qty=?, filled_at=?, updated_at=? WHERE id=?
            """,
            (alpaca_order_id, alpaca_order_status, fill_price, filled_qty, filled_at,
             datetime.now().isoformat(), position_id),
        )
        conn.commit()
    finally:
        conn.close()


def correct_position(position_id: int, qty: float, fill_price: float, db_path: str = DB_PATH) -> None:
    """Correct a position's recorded qty/fill_price against Alpaca's actual
    position ledger — for use when an audit finds drift."""
    conn = _connect(db_path)
    try:
        conn.execute(
            "UPDATE positions SET qty=?, fill_price=?, updated_at=? WHERE id=?",
            (qty, fill_price, datetime.now().isoformat(), position_id),
        )
        conn.commit()
    finally:
        conn.close()


def reopen_position(position_id: int, db_path: str = DB_PATH) -> None:
    """Reverts a position row back to 'open', clearing exit fields — manual-
    recovery tool for a falsely-recorded close. Not part of the normal
    trading flow."""
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            UPDATE positions SET status='open', exit_reason=NULL, exit_price=NULL,
                exit_time=NULL, pnl_dollar=NULL, held_seconds=NULL, is_valid_trade=NULL,
                close_client_order_id=NULL, close_alpaca_order_id=NULL, updated_at=?
            WHERE id=?
            """,
            (datetime.now().isoformat(), position_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_position_failed(
    position_id: int,
    reason: str,
    alpaca_order_id: Optional[str] = None,
    db_path: str = DB_PATH,
) -> None:
    """alpaca_order_id should be recorded whenever an order actually reached
    Alpaca before failing/timing out. Callers that time out waiting for a
    fill MUST cancel the live order at Alpaca before calling this."""
    conn = _connect(db_path)
    try:
        conn.execute(
            "UPDATE positions SET status='failed', exit_reason=?, alpaca_order_id=?, updated_at=? WHERE id=?",
            (reason, alpaca_order_id, datetime.now().isoformat(), position_id),
        )
        conn.commit()
    finally:
        conn.close()


def close_position(
    position_id: int,
    exit_reason: str,
    exit_price: float,
    exit_time: str,
    pnl_dollar: float,
    held_seconds: float,
    is_valid_trade: bool,
    close_client_order_id: Optional[str] = None,
    close_alpaca_order_id: Optional[str] = None,
    db_path: str = DB_PATH,
) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            UPDATE positions SET
                status='closed', exit_reason=?, exit_price=?, exit_time=?,
                pnl_dollar=?, held_seconds=?, is_valid_trade=?,
                close_client_order_id=?, close_alpaca_order_id=?, updated_at=?
            WHERE id=?
            """,
            (exit_reason, exit_price, exit_time, pnl_dollar, held_seconds,
             int(is_valid_trade), close_client_order_id, close_alpaca_order_id,
             datetime.now().isoformat(), position_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_profit_share(position_id: int, profit_share_of_total: float, db_path: str = DB_PATH) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(
            "UPDATE positions SET profit_share_of_total=?, updated_at=? WHERE id=?",
            (profit_share_of_total, datetime.now().isoformat(), position_id),
        )
        conn.commit()
    finally:
        conn.close()


def list_open_positions(db_path: str = DB_PATH) -> list:
    conn = _connect(db_path)
    try:
        rows = conn.execute("SELECT * FROM positions WHERE status = 'open'").fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def list_pending_positions(older_than_minutes: int = 3, db_path: str = DB_PATH) -> list:
    """Pending rows old enough that they're not just an in-flight submission
    from the current tick (1-minute cadence, so a short threshold here)."""
    conn = _connect(db_path)
    try:
        rows = conn.execute("SELECT * FROM positions WHERE status = 'pending'").fetchall()
        cutoff = datetime.now().timestamp() - older_than_minutes * 60
        return [
            _row_to_dict(r) for r in rows
            if datetime.fromisoformat(r["created_at"]).timestamp() < cutoff
        ]
    finally:
        conn.close()


def list_positions_for_date(trading_date: str, db_path: str = DB_PATH) -> list:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM positions WHERE trading_date = ? ORDER BY created_at",
            (trading_date,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def count_valid_trades(db_path: str = DB_PATH) -> int:
    """Lifetime count toward the rulebook's 20-valid-trade minimum. Purely
    a counter — has no write-path back into signal/entry logic, by design."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) as n FROM positions WHERE status='closed' AND is_valid_trade=1"
        ).fetchone()
        return row["n"]
    finally:
        conn.close()


# ── breaches ─────────────────────────────────────────────────────────────

def record_breach(trading_date: str, rule: str, detail: str, action_taken: str, db_path: str = DB_PATH) -> None:
    conn = _connect(db_path)
    now = datetime.now().isoformat()
    try:
        conn.execute(
            """
            INSERT INTO breaches (trading_date, occurred_at, rule, detail, action_taken, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (trading_date, now, rule, detail, action_taken, now),
        )
        conn.commit()
    finally:
        conn.close()


def list_breaches_for_date(trading_date: str, db_path: str = DB_PATH) -> list:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM breaches WHERE trading_date = ? ORDER BY occurred_at", (trading_date,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


# ── eod_reports ──────────────────────────────────────────────────────────

def save_eod_report(
    trading_date: str,
    trade_table_markdown: str,
    reflection_text: Optional[str],
    model_used: Optional[str],
    tokens_in: Optional[int],
    tokens_out: Optional[int],
    db_path: str = DB_PATH,
) -> None:
    conn = _connect(db_path)
    now = datetime.now().isoformat()
    try:
        conn.execute(
            """
            INSERT INTO eod_reports (
                trading_date, trade_table_markdown, reflection_text, model_used,
                tokens_in, tokens_out, generated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trading_date) DO UPDATE SET
                trade_table_markdown=excluded.trade_table_markdown,
                reflection_text=excluded.reflection_text,
                model_used=excluded.model_used,
                tokens_in=excluded.tokens_in,
                tokens_out=excluded.tokens_out,
                generated_at=excluded.generated_at
            """,
            (trading_date, trade_table_markdown, reflection_text, model_used,
             tokens_in, tokens_out, now),
        )
        conn.commit()
    finally:
        conn.close()


def get_eod_report(trading_date: str, db_path: str = DB_PATH) -> Optional[dict]:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM eod_reports WHERE trading_date = ?", (trading_date,)
        ).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Initialized {DB_PATH}")
    print("Account state:", get_account_state())
