#!/usr/bin/env python3
"""
Execution Daemon — runs undercutter + parasite hunter continuously.

Runs as a third Docker container alongside okx-nft-bot (idle scheduler)
and okx-nft-bot-telegram (TG commands).

Cycle:
  1. Parasite Hunter scan (WL capture + parasite undercut + missclick check)
  2. Undercutter cycle (manage own listings/offers)
  3. Sleep SCAN_INTERVAL seconds
  4. Repeat

Env vars (from .env):
  EXECUTION_DAEMON_ENABLED=1
  EXECUTION_SCAN_INTERVAL=300  # seconds between full cycles
  PARASITE_HUNTER_ENABLED=1
  UNDERCUTTER_ENABLED=1
  DRY_RUN=0
"""
from __future__ import annotations
import logging
import signal

_shutdown_requested = False

def _handle_shutdown(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True
    logging.getLogger(__name__).info("Shutdown signal received, finishing current cycle...")

signal.signal(signal.SIGTERM, _handle_shutdown)
signal.signal(signal.SIGINT, _handle_shutdown)
import os
import sys
import time

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


def main():
    scan_interval = int(os.getenv("EXECUTION_SCAN_INTERVAL", "300"))
    parasite_enabled = _env_bool("PARASITE_HUNTER_ENABLED", True)
    undercut_enabled = _env_bool("UNDERCUTTER_ENABLED", True)
    daemon_enabled = _env_bool("EXECUTION_DAEMON_ENABLED", True)

    if not daemon_enabled:
        log.info("Execution daemon disabled (EXECUTION_DAEMON_ENABLED=0). Exiting.")
        sys.exit(0)

    log.info("=== Execution Daemon starting ===")
    log.info(f"Scan interval: {scan_interval}s, Parasite: {parasite_enabled}, "
             f"Undercutter: {undercut_enabled}")

    # Lazy imports — heavy deps, only load what's needed
    import json as _json
    from pathlib import Path
    from okx_nft_bot.config import load_settings
    settings = load_settings()

    # Init parasite hunter (needs binance_whitelist + buy_config)
    hunter = None
    if parasite_enabled:
        try:
            from okx_nft_bot.sniper.parasite_hunter import ParasiteHunter

            wl_path = Path(os.getenv("BINANCE_WHITELIST_PATH", "./data/binance_whitelist.json"))
            buy_path = Path(os.getenv("BUY_CONFIG_PATH", "./config/buy_config.json"))
            wl = {}
            if wl_path.exists():
                wl_data = _json.loads(wl_path.read_text())
                wl = {item["contract_address"].lower(): item
                      for item in wl_data if item.get("contract_address")}
            buy_cfg = _json.loads(buy_path.read_text()) if buy_path.exists() else {}

            hunter = ParasiteHunter(wl, buy_cfg)
            log.info(f"ParasiteHunter initialized: dry_run={hunter.dry_run}, "
                     f"chains={hunter.chains}, wl={len(wl)} collections")
        except Exception as e:
            log.error(f"Failed to init ParasiteHunter: {e}", exc_info=True)

    # Init undercutter
    undercut_engine = None
    if undercut_enabled:
        try:
            from okx_nft_bot.undercutter.engine import UndercutEngine
            undercut_engine = UndercutEngine(settings=settings)
            log.info("UndercutEngine initialized")
        except Exception as e:
            log.error(f"Failed to init UndercutEngine: {e}")

    cycle = 0
    while True:
        cycle += 1
        t0 = time.time()
        log.info(f"--- Execution cycle {cycle} ---")

        # Phase 1: Parasite Hunter
        if hunter:
            try:
                log.info("[PARASITE] Starting scan...")
                report = hunter.scan_wallet()
                log.info(f"[PARASITE] Scan complete: "
                         f"collections_scanned={getattr(report, 'collections_scanned', '?')}, "
                         f"offers_placed={getattr(report, 'offers_placed', '?')}, "
                         f"offers_skipped={getattr(report, 'offers_skipped', '?')}")
            except Exception as e:
                log.error(f"[PARASITE] Scan failed: {e}")

        # Phase 2: Undercutter
        if undercut_engine:
            try:
                log.info("[UNDERCUT] Starting cycle...")
                actions = undercut_engine.run_cycle(chain="bsc")
                executed = sum(1 for a in actions if a.executed)
                log.info(f"[UNDERCUT] Cycle complete: "
                         f"{len(actions)} actions, {executed} executed")
            except Exception as e:
                log.error(f"[UNDERCUT] Cycle failed: {e}")

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

        # Wait for next cycle
        wait = max(0, scan_interval - elapsed)
        if wait > 0:
            log.info(f"Sleeping {wait:.0f}s until next cycle...")
            time.sleep(wait)


if __name__ == "__main__":
    main()
