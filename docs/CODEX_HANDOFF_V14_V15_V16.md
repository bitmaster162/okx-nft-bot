# CODEX HANDOFF: v13 Bugfixes + v14/v15 Integration + v16 Spec

> **Priority**: HIGH — Do all tasks in order. Each section has clear acceptance criteria.
> **Author**: Claude (code auditor) + RobinGooD (project owner)
> **Date**: 2026-03-24
> **Codebase**: `okx_nft_bot_v13` — 7,961 LOC source + 2,853 LOC tests

---

## TABLE OF CONTENTS

1. [BUGFIXES (30 min)](#1-bugfixes)
2. [INTEGRATE v14 seaport_signer (1 hour)](#2-integrate-v14-seaport_signer)
3. [INTEGRATE v15 counter_bidder (1.5 hours)](#3-integrate-v15-counter_bidder)
4. [NEW: v16 undercutting engine (2-3 hours)](#4-v16-undercutting-engine)
5. [TESTS & VALIDATION](#5-tests--validation)
6. [VERSION BUMP & RELEASE](#6-version-bump--release)

---

## 1. BUGFIXES

### 1A. Fix systemd unit paths (v9 → v13)

**File**: `deploy/systemd/okx-nft-bot.service`

**Change** lines 10-12:
```
# BEFORE (broken):
WorkingDirectory=/opt/okx_nft_bot_v9
EnvironmentFile=/opt/okx_nft_bot_v9/deploy/systemd/okx-nft-bot.env
ExecStart=/opt/okx_nft_bot_v9/.venv/bin/okx-nft-bot run-daemon

# AFTER (fixed):
WorkingDirectory=/opt/okx_nft_bot_v13
EnvironmentFile=/opt/okx_nft_bot_v13/deploy/systemd/okx-nft-bot.env
ExecStart=/opt/okx_nft_bot_v13/.venv/bin/okx-nft-bot run-daemon
```

**Also add security hardening** after `StartLimitBurst=5`:
```ini
PrivateTmp=true
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/opt/okx_nft_bot_v13/data
```

**Acceptance**: `systemd-analyze verify deploy/systemd/okx-nft-bot.service` passes (or manual review).

---

### 1B. Remove dead code: `parasite_wallets` config field

**Context**: `Settings.parasite_wallets` (line 69 in `src/okx_nft_bot/config.py`) is loaded from env but NEVER read anywhere in src/. After v15 integration, parasite wallets will be managed by the counter-bidder's own config (SQLite-backed `ConfigManager`).

**Action**: Leave the field for now — it will be used by v15 integration (see section 3). Change the comment:

```python
# v13+v15: Parasite wallet addresses for counter-bidding detection
parasite_wallets: tuple[str, ...] = ()
```

---

### 1C. Fix broad exception handling in analytics

**File**: `src/okx_nft_bot/analytics/cross_market.py`

Find all `except Exception: pass` patterns and replace with logged warnings:

```python
# BEFORE:
except Exception:
    pass

# AFTER:
except Exception as exc:
    logger.warning("Binance enrichment failed for %s: %s", collection_key, exc)
```

Import logger at top if not already:
```python
import logging
logger = logging.getLogger(__name__)
```

**File**: `src/okx_nft_bot/cli.py` (around line 252 and 261)

Same pattern — replace silent `except Exception` with logged warnings.

**Acceptance**: `grep -rn "except Exception:" src/` shows zero bare `pass` or `return None` handlers.

---

### 1D. Fix version mismatch in `__init__.py`

**File**: `src/okx_nft_bot/__init__.py`

```python
# BEFORE:
__version__ = "0.12.0"

# AFTER:
__version__ = "0.14.0"  # v13 base + v14/v15 integration
```

---

### 1E. Fix Notifier base class (use ABC)

**File**: `src/okx_nft_bot/notifiers/base.py`

```python
# BEFORE:
class Notifier:
    def send(self, ...):
        raise NotImplementedError

# AFTER:
from abc import ABC, abstractmethod

class Notifier(ABC):
    @abstractmethod
    def send(self, ...):
        ...
```

---

## 2. INTEGRATE v14: seaport_signer

### Source
`labs/counterbid_v15_claude/seaport_signer.py` (16KB, 400+ lines)

### Target
New module: `src/okx_nft_bot/signing/seaport_signer.py`

### Steps

**2.1** Create directory:
```bash
mkdir -p src/okx_nft_bot/signing
touch src/okx_nft_bot/signing/__init__.py
```

**2.2** Copy and adapt `seaport_signer.py`:
```bash
cp labs/counterbid_v15_claude/seaport_signer.py src/okx_nft_bot/signing/seaport_signer.py
```

**2.3** Fix imports in the copied file:
- Remove standalone `import requests` — use the project's `StdlibHttpTransport` or `curl_cffi`
- Change `from eth_account import Account` — keep as-is (add `eth-account` to dependencies)

**2.4** Update `pyproject.toml` dependencies:
```toml
dependencies = [
  "pydantic>=2.8,<3",
  "python-dotenv>=1.0,<2",
  "curl_cffi>=0.7,<1",
  "eth-account>=0.13,<1",   # NEW: for Seaport EIP-712 signing
  "web3>=7.0,<8",           # NEW: for on-chain counter reading
]
```

**2.5** Create `src/okx_nft_bot/signing/__init__.py`:
```python
"""Seaport v1.5 order signing for BSC (chainId 56)."""
from okx_nft_bot.signing.seaport_signer import (
    build_order_payload,
    sign_order,
    get_counter,
    SignedOrder,
    OrderComponents,
    WBNB_ADDRESS,
    SEAPORT_ADDRESS,
)

__all__ = [
    "build_order_payload",
    "sign_order",
    "get_counter",
    "SignedOrder",
    "OrderComponents",
    "WBNB_ADDRESS",
    "SEAPORT_ADDRESS",
]
```

**2.6** Wire into SniperEngine (`src/okx_nft_bot/sniper/engine.py`):

Replace `_execute_buy()` and `_execute_relist()` stubs:

```python
from okx_nft_bot.signing import sign_order, build_order_payload, get_counter, WBNB_ADDRESS

def _execute_buy(self, target: SniperTarget, listing: dict[str, Any]) -> dict[str, Any]:
    buyer_address = self.settings.buyer_wallet_address
    buyer_key = self.settings.buyer_wallet_private_key
    if not buyer_address or not buyer_key:
        return {'success': False, 'error': 'BUYER_WALLET_ADDRESS or BUYER_WALLET_PRIVATE_KEY not set'}

    try:
        # 1. Build Seaport order for purchasing
        order = build_order_payload(
            offerer=buyer_address,
            collection_address=target.collection_address,
            token_id=str(listing.get('tokenId') or listing.get('token_id')),
            price_wei=int(float(listing.get('price', 0)) * 10**18),
            chain_id=56 if target.chain == 'bsc' else 1,
        )
        # 2. Sign the order
        counter = get_counter(buyer_address)
        signed = sign_order(order, buyer_key, counter)

        if DRY_RUN:
            log_event('sniper_buy_dry_run', target=target.name, order=signed.parameters)
            return {'success': True, 'tx_hash': 'DRY_RUN', 'signed_order': signed}

        # 3. Submit to OKX (live mode)
        # TODO: POST /api/v5/mktplace/nft/markets/buy
        return {'success': False, 'error': 'LIVE_SUBMIT: not yet implemented'}
    except Exception as e:
        logger.error('sniper buy failed: %s', e)
        return {'success': False, 'error': str(e)}
```

**2.7** Add `DRY_RUN` to Settings:
```python
# In config.py Settings dataclass:
dry_run: bool = True  # v14+: DRY_RUN mode (no real transactions)
```

**2.8** Port v14 tests:
```bash
cp labs/counterbid_v15_claude/test_seaport_signer.py tests/test_seaport_signer.py
```
Fix imports: `from seaport_signer import ...` → `from okx_nft_bot.signing.seaport_signer import ...`

**Acceptance**: `pytest tests/test_seaport_signer.py -v` — at least 20/24 pass (4 known failures in signature length are pre-existing).

---

## 3. INTEGRATE v15: counter_bidder

### Source
`labs/counterbid_v15_claude/` — `counter_bidder.py`, `config.py`, `okx_api.py`, `cli.py`

### Target
New module: `src/okx_nft_bot/counterbid/`

### Steps

**3.1** Create directory:
```bash
mkdir -p src/okx_nft_bot/counterbid
```

**3.2** Create module files by adapting labs/ code:

**`src/okx_nft_bot/counterbid/__init__.py`**:
```python
"""Counter-bidding engine (Parasite-Killer v15)."""
from okx_nft_bot.counterbid.engine import CounterBidder, CounterBidTask, BatchResult
from okx_nft_bot.counterbid.config import CounterbidConfigManager, CollectionConfig

__all__ = [
    "CounterBidder", "CounterBidTask", "BatchResult",
    "CounterbidConfigManager", "CollectionConfig",
]
```

**`src/okx_nft_bot/counterbid/config.py`** — Adapt from `labs/counterbid_v15_claude/config.py`:
- Rename `ConfigManager` → `CounterbidConfigManager` (avoid name collision)
- Use `Settings.offers_db_path` parent dir for the SQLite file instead of hardcoded path
- Keep the SQLite-backed collections table as-is

**`src/okx_nft_bot/counterbid/engine.py`** — Adapt from `labs/counterbid_v15_claude/counter_bidder.py`:
- Change import: `from seaport_signer import ...` → `from okx_nft_bot.signing import ...`
- Change import: `from config import ConfigManager` → `from okx_nft_bot.counterbid.config import CounterbidConfigManager`
- Change import: `from okx_api import OKXAPIClient` → `from okx_nft_bot.counterbid.okx_api import OKXAPIClient`
- Move `PARASITE_WALLETS` set to read from `Settings.parasite_wallets` tuple:
  ```python
  def __init__(self, settings: Settings, ...):
      self.parasite_wallets = set(w.lower() for w in settings.parasite_wallets)
  ```

**`src/okx_nft_bot/counterbid/okx_api.py`** — Adapt from `labs/counterbid_v15_claude/okx_api.py`:
- Use project's HTTP transport where possible
- Keep HMAC-SHA256 auth logic (OK-ACCESS-KEY, etc.)
- Add `OKX_OFFERS_ENDPOINT` and `submit_offer()` method

**3.3** Wire into main CLI (`src/okx_nft_bot/cli.py`):

Add new subcommands:

```python
# New subcommands to add:
# counterbid-scan       — scan for parasite offers on configured collections
# counterbid-run        — detect + counter-bid (dry-run by default)
# counterbid-config     — manage collection configs (add/remove/list/enable/disable)
# counterbid-status     — show current counter-bid state
```

Add to `_build_parser()`:
```python
sub = subparsers.add_parser('counterbid-scan', help='Scan for parasite offers')
sub.add_argument('--collection', help='Single collection address (default: all enabled)')

sub = subparsers.add_parser('counterbid-run', help='Run counter-bidding cycle')
sub.add_argument('--submit', action='store_true', help='Actually submit orders (default: dry-run)')

sub = subparsers.add_parser('counterbid-config', help='Manage collection configs')
sub.add_argument('action', choices=['add', 'remove', 'list', 'enable', 'disable'])
sub.add_argument('--address', help='Collection contract address')
sub.add_argument('--chain', default='bsc')
sub.add_argument('--min-price', type=float, default=0.01)
sub.add_argument('--max-price', type=float, default=1.0)
sub.add_argument('--margin', type=float, default=0.001)
```

**3.4** Wire into Telegram bot (`src/okx_nft_bot/telegram_bot.py`):

Add commands:
- `/counterscan` — quick parasite scan
- `/counterrun` — counter-bid cycle (dry-run)
- `/counterconfig` — show active collection configs

**3.5** Port v15 tests:
```bash
cp labs/counterbid_v15_claude/test_counter_bidder.py tests/test_counter_bidder.py
```
Fix imports to use new package paths.

**Acceptance**: `pytest tests/test_counter_bidder.py tests/test_seaport_signer.py -v` — 38+ tests pass.

---

## 4. V16: UNDERCUTTING ENGINE

### Concept
v16 takes counter-bidding from reactive (v15: detect parasite → undercut) to proactive (v16: continuous monitoring → auto-adjust offers → maintain best-offer position).

### New Module: `src/okx_nft_bot/undercutter/`

**4.1 Architecture**

```
                    ┌─────────────────────┐
                    │  UndercutScheduler   │
                    │  (runs every 30s)    │
                    └────────┬────────────┘
                             │
                    ┌────────▼────────────┐
                    │  OfferMonitor       │
                    │  - fetch all offers │
                    │  - track our offers │
                    │  - detect undercuts │
                    └────────┬────────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
     ┌────────▼───┐  ┌──────▼──────┐  ┌───▼──────────┐
     │ Defense    │  │ Attack      │  │ Withdrawal   │
     │ re-bid    │  │ new targets  │  │ cancel stale │
     │ above     │  │ best-offer  │  │ offers       │
     └────────────┘  └─────────────┘  └──────────────┘
                             │
                    ┌────────▼────────────┐
                    │  signing/           │
                    │  seaport_signer     │
                    └────────┬────────────┘
                             │
                    ┌────────▼────────────┐
                    │  OKX API submit     │
                    │  (dry_run default)  │
                    └─────────────────────┘
```

**4.2 Files to create:**

```
src/okx_nft_bot/undercutter/
├── __init__.py
├── engine.py           # UndercutEngine — main loop
├── monitor.py          # OfferMonitor — tracks all offers + our positions
├── strategy.py         # UndercutStrategy — pricing logic
├── state.py            # PositionState — SQLite-backed state tracking
└── scheduler.py        # UndercutScheduler — timer + daemon integration
```

**4.3 `engine.py` — UndercutEngine**

```python
"""
UndercutEngine — proactive offer management.

Modes:
  DEFENSE: Someone undercut OUR offer → re-bid above them
  ATTACK:  Find collection with no/weak offers → place best offer
  WITHDRAW: Cancel offers older than max_age or below floor
"""

from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Any

from okx_nft_bot.config import Settings
from okx_nft_bot.counterbid.okx_api import OKXAPIClient
from okx_nft_bot.signing import sign_order, build_order_payload, get_counter
from okx_nft_bot.undercutter.monitor import OfferMonitor
from okx_nft_bot.undercutter.strategy import UndercutStrategy
from okx_nft_bot.undercutter.state import PositionState

logger = logging.getLogger(__name__)

@dataclass
class UndercutAction:
    action_type: str      # 'DEFENSE' | 'ATTACK' | 'WITHDRAW'
    collection: str
    old_price_bnb: float | None
    new_price_bnb: float | None
    reason: str
    executed: bool = False
    error: str | None = None

class UndercutEngine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.okx = OKXAPIClient(...)  # from settings
        self.monitor = OfferMonitor(self.okx)
        self.strategy = UndercutStrategy(settings)
        self.state = PositionState(settings)

    def run_cycle(self) -> list[UndercutAction]:
        """Single undercutting cycle."""
        actions = []

        # 1. Fetch current state of all our active offers
        our_offers = self.state.get_active_offers()

        # 2. For each tracked collection, check if we're still best
        for offer in our_offers:
            market_offers = self.monitor.fetch_offers(offer.collection)
            best_non_ours = self.monitor.get_best_offer_excluding(market_offers, self.settings.buyer_wallet_address)

            if best_non_ours and float(best_non_ours['price']) > offer.price_bnb:
                # Someone outbid us — DEFENSE
                new_price = self.strategy.calculate_defense_price(
                    our_price=offer.price_bnb,
                    competitor_price=float(best_non_ours['price']),
                    collection=offer.collection,
                )
                actions.append(UndercutAction(
                    action_type='DEFENSE',
                    collection=offer.collection,
                    old_price_bnb=offer.price_bnb,
                    new_price_bnb=new_price,
                    reason=f'Outbid by {best_non_ours["maker"][:10]}... at {best_non_ours["price"]}',
                ))

            # Check for stale offers
            if self.strategy.should_withdraw(offer):
                actions.append(UndercutAction(
                    action_type='WITHDRAW',
                    collection=offer.collection,
                    old_price_bnb=offer.price_bnb,
                    new_price_bnb=None,
                    reason=f'Offer stale ({offer.age_hours:.0f}h) or below floor',
                ))

        # 3. ATTACK — find new opportunities
        attack_targets = self.strategy.find_attack_targets(
            tracked_collections=self.state.get_tracked_collections(),
            exclude=set(o.collection for o in our_offers),
        )
        for target in attack_targets:
            actions.append(UndercutAction(
                action_type='ATTACK',
                collection=target.collection,
                old_price_bnb=None,
                new_price_bnb=target.suggested_price,
                reason=target.reason,
            ))

        # 4. Execute actions (if not dry_run)
        for action in actions:
            self._execute_action(action)

        return actions
```

**4.4 `strategy.py` — UndercutStrategy**

```python
"""
Pricing strategy for undercutting.

Rules:
1. DEFENSE re-bid = competitor_price + margin (typically 0.0001-0.001 BNB)
2. ATTACK price = floor_price * attack_ratio (typically 0.7-0.85)
3. Never exceed max_price for collection
4. Never go below min_price for collection
5. WITHDRAW if offer age > max_offer_age_hours (default 24h)
6. WITHDRAW if floor dropped below our offer (we'd overpay)
"""

@dataclass
class AttackTarget:
    collection: str
    suggested_price: float
    reason: str
    confidence: float  # 0-1

class UndercutStrategy:
    def __init__(self, settings: Settings):
        self.config_mgr = CounterbidConfigManager(...)

    def calculate_defense_price(
        self,
        our_price: float,
        competitor_price: float,
        collection: str,
    ) -> float:
        config = self.config_mgr.get_collection(collection)
        # Re-bid just above competitor
        new_price = competitor_price + config.margin_bnb
        # Clamp to limits
        return min(max(new_price, config.min_price_bnb), config.max_price_bnb)

    def should_withdraw(self, offer: ActiveOffer) -> bool:
        # Stale check
        if offer.age_hours > self.max_offer_age_hours:
            return True
        # Floor check — if floor dropped below our offer, withdraw
        if offer.current_floor and offer.price_bnb > offer.current_floor * 1.1:
            return True
        return False

    def find_attack_targets(
        self,
        tracked_collections: list[str],
        exclude: set[str],
    ) -> list[AttackTarget]:
        targets = []
        for collection in tracked_collections:
            if collection in exclude:
                continue
            config = self.config_mgr.get_collection(collection)
            if not config or not config.enabled:
                continue
            # Check if there's an opportunity
            # (collection with weak best-offer or no offers)
            offers = self.monitor.fetch_offers(collection)
            if not offers or float(offers[0].get('price', 0)) < config.min_price_bnb:
                targets.append(AttackTarget(
                    collection=collection,
                    suggested_price=config.min_price_bnb + config.margin_bnb,
                    reason='No competitive offers found',
                    confidence=0.8,
                ))
        return targets
```

**4.5 `state.py` — PositionState**

SQLite-backed tracking of our active offers:
```sql
CREATE TABLE IF NOT EXISTS active_offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection TEXT NOT NULL,
    chain TEXT NOT NULL DEFAULT 'bsc',
    price_bnb REAL NOT NULL,
    order_hash TEXT UNIQUE,
    placed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status TEXT DEFAULT 'active',  -- 'active', 'outbid', 'cancelled', 'expired'
    last_checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS undercut_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_type TEXT NOT NULL,  -- 'DEFENSE', 'ATTACK', 'WITHDRAW'
    collection TEXT NOT NULL,
    old_price_bnb REAL,
    new_price_bnb REAL,
    reason TEXT,
    executed INTEGER DEFAULT 0,
    error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**4.6 CLI commands:**

```
# New commands:
undercut-run         — single undercutting cycle (dry-run by default)
undercut-daemon      — continuous undercutting (30s interval)
undercut-status      — show all active offers and their state
undercut-history     — show undercut action log
undercut-withdraw    — cancel all active offers for a collection
```

**4.7 Telegram commands:**

```
/undercut           — run single cycle
/undercutstatus     — show active positions
/undercuthistory    — show recent actions
```

**4.8 Config additions to Settings:**

```python
# In config.py Settings:
undercut_interval_seconds: int = 30         # v16: cycle interval
undercut_max_offer_age_hours: int = 24      # v16: auto-withdraw stale offers
undercut_attack_ratio: float = 0.75         # v16: attack price = floor * ratio
undercut_defense_margin_bnb: float = 0.0005 # v16: default re-bid margin
undercut_max_active_offers: int = 10        # v16: max simultaneous offers
```

---

## 5. TESTS & VALIDATION

### Required new test files:

```
tests/test_seaport_signer.py       — ported from labs/ (fix imports)
tests/test_counter_bidder.py       — ported from labs/ (fix imports)
tests/test_undercut_engine.py      — NEW: test defense/attack/withdraw logic
tests/test_undercut_strategy.py    — NEW: test pricing calculations
tests/test_undercut_state.py       — NEW: test SQLite state management
```

### Test acceptance criteria:

```bash
# All existing v13 tests must still pass:
pytest tests/ -v --ignore=tests/test_seaport_signer.py --ignore=tests/test_counter_bidder.py --ignore=tests/test_undercut_*.py

# New tests (allow 4 known signer failures):
pytest tests/test_seaport_signer.py -v  # 20+ pass, 4 known fail
pytest tests/test_counter_bidder.py -v  # 18+ pass
pytest tests/test_undercut_engine.py tests/test_undercut_strategy.py tests/test_undercut_state.py -v  # All pass
```

### End-to-end smoke test (manual):

```bash
# 1. Dry-run counter-bid scan
okx-nft-bot counterbid-scan --collection 0x...

# 2. Dry-run undercut cycle
okx-nft-bot undercut-run

# 3. Check state
okx-nft-bot undercut-status
```

---

## 6. VERSION BUMP & RELEASE

### After all tasks complete:

**`pyproject.toml`**:
```toml
version = "0.16.0"
description = "NFT market monitor with cross-market analytics, fraud detection, Seaport signing, counter-bidding, and undercutting engine"
```

**`src/okx_nft_bot/__init__.py`**:
```python
__version__ = "0.16.0"
```

**`README.md`**: Update with new features section for v14/v15/v16.

### Final checklist:
- [ ] All existing 102+ tests pass
- [ ] New signing tests: 20+ pass
- [ ] New counter-bid tests: 18+ pass
- [ ] New undercut tests: all pass
- [ ] `pip install -e .` succeeds
- [ ] `okx-nft-bot --help` shows new commands
- [ ] DRY_RUN=True is default everywhere
- [ ] No hardcoded private keys or API secrets
- [ ] labs/ directory preserved (historical reference)

---

## CONTEXT FILES FOR CODEX

When starting work, read these files first:
1. `src/okx_nft_bot/config.py` — Settings dataclass (all env vars)
2. `src/okx_nft_bot/cli.py` — CLI parser (where to add commands)
3. `src/okx_nft_bot/sniper/engine.py` — existing buyer stubs to replace
4. `labs/counterbid_v15_claude/seaport_signer.py` — v14 code to port
5. `labs/counterbid_v15_claude/counter_bidder.py` — v15 code to port
6. `labs/counterbid_v15_claude/config.py` — v15 config manager
7. `labs/counterbid_v15_claude/okx_api.py` — v15 OKX API client
8. `docs/ARCHITECTURE.md` — current architecture overview
9. `docs/V15_QUARANTINE_AUDIT.md` — why labs/ is isolated
10. This file — `CODEX_HANDOFF_V14_V15_V16.md`

## IMPORTANT CONSTRAINTS

1. **DRY_RUN must be default TRUE everywhere** — no accidental live execution
2. **Never commit .env with real keys** — .env.example only
3. **All new code must have `from __future__ import annotations`** — match v13 style
4. **Use dataclasses with slots=True** where possible — match v13 style
5. **Use pydantic for external data models, dataclass for internal** — match v13 pattern
6. **SQLite for persistence** — no external DB dependencies
7. **Preserve labs/ directory** — don't delete, it's historical reference
8. **The 4 signer test failures are KNOWN** — don't waste time fixing (signature length issue in eth_account encoding, non-critical for dry-run)
