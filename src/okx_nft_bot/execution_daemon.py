#!/usr/bin/env python3
"""
Execution Daemon — runs undercutter + rival scanner continuously.

Runs as a third Docker container alongside okx-nft-bot (idle scheduler)
and okx-nft-bot-telegram (TG commands).

Cycle:
  1. Rival Scanner scan (WL capture + rival undercut + missclick check)
  2. Undercutter cycle (manage own listings/offers)
  3. Sleep SCAN_INTERVAL seconds
  4. Repeat

Env vars (from .env; execution paths are opt-in / fail-closed):
  EXECUTION_DAEMON_ENABLED=0
  EXECUTION_SCAN_INTERVAL=300  # seconds between full cycles
  COUNTERBID_ENABLED=0
  UNDERCUTTER_ENABLED=0
  UNDERCUTTER_CHAINS=bsc       # comma-separated; defaults to EXECUTION_CHAIN
  DRY_RUN=1
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time

_shutdown_requested = False


def _handle_shutdown(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True
    logging.getLogger(__name__).info("Shutdown signal received, finishing current cycle...")


signal.signal(signal.SIGTERM, _handle_shutdown)
signal.signal(signal.SIGINT, _handle_shutdown)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("execution_daemon")


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name, "")
    if not val:
        return default
    return val.lower() in ("1", "true", "yes", "on")


def _configured_undercut_chains(default_chain: str) -> tuple[str, ...]:
    """Resolve explicitly configured undercutter chains without widening defaults.

    UNDERCUTTER_CHAINS is opt-in for multi-chain execution. When it is absent
    or blank, preserve the historical behavior by using EXECUTION_CHAIN only.
    """
    from okx_nft_bot.config import validate_execution_chain

    raw = os.getenv("UNDERCUTTER_CHAINS")
    requested = [
        part.strip().lower()
        for part in (raw if raw is not None else default_chain).split(",")
        if part.strip()
    ]
    if not requested:
        requested = [default_chain]

    resolved: list[str] = []
    for chain in requested:
        validated = validate_execution_chain(chain)
        if validated not in resolved:
            resolved.append(validated)
    return tuple(resolved)


def _sleep_until_next_cycle(seconds: float) -> None:
    """Sleep in bounded chunks so SIGTERM/SIGINT is consumed promptly."""
    deadline = time.monotonic() + max(0.0, seconds)
    while not _shutdown_requested:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(1.0, remaining))


def main():
    scan_interval = int(os.getenv("EXECUTION_SCAN_INTERVAL", "300"))
    rival_enabled = _env_bool("COUNTERBID_ENABLED", False)
    undercut_enabled = _env_bool("UNDERCUTTER_ENABLED", False)
    daemon_enabled = _env_bool("EXECUTION_DAEMON_ENABLED", False)

    if not daemon_enabled:
        log.info("Execution daemon disabled (EXECUTION_DAEMON_ENABLED=0). Exiting.")
        sys.exit(0)

    log.info("=== Execution Daemon starting ===")
    log.info(f"Scan interval: {scan_interval}s, Rival: {rival_enabled}, "
             f"Undercutter: {undercut_enabled}")

    # Lazy imports — heavy deps, only load what's needed
    import json as _json
    from pathlib import Path
    from okx_nft_bot.config import load_settings
    settings = load_settings()

    # Init rival scanner (needs binance_whitelist + buy_config)
    scanner = None
    if rival_enabled:
        try:
            from okx_nft_bot.sniper.counter_bidder import CounterBidder

            wl_path = Path(os.getenv("BINANCE_WHITELIST_PATH", "./data/binance_whitelist.json"))
            buy_path = Path(os.getenv("BUY_CONFIG_PATH", "./config/buy_config.json"))
            wl = {}
            if wl_path.exists():
                wl_data = _json.loads(wl_path.read_text())
                wl = {item["contract_address"].lower(): item
                      for item in wl_data if item.get("contract_address")}
            buy_cfg = _json.loads(buy_path.read_text()) if buy_path.exists() else {}

            scanner = CounterBidder(wl, buy_cfg)
            log.info(f"CounterBidder initialized: dry_run={scanner.dry_run}, "
                     f"chains={scanner.chains}, wl={len(wl)} collections")
        except Exception as e:
            log.error(f"Failed to init CounterBidder: {e}", exc_info=True)

    # Init undercutter. Multi-chain execution is explicit: when
    # UNDERCUTTER_CHAINS is unset, only settings.execution_chain is used.
    undercut_engine = None
    undercut_chains: tuple[str, ...] = ()
    if undercut_enabled:
        try:
            from okx_nft_bot.undercutter.engine import UndercutEngine

            candidate_engine = UndercutEngine(settings=settings)
            candidate_chains = _configured_undercut_chains(settings.execution_chain)
            undercut_engine = candidate_engine
            undercut_chains = candidate_chains
            log.info(f"UndercutEngine initialized: chains={','.join(undercut_chains)}")
        except Exception as e:
            log.error(f"Failed to init UndercutEngine: {e}")

    cycle = 0
    while not _shutdown_requested:
        cycle += 1
        t0 = time.time()
        log.info(f"--- Execution cycle {cycle} ---")

        # Phase 1: Rival Scanner
        if scanner:
            try:
                log.info("[RIVAL] Starting scan...")
                report = scanner.scan_wallet()
                log.info(f"[RIVAL] Scan complete: "
                         f"collections_scanned={getattr(report, 'collections_scanned', '?')}, "
                         f"offers_placed={getattr(report, 'offers_placed', '?')}, "
                         f"offers_skipped={getattr(report, 'offers_skipped', '?')}")
            except Exception as e:
                log.error(f"[RIVAL] Scan failed: {e}")

        # Phase 2: Undercutter. Each execution chain must be explicitly
        # selected by UNDERCUTTER_CHAINS; the default remains EXECUTION_CHAIN.
        if undercut_engine:
            for undercut_chain in undercut_chains:
                try:
                    log.info(f"[UNDERCUT:{undercut_chain}] Starting cycle...")
                    actions = undercut_engine.run_cycle(chain=undercut_chain)
                    executed = sum(1 for a in actions if a.executed)
                    log.info(f"[UNDERCUT:{undercut_chain}] Cycle complete: "
                             f"{len(actions)} actions, {executed} executed")
                except Exception as e:
                    log.error(f"[UNDERCUT:{undercut_chain}] Cycle failed: {e}")

        # Phase 3: Reconcile local state with exchange (K8: once per cycle;
        # K9: iterate all active chains instead of hardcoding bsc)
        if undercut_engine is not None:
            governor = getattr(undercut_engine, "governor", None)
            if governor is not None:
                recon_chains = [
                    c.strip().lower()
                    for c in os.getenv("RECONCILE_CHAINS", "bsc,eth").split(",")
                    if c.strip()
                ]
                for rc in recon_chains:
                    try:
                        rec = governor.reconcile_active_offers(chain=rc)
                        log.info(
                            f"[RECONCILE:{rc}] exchange_seen={rec.exchange_seen} "
                            f"local_active={rec.local_active_seen} "
                            f"missing={rec.local_marked_missing} "
                            f"added={rec.local_added_from_exchange}"
                        )
                    except Exception as rexc:
                        log.warning(f"[RECONCILE:{rc}] failed: {rexc}")

        elapsed = time.time() - t0
        log.info(f"Execution cycle {cycle} done in {elapsed:.1f}s")

        if _shutdown_requested:
            log.info("Shutdown requested; current cycle complete. Exiting.")
            break

        # Wait for next cycle. Use a shutdown-aware bounded sleep so Python's
        # restarted system calls cannot leave SIGTERM waiting for the full
        # scan interval.
        wait = max(0, scan_interval - elapsed)
        if wait > 0:
            log.info(f"Sleeping {wait:.0f}s until next cycle...")
            _sleep_until_next_cycle(wait)

    log.info("=== Execution Daemon stopped ===")


if __name__ == "__main__":
    main()
