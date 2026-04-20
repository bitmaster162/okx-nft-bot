# Parasite-Killer v15 — START HERE

**Status:** ✓ Complete & Ready for Integration
**Build Date:** 2026-03-22
**Location:** `/v15/`

---

## What Is This?

Parasite-Killer v15 is a **counter-bidder** that automatically detects parasite wallet offers and undercuts them with configurable price limits. Built on top of v14's Seaport signer.

**Key insight:** When a known bad actor (parasite) bids 0.5 BNB, v15 bids 0.501 BNB to beat them, respecting your min/max price limits.

---

## Files Overview

| File | Purpose | Status |
|------|---------|--------|
| `config.py` | Collection configuration (SQLite) | ✓ Ready |
| `okx_api.py` | OKX API client with auth & rate limit | ✓ Ready |
| `counter_bidder.py` | Counter-bidding logic & parasite detection | ✓ Ready |
| `cli.py` | Command-line interface (6 commands) | ✓ Ready |
| `test_counter_bidder.py` | 18 unit tests (mocked API) | ✓ Ready |
| `requirements_v15.txt` | Dependencies | ✓ Ready |
| `README_v15.md` | Full documentation (450+ lines) | ✓ Ready |
| `QUICKSTART.md` | 5-minute setup guide | ✓ Ready |
| `BUILD_SUMMARY.md` | Build report & technical details | ✓ Ready |

**Total:** 2,846 lines of code, tests, and documentation

---

## Quick Start (5 minutes)

### 1. Copy v14 signer
```bash
cp ../v14/seaport_signer.py .
```

### 2. Install dependencies
```bash
pip install -r requirements_v15.txt
```

### 3. Create `.env`
```bash
cat > .env << 'EOF'
PRIVATE_KEY=0xYOUR_KEY
OK_ACCESS_KEY=your_key
OK_ACCESS_SECRET=your_secret
OK_ACCESS_PASSPHRASE=your_passphrase
