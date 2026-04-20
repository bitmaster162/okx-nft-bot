# Parasite-Killer v15 — Quick Start

Get up and running in 5 minutes.

---

## 1. Install & Setup (2 min)

### Copy seaport_signer from v14
```bash
cp ../v14/seaport_signer.py .
```

### Install dependencies
```bash
pip install -r requirements_v15.txt
```

### Create .env
```bash
cat > .env << 'EOF'
PRIVATE_KEY=0xYOUR_PRIVATE_KEY_HERE
OK_ACCESS_KEY=your_okx_api_key
OK_ACCESS_SECRET=your_okx_api_secret
OK_ACCESS_PASSPHRASE=your_okx_passphrase
EOF
```

**Never commit `.env`:**
```bash
echo ".env" >> .gitignore
```

---

## 2. Add a Collection (1 min)

```bash
python -m cli add-collection \
  --address 0xCollection_Address_Here \
  --chain bsc \
  --min-price 0.01 \
  --max-price 10.0 \
  --margin 0.001
```

**Example:**
```bash
python -m cli add-collection \
  --address 0x1234567890123456789012345678901234567890 \
  --chain bsc \
  --min-price 0.01 \
  --max-price 10.0 \
  --margin 0.001
```

### What these mean:
- `--min-price 0.01` → Never bid less than 0.01 BNB
- `--max-price 10.0` → Never bid more than 10.0 BNB
- `--margin 0.001` → If parasite bids 0.5, we bid 0.501 (0.5 + 0.001)

### Verify
```bash
python -m cli list-collections
```

---

## 3. Test (Dry-Run) (1 min)

### Single collection
```bash
python -m cli counterbid --collection 0x1234... --chain bsc
```

Output:
```
── ANALYSIS ──────────────────────────────────────────────────────
Parasite offer:  0.500000 BNB
Counter bid:     0.501000 BNB
Reason:          Parasite 0.500000 + margin 0.001000
Valid:           ✓ YES
```

### All collections
```bash
python -m cli counterbid-all --chain bsc
```

**Nothing is submitted yet** — these are dry-runs.

---

## 4. Go Live (1 min)

### Option A: Single counter-bid
```bash
python -m cli counterbid --collection 0x1234... --chain bsc --submit
```

### Option B: All collections
```bash
python -m cli counterbid-all --chain bsc --submit
```

### Option C: Continuous monitor (every 5 min)
```bash
python -m cli monitor --chain bsc --interval 300 --submit
```

Stop with `Ctrl+C`.

---

## Common Tasks

### Add multiple collections
```bash
# Collection 1
python -m cli add-collection \
  --address 0x1111... --chain bsc \
  --min-price 0.01 --max-price 10.0 --margin 0.001

# Collection 2
python -m cli add-collection \
  --address 0x2222... --chain bsc \
  --min-price 0.05 --max-price 20.0 --margin 0.005

# Collection 3
python -m cli add-collection \
  --address 0x3333... --chain bsc \
  --min-price 0.001 --max-price 50.0 --margin 0.0001
```

### List all
```bash
python -m cli list-collections
```

### Disable a collection (don't monitor)
```bash
# (via SQL or config.py API — CLI doesn't have disable yet, but you can remove/re-add)
python -m cli remove-collection --address 0x1234...
```

### Run tests
```bash
pytest test_counter_bidder.py -v
```

---

## How It Works (Simple Explanation)

1. **Fetch** → v15 asks OKX: "What offers exist for collection X?"
2. **Detect** → v15 checks if any are from parasite wallets
3. **Calculate** → v15 figures out: "If parasite bid 0.5, we bid 0.501"
4. **Validate** → v15 checks: "Is 0.501 within our 0.01-10.0 range? Yes!"
5. **Sign** → v15 uses your private key to sign the order (locally, stays secure)
6. **Submit** → v15 sends the signed order to OKX (only with `--submit`)

---

## Example: Real Workflow

### Day 1: Setup
```bash
# Install
pip install -r requirements_v15.txt

# Copy v14 signer
cp ../v14/seaport_signer.py .

# Create .env
cat > .env << 'EOF'
PRIVATE_KEY=0x...
OK_ACCESS_KEY=...
OK_ACCESS_SECRET=...
OK_ACCESS_PASSPHRASE=...
EOF

# Add 3 collections
python -m cli add-collection --address 0x111... --chain bsc --min-price 0.01 --max-price 10.0 --margin 0.001
python -m cli add-collection --address 0x222... --chain bsc --min-price 0.05 --max-price 20.0 --margin 0.005
python -m cli add-collection --address 0x333... --chain bsc --min-price 0.001 --max-price 50.0 --margin 0.0001

# List to verify
python -m cli list-collections
```

### Day 2: Test dry-run
```bash
# Batch dry-run (check what would happen)
python -m cli counterbid-all --chain bsc

# Output shows:
# - Which collections have parasite offers
# - What we WOULD bid
# - Validation results
```

### Day 3: Go live
```bash
# Start monitoring (every 5 min, submit for real)
python -m cli monitor --chain bsc --interval 300 --submit
```

Output (continuous):
```
[1] 2026-03-22 10:00:00 — Starting batch process
    Valid: 2 | Submitted: 2
    Waiting 300s before next check...

[2] 2026-03-22 10:05:00 — Starting batch process
    Valid: 1 | Submitted: 1
    Waiting 300s before next check...
```

---

## Troubleshooting

### "PRIVATE_KEY not found"
```bash
# Check .env exists:
cat .env | grep PRIVATE_KEY

# If not, create it:
echo "PRIVATE_KEY=0x..." >> .env
```

### "OKX API credentials missing"
```bash
# Check .env has all 3:
cat .env | grep OK_ACCESS
# Should show: OK_ACCESS_KEY, OK_ACCESS_SECRET, OK_ACCESS_PASSPHRASE
```

### "Collection not found"
```bash
# Check it's registered:
python -m cli list-collections

# If not, add it:
python -m cli add-collection --address 0x... --chain bsc --min-price 0.01 --max-price 10.0 --margin 0.001
```

### "seaport_signer not available"
```bash
# Copy v14 file:
cp ../v14/seaport_signer.py .
```

### Tests fail
```bash
# Ensure pytest installed:
pip install pytest

# Run with details:
pytest test_counter_bidder.py -v -s
```

---

## Key Concepts

| Term | Meaning |
|------|---------|
| Parasite | Known bad-actor wallet that we want to undercut |
| Undercut | We bid slightly higher than parasite (e.g., 0.501 vs 0.500) |
| Margin | How much above parasite we bid (e.g., 0.001 BNB) |
| Min price | Don't bid less than this (safety floor) |
| Max price | Don't bid more than this (safety ceiling) |
| Dry-run | Show what WOULD happen, don't actually submit |
| Submit | Go live; actually send bids to OKX |

---

## Safety Reminders

✓ **Dry-run by default** — Commands show what WOULD happen unless you add `--submit`

✓ **Price limits** — Bids are clamped to `[min_price, max_price]` per collection

✓ **Private key security** — Never hardcode; load from `.env` (add to `.gitignore`)

✓ **Parasite detection** — Only 2 hardcoded parasite wallets; undercuts them only

✓ **Rate limiting** — Automatic (20 req/s); no manual throttling needed

---

## Next Steps

1. **Review full docs:** `README_v15.md`
2. **Run tests:** `pytest test_counter_bidder.py -v`
3. **Set up multiple collections** and monitor continuously
4. **Track results** (v16 will have better metrics)

---

## Support

- **Dry-run not working?** Check `.env` exists
- **Submission not working?** Add `--submit` flag
- **Parasite not detected?** Ensure OKX API credentials are correct
- **Price validation failing?** Review min/max in config
- **Still stuck?** See `README_v15.md` troubleshooting section

Good luck! 🚀
