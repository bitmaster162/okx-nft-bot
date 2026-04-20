# Parasite-Killer v14 — Seaport Signer (dry-run)

## Placement in the v13 repo

```
okx_nft_bot_v13/
└── src/
    └── okx_nft_bot/
        ├── __init__.py
        ├── cli.py                    ← add preview-counterbid command here
        ├── http_client.py
        ├── offers.py
        ├── storage.py
        └── seaport_signer.py         ← COPY THIS FILE HERE (v14)
```

Copy `seaport_signer.py` into `src/okx_nft_bot/`.

---

## Install dependencies

```bash
pip install eth-account requests
```

Or use the full requirements file:

```bash
pip install -r requirements_v14.txt
```

---

## .env setup

```env
PRIVATE_KEY=0xYOUR_PRIVATE_KEY_HERE
```

The signer reads `PRIVATE_KEY` from environment or `.env` in the current directory.
**Never hardcode keys.**

---

## CLI usage

### Standalone (during development)
```bash
python seaport_signer.py preview-counterbid \
    --collection 0xCollection... \
    --price 0.05 \
    --chain bsc
```

### After integration into okx_nft_bot
Add to `cli.py`:

```python
from okx_nft_bot.seaport_signer import cli_preview_counterbid

@app.command("preview-counterbid")
def preview_counterbid(
    collection: str = typer.Option(..., help="NFT collection address"),
    price:      float = typer.Option(..., help="Bid price in BNB"),
    chain:      str   = typer.Option("bsc", help="Chain"),
):
    cli_preview_counterbid(collection, price, chain)
```

Then run:
```bash
okx-nft-bot preview-counterbid --collection 0xABC... --price 0.05 --chain bsc
```

---

## Example output

```
============================================================
  PARASITE-KILLER v14  |  preview-counterbid
  *** DRY RUN ONLY — NO REAL SUBMISSION ***
============================================================

[+] Offerer:     0xYourAddress...
[+] Collection:  0xCollectionAddress...
[+] Price:       0.05 BNB  (50000000000000000 wei)
[+] Chain:       BSC  (chainId=56)

[+] Fetching counter from Seaport (https://bsc-dataseed.binance.org/)...
[+] Counter:     7

[+] Signing order (EIP-712)...

── ORDER PAYLOAD ─────────────────────────────────────
{
  "offerer": "0xYourAddress...",
  "zone": "0x0000000000000000000000000000000000000000",
  "offer": [{"itemType": 1, "token": "0xbb4C...", ...}],
  "consideration": [{"itemType": 4, "token": "0xCollection...", ...}],
  "orderType": 0,
  "startTime": "1742600000",
  "endTime": "1742686400",
  ...
}

── SIGNATURE ─────────────────────────────────────────
0xabc123...

── SUMMARY ───────────────────────────────────────────
  Offerer:      0xYourAddress...
  Collection:   0xCollectionAddress...
  Bid amount:   0.05 WBNB
  ...

============================================================
  *** DRY RUN COMPLETE — PAYLOAD NOT SUBMITTED ***
============================================================
```

---

## Running tests

```bash
pytest test_seaport_signer.py -v
```

Tests cover:
- `build_order_payload` — structure, fields, types, amounts, timing
- `sign_order` — hex output, length, determinism, signer recovery
- `get_counter` — mocked RPC, error handling
- Stubs — `submit_order`, `cancel_order` stay as stubs

---

## v14 constraints (enforced in code)

| Flag | Value |
|------|-------|
| `DRY_RUN` | `True` |
| `EXECUTION_ENABLED` | `False` |
| `submit_order()` | Stub only |
| `cancel_order()` | Stub only |

Live execution is **not** implemented and will raise `RuntimeError` if attempted.

---

## What comes next — v15

v15 will add:
- Counter-bidding logic with price limits
- Parasite detection (already fetched via v13 `fetch-offers`)
- Auto-undercut calculation
- Batch offer processing
- (Still dry-run until explicitly enabled)
