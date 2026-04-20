# OKX NFT Bot v13 - Complete Configuration Audit Report

## Executive Summary
The bot has **critical database corruption issues** affecting main data stores (okx_nft_bot.sqlite3, execution.sqlite3, sales_stream.sqlite3). Configuration files are generally well-formed and valid. .env settings are properly configured with all required credentials present.

---

## 1. BUY CONFIG (config/buy_config.json)

### Status: HEALTHY
- **Total Collections**: 949
- **Enabled**: 946 (99.7%)
- **Disabled**: 3 (0.3%)
- **Valid Addresses**: 949/949 (100%)
  - All addresses match 0x format with 40 hex characters (42 total)
  - No duplicates found

### Price Limits Analysis
- **Sample max_buy_prices**: 0.0134 - 1.0703 USD
- **Sample max_offer_prices**: 0.0134 - 1.0703 USD
- **Sample low_prices**: 0.01474 - 1.17733 USD
- **Defaults**:
  - ETH: 0.000243 ETH (~$0.51 at $2100/ETH)
  - BSC: 0.00085 BNB (~$0.51 at $600/BNB)

### Notable Items
- **10 collections with very low prices** (< 0.01 USD):
  - Axolittles (0.0092 USD)
  - iNFT Personality Pod (0.0089 USD)
  - Oh Ottie! (0.0084 USD)
  - Art Gobblers (0.0081 USD)
  - tubby cats (0.008 USD)
  - And 5 others
  - **ASSESSMENT**: These may be legitimate micro-cap tokens or stale data

### Offer Settings
- **Enabled**: Yes
- **Max Offers Per Collection**: 3
- **Max Total Offers**: 50
- **Offer Below Floor**: 15%
- **Offer Duration**: 720 hours (30 days)

### Sell Settings
- **Enabled**: Yes
- **Undercut Step**: 50 bps (0.5%)
- **Listing Duration**: 7 days

### Buy Settings
- **Enabled**: Yes
- **Max Buys Per Collection**: 2
- **Max Total Buys Per Cycle**: 5
- **Cooldown**: 60 seconds

---

## 2. .ENV CONFIGURATION

### Status: HEALTHY (All Critical Settings Present)

### API Credentials
| Key | Status | Notes |
|-----|--------|-------|
| OKX_API_KEY | ✓ PRESENT | 36 chars (UUID format) |
| OKX_API_SECRET | ✓ PRESENT | 32 chars |
| OKX_API_PASSPHRASE | ✓ PRESENT | 12 chars |
| OPENSEA_API_KEY | ✗ MISSING | Not configured (expected - not primary market) |
| MAGICEDEN_API_KEY | ✗ MISSING | Not configured (expected - not primary market) |

### Wallet Configuration
| Key | Status | Validity |
|-----|--------|----------|
| BUYER_WALLET_ADDRESS | ✓ PRESENT | Valid 0x address (42 chars) |
| BUYER_WALLET_PRIVATE_KEY | ✓ PRESENT | Valid 0x private key (64 hex chars) |

### Parasite Hunter Settings (v4)
| Setting | Value | Assessment |
|---------|-------|------------|
| ENABLED | 1 | Active |
| DRY_RUN | 0 | **LIVE MODE** (not dry-run) |
| MAX_PER_COLLECTION | 50 | Reasonable limit |
| UNDERCUT_BPS | 50 bps | 0.5% undercut per offer |
| MAX_USD | $0.51 | Per-offer limit |
| DELAY_SECONDS | 1.0 sec | Between offers |
| SCAN_INTERVAL | 300 sec | 5-minute scans |
| COLLECTION_DELAY | 2.0 sec | Between collections |
| CHAINS | bsc, eth | Both chains active |
| OFFER_CURRENCIES | WBNB, WETH, USDT, BUSD, USDC, DAI | 6 currencies |
| NONWL_MAX_USD | $0.10 | Phase 2 limit (non-whitelist) |
| NONWL_QTY | 10 | Max non-WL offers |

### Dry-Run Conflict Check
- **DRY_RUN**: 0 (LIVE)
- **MASS_OFFER_DRY_RUN**: 0 (LIVE)
- **PARASITE_HUNTER_DRY_RUN**: 0 (LIVE)
- **ASSESSMENT**: ✓ No conflicts. All execution modes are synchronized to LIVE (not dry-run)

### Rate Limits
| Endpoint | Rate | Notes |
|----------|------|-------|
| OKX | 1.5 req/sec | Primary market |
| OpenSea | 2.0 req/sec | Secondary |
| MagicEden | 2.0 req/sec | Secondary |

### Database & File Paths
| Path | Status |
|------|--------|
| ./data/okx_nft_bot.sqlite3 | ✓ PRESENT |
| ./data/execution.sqlite3 | ✓ PRESENT |
| ./data/offers.sqlite3 | ✓ PRESENT |
| ./data/backups | ✓ EXISTS (empty) |
| ./config/rule_packs.json | ✓ PRESENT |
| ./config/collections_registry.json | ✓ PRESENT |

### Telegram Configuration
| Setting | Status |
|---------|--------|
| TELEGRAM_BOT_TOKEN | ✓ PRESENT (46 chars) |
| TELEGRAM_CHAT_ID | ✓ PRESENT (932299051) |
| TELEGRAM_ADMIN_CHAT_IDS | ✗ EMPTY (no admin override) |

### RPC URLs
- **SNIPER_RPC_URL**: https://bsc-dataseed.binance.org/ (Binance BSC node)

### Parasite Wallets
- **Total Monitored Wallets**: 45
- **All Valid Format**: ✓ Yes (all 0x addresses, 42 chars)

---

## 3. DATA DIRECTORY AUDIT

### Database Status: CRITICAL CORRUPTION

| Database | Size | Status | Severity |
|----------|------|--------|----------|
| okx_nft_bot.sqlite3 | 1.7 GB | **MALFORMED** | CRITICAL |
| execution.sqlite3 | 68 MB | **MALFORMED** | CRITICAL |
| sales_stream.sqlite3 | 35.2 MB | **CORRUPTED** | CRITICAL |
| offers.sqlite3 | 32 KB | ✓ OK | OK |

**Errors Detected**:
- `okx_nft_bot.sqlite3`: "database disk image is malformed"
- `execution.sqlite3`: "database disk image is malformed"
- `sales_stream.sqlite3`: "*** in database main ***" with 100+ btreeInitPage() errors

**Impact**: These databases likely cannot be read or written without recovery.

### Log Files (10 total)
- **bot.log**: 66.0 MB (current)
- **bot_snapshot.log**: 71.6 MB (current)
- **debug.log**: 0.2 MB (current)
- **debug.log.1**: 1.0 MB (current)
- **Stale test artifacts**: pytest_10m.log, pytest_scheduler_hang.log, test_bsc_maker.log (Mar 30)

### JSON Data Files (8 total)
- **binance_whitelist.json**: 708 items (OK)
- **bsc_missing_collections.json**: 3548 items (OK)
- **bsc_missing_output.json**: 3548 items (OK - possible duplicate)
- **okx_bsc_full.json**: 5551 keys (OK)
- **parasite_bsc_found.json**: 12 keys (OK)
- **parasite_profiles.json**: 2 keys (OK)
- **runtime_metrics.json**: TRUNCATED (line 88 - unterminated JSON)
- **suggested_price_config.json**: 13 keys (OK)

---

## 4. ADDITIONAL CONFIG FILES

### binance_price_config.json
- **Type**: Dictionary (collection price mappings)
- **Size**: 1733 collections
- **Status**: OK

### collections_registry.json
- **Collections**: 2 entries (CR7 collection)
- **Status**: OK

### eth_auto_config.json
- **Collections**: 366 ETH addresses
- **Status**: OK (auto-generated)

### okx_cookies.json
- **Cookies**: 30 browser cookies
- **Status**: OK

### rule_packs.json
- **Enabled Packs**: 3 (high_value_sales, watch_target_collection, cr7_all_events)
- **Status**: OK

### sniper_config.json
- **Targets**: 1 (CR7 - disabled)
- **Status**: OK

### stream_filters.json
- **Global Min/Max**: 0.0001 - 1000.0
- **Min USD**: $0.10
- **Status**: OK

### binance_whitelist.json
- **Total**: 708 collections
- **Networks**: ETH 548, BSC 153, Polygon 7
- **Address Validity**: 100% valid
- **Status**: OK

---

## 5. DEPENDENCIES & REQUIREMENTS

### Python Version
- **Requirement**: >= 3.10

### Core Dependencies
- pydantic >= 2.8, < 3 (validation)
- python-dotenv >= 1.0, < 2 (config)
- curl_cffi >= 0.7, < 1 (HTTP)
- eth-account >= 0.13, < 1 (signing)
- web3 >= 7.0, < 8 (blockchain)
- playwright >= 1.49, < 2 (browser automation)

### Assessment
- All dependencies modern and well-maintained
- Versions pinned to prevent breakage

---

## 6. CRITICAL FINDINGS & RECOMMENDATIONS

### CRITICAL ISSUES

1. **Database Corruption (okx_nft_bot.sqlite3)**
   - Size: 1.7 GB
   - Error: "database disk image is malformed"
   - Path: `/sessions/trusting-sleepy-clarke/mnt/okx_nft_bot_v13/data/okx_nft_bot.sqlite3`
   - Action: Attempt VACUUM recovery or restore from backup

2. **Execution Database Corruption (execution.sqlite3)**
   - Size: 68 MB
   - Error: "database disk image is malformed"
   - Path: `/sessions/trusting-sleepy-clarke/mnt/okx_nft_bot_v13/data/execution.sqlite3`
   - Action: Attempt recovery or reinitialize

3. **Sales Stream Database Corruption (sales_stream.sqlite3)**
   - Size: 35.2 MB
   - Error: btreeInitPage() errors on 100+ pages
   - Path: `/sessions/trusting-sleepy-clarke/mnt/okx_nft_bot_v13/data/sales_stream.sqlite3`
   - Action: Attempt PRAGMA recover or rebuild

4. **runtime_metrics.json Truncation**
   - Issue: File ends abruptly at line 88
   - Path: `/sessions/trusting-sleepy-clarke/mnt/okx_nft_bot_v13/data/runtime_metrics.json`
   - Action: Truncate and reinitialize

### WARNINGS

1. **Parasite Hunter in LIVE Mode** (DRY_RUN=0, PARASITE_HUNTER_DRY_RUN=0)
   - System will submit real offers and may execute transactions
   - Ensure wallet is properly funded

2. **Private Key in .env**
   - Stored in plaintext
   - Recommended for production: Use secrets manager or HSM

3. **Very Low Collection Prices**
   - 10 collections < 0.01 USD
   - Verify these are intentional targets

4. **Stale Test Logs** (3 files from Mar 30)
   - Safe to delete

5. **Duplicate Data Files**
   - bsc_missing_collections.json vs bsc_missing_output.json
   - Both 3548 items - likely duplicates

### HEALTHY FINDINGS

1. Configuration Files: All valid JSON (8/8 OK)
2. Collection Addresses: 100% valid (949/949)
3. Wallet Addresses: Valid format
4. API Credentials: All OKX keys present (3/3)
5. Dependencies: Modern and well-pinned
6. Price Limits: Sensible ranges
7. Parasite Wallet List: 45 valid addresses
8. Data Freshness: Current (Apr 3)

---

## Summary Table

| Category | Status | Items |
|----------|--------|-------|
| Configuration Files | OK | 8/8 |
| Collection Addresses | Valid | 949/949 (100%) |
| Database Integrity | CRITICAL | 3/4 CORRUPTED |
| API Credentials | Present | 3/3 OKX keys |
| Wallet Setup | Valid | Address + Private Key |
| Environment Variables | Complete | 87 vars |
| Dependencies | Sound | 6 core packages |
| Data Freshness | Current | Apr 3 |

---

**Report Generated**: 2026-04-03
**Bot Version**: v13 (v0.16.0)
**Base Path**: /sessions/trusting-sleepy-clarke/mnt/okx_nft_bot_v13/
