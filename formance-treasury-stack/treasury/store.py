"""Operational state store. One code path for SQLite (dev/tests) and PostgreSQL (production, required for >1 replica).

The Ledger holds all money. This store holds only workflow state: payout/auth records, card controls, quotes, facility terms,
reconciliation findings, and an append-only audit log. All statements are ANSI-compatible (`?` placeholders are translated for
Postgres); the connection is autocommit with idempotent, single-statement writes, so no cross-statement DB transaction is assumed.

DATABASE_URL: `postgresql://user:pass@host:5432/db`  |  `sqlite:///path/to/file.db`  |  `:memory:`
"""
import sqlite3
import threading

SCHEMA_V1 = [
    """create table if not exists payouts(id text primary key, req_fp text, wallet text, amount bigint, ccy text, dest text,
       urgency text, status text, rail text, bank text, bank_ref text, eta text, attempts text, plan text, reason text,
       created text, updated text, lease_until double precision)""",
    """create table if not exists cards(card_id text primary key, wallet text, status text, per_txn_limit bigint, daily_limit bigint,
       blocked_mcc text, allowed_countries text, credit_id text)""",
    """create table if not exists auths(auth_id text primary key, req_fp text, card_id text, wallet text, amount bigint, ccy text,
       merchant text, mcc text, country text, status text, held_wallet bigint, held_credit bigint, credit_id text,
       captured bigint default 0, released bigint default 0, created text, expires text, response text)""",
    """create table if not exists auth_events(auth_id text, kind text, seq text, amount bigint, released bigint,
       primary key(auth_id, kind, seq))""",
    """create table if not exists quotes(id text primary key, wallet text, sell_ccy text, buy_ccy text, sell bigint, buy bigint,
       rate double precision, expires text, status text)""",
    """create table if not exists facilities(id text primary key, borrower text, ccy text, limit_minor bigint, apr_bps bigint,
       status text, last_accrual text)""",
    """create table if not exists guarantees(id text primary key, applicant text, beneficiary text, ccy text, amount bigint,
       collateral bigint, claimed bigint default 0, status text, expiry text)""",
    """create table if not exists recon(bank text, bank_ref text, kind text, status text, detail text, primary key(bank, bank_ref))""",
    """create table if not exists audit(id text primary key, ts text, actor text, action text, ref text, detail text)""",
    "create index if not exists payouts_status on payouts(status)",
    "create index if not exists auths_status on auths(status, expires)",
]


class Store:
    def __init__(self, url=":memory:"):
        self.pg = url.startswith(("postgres://", "postgresql://"))
        self.url = url
        if self.pg:
            import psycopg
            from psycopg.rows import dict_row
            self._psycopg, self._dict_row = psycopg, dict_row
            self._tl = threading.local()
        else:
            path = url[len("sqlite:///"):] if url.startswith("sqlite:///") else url
            self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            self._db.row_factory = sqlite3.Row
            self._db.execute("pragma journal_mode=wal")
            self._db.execute("pragma synchronous=full")
            self._db.execute("pragma busy_timeout=5000")
            self._lock = threading.RLock()
        for stmt in SCHEMA_V1:
            self.x(stmt)

    # -- connections -------------------------------------------------------------------------------------------
    def _pg_conn(self):
        c = getattr(self._tl, "c", None)
        if c is None or c.closed:
            c = self._tl.c = self._psycopg.connect(self.url, autocommit=True, row_factory=self._dict_row, connect_timeout=5)
        return c

    def _run(self, sql, args):
        if not self.pg:
            with self._lock:
                cur = self._db.execute(sql, args)
                return [dict(r) for r in cur.fetchall()] if cur.description else [], cur.rowcount
        sql = sql.replace("?", "%s")
        for attempt in (0, 1):
            try:
                with self._pg_conn().cursor() as cur:
                    cur.execute(sql, args)
                    return (cur.fetchall() if cur.description else []), cur.rowcount
            except self._psycopg.OperationalError:
                self._tl.c = None            # dropped connection (failover/restart): retry once on a fresh one
                if attempt:
                    raise

    # -- API ---------------------------------------------------------------------------------------------------
    def q(self, sql, *args):
        return self._run(sql, args)[0]

    def q1(self, sql, *args):
        rows = self.q(sql, *args)
        return dict(rows[0]) if rows else None

    def x(self, sql, *args):
        """Execute a write; returns affected row count (use `on conflict do nothing` + rowcount for race-free first-writer-wins)."""
        return self._run(sql, args)[1]

    def ping(self):
        try:
            self.q("select 1")
            return True
        except Exception:
            return False
