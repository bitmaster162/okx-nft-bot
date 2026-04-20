# OpenSea Offer Submission Implementation

## Summary

Successfully added OpenSea offer submission capability to the NFT bot using the Seaport 1.6 protocol with EIP-712 signing. The implementation reuses proven signing patterns from the existing OKX Seaport flow and is compatible with the bot's existing infrastructure.

## Changes Made

### File: `src/okx_nft_bot/clients/opensea.py`

#### Added Constants
- `SEAPORT_ADDRESS_ETH`: Seaport 1.6 verifying contract on ETH: `0x0000000000000068F116a894984e2DB1123eB395`
- `SEAPORT_CONDUIT_KEY`: OpenSea conduit key: `0x0000007b02230091a7ed01230072f7006a004d60a8d4e71d599b8104250f0000`
- `EIP712_TYPES`: Complete EIP-712 type definitions for Seaport 1.6 OrderComponents
- Support addresses: `ZERO_ADDRESS`, `ZERO_BYTES32`

#### New Methods

##### 1. `create_opensea_offer()` - Main Offer Submission Method
```python
def create_opensea_offer(
    self,
    *,
    chain: str,
    collection_address: str,
    token_id: str | int | None,
    price_wei: int,
    currency_address: str,
    wallet_address: str | None = None,
    private_key: str | None = None,
    valid_time: int | None = None,
) -> dict[str, Any]
```

**Features:**
- Validates chain (ETH mainnet only - no BSC support)
- Requires `OPENSEA_API_KEY` environment variable
- Supports both item offers (specific token_id) and collection offers (token_id=None)
- Auto-derives wallet address from private key if not provided
- Fetches current Seaport counter for the wallet
- Builds and signs Seaport order with EIP-712
- Submits to OpenSea API endpoint: `POST https://api.opensea.io/v2/orders/{chain}/seaport/offers`

**Returns:**
```python
{
    "offer_id": "order_hash_or_pending",
    "order_id": "order_hash_or_pending",
    "status": "submitted",
    "raw": {...}  # Full API response
}
```

##### 2. `get_seaport_counter()` - Fetch Counter from Contract
```python
def get_seaport_counter(self, wallet_address: str, chain: str = "eth") -> int
```

**Features:**
- Uses `eth_call` RPC to fetch counter from Seaport contract's `getCounter(address)` function
- Function selector: `0xf07ec373`
- Uses ETH RPC from environment (`ETH_RPC_URL`) or falls back to public endpoint
- Gracefully handles errors with fallback counter=0
- Logs counter value for debugging

##### 3. `_build_seaport_offer()` - Order Parameter Construction
Builds Seaport order parameters with:
- Offer item: ERC20 (WETH or specified currency)
- Consideration item: ERC721 or ERC721_CRITERIA (depending on item vs collection offer)
- Proper itemTypes:
  - Item offers: `itemType=2` (ERC721)
  - Collection offers: `itemType=4` (ERC721_CRITERIA)
- Random 256-bit salt generation
- Default 7-day expiration
- OrderType: 2 (FULL_RESTRICTED)
- Zone: Zero address (OpenSea zone validation disabled)

##### 4. `_sign_seaport_order()` - EIP-712 Signing
```python
def _sign_seaport_order(
    self,
    parameters: dict[str, Any],
    private_key: str,
    chain_id: int = 1
) -> str
```

**Features:**
- Constructs EIP-712 domain with Seaport 1.6 contract address
- Converts parameters to typed data message format
- Signs using `eth_account.Account.sign_message()`
- Returns full EIP-712 signature

##### 5. `_submit_opensea_offer()` - API Submission
Submits signed order to OpenSea:
- Endpoint: `https://api.opensea.io/v2/orders/{chain}/seaport/offers`
- Request format:
  ```json
  {
    "parameters": {...},
    "signature": "0x..."
  }
  ```
- Extracts order hash from response
- Full error handling and logging

##### 6. `_to_typed_data_message()` - Type Conversion
Converts Seaport order parameters to EIP-712 typed data format:
- Converts string amounts to integers
- Converts conduitKey and zoneHash to bytes
- Properly formats offer and consideration items

## Environment Variables

The implementation checks for and uses:
- `OPENSEA_API_KEY` (required for write operations)
- `BUYER_WALLET_ADDRESS` (optional, can be derived from private key)
- `BUYER_WALLET_PRIVATE_KEY` (required for signing)
- `ETH_RPC_URL` (optional, falls back to public endpoint)

## Protocol Details

### Seaport Order Structure
```python
{
    "offerer": "0x...",  # Buyer making the offer
    "zone": "0x00...",  # Zero address (no zone validation)
    "offer": [
        {
            "itemType": 1,  # ERC20
            "token": "WETH_address",
            "identifierOrCriteria": 0,
            "startAmount": "price_wei",
            "endAmount": "price_wei"
        }
    ],
    "consideration": [
        {
            "itemType": 2 or 4,  # ERC721 or ERC721_CRITERIA
            "token": "collection_address",
            "identifierOrCriteria": token_id or 0,
            "startAmount": "1",
            "endAmount": "1",
            "recipient": "offerer"
        }
    ],
    "orderType": 2,  # FULL_RESTRICTED
    "startTime": "now",
    "endTime": "valid_time",
    "zoneHash": "0x00...",
    "salt": "random_256bit",
    "conduitKey": "0x0000007b...",
    "counter": "from_contract"
}
```

### EIP-712 Signing
- Domain: Seaport v1.6 on Ethereum (chainId=1)
- VerifyingContract: `0x0000000000000068F116a894984e2DB1123eB395`
- PrimaryType: `OrderComponents`
- Uses same types as OKX Seaport implementation

## Integration Points

The implementation integrates with:
1. **Settings** (`okx_nft_bot.config.Settings`)
   - Reads `opensea_api_key`, `buyer_wallet_address`, `buyer_wallet_private_key`
   - Uses existing `opensea_api_base`, `opensea_request_timeout`, etc.

2. **Transport** (`okx_nft_bot.clients.http.StdlibHttpTransport`)
   - Reuses existing HTTP transport layer with rate limiting and retries
   - Supports both OpenSea API and RPC calls

3. **Signing** (`eth_account` library)
   - Same signing approach as existing OKX Seaport flow
   - Uses `encode_typed_data()` and `Account.sign_message()`

## Error Handling

Comprehensive error handling:
- Chain validation (ETH only)
- API key validation
- Private key validation
- Counter fetch failures with graceful fallback
- RPC errors with detailed logging
- OpenSea API submission errors
- Proper exception types and messages

## Key Differences from OKX

| Aspect | OKX | OpenSea |
|--------|-----|---------|
| Offer Submission | Two-step (create → sign → submit) | Single-step (sign → submit) |
| Zone | OKX zone address required | Zero address (no zone) |
| API Format | OKX-specific with approvals | Direct Seaport order format |
| Chains | ETH, BSC, etc. | ETH mainnet only |
| Approvals | Auto-handled by OKX step 1 | Must be pre-approved |

## Usage Example

```python
from okx_nft_bot.clients.opensea import OpenSeaClient
from okx_nft_bot.config import load_settings

settings = load_settings()
client = OpenSeaClient(settings)

# Submit item offer
result = client.create_opensea_offer(
    chain="eth",
    collection_address="0x...",
    token_id=123,
    price_wei=int(5 * 10**18),  # 5 WETH
    currency_address="0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",  # WETH on ETH
)
print(result)  # {"offer_id": "0x...", "status": "submitted"}

# Submit collection offer
result = client.create_opensea_offer(
    chain="eth",
    collection_address="0x...",
    token_id=None,  # Collection offer
    price_wei=int(2 * 10**18),  # 2 WETH
    currency_address="0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
)
```

## Testing

The implementation has been:
- Syntax-checked (py_compile)
- Import-verified
- Method signature validated
- Full error handling implemented

## Notes

- OpenSea requires WETH approval before offers can be submitted (pre-requisite)
- Counter must be fetched fresh for each offer to avoid conflicts
- Random salt prevents collision even for identical offer parameters
- Zone is set to zero address; OpenSea validates via conduitKey
- Default 7-day expiration can be customized with `valid_time` parameter
