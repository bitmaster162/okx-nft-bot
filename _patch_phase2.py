"""Patch parasite_hunter.py on server: disable Phase 2 via env flag."""
import sys
from pathlib import Path

PATH = "/root/okx-nft-bot/src/okx_nft_bot/sniper/parasite_hunter.py"  # host path

src = Path(PATH).read_text(encoding="utf-8")

# ── Edit 1: add phase2_enabled flag right after nonwl_qty line ──
OLD1 = '        self.nonwl_qty = _env_int("PARASITE_HUNTER_NONWL_QTY", 10)\n'
NEW1 = (
    '        self.nonwl_qty = _env_int("PARASITE_HUNTER_NONWL_QTY", 10)\n'
    '        # Phase 2 toggle (default OFF — disable non-WL parasite hunt)\n'
    '        self.phase2_enabled = _env_bool("PARASITE_HUNTER_PHASE2_ENABLED", False)\n'
)
if OLD1 not in src:
    print("ERROR: OLD1 marker not found"); sys.exit(2)
if 'self.phase2_enabled' in src:
    print("Already patched (phase2_enabled present) — skipping edit 1")
else:
    src = src.replace(OLD1, NEW1, 1)
    print("Edit 1 applied")

# ── Edit 2: guard the phase2 fetch ──
OLD2 = '            log.info("═══ PHASE 2: NON-WL PARASITE HUNT on %s ═══", chain.upper())\n\n            all_parasite = self._fetch_wallet_offers(chain)\n'
NEW2 = (
    '            log.info("═══ PHASE 2: NON-WL PARASITE HUNT on %s ═══", chain.upper())\n'
    '\n'
    '            if not self.phase2_enabled:\n'
    '                log.info("⏭ PHASE 2 disabled (PARASITE_HUNTER_PHASE2_ENABLED=0) — skipping")\n'
    '                all_parasite = []\n'
    '            else:\n'
    '                all_parasite = self._fetch_wallet_offers(chain)\n'
)
if OLD2 not in src:
    print("ERROR: OLD2 marker not found (search pattern changed?)"); sys.exit(3)
if 'PHASE 2 disabled (PARASITE_HUNTER_PHASE2_ENABLED' in src:
    print("Already patched (guard present) — skipping edit 2")
else:
    src = src.replace(OLD2, NEW2, 1)
    print("Edit 2 applied")

Path(PATH).write_text(src, encoding="utf-8")
print(f"Wrote {PATH}")
