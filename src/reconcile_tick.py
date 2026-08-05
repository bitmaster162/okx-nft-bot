#!/usr/bin/env python3
"""Out-of-band reconcile tick for the OKX bot.

The execution_daemon reconciles only at the END of each full cycle, but the
rival scan (Phase 1) is so slow (minutes per collection, hundreds of collections)
that a cycle can take many hours — so last_reconcile_at goes stale past the
EXECUTION_RECONCILE_MAX_STALENESS_SECONDS threshold and healthcheck reports
unhealthy. This runs the SAME reconcile the daemon runs, on its own 10-min
cadence, keeping local active_offers synced with the exchange and last_reconcile_at
fresh — without touching the slow trading loop. Idempotent; safe to run alongside
the daemon (SQLite WAL + busy_timeout serialize writes).
"""
import os
import sys

from okx_nft_bot.config import load_settings
from okx_nft_bot.execution_governor import ExecutionGovernor

chains = [x.strip().lower() for x in os.getenv("RECONCILE_CHAINS", "bsc").split(",") if x.strip()]
settings = load_settings()
gov = ExecutionGovernor(settings=settings)
rc = 0
for ch in chains:
    try:
        r = gov.reconcile_active_offers(chain=ch)
        print(f"[reconcile:{ch}] exchange_seen={r.exchange_seen} local_active={r.local_active_seen} "
              f"missing={r.local_marked_missing} added={r.local_added_from_exchange}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[reconcile:{ch}] FAILED: {e!r}", flush=True)
        rc = 1
sys.exit(rc)
