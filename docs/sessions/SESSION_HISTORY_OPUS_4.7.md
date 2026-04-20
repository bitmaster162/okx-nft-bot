# OKX NFT Bot - Session History for Opus 4.7
## Session: Apr 19, 2026

---

## 1. INITIAL STATE

**Containers:**
- okx-nft-bot: Running (healthcheck passing)
- okx-nft-bot-exec: Running  
- okx-nft-bot-telegram: Up 2 hours (unhealthy)

**Configuration:**
- PARASITE_HUNTER_MAX_USD=0.7 (WL collections)
- PARASITE_HUNTER_NONWL_MAX_USD=0.7 (non-WL collections) 
- Price cap unified from 0.54/0.51 to 0.7 USD
- 20% threshold: TEMPORARILY DISABLED (later re-enabled)
- Live ARM: Extended to 120 minutes
- Active offers: 265 (all "live")

**Balance:**
- WBNB: 0.016 (~$10.4)
- USDT: ~$12

---

## 2. PROBLEMS IDENTIFIED

### Problem A: "No nftId" errors
- Collections missing nftId cannot query offers API
- Prevents bot from acting on those collections

### Problem B: "governed_submit_failed"
- Failed submit for collection 0x1b26e0f75c623f at $9.16
- Root cause: Insufficient balance for high-priced offers
- Exposure: $0 (no active offers on that collection)

### Problem C: Bot only showing "KEEP" actions
- Not placing NEW offers
- Existing 265 offers all "winning" (our ≥ enemy)
- No new parasite collections being targeted

### Problem D: Signature verification
- Initially concerned about "Signature verification unsuccessful" errors
- Verification: All signatures working correctly (code=0, successOrderIds returned)

---

## 3. CODE CHANGES MADE

### Change 1: Price Cap Unification (.env)
```
# Before:
PARASITE_HUNTER_MAX_USD=999
PARASITE_HUNTER_NONWL_MAX_USD=0.54

# After:
PARASITE_HUNTER_MAX_USD=0.7
PARASITE_HUNTER_NONWL_MAX_USD=0.7
```

### Change 2: Add New Parasite Wallet (.env)
```
PARASITE_WALLETS=0x8389a3e684d532a3b0b4cdbb72c9c7797e6fa4f7,0xf1771cf8831393422189330a79dd896223c357a4,0x660bfc61854a8cc6fa0bd03b1331fd85709af7dc
```

### Change 3: 20% Threshold Toggle (parasite_hunter.py:1563-1571)
```python
# TEMPORARILY DISABLED:
# if best_enemy is not None:
#     if best_enemy_usd > 0 and our_usd < best_enemy_usd * 0.20:
#         log.info("  🚫 %s: our $%.2f < 20%% of best offer $%.2f — SKIP")

# LATER RE-ENABLED:
if best_enemy is not None:
    if best_enemy_usd > 0 and our_usd < best_enemy_usd * 0.20:
        log.info("  🚫 %s: our $%.2f < 20%% of best offer $%.2f — SKIP")
        return HuntResult(addr, name, chain, len(offers), 0, 0, 1)
```

### Change 4: Live ARM Extension (arm_fix.py)
- Created script to extend live_arm via direct SQLite update
- Updates execution_runtime_state table
- Keys: live_armed_at, live_armed_until, live_armed_by, live_armed_reason

---

## 4. KEY LOG PATTERNS OBSERVED

### Pattern 1: "KEEP" (Existing offers winning)
```
✅ 0xdf1dd618f3b5: our $0.1011 ≥ enemy $0.1006, within 20% of target $0.1011 — KEEP
✅ 0x0439d1868f30: our $0.0050 ≥ enemy $0.0050 — KEEP
```

### Pattern 2: "SKIP" (Too far below market)
```
🚫 CollectionX: our $0.54 < 20% of best offer $242 — SKIP (too far below market)
```

### Pattern 3: "No nftId"
```
⚠️ No nftId for 0xe6013f913eef — cannot query offers API
```

### Pattern 4: Failed submissions
```
Collection: 0x1b26e0f75c623fe9357dbc6c1871ab745faccf04
  Price: $9.1620
  Status: failed
  Reason: governed_submit_failed
  Time: 2026-04-19T14:15:57
```

---

## 5. CRITICAL DISCOVERY

### The "$9 vs $186" Problem
- Collection: 0x1b26e0f75c623fe9357dbc6c1871ab745faccf04
- Floor price: $186.97
- Enemy best offer: $9.13
- Bot wanted to place: $9.16

**User complaint:** "Why are we placing offers ABOVE floor?"
**Reality:** $9.16 is BELOW $186.97, not above

**User's actual concern:** Bot should not chase garbage offers ($9) that are 95% below floor price. These offers will never be accepted.

**Solution:** Keep 20% threshold enabled, but consider adding floor-based filtering (ignore enemy offers < X% of floor).

---

## 6. CR7 COLLECTION ISSUE

**Collection:** 0x45f0385354dc (Cristiano Ronaldo Forever CR7)
- User manually cancelled "a bunch" of offers
- Current active offers: 0
- User states bot was placing offers "above floor" here

**Requires investigation:** Check if bot was placing offers >= floor price on this specific collection.

---

## 7. FILES CREATED

| File | Purpose |
|------|---------|
| arm_fix.py | Extend live ARM via SQLite |
| stats.py | Submit statistics (30 min) |
| check_fail.py | Failed submission details |
| check_exposure.py | Exposure tracking |
| check_cr7.py | CR7 collection stats |

---

## 8. PENDING ISSUES

1. **CR7 Collection:** Bot allegedly placing offers above floor - NEEDS VERIFICATION
2. **"No nftId":** Preventing action on some collections
3. **Balance management:** $9+ offers failing due to insufficient WBNB
4. **Floor-based filtering:** Consider adding guard to ignore parasite offers < 10% of floor

---

## 9. BOT LOGIC SUMMARY

```
1. Scan collections with active parasite offers
2. For each collection:
   a. Find best enemy offer (from PARASITE_WALLETS)
   b. Calculate our price: best_enemy + 0.01, capped at MAX_USD (0.7)
   c. Check 20% threshold: our_offer >= 20% of best_enemy?
   d. Check floor guard: our_offer < floor_price?
   e. Check balance: sufficient for offer?
   f. Submit if all pass
3. Result codes:
   - KEEP: Our existing offer is winning
   - SKIP: Threshold/balance/safety check failed
   - PLACE: New offer submitted
```

---

## 10. USER COMMUNICATION STYLE

- Direct, informal, occasional frustration
- Expects immediate understanding of context
- Prefers concise answers with actionable items
- Uses Russian with technical English terms
- Key phrases: "что по..." (status check), "делай" (action command)

---

**END OF SESSION HISTORY**
**Prepared for: Claude Opus 4.7**
**Date: 2026-04-19**
