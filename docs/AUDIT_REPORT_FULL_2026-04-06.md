# OKX NFT Bot v13 — Full Audit Report

**Date:** 2026-04-06
**Scope:** File integrity, code review, configuration, all fixes verification

---

## 1. Executive Summary

The audit found **9 corrupted/truncated Python files** caused by Google Drive sync issues. All files have been restored from backup. All 11 critical fixes from previous sessions are verified as present. Docker configuration is correct (PYTHONDONTWRITEBYTECODE=1 is set). A startup integrity checker script has been added.

**Current status: 87/87 Python files compile cleanly, 0 issues.**

---

## 2. File Integrity

### Corrupted Files Found & Fixed

| File | Issue | Status |
|------|-------|--------|
| `sniper/parasite_hunter.py` | Truncated 3x (Google Drive sync) | FIXED |
| `counterbid/okx_api.py` | Missing 77 lines, incomplete method | FIXED |
| `telegram_bot.py` | Truncated mid-function | FIXED |
| `cli.py` | 34 null bytes at EOF | FIXED |
| `pipeline/normalize.py` | Missing return statement | FIXED |
| `providers/opensea_marketplace.py` | Missing `_to_int()` body | FIXED |
| `history_backfill.py` | Unclosed dict literal | FIXED |
| `playwright_okx_stream.py` | Incomplete property | FIXED |
| `storage/sqlite.py` | Missing method bodies | FIXED |
| `normalizers/offers.py` | Missing exception handler | FIXED |

### Root Cause

The project lives on Google Drive (`C:\Users\coins\My Drive\...`). Google Drive sync truncates large files during concurrent writes (container running + sync in progress). The `parasite_hunter.py` (3450 lines) was corrupted **3 times** during this session alone.

---

## 3. Critical Fixes Verification

All 11 fixes from previous sessions are confirmed present:

| # | Fix | Status | Location |
|---|-----|--------|----------|
| 1 | DRY_RUN respects env var | PRESENT | `parasite_hunter.py:222` |
| 2 | qty=1 (was 40) | PRESENT | `parasite_hunter.py:1466` |
| 3 | BUSD to USDT mapping | PRESENT | `parasite_hunter.py:1428-1435` |
| 4 | Smart KEEP logic | PRESENT | `parasite_hunter.py:1593-1617` |
| 5 | currencyAddress priority | PRESENT | `parasite_hunter.py:2200-2268` |
| 6 | _find_our_offer fallback | PRESENT | `parasite_hunter.py:2168-2231` |
| 7 | Phase dedup | PRESENT | `parasite_hunter.py:549,642,689-703` |
| 8 | _submit_bsc (governed path) | PRESENT | `parasite_hunter.py:2573-2636` |
| 9 | Phase 0 / WL priority | PRESENT | `parasite_hunter.py:537-582` |
| 10 | create_offer count=1 | PRESENT | `okx_api.py:282,292` |
| 11 | Two-step Seaport flow | PRESENT | `okx_api.py:276,331-383` |

---

## 4. Docker & Config Audit

### compose.yaml
- 3 services: `okx-nft-bot`, `okx-nft-bot-sales-stream`, `okx-nft-bot-telegram`
- Volume mounts: `./src:/app/src`, `./data:/app/data`, `./config:/app/config`
- Restart policy: `unless-stopped`
- Health checks configured for all services

### Dockerfiles
- `PYTHONDONTWRITEBYTECODE=1` — prevents .pyc cache (prevents stale code issues)
- `PYTHONUNBUFFERED=1` — immediate log output
- Base: `python:3.12-slim`, non-root user
- Sales-stream includes Playwright/Chromium

### .env Key Settings
- `PARASITE_HUNTER_ENABLED=1`
- `PARASITE_HUNTER_DRY_RUN=0` (LIVE mode)
- `DRY_RUN=0`
- `AUTO_BUY_DRY_RUN=1` (auto-buy is dry-run)

### .dockerignore / .gitignore
- Both correctly exclude `__pycache__/`, `*.pyc`, `.env`

---

## 5. Project Structure

```
src/okx_nft_bot/
  sniper/
    parasite_hunter.py    (3450 lines) — main bot logic
    buyer.py              (643 lines)  — instant buy logic
    offer_blaster.py      (658 lines)  — mass offer logic
  counterbid/
    okx_api.py            (1619 lines) — OKX API client, Seaport signing
    engine.py             — counterbid engine
  sales_stream.py         (1457 lines) — market data streaming
  telegram_bot.py         (1084 lines) — Telegram control interface
  cli.py                  (1241 lines) — CLI entry points
  signing/seaport_signer.py — EIP-712 signing
  + 75 more files (models, storage, fraud detection, etc.)
```

**Total: 87 Python files, all compiling cleanly.**

---

## 6. Recommendations

### CRITICAL: Move src/ out of Google Drive

Google Drive sync is the #1 source of file corruption. Options:
1. Move `src/` to a local folder outside Google Drive, symlink into project
2. Use Git for source control instead of relying on Google Drive sync
3. At minimum, pause Google Drive sync before editing/restarting

### Add Integrity Check to Container Startup

Script added: `scripts/check_integrity.py`
- Validates all Python files compile
- Checks for null bytes (corruption)
- Checks minimum line counts for critical files (truncation)
- Usage: `python scripts/check_integrity.py --strict`

### Container Restart Procedure

To safely restart after code changes:
```bash
# 1. Delete any stale .pyc (shouldn't exist, but safety)
del src\okx_nft_bot\sniper\__pycache__\*.pyc 2>nul

# 2. Restart (NOT rebuild)
docker compose restart okx-nft-bot-sales-stream

# 3. Verify
docker compose logs --tail=10 okx-nft-bot-sales-stream | findstr "LIVE DRY ParasiteHunter"
```

---

## 7. Verified Bot Operation (from logs)

After fixes applied, bot successfully:
- Started in **LIVE** mode (not DRY RUN)
- Placed **30+ offers** via direct OKX Seaport API
- All offers returned `SUCCESS` with valid offer_ids
- Used both WBNB and USDT currencies on BSC
- qty=1 per offer (within wallet balance)
- Phase 0 KEEP logic working (existing good offers preserved)
- EIP-712 signatures generated and accepted by OKX
