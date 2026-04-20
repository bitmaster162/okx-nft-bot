# CODEX TASK: Per-Item Mass Offer Module (Parasite Killer v17+)

## Context

The bot already has:
- **Collection-level offers** via Seaport: `src/okx_nft_bot/signing/seaport_signer.py` → `build_order_payload()` creates collection offers using `ERC721_CRITERIA` with `identifierOrCriteria=0`
- **Submit offer**: `src/okx_nft_bot/counterbid/okx_api.py` → `submit_offer()` POSTs to `/api/v5/mktplace/nft/markets/offers`
- **Cancel offer**: same file → `cancel_offer()`
- **OKX client**: `src/okx_nft_bot/clients/okx.py` has `get_nft_list()`, `get_nft_details()`, `get_listings()`, `get_offers()`
- **Seaport signing**: full EIP-712 signing with private key in `signing/seaport_signer.py`
- **Config**: `src/okx_nft_bot/config.py` with Settings dataclass, .env loading

## Problem

Collection offers are bad for collections with mixed rarities (e.g. CR7 NFT Collection has N, R, SR, SSR tiers). A collection offer gets filled by the cheapest (N-tier) NFTs. We need **per-item offers** targeting specific NFTs by tokenId.

Also: many NFTs are unlisted ("Не выставлено"). Sending lowball per-item offers to unlisted NFT owners can result in cheap acquisitions.

## Requirements

### 1. Per-Item Offer Builder (`signing/seaport_signer.py`)

Create `build_per_item_offer()` alongside existing `build_order_payload()`:

```python
def build_per_item_offer(
    offerer: str,
    collection: str,      # contract address
    token_id: int,        # specific token ID
    price_wei: int,
    counter: int,
    duration_s: int = 86_400,
    ...
) -> dict[str, Any]:
```

Key difference from collection offer:
- `consideration[0].itemType` = `ItemType.ERC721` (value 2) instead of `ERC721_CRITERIA` (value 4)
- `consideration[0].identifierOrCriteria` = `token_id` instead of `0`

Everything else (signing, Seaport contract, WBNB, etc.) stays the same.

### 2. Mass Offer Engine (`src/okx_nft_bot/mass_offer/engine.py`)

New module that:

a) **Scans collection** — calls `get_nft_list()` to enumerate all NFTs in a collection, paginating through all pages.

b) **Filters targets** by configurable criteria:
   - `rarity_filter`: list of rarity tiers to target (e.g. ["R", "SR", "SSR"]) — check traits/attributes
   - `unlisted_only`: bool — only target NFTs not currently listed for sale
   - `exclude_own`: bool — skip NFTs owned by our wallet
   - `max_existing_offer`: float — skip NFTs where an active offer already exceeds this price
   - `min_token_id` / `max_token_id`: optional range filter

c) **Sends per-item offers** on each target:
   - Builds Seaport order with `build_per_item_offer()`
   - Signs with buyer wallet private key
   - Submits via `okx_api.submit_offer()`
   - Rate limiting: configurable delay between offers (default 2s)
   - Tracks submitted offers in SQLite

d) **Dry-run mode** — preview what would be sent without submitting

e) **Configurable via .env**:
   ```
   MASS_OFFER_PRICE_BNB=0.01
   MASS_OFFER_DURATION_HOURS=24
   MASS_OFFER_DELAY_SECONDS=2
   MASS_OFFER_MAX_TOTAL=100
   MASS_OFFER_DRY_RUN=true
   ```

### 3. CLI Commands (`cli.py`)

Add subcommands:

```
okx-nft-bot mass-offer \
  --collection 0x102a35917e9f2ff08ffc5dc4fe3e5a400e4f33a7 \
  --chain bsc \
  --price 0.01 \
  --rarity R,SR,SSR \
  --unlisted-only \
  --max-offers 50 \
  --dry-run

okx-nft-bot mass-offer-status   # show active mass offers
okx-nft-bot mass-offer-cancel   # cancel all active per-item offers
```

### 4. Telegram Commands (`telegram_bot.py`)

- `/massoffer <collection> <price> [rarity_filter]` — start mass offer campaign
- `/massofferstatus` — show campaign progress
- `/massoffercancel` — cancel all

### 5. Tests

Add tests in `tests/` covering:
- `build_per_item_offer()` generates correct Seaport payload with ERC721 itemType
- Mass offer engine filters correctly (rarity, unlisted, exclude own)
- Dry-run mode doesn't submit
- Rate limiting works

## Key Files to Modify

- `src/okx_nft_bot/signing/seaport_signer.py` — add `build_per_item_offer()`
- `src/okx_nft_bot/clients/okx.py` — may need pagination helper for `get_nft_list()`
- `src/okx_nft_bot/counterbid/okx_api.py` — reuse `submit_offer()` and `cancel_offer()`
- `src/okx_nft_bot/config.py` — add MASS_OFFER_* settings
- `src/okx_nft_bot/cli.py` — add mass-offer subcommands
- `src/okx_nft_bot/telegram_bot.py` — add /massoffer commands

## New Files to Create

- `src/okx_nft_bot/mass_offer/__init__.py`
- `src/okx_nft_bot/mass_offer/engine.py`
- `src/okx_nft_bot/mass_offer/scanner.py` — NFT enumeration + filtering
- `src/okx_nft_bot/mass_offer/tracker.py` — SQLite tracking of sent offers
- `tests/test_mass_offer.py`
- `tests/test_per_item_seaport.py`

## Important Notes

- The bot runs on BSC (BNB Smart Chain). WBNB address: `0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c`
- Seaport contract and conduit key are already defined in `seaport_signer.py`
- All offer amounts are in WBNB (ERC20), NOT native BNB
- OKX API auth uses HMAC-SHA256 signing — see `sign_okx_request()` in `clients/okx.py`
- Buyer wallet is configured via `BUYER_WALLET_ADDRESS` and `BUYER_WALLET_PRIVATE_KEY` in .env
- `DRY_RUN=true` by default — must explicitly set to false for live execution
- Follow existing code patterns (logging, error handling, Settings dataclass)
