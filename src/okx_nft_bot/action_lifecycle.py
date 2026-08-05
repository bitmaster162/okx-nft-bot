#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
action_lifecycle.py — MVCS #3 action state machine journal (append-only).

Journals each action's transitions through PROPOSED -> VALIDATED -> SUBMITTED
(or PROPOSED -> REJECTED on a blocked gate), and CONFIRMED once the order is
observed live on the exchange (active_offers). Written to a SEPARATE sqlite file
(action_lifecycle.sqlite3) so it NEVER contends with the live execution DB.

Two entry points:
  * record_submit(db, ...)  — called additively from the bot's single submit
    choke point (_record_execution_submit_event), in its own try/except so it
    can never affect trading.
  * confirm_sweep(...)      — CLI, reads active_offers READ-ONLY and journals
    CONFIRMED for orders now live on the exchange. Run periodically off-path.

States: PROPOSED, VALIDATED, APPROVED, SUBMITTED, CONFIRMED, REJECTED, FAILED.
"""
import os
import sqlite3
import sys
import uuid

DEFAULT_LIFECYCLE_DB = "/root/okx-nft-bot/data/action_lifecycle.sqlite3"
DEFAULT_EXECUTION_DB = "/root/okx-nft-bot/data/execution.sqlite3"

_DDL = """
CREATE TABLE IF NOT EXISTS action_lifecycle (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    state TEXT NOT NULL,
    engine TEXT,
    action_type TEXT,
    collection TEXT,
    chain TEXT,
    price_bnb REAL,
    reason TEXT,
    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_alc_action ON action_lifecycle(action_id);
CREATE INDEX IF NOT EXISTS idx_alc_state ON action_lifecycle(state);
"""


def _rw(db):
    con = sqlite3.connect(db, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.executescript(_DDL)
    # best-effort: keep the journal writable by both the bot (uid 1000) and root-run
    # sweeps, so ownership races never silently break journaling.
    for suffix in ("", "-wal", "-shm"):
        try:
            os.chmod(db + suffix, 0o666)
        except OSError:
            pass
    return con


def record_submit(db, *, engine, action_type, collection, chain,
                  price_bnb=None, status, reason=None, action_id=None):
    """Journal the transition sequence for one submit outcome. Append-only."""
    aid = action_id or uuid.uuid4().hex
    st = (status or "").lower()
    if st == "submitted":
        seq = [("PROPOSED", None), ("VALIDATED", None), ("SUBMITTED", reason)]
    elif st in ("blocked", "rejected"):
        seq = [("PROPOSED", None), ("REJECTED", reason)]
    else:
        seq = [("PROPOSED", None), ("FAILED", reason or st)]
    con = _rw(db)
    try:
        con.executemany(
            "INSERT INTO action_lifecycle(action_id,seq,state,engine,action_type,"
            "collection,chain,price_bnb,reason) VALUES(?,?,?,?,?,?,?,?,?)",
            [(aid, i, s, engine, action_type, collection, chain, price_bnb, r)
             for i, (s, r) in enumerate(seq)],
        )
        con.commit()
    finally:
        con.close()
    return aid


def confirm_sweep(lifecycle_db=DEFAULT_LIFECYCLE_DB, execution_db=DEFAULT_EXECUTION_DB):
    """Journal CONFIRMED for orders now live on the exchange (active_offers).
    Reads execution DB READ-ONLY; writes only the separate lifecycle DB."""
    ro = sqlite3.connect(f"file:{execution_db}?mode=ro", uri=True, timeout=15)
    try:
        try:
            active = ro.execute(
                "SELECT order_hash, collection, chain, price_bnb FROM active_offers "
                "WHERE status='active'").fetchall()
        except sqlite3.OperationalError:
            active = []
    finally:
        ro.close()
    con = _rw(lifecycle_db)
    try:
        seen = {r[0] for r in con.execute(
            "SELECT action_id FROM action_lifecycle WHERE state='CONFIRMED'")}
        new = 0
        for oh, coll, chain, price in active:
            if not oh or oh in seen:
                continue
            con.execute(
                "INSERT INTO action_lifecycle(action_id,seq,state,engine,action_type,"
                "collection,chain,price_bnb,reason) VALUES(?,?,?,?,?,?,?,?,?)",
                (oh, 3, "CONFIRMED", "reconcile", "OFFER_LIVE", coll, chain, price,
                 "observed active on exchange"))
            new += 1
        con.commit()
    finally:
        con.close()
    print(f"confirm_sweep: {new} newly CONFIRMED (of {len(active)} active offers)")
    return new


def _summary(lifecycle_db=DEFAULT_LIFECYCLE_DB):
    con = _rw(lifecycle_db)
    try:
        rows = con.execute("SELECT state, COUNT(*) FROM action_lifecycle GROUP BY state").fetchall()
    finally:
        con.close()
    print("action_lifecycle states:", dict(rows) if rows else "(empty)")


if __name__ == "__main__":
    if "--confirm-sweep" in sys.argv:
        confirm_sweep()
    elif "--summary" in sys.argv:
        _summary()
    else:
        print("usage: action_lifecycle.py [--confirm-sweep|--summary]")
