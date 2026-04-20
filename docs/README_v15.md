# Parasite-Killer v15 — Counter-bidder with Limits

## Overview

v15 is a **counter-bidding engine** that detects parasite wallet offers and automatically undercuts them with configurable price limits. It builds on top of v14's `seaport_signer.py` for order construction and signing, and integrates with the OKX Web3 NFT marketplace API.

**Key features:**
- Parasite detection (watches for offers from known bad actors)
- Automatic undercut calculation (parasite price + margin)
- Per-collection price limits (min/max BNB)
- Batch processing (multiple collections per run)
- Dry-run by default (safe; explicit `--submit` flag required to execute)
- SQLite-backed configuration
- Continuous monitoring loop
- OKX API integration with rate limiting (20 req/s)

**Target parasites:**
- Primary: `0x8389a3e684d532a3b0b4cdbb72c9c7797e6fa4f7`
- Secondary: `0xf1771cf8831393422189330a79dd896223c357a4`

---

## Architecture

### Integration with v14 & v13

```
v13 (okx_nft_bot_v13/)
  ├── fetch-offers          ← Fetch existing offers from OKX
  └── seaport_signer.py     ← (v14) Build + sign Seaport orders

v15 (counter_bidder/)
  ├── counter_bidder.py     ← Main logic: detect parasite, calculate counter price
  ├── config.py             ← SQLite collection config storage
  ├── okx_api.py            ← OKX Web3 API client (auth, rate limiting)
  ├── cli.py                ← CLI commands
  └── test_counter_bidder.py ← Unit tests (mocked)
```

**Dependencies:**
- `seaport_signer.py` from v14 (imported for `build_order_payload`, `sign_order`, `get_counter`)
- OKX Web3 API (base: `web3.okx.com`, endpoint: `/api/v5/mktplace/nft/markets/offers`)
- BSC RPC (default: `https://bsc-dataseed.binance.org/`)

---

## Installation

### 1. Clone and dependencies

```bash
# Install dependencies
pip install -r requirements_v15.txt
```

**Or individually:**
```bash
pip install eth-account requests curl_cffi pytest
```

### 2. Environment setup

Create a `.env` file in the working directory:

```env
# Private key for signing offers (NOT committing; never hardcode)
PRIVATE_KEY=0xYOUR_PRIVATE_KEY_HERE

# OKX Web3 API credentials
OK_ACCESS_KEY=your_api_key
OK_ACCESS_SECRET=your_api_secret
OK_ACCESS_PASSPHRASE=your_passphrase
```

**Do NOT commit `.env`** — add it to `.gitignore`:
```bash
echo ".env" >> .gitignore
```

### 3. Copy v14 signer

Copy `seaport_signer.py` from v14 into the v15 directory:

```bash
cp ../v14/seaport_signer.py .
```

Or use it as a module if v14 is in the Python path.

---

## CLI Commands

### Configuration Management

#### Add a collection
```bash
python -m cli add-collection \
  --address 0xCollectionAddress \
  --chain bsc \
  --min-price 0.01 \
  --max-price 10.0 \
  --margin 0.001
```

**Parameters:**
- `--address`: NFT contract address
- `--chain`: blockchain (default: `bsc`)
- `--min-price`: minimum acceptable offer (BNB)
- `--max-price`: maximum acceptable offer (BNB)
- `--margin`: undercut margin above parasite (BNB)

Example: If parasite offers 0.5 BNB with margin 0.001, our counter-bid is 0.501 BNB.

#### List collections
```bash
python -m cli list-collections
python -m cli list-collections --chain eth
```

#### Remove a collection
```bash
python -m cli remove-collection --address 0xCollectionAddress
```

---

### Counter-bidding

#### Single collection (dry-run)
```bash
python -m cli counterbid \
  --collection 0xCollectionAddress \
  --chain bsc
```

Example output:
```
── ANALYSIS ──────────────────────────────────────────────────────
Parasite offer:  0.500000 BNB
Counter bid:     0.501000 BNB
Reason:          Parasite 0.500000 + margin 0.001000
Valid:           ✓ YES
```

#### Submit (requires explicit flag)
```bash
python -m cli counterbid \
  --collection 0xCollectionAddress \
  --chain bsc \
  --submit
```

#### All enabled collections (dry-run)
```bash
python -m cli counterbid-all --chain bsc
```

#### Submit all
```bash
python -m cli counterbid-all --chain bsc --submit
```

---

### Monitoring

#### Continuous loop (every 5 min, dry-run)
```bash
python -m cli monitor --chain bsc --interval 300
```

#### With submission enabled
```bash
python -m cli monitor --chain bsc --interval 300 --submit
```

#### Max iterations
```bash
python -m cli monitor --chain bsc --max-iterations 10
```

Stops automatically after 10 checks.

---

## Configuration Database

v15 uses SQLite to persist collection settings:

**File:** `parasite_killer_v15.db` (auto-created)

**Schema:**
```sql
CREATE TABLE collections (
    address           TEXT PRIMARY KEY,     -- 0x...
    chain             TEXT NOT NULL,         -- bsc, eth, ...
    min_price_bnb     REAL NOT NULL,         -- e.g., 0.01
    max_price_bnb     REAL NOT NULL,         -- e.g., 10.0
    margin_bnb        REAL NOT NULL,         -- e.g., 0.001
    enabled           INTEGER NOT NULL DEFAULT 1,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Example:**
```sqlite
INSERT INTO collections VALUES
('0xabc123', 'bsc', 0.01, 10.0, 0.001, 1, ..., ...);
```

---

## Counter-bidding Logic

### Flow

1. **Fetch offers** → OKX API: `GET /api/v5/mktplace/nft/markets/offers?collection=...`
2. **Detect parasite** → Check if any offer is from a known parasite wallet
3. **Calculate counter price** → `max(parasite + margin, min_price)`, clamp to `[min, max]`
4. **Validate** → Ensure price is within limits and above parasite (if exists)
5. **Sign** → Use v14's `seaport_signer.sign_order()` to create EIP-712 signature
6. **Submit** → POST to OKX API (only if `dry_run=False`)

### Price Calculation

**Case 1: Parasite exists**
```
counter_price = max(parasite_price + margin, min_price)
counter_price = min(counter_price, max_price)
```

**Case 2: No parasite**
```
counter_price = min_price
```

**Example:**
- Parasite: 0.5 BNB
- Margin: 0.001 BNB
- Min: 0.01 BNB
- Max: 10.0 BNB
- → Counter: 0.501 BNB (within limits, above parasite)

### Validation

A counter-bid is **invalid** if:
- Collection not in config
- Collection is disabled
- OKX API fetch fails
- Counter price < min_price
- Counter price > max_price
- Counter price <= parasite price (if parasite exists)

---

## Code Structure

### `config.py`
**CollectionConfig** dataclass and **ConfigManager** for SQLite operations.

Key methods:
- `add_collection()` — Register a new collection
- `get_collection()` — Fetch by address
- `list_collections()` — Filter by chain or enabled status
- `remove_collection()` — Delete
- `enable_collection()` / `disable_collection()`

### `okx_api.py`
**OKXAPIClient** for marketplace communication.

Key methods:
- `fetch_offers(collection, chain, maker, limit)` — Get existing offers
- `submit_offer(signed_payload, dry_run=True)` — Post signed order
- `cancel_offer(order_id, dry_run=True)` — Cancel an order

Rate limiting: 20 req/s (enforced automatically).

### `counter_bidder.py`
**CounterBidder** — main engine.

Key methods:
- `detect_parasite_offers(offers)` — Find best parasite offer
- `calculate_counter_price(config, parasite_price)` — Auto-undercut logic
- `validate_counter_bid(config, counter_price, parasite_price)` — Validation
- `build_signed_counter_bid(collection, price_bnb, counter)` — Sign with v14
- `process_single_collection()` — Full flow for one collection
- `process_batch()` — Batch processing
- `submit_batch()` — Submit all signed orders

**Data classes:**
- `CounterBidTask` — Result of single collection processing
- `BatchResult` — Result of batch processing

### `cli.py`
Command-line interface (argparse).

Commands:
- `add-collection`, `list-collections`, `remove-collection`
- `counterbid`, `counterbid-all`
- `monitor`

---

## Testing

Run the test suite:

```bash
pytest test_counter_bidder.py -v
```

**Test coverage:**
- ConfigManager: add, get, list, remove, enable/disable
- CounterBidder: parasite detection, price calculation, validation
- Integration: single and batch processing

**Mocking:**
- `ConfigManager` uses temp SQLite DB per test
- `OKXAPIClient` is fully mocked (no real API calls)
- `seaport_signer` functions are patched (no real signing)

---

## Example Workflow

### Setup (one-time)

```bash
# 1. Create .env
cat > .env << 'EOF'
PRIVATE_KEY=0x...
OK_ACCESS_KEY=...
OK_ACCESS_SECRET=...
OK_ACCESS_PASSPHRASE=...
EOF

# 2. Add collections
python -m cli add-collection \
  --address 0xABC... \
  --chain bsc \
  --min-price 0.01 \
  --max-price 10.0 \
  --margin 0.001

python -m cli add-collection \
  --address 0xDEF... \
  --chain bsc \
  --min-price 0.05 \
  --max-price 20.0 \
  --margin 0.0005

# 3. Verify
python -m cli list-collections
```

### Daily operations

```bash
# Option A: One-off counter-bid (dry-run)
python -m cli counterbid --collection 0xABC... --chain bsc

# Option B: Batch counter-bid (all collections, dry-run)
python -m cli counterbid-all --chain bsc

# Option C: Continuous monitor (every 5 min, dry-run)
python -m cli monitor --chain bsc --interval 300

# Once validated, enable submission:
python -m cli monitor --chain bsc --interval 300 --submit
```

---

## Safety & Constraints

### DRY-RUN by Default

**All commands default to dry-run mode.** No real bids are submitted unless you explicitly pass `--submit`.

```bash
# Dry-run (shows what WOULD happen)
python -m cli counterbid-all --chain bsc

# Actually submit (requires explicit flag)
python -m cli counterbid-all --chain bsc --submit
```

### Private Key Security

- Never hardcode private keys in code
- Load from `.env` (not committed to git)
- Validate `.gitignore` includes `.env`

### OKX API Credentials

- Store in `.env`
- Never commit credentials
- Use `OK_ACCESS_KEY`, `OK_ACCESS_SECRET`, `OK_ACCESS_PASSPHRASE`

### Rate Limiting

- OKX enforces 20 req/s
- v15 auto-manages this via `RateLimiter` class
- No manual throttling needed

---

## Troubleshooting

### "PRIVATE_KEY not found"
```bash
Check .env file exists and has:
PRIVATE_KEY=0x...
```

### "OKX API credentials missing"
```bash
Check .env has:
OK_ACCESS_KEY=...
OK_ACCESS_SECRET=...
OK_ACCESS_PASSPHRASE=...
```

### "Collection not found in config"
```bash
# Add it first:
python -m cli add-collection --address 0x... --chain bsc --min-price ... --max-price ... --margin ...
```

### "seaport_signer not available"
```bash
# Copy v14's seaport_signer.py to v15 directory:
cp ../v14/seaport_signer.py .
```

### Tests fail
```bash
# Ensure pytest is installed:
pip install -r requirements_v15.txt

# Run with verbose output:
pytest test_counter_bidder.py -v -s
```

---

## What's Next — v16

Potential enhancements:
- Persistent order tracking (which offers were submitted when)
- Metrics & dashboards (success rate, avg undercut %, etc.)
- Advanced parasite detection (wallet behavior learning)
- Multi-chain support (currently BSC focus)
- Slack/email notifications
- WebSocket feed for real-time updates

---

## References

- **v14**: `seaport_signer.py` — Order building & signing
- **v13**: Offer fetching logic (reusable)
- **OKX API**: https://www.okx.com/api/v5/mktplace/nft/
- **Seaport v1.5**: https://github.com/ProjectOpenSea/seaport
- **EIP-712**: https://eips.ethereum.org/EIPS/eip-712
- **BSC**: https://www.binance.org/en/smartChain

---

## License

Parasite-Killer v15 (Counter-bidder with limits).
For internal use only.
