from __future__ import annotations

from typing import Any


def _chain_name(chain_id: int) -> str:
    return {
        56: "bsc",
        1: "eth",
        137: "polygon",
        42161: "arbitrum",
    }.get(chain_id, "bsc")


def _approval_governor(
    client: Any,
    *,
    action_type: str,
    subject_address: str,
    chain_name: str,
):
    """Return a live-approved governor immediately before nonce reservation.

    Approval transactions expand exchange/contract permissions, so they are
    ordinary live execution effects rather than safety-cancel effects. They must
    therefore respect the same dry-run, force-dry, killswitch, live-arm, health,
    and shared rate gates as other live submissions.
    """
    from okx_nft_bot.execution_governor import ExecutionGovernor

    governor = ExecutionGovernor(settings=client.settings)
    blocked = governor.check_live_submit_allowed(
        action_type=action_type,
        collection=str(subject_address or "").lower(),
        chain=chain_name,
        price_bnb=0.0,
    )
    if blocked:
        raise RuntimeError(f"{action_type.lower()} blocked: {blocked}")
    return governor


def _auto_approve_erc20(
    self: Any,
    *,
    token_address: str,
    spender_address: str,
    private_key: str,
    chain_id: int,
) -> str:
    """Guarded ERC20 approve(spender, maxUint256) on-chain."""
    from eth_account import Account
    from web3 import Web3

    chain_name = _chain_name(chain_id)
    rpc_url = self._primary_rpc(chain_name)
    w3 = Web3(Web3.HTTPProvider(rpc_url))

    erc20_abi = [
        {
            "constant": False,
            "inputs": [
                {"name": "spender", "type": "address"},
                {"name": "amount", "type": "uint256"},
            ],
            "name": "approve",
            "outputs": [{"name": "", "type": "bool"}],
            "type": "function",
        }
    ]

    token_contract = w3.eth.contract(
        address=Web3.to_checksum_address(token_address),
        abi=erc20_abi,
    )
    account = Account.from_key(private_key)
    max_uint256 = 2**256 - 1

    # Finish the external gas-price read before the final execution gate. The
    # shared wallet nonce is reserved only after this gate passes.
    gas_price = w3.eth.gas_price
    governor = _approval_governor(
        self,
        action_type="LIVE_APPROVE_ERC20",
        subject_address=token_address,
        chain_name=chain_name,
    )
    nonce = governor.allocate_nonce(account.address, chain_name)

    tx = token_contract.functions.approve(
        Web3.to_checksum_address(spender_address),
        max_uint256,
    ).build_transaction(
        {
            "from": account.address,
            "nonce": nonce,
            "gasPrice": int(gas_price * 1.1),
            "gas": 60_000,
            "chainId": chain_id,
        }
    )

    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    tx_hex = tx_hash.hex()

    import logging

    log = logging.getLogger("counterbid.okx_api")
    log.info(
        "auto_approve_erc20: %s approve(%s) tx=%s — waiting confirmation...",
        token_address[:14],
        spender_address[:14],
        tx_hex,
    )

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    if receipt["status"] != 1:
        raise RuntimeError(f"approve tx reverted: {tx_hex}")

    log.info(
        "auto_approve_erc20: CONFIRMED tx=%s gasUsed=%d",
        tx_hex,
        receipt["gasUsed"],
    )
    return tx_hex


def _auto_approve_nft(
    self: Any,
    *,
    nft_address: str,
    operator_address: str,
    private_key: str,
    chain_id: int,
) -> str:
    """Guarded ERC721/1155 setApprovalForAll(operator, true) on-chain."""
    from eth_account import Account
    from web3 import Web3

    chain_name = _chain_name(chain_id)
    rpc_url = self._primary_rpc(chain_name)
    w3 = Web3(Web3.HTTPProvider(rpc_url))

    nft_abi = [
        {
            "constant": False,
            "inputs": [
                {"name": "operator", "type": "address"},
                {"name": "approved", "type": "bool"},
            ],
            "name": "setApprovalForAll",
            "outputs": [],
            "type": "function",
        }
    ]

    nft_contract = w3.eth.contract(
        address=Web3.to_checksum_address(nft_address),
        abi=nft_abi,
    )
    account = Account.from_key(private_key)

    # Finish the external gas-price read before the final execution gate. The
    # shared wallet nonce is reserved only after this gate passes.
    gas_price = w3.eth.gas_price
    governor = _approval_governor(
        self,
        action_type="LIVE_APPROVE_NFT",
        subject_address=nft_address,
        chain_name=chain_name,
    )
    nonce = governor.allocate_nonce(account.address, chain_name)

    tx = nft_contract.functions.setApprovalForAll(
        Web3.to_checksum_address(operator_address),
        True,
    ).build_transaction(
        {
            "from": account.address,
            "nonce": nonce,
            "gasPrice": int(gas_price * 1.1),
            "gas": 80_000,
            "chainId": chain_id,
        }
    )

    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    tx_hex = tx_hash.hex()

    import logging

    log = logging.getLogger("counterbid.okx_api")
    log.info(
        "auto_approve_nft: %s setApprovalForAll(%s) tx=%s — waiting confirmation...",
        nft_address[:14],
        operator_address[:14],
        tx_hex,
    )

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    if receipt["status"] != 1:
        raise RuntimeError(f"setApprovalForAll tx reverted: {tx_hex}")

    log.info(
        "auto_approve_nft: CONFIRMED tx=%s gasUsed=%d",
        tx_hex,
        receipt["gasUsed"],
    )
    return tx_hex


def install_approval_safety(client_class: type[Any]) -> None:
    """Install the R15 guarded approval implementations on OKXAPIClient."""
    client_class._auto_approve_erc20 = _auto_approve_erc20
    client_class._auto_approve_nft = _auto_approve_nft
