"""Trade ledger — SQLite, stdlib only.

Positions use weighted-average cost basis, so realized PnL on a sell is
(proceeds - avg_cost * qty) - fees. Fees are charged into the ledger at fill
time, including the fees of transactions that FAILED, because those are real
money and a bot that hides them will look profitable while draining a wallet.
"""

import datetime
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL    NOT NULL,
    day          TEXT    NOT NULL,
    mode         TEXT    NOT NULL,
    strategy     TEXT    NOT NULL,
    pair         TEXT    NOT NULL,
    action       TEXT    NOT NULL,
    in_symbol    TEXT    NOT NULL,
    in_amount    REAL    NOT NULL,
    out_symbol   TEXT    NOT NULL,
    out_amount   REAL    NOT NULL,
    price_usd    REAL    NOT NULL,
    notional_usd REAL    NOT NULL,
    fee_usd      REAL    NOT NULL DEFAULT 0,
    realized_pnl REAL    NOT NULL DEFAULT 0,
    edge_bps     REAL    NOT NULL DEFAULT 0,
    tx_sig       TEXT    NOT NULL DEFAULT '',
    status       TEXT    NOT NULL DEFAULT 'filled',
    note         TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_fills_day  ON fills(day);
CREATE INDEX IF NOT EXISTS idx_fills_pair ON fills(pair);

CREATE TABLE IF NOT EXISTS positions (
    pair        TEXT PRIMARY KEY,
    quantity    REAL NOT NULL DEFAULT 0,
    avg_cost    REAL NOT NULL DEFAULT 0,
    realized    REAL NOT NULL DEFAULT 0,
    fees        REAL NOT NULL DEFAULT 0,
    updated_at  REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    day     TEXT NOT NULL,
    level   TEXT NOT NULL,
    kind    TEXT NOT NULL,
    message TEXT NOT NULL,
    data    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS equity (
    ts         REAL NOT NULL,
    day        TEXT NOT NULL,
    equity_usd REAL NOT NULL,
    cash_usd   REAL NOT NULL DEFAULT 0
);
"""


def _today(ts=None):
    return datetime.datetime.fromtimestamp(ts or time.time(), datetime.timezone.utc).strftime("%Y-%m-%d")


@dataclass
class Position:
    pair: str
    quantity: float = 0.0
    avg_cost: float = 0.0
    realized: float = 0.0
    fees: float = 0.0

    @property
    def cost_basis(self) -> float:
        return self.quantity * self.avg_cost

    def unrealized(self, mark_usd: float) -> float:
        return (mark_usd - self.avg_cost) * self.quantity


class Ledger:
    def __init__(self, path="crypto_trades.db"):
        self.path = path
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    def close(self):
        with self._lock:
            try:
                self._db.close()
            except Exception:
                pass

    # ---------------------------------------------------------------- reads
    def position(self, pair: str) -> Position:
        with self._lock:
            row = self._db.execute("SELECT * FROM positions WHERE pair = ?", (pair,)).fetchone()
        if not row:
            return Position(pair=pair)
        return Position(pair=pair, quantity=row["quantity"], avg_cost=row["avg_cost"],
                        realized=row["realized"], fees=row["fees"])

    def positions(self):
        with self._lock:
            rows = self._db.execute("SELECT * FROM positions ORDER BY pair").fetchall()
        return [Position(pair=r["pair"], quantity=r["quantity"], avg_cost=r["avg_cost"],
                         realized=r["realized"], fees=r["fees"]) for r in rows]

    def realized_pnl(self, day=None) -> float:
        q = "SELECT COALESCE(SUM(realized_pnl), 0) AS v FROM fills"
        args = ()
        if day:
            q += " WHERE day = ?"
            args = (day,)
        with self._lock:
            return float(self._db.execute(q, args).fetchone()["v"])

    def fees_paid(self, day=None) -> float:
        q = "SELECT COALESCE(SUM(fee_usd), 0) AS v FROM fills"
        args = ()
        if day:
            q += " WHERE day = ?"
            args = (day,)
        with self._lock:
            return float(self._db.execute(q, args).fetchone()["v"])

    def net_pnl(self, day=None) -> float:
        """Realized PnL already has fees deducted at fill time."""
        return self.realized_pnl(day)

    def today_pnl(self) -> float:
        return self.net_pnl(_today())

    def trade_count(self, day=None) -> int:
        q = "SELECT COUNT(*) AS v FROM fills WHERE status = 'filled'"
        args = ()
        if day:
            q += " AND day = ?"
            args = (day,)
        with self._lock:
            return int(self._db.execute(q, args).fetchone()["v"])

    def recent_fills(self, limit=10):
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM fills ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def recent_events(self, limit=20):
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def consecutive_losses(self, lookback=25) -> int:
        with self._lock:
            rows = self._db.execute(
                "SELECT realized_pnl FROM fills WHERE status='filled' AND action='SELL' "
                "ORDER BY id DESC LIMIT ?", (int(lookback),)).fetchall()
        n = 0
        for r in rows:
            if r["realized_pnl"] < 0:
                n += 1
            else:
                break
        return n

    # --------------------------------------------------------------- writes
    def record_fill(self, *, mode, strategy, pair, action, in_symbol, in_amount,
                    out_symbol, out_amount, price_usd, notional_usd, fee_usd=0.0,
                    edge_bps=0.0, tx_sig="", status="filled", note=""):
        """Record a fill and roll the position forward. Returns realized PnL."""
        ts = time.time()
        realized = 0.0
        with self._lock:
            row = self._db.execute("SELECT * FROM positions WHERE pair = ?", (pair,)).fetchone()
            qty = row["quantity"] if row else 0.0
            avg = row["avg_cost"] if row else 0.0
            acc_realized = row["realized"] if row else 0.0
            acc_fees = row["fees"] if row else 0.0

            if status == "filled":
                if action == "BUY":
                    # out_amount is base acquired; notional is what we paid.
                    new_qty = qty + out_amount
                    if new_qty > 0:
                        avg = ((qty * avg) + notional_usd + fee_usd) / new_qty
                    qty = new_qty
                    realized = -0.0
                elif action == "SELL":
                    sold = min(in_amount, qty) if qty > 0 else in_amount
                    proceeds = notional_usd
                    realized = proceeds - (avg * sold) - fee_usd
                    qty = max(0.0, qty - in_amount)
                    if qty <= 1e-12:
                        qty, avg = 0.0, 0.0
                acc_realized += realized
            else:
                # A failed transaction moves no inventory but still costs gas.
                realized = -abs(fee_usd)
                acc_realized += realized
            acc_fees += fee_usd

            self._db.execute(
                "INSERT INTO fills (ts, day, mode, strategy, pair, action, in_symbol, in_amount,"
                " out_symbol, out_amount, price_usd, notional_usd, fee_usd, realized_pnl, edge_bps,"
                " tx_sig, status, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, _today(ts), mode, strategy, pair, action, in_symbol, in_amount, out_symbol,
                 out_amount, price_usd, notional_usd, fee_usd, realized, edge_bps, tx_sig, status, note),
            )
            self._db.execute(
                "INSERT INTO positions (pair, quantity, avg_cost, realized, fees, updated_at)"
                " VALUES (?,?,?,?,?,?) ON CONFLICT(pair) DO UPDATE SET"
                " quantity=excluded.quantity, avg_cost=excluded.avg_cost,"
                " realized=excluded.realized, fees=excluded.fees, updated_at=excluded.updated_at",
                (pair, qty, avg, acc_realized, acc_fees, ts),
            )
            self._db.commit()
        return realized

    def record_event(self, level, kind, message, data=None):
        ts = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO events (ts, day, level, kind, message, data) VALUES (?,?,?,?,?,?)",
                (ts, _today(ts), level, kind, str(message)[:2000], json.dumps(data or {}, default=str)[:4000]),
            )
            self._db.commit()

    def snapshot_equity(self, equity_usd, cash_usd=0.0):
        ts = time.time()
        with self._lock:
            self._db.execute("INSERT INTO equity (ts, day, equity_usd, cash_usd) VALUES (?,?,?,?)",
                             (ts, _today(ts), equity_usd, cash_usd))
            self._db.commit()

    def stats(self, day=None):
        scope = "WHERE day = ?" if day else ""
        args = (day,) if day else ()
        with self._lock:
            row = self._db.execute(
                f"SELECT COUNT(*) AS n,"
                f" COALESCE(SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END),0) AS wins,"
                f" COALESCE(SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END),0) AS losses,"
                f" COALESCE(SUM(realized_pnl),0) AS pnl,"
                f" COALESCE(SUM(fee_usd),0) AS fees,"
                f" COALESCE(SUM(CASE WHEN status != 'filled' THEN 1 ELSE 0 END),0) AS failed"
                f" FROM fills {scope}", args).fetchone()
        n, wins, losses = int(row["n"]), int(row["wins"]), int(row["losses"])
        decided = wins + losses
        return {
            "trades": n,
            "wins": wins,
            "losses": losses,
            "failed_txs": int(row["failed"]),
            "win_rate": (wins / decided * 100) if decided else 0.0,
            "pnl_usd": float(row["pnl"]),
            "fees_usd": float(row["fees"]),
        }
