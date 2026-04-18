from __future__ import annotations

from eth_account import Account

from okx_nft_bot.signing.seaport_signer import (
    CONDUIT_KEY,
    ItemType,
    OrderType,
    WBNB_ADDRESS,
    build_per_item_offer,
)

TEST_PRIVATE_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
TEST_ACCOUNT = Account.from_key(TEST_PRIVATE_KEY)
TEST_COLLECTION = "0x1234567890123456789012345678901234567890"


def test_build_per_item_offer_uses_specific_erc721_token() -> None:
    payload = build_per_item_offer(
        offerer=TEST_ACCOUNT.address,
        collection=TEST_COLLECTION,
        token_id=321,
        price_wei=10**16,
        counter=9,
        salt=123,
    )

    assert payload["offerer"] == TEST_ACCOUNT.address
    assert payload["conduitKey"] == CONDUIT_KEY
    assert int(payload["orderType"]) == int(OrderType.FULL_RESTRICTED)
    assert payload["offer"][0]["token"] == WBNB_ADDRESS
    assert int(payload["offer"][0]["itemType"]) == int(ItemType.ERC20)
    assert payload["consideration"][0]["token"] == TEST_COLLECTION
    assert int(payload["consideration"][0]["itemType"]) == int(ItemType.ERC721)
    assert int(payload["consideration"][0]["identifierOrCriteria"]) == 321
