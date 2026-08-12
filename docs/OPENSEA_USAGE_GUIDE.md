# OpenSea Offer Submission - Usage Guide

## Quick Start

### Setup Environment Variables

```bash
# Required
export OPENSEA_API_KEY="your_opensea_api_key"
export BUYER_WALLET_PRIVATE_KEY="your_private_key"

# Optional (auto-derived if not set)
export BUYER_WALLET_ADDRESS="0x..."
export ETH_RPC_URL="https://eth-mainnet.g.alchemy.com/v2/your-key"
```

### Basic Usage

```python
from okx_nft_bot.clients.opensea import OpenSeaClient
from okx_nft_bot.config import load_settings

# Initialize client
settings = load_settings()
client = OpenSeaClient(settings)

# Submit item offer (specific token)
result = client.create_opensea_offer(
    chain="eth",
    collection_address="0x...",
    token_id=123,
    price_wei=int(5 * 10**18),  # 5 WETH in wei
    currency_address="0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",  # WETH
)

print(result)
# Output: {
#   "offer_id": "0x...",
#   "order_id": "0x...",
#   "status": "submitted",
#   "raw": {...}
# }
```

## Common Patterns

### Collection Offer (Any Token in Collection)

```python
result = client.create_opensea_offer(
    chain="eth",
    collection_address="0x...",
    token_id=None,  # or "" - explicit absence signals collection offer; 0/"0" is item #0
    price_wei=int(2 * 10**18),  # 2 WETH
    currency_address="0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
)
```

### Custom Expiration Time

```python
import time

# Offer expires in 30 days
expiration = int(time.time()) + (30 * 24 * 3600)

result = client.create_opensea_offer(
    chain="eth",
    collection_address="0x...",
    token_id=456,
    price_wei=int(3 * 10**18),
    currency_address="0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
    valid_time=expiration,
)
```

### Using Different Currency

```python
# USDC instead of WETH
result = client.create_opensea_offer(
    chain="eth",
    collection_address="0x...",
    token_id=789,
    price_wei=int(1500 * 10**6),  # 1500 USDC (6 decimals)
    currency_address="0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # USDC
)
```

### With Explicit Wallet Control

```python
result = client.create_opensea_offer(
    chain="eth",
    collection_address="0x...",
    token_id=999,
    price_wei=int(10 * 10**18),
    currency_address="0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
    wallet_address="0x...",  # Explicit wallet
    private_key="0x...",  # Explicit private key
)
```

## Integration with Parasite Hunter

To add OpenSea support to the sniper's `_submit_eth()` method:

```python
def _submit_eth_opensea(
    self, collection_address: str, token_id: str,
    price: float, currency: str = "WETH",
) -> bool:
    """Use OpenSea API create_opensea_offer for ETH offers."""
    try:
        from okx_nft_bot.clients.opensea import OpenSeaClient

        client = OpenSeaClient(settings=self.settings)

        # Convert price to wei
        decimals = 6 if currency.upper() in ("USDT", "USDC") else 18
        price_wei = int(price * (10 ** decimals))

        # Get currency address
        currency_address = self._get_currency_address(currency, "eth")
        if not currency_address:
            return False

        result = client.create_opensea_offer(
            chain="eth",
            collection_address=collection_address,
            token_id=token_id,
            price_wei=price_wei,
            currency_address=currency_address,
        )

        return result.get("status") == "submitted"
    except Exception as exc:
        log.error("OpenSea offer failed: %s", exc)
        return False
```

## Prerequisite: WETH Approval

Before submitting offers, the wallet must approve WETH for the Seaport contract:

```python
# Using existing approval infrastructure
client._auto_approve_erc20(
    token_address="0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",  # WETH
    spender_address="0x0000000000000068F116a894984e2DB1123eB395",  # Seaport
    private_key=settings.buyer_wallet_private_key,
    chain_id=1,
)
```

Or via web3.py:

```python
from web3 import Web3

# Approve unlimited WETH
w3 = Web3(Web3.HTTPProvider("https://eth-mainnet.example.com"))
# ... build approval transaction ...
```

## Error Handling

The implementation includes comprehensive error handling:

```python
try:
    result = client.create_opensea_offer(...)
except ValueError as e:
    # Chain not supported
    print(f"Invalid chain: {e}")
except RuntimeError as e:
    # Missing API key, private key, counter fetch failed, etc.
    print(f"Configuration error: {e}")
except Exception as e:
    # OpenSea API error, network error, etc.
    print(f"Submission failed: {e}")
```

## Key Methods Reference

### `create_opensea_offer()`
Main method to submit offers. See parameters above.

### `get_seaport_counter(wallet_address, chain="eth")`
Fetch the current Seaport counter for a wallet. Used internally but can be called separately:

```python
counter = client.get_seaport_counter("0x...", chain="eth")
print(f"Current counter: {counter}")
```

## Debugging

Enable debug logging to see detailed request/response information:

```python
import logging
logging.getLogger("clients.opensea").setLevel(logging.DEBUG)
```

Log output includes:
- Seaport counter fetches
- EIP-712 message structure
- Signature generation
- OpenSea API request/response
- Error details with context

## Common Issues

### "OPENSEA_API_KEY not set in environment"
Set the environment variable:
```bash
export OPENSEA_API_KEY="your_key"
```

### "Failed to fetch counter"
The RPC call to Seaport's getCounter failed. Check:
- ETH_RPC_URL is set and working
- Network connectivity
- Seaport contract address is correct

### "Wallet address mismatch"
The provided wallet_address doesn't match the private_key. Either:
- Don't provide wallet_address (auto-derive from key)
- Use matching wallet_address and private_key

### "WETH approval needed"
The wallet doesn't have WETH approved for Seaport. Approve first using the web3 or existing approval infrastructure.

## Differences from OKX Offers

| Feature | OKX | OpenSea |
|---------|-----|---------|
| Submission | Two-step (create + sign) | One-step (sign + submit) |
| Zone Validation | Required (OKX zone) | None (zero address) |
| Approvals | Auto-handled | Must be pre-approved |
| API Key | OKX credentials | OPENSEA_API_KEY |
| Chains Supported | Multiple | ETH only |
| Response Format | OKX-specific | Seaport order hash |

## Performance Notes

- Counter fetching adds ~500ms to first offer
- EIP-712 signing is very fast (~1-2ms)
- OpenSea API submission typically responds within 1-2 seconds
- Total time: ~2-3 seconds per offer

## Testing

To test without submitting to OpenSea:

```python
# Mock the submission
result = client._build_seaport_offer(
    offerer="0x...",
    collection_address="0x...",
    token_id=123,
    price_wei=int(5 * 10**18),
    currency_address="0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
    counter=0,
    valid_time=int(time.time()) + (7 * 24 * 3600),
)
print(f"Order parameters: {result}")

# Sign locally
signature = client._sign_seaport_order(result, "0x...")
print(f"Signature: {signature}")
```

## Support

For issues or questions:
1. Check logs for detailed error messages
2. Verify all environment variables are set
3. Ensure WETH is approved for Seaport
4. Confirm wallet has sufficient WETH balance
5. Test counter fetching separately

## References

- [OpenSea API Documentation](https://docs.opensea.io/)
- [Seaport Protocol](https://github.com/ProjectOpenSea/seaport)
- [EIP-712 Specification](https://eips.ethereum.org/EIPS/eip-712)
