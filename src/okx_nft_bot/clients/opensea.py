from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Mapping

from eth_account import Account
from eth_account.messages import encode_typed_data

from okx_nft_bot.clients.http import StdlibHttpTransport, build_url
from okx_nft_bot.config import Settings

log = logging.getLogger("clients.opensea")

# Seaport 1.6 on Ethereum mainnet
SEAPORT_ADDRESS_ETH = "0x0000000000000068F116a894984e2DB1123eB395"
SEAPORT_CONDUIT_KEY = "0x0000007b02230091a7ed01230072f7006a004d60a8d4e71d599b8104250f0000"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
ZERO_BYTES32 = "0x" + ("00" * 32)

# EIP-712 types for Seaport 1.6
EIP712_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "OrderComponents": [
        {"name": "offerer", "type": "address"},
        {"name": "zone", "type": "address"},
        {"name": "offer", "type": "OfferItem[]"},
        {"name": "consideration", "type": "ConsiderationItem[]"},
        {"name": "orderType", "type": "uint8"},
        {"name": "startTime", "type": "uint256"},
        {"name": "endTime", "type": "uint256"},
        {"name": "zoneHash", "type": "bytes32"},
        {"name": "salt", "type": "uint256"},
        {"name": "conduitKey", "type": "bytes32"},
        {"name": "counter", "type": "uint256"},
    ],
    "OfferItem": [
        {"name": "itemType", "type": "uint8"},
        {"name": "token", "type": "address"},
        {"name": "identifierOrCriteria", "type": "uint256"},
        {"name": "startAmount", "type": "uint256"},
        {"name": "endAmount", "type": "uint256"},
    ],
    "ConsiderationItem": [
        {"name": "itemType", "type": "uint8"},
        {"name": "token", "type": "address"},
        {"name": "identifierOrCriteria", "type": "uint256"},
        {"name": "startAmount", "type": "uint256"},
        {"name": "endAmount", "type": "uint256"},
        {"name": "recipient", "type": "address"},
    ],
}


class OpenSeaClient:
    def __init__(self, settings: Settings, transport: StdlibHttpTransport | None = None) -> None:
        self.settings = settings
        self.transport = transport or StdlibHttpTransport(
            timeout=settings.opensea_request_timeout,
            max_retries=settings.opensea_max_retries,
            rate_limit_per_sec=settings.opensea_rate_limit_per_sec,
        )

    def get_collection_events(
        self,
        *,
        slug: str,
        event_type: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        return self._get(
            f'/api/v2/events/collection/{slug}',
            {
                'event_type': event_type,
                'next': cursor,
                'limit': limit,
            },
        )

    def get_collection_stats(self, *, slug: str) -> dict[str, Any]:
        return self._get(f'/api/v2/collections/{slug}/stats', {})

    def get_collection(self, *, slug: str) -> dict[str, Any]:
        return self._get(f'/api/v2/collections/{slug}', {})

    def get_best_listings(self, *, slug: str, cursor: str | None = None) -> dict[str, Any]:
        return self._get(
            f'/api/v2/listings/collection/{slug}/best',
            {'next': cursor},
        )

    def get_offers(self, *, slug: str, cursor: str | None = None, limit: int | None = None) -> dict[str, Any]:
        return self._get(
            f'/api/v2/offers/collection/{slug}/all',
            {'next': cursor, 'limit': limit},
        )

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
    ) -> dict[str, Any]:
        """Submit an offer to OpenSea using Seaport 1.6 protocol.

        OpenSea only supports ETH mainnet (not BSC).

        Args:
            chain: Chain identifier (must be 'eth' for OpenSea)
            collection_address: NFT collection contract address
            token_id: Token ID (None for collection offers)
            price_wei: Offer price in wei
            currency_address: Currency token address (typically WETH)
            wallet_address: Buyer wallet address (auto-derived from private_key if not provided)
            private_key: Wallet private key for signing
            valid_time: Unix timestamp when offer expires (default: 7 days from now)

        Returns:
            Dictionary with keys: offer_id, order_id, status, and optionally raw response
        """
        if chain.lower() not in ("eth", "ethereum", "1"):
            raise ValueError(f"OpenSea only supports ETH mainnet; got {chain}")

        if not self.settings.opensea_api_key:
            raise RuntimeError("OPENSEA_API_KEY not set in environment")

        # Use provided private key or fall back to buyer wallet
        _private_key = private_key or self.settings.buyer_wallet_private_key
        if not _private_key:
            raise RuntimeError("private_key or BUYER_WALLET_PRIVATE_KEY required")

        # Derive wallet address if not provided
        _wallet_address = wallet_address or self.settings.buyer_wallet_address
        if not _wallet_address:
            account = Account.from_key(_private_key)
            _wallet_address = account.address
        else:
            # Validate the wallet matches the private key
            account = Account.from_key(_private_key)
            if account.address.lower() != _wallet_address.lower():
                log.warning("Wallet address mismatch: provided=%s, derived=%s", _wallet_address, account.address)
                _wallet_address = account.address

        # Default valid_time: 7 days from now
        if valid_time is None:
            valid_time = int(time.time()) + (7 * 24 * 3600)

        # Fetch current counter for the wallet
        try:
            counter = self.get_seaport_counter(_wallet_address, chain)
        except Exception as exc:
            log.error("Failed to fetch seaport counter: %s", exc)
            raise RuntimeError(f"Failed to fetch counter: {exc}") from exc

        # Build Seaport order parameters
        order_params = self._build_seaport_offer(
            offerer=_wallet_address,
            collection_address=collection_address,
            token_id=token_id,
            price_wei=price_wei,
            currency_address=currency_address,
            counter=counter,
            valid_time=valid_time,
        )

        # Sign the order with EIP-712
        signature = self._sign_seaport_order(order_params, _private_key, chain_id=1)

        # Submit to OpenSea API
        return self._submit_opensea_offer(order_params, signature, chain)

    def get_seaport_counter(self, wallet_address: str, chain: str = "eth") -> int:
        """Fetch the current counter for a wallet from the Seaport contract.

        Args:
            wallet_address: Wallet address to fetch counter for
            chain: Chain identifier (default: 'eth')

        Returns:
            Current counter value
        """
        if chain.lower() not in ("eth", "ethereum", "1"):
            raise ValueError(f"OpenSea only supports ETH mainnet; got {chain}")

        # Use eth_call to Seaport contract's getCounter(address) function
        # Selector for getCounter(address): 0xf07ec373
        addr_clean = wallet_address.lower().replace("0x", "")
        calldata = "0xf07ec373" + addr_clean.zfill(64)

        # Use Infura or public RPC endpoint
        rpc_url = os.getenv("ETH_RPC_URL", "https://eth.rpc.bloxroute.com/public")

        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [
                {"to": SEAPORT_ADDRESS_ETH, "data": calldata},
                "latest",
            ],
            "id": 1,
        }

        try:
            result = self.transport.request_json(
                method="POST",
                url=rpc_url,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                body=json.dumps(payload),
            )

            if "error" in result:
                raise RuntimeError(f"RPC error: {result['error']}")

            counter_hex = result.get("result", "0x0")
            counter = int(counter_hex, 16)
            log.info("Seaport counter for %s: %d", wallet_address[:14], counter)
            return counter
        except Exception as exc:
            log.error("Failed to fetch counter from RPC: %s", exc)
            # Fallback to counter=0 (will fail if wallet has existing orders, but safer than raising)
            log.warning("Using fallback counter=0")
            return 0

    def _build_seaport_offer(
        self,
        *,
        offerer: str,
        collection_address: str,
        token_id: str | int | None,
        price_wei: int,
        currency_address: str,
        counter: int,
        valid_time: int,
    ) -> dict[str, Any]:
        """Build Seaport order parameters for an offer.

        Args:
            offerer: Wallet address making the offer
            collection_address: NFT collection address
            token_id: Token ID (None for collection offers)
            price_wei: Offer price in wei
            currency_address: Currency token address
            counter: Current counter for the wallet
            valid_time: Unix timestamp when offer expires

        Returns:
            Dictionary of Seaport order parameters
        """
        now = int(time.time())

        # Generate a random salt (256-bit)
        import hashlib
        entropy = os.urandom(8).hex()
        salt_input = f"{offerer}:{collection_address}:{token_id}:{price_wei}:{now}:{entropy}".encode("utf-8")
        salt = int(hashlib.sha256(salt_input).hexdigest(), 16)

        # Determine if this is a collection offer or item offer
        is_collection_offer = token_id is None or str(token_id) in ("", "0")
        if is_collection_offer:
            # Collection offer: itemType=4 (ERC721_CRITERIA), identifierOrCriteria=0
            consideration_item_type = 4
            consideration_identifier = 0
        else:
            # Item offer: itemType=2 (ERC721), identifierOrCriteria=token_id
            consideration_item_type = 2
            consideration_identifier = int(token_id)

        # Offer item: WETH (or specified currency)
        offer = [
            {
                "itemType": 1,  # ERC20
                "token": currency_address,
                "identifierOrCriteria": 0,
                "startAmount": str(price_wei),
                "endAmount": str(price_wei),
            }
        ]

        # Consideration item: NFT
        consideration = [
            {
                "itemType": consideration_item_type,
                "token": collection_address,
                "identifierOrCriteria": consideration_identifier,
                "startAmount": "1",
                "endAmount": "1",
                "recipient": offerer,
            }
        ]

        # ── PATCH 2026-08-06 (OPENSEA_ORDER_SHAPE) ──
        # Ордер собирался в форме, которую OpenSea не принимает. Сверка с пятью
        # живыми офферами из их же API (get_offers → protocol_data.parameters):
        #
        #                             было у нас        у живых OpenSea
        #   zone                      0x0000…0000       0x000056f7…ffd100
        #   totalOriginalConsider…    1                 2
        #   consideration             [NFT]             [NFT, комиссия]
        #
        # 1) orderType=2 это FULL_RESTRICTED — такой ордер обязан утверждать zone.
        #    С нулевым адресом утверждать его некому, исполнить оффер не смог бы
        #    никто. Живые ордера стоят на OpenSea SignedZone.
        # 2) Обязательная комиссия OpenSea в consideration отсутствовала.
        #    get_collection(fees) отдаёт: 1.0%, получатель 0x0000a26b…faa719,
        #    "required": true. Замер по пяти живым офферам — ровно 100 bps
        #    у всех пяти (38.38 → 0.3838, 13.5 → 0.135, 9.57 → 0.0957 …).
        #    Ордер без обязательной комиссии их API отклоняет.
        #
        # Комиссия берётся ИЗ суммы оффера: предлагаем price_wei, продавец
        # получает 99%, 1% уходит OpenSea. Наш потолок при этом не двигается.
        _fee_bps = int(os.environ.get("OPENSEA_FEE_BPS", "100") or 100)
        _fee_recipient = os.environ.get(
            "OPENSEA_FEE_RECIPIENT", "0x0000a26b00c1f0df003000390027140000faa719")
        _fee_wei = (int(price_wei) * _fee_bps) // 10000
        if _fee_wei > 0 and _fee_recipient:
            consideration.append({
                "itemType": 1,  # ERC20
                "token": currency_address,
                "identifierOrCriteria": 0,
                "startAmount": str(_fee_wei),
                "endAmount": str(_fee_wei),
                "recipient": _fee_recipient,
            })

        _zone = os.environ.get(
            "OPENSEA_ZONE", "0x000056f7000000ece9003ca63978907a00ffd100")

        return {
            "offerer": offerer,
            "zone": _zone,
            "offer": offer,
            "consideration": consideration,
            "orderType": 2,  # FULL_RESTRICTED — утверждает SignedZone
            "startTime": str(now),
            "endTime": str(valid_time),
            "zoneHash": ZERO_BYTES32,
            "salt": str(salt),
            "conduitKey": SEAPORT_CONDUIT_KEY,
            "counter": str(counter),
            "totalOriginalConsiderationItems": len(consideration),
        }

    def _sign_seaport_order(self, parameters: dict[str, Any], private_key: str, chain_id: int = 1) -> str:
        """Sign a Seaport order with EIP-712.

        Args:
            parameters: Seaport order parameters
            private_key: Wallet private key
            chain_id: Chain ID (default: 1 for Ethereum)

        Returns:
            EIP-712 signature string
        """
        # Build EIP-712 domain
        domain = {
            "name": "Seaport",
            "version": "1.6",
            "chainId": chain_id,
            "verifyingContract": SEAPORT_ADDRESS_ETH,
        }

        # Convert parameters to typed data message format
        message = self._to_typed_data_message(parameters)

        # Build full EIP-712 structure
        structured = {
            "types": EIP712_TYPES,
            "domain": domain,
            "primaryType": "OrderComponents",
            "message": message,
        }

        # Sign
        encoded = encode_typed_data(full_message=structured)
        signed = Account.sign_message(encoded, private_key=private_key)

        from okx_nft_bot.signing.seaport_signer import hex_with_prefix
        signature = hex_with_prefix(signed.signature)
        log.info("Signed Seaport offer: signature=%s...", signature[:20])
        return signature

    @staticmethod
    def _to_typed_data_message(parameters: dict[str, Any]) -> dict[str, Any]:
        """Convert Seaport parameters to EIP-712 typed data message format."""
        def _offer_item(item: dict[str, Any]) -> dict[str, Any]:
            return {
                "itemType": int(item["itemType"]),
                "token": item["token"],
                "identifierOrCriteria": int(item["identifierOrCriteria"]),
                "startAmount": int(item["startAmount"]),
                "endAmount": int(item["endAmount"]),
            }

        def _consideration_item(item: dict[str, Any]) -> dict[str, Any]:
            return {
                "itemType": int(item["itemType"]),
                "token": item["token"],
                "identifierOrCriteria": int(item["identifierOrCriteria"]),
                "startAmount": int(item["startAmount"]),
                "endAmount": int(item["endAmount"]),
                "recipient": item["recipient"],
            }

        return {
            "offerer": parameters["offerer"],
            "zone": parameters["zone"],
            "offer": [_offer_item(item) for item in parameters["offer"]],
            "consideration": [_consideration_item(item) for item in parameters["consideration"]],
            "orderType": int(parameters["orderType"]),
            "startTime": int(parameters["startTime"]),
            "endTime": int(parameters["endTime"]),
            "zoneHash": bytes.fromhex(str(parameters["zoneHash"]).replace("0x", "")),
            "salt": int(parameters["salt"]),
            "conduitKey": bytes.fromhex(str(parameters["conduitKey"]).replace("0x", "")),
            "counter": int(parameters["counter"]),
        }

    def _live_submit_block_reason(self, *, chain: str = "eth") -> str | None:
        """Return a safety-state block reason without re-applying spend/cooldown caps.

        OpenSea mirroring happens immediately after a successful OKX submit, so
        the outer execution path owns price, budget, and shared cooldown checks.
        This boundary recheck is intentionally limited to state that can change
        while the OpenSea counter is fetched and the order is signed.
        """
        from okx_nft_bot.execution_governor import ExecutionGovernor

        governor = ExecutionGovernor(settings=self.settings)
        if governor.effective_dry_run():
            return "dry_run_enabled"

        failed = governor.state.get_killswitch_failed_offers(chain=chain)
        if failed:
            return (
                f"killswitch_failed: {len(failed)} zombie offer(s) need manual cancel"
            )

        if governor.state.is_force_dry_run():
            return "force_dry_run_enabled"

        arm_state = governor.get_live_arm_state()
        if not arm_state["armed"]:
            if arm_state.get("expires_at"):
                return "live arm expired"
            return "live arm required"
        return None

    def _submit_opensea_offer(
        self, parameters: dict[str, Any], signature: str, chain: str = "eth"
    ) -> dict[str, Any]:
        """Submit the signed offer to OpenSea API.

        Args:
            parameters: Seaport order parameters
            signature: EIP-712 signature
            chain: Chain identifier (default: 'eth')

        Returns:
            Dictionary with offer_id, order_id, status
        """
        if not self.settings.opensea_api_key:
            raise RuntimeError("OPENSEA_API_KEY not set")

        # Map chain to OpenSea chain name
        chain_lower = chain.lower()
        if chain_lower in ("eth", "ethereum", "1"):
            opensea_chain = "ethereum"
            execution_chain = "eth"
        else:
            raise ValueError(f"Unsupported chain: {chain}")

        # Re-check volatile execution safety immediately before the effectful
        # OpenSea POST. This closes the window created by counter RPC + signing.
        blocked_reason = self._live_submit_block_reason(chain=execution_chain)
        if blocked_reason:
            raise RuntimeError(f"OpenSea live submit blocked: {blocked_reason}")

        # Build request body.
        # PATCH 2026-08-06 (OPENSEA_PROTOCOL_ADDRESS): без protocol_address
        # OpenSea отвечает HTTP 400 "Missing required field 'protocol_address'".
        # За первый живой прогон так отвалились все 23 попытки. Значение —
        # адрес Seaport 1.6, тот же, что стоит verifyingContract в подписи
        # EIP-712 и что отдают их собственные живые ордера в protocol_address.
        body = {
            "parameters": parameters,
            "signature": signature,
            "protocol_address": SEAPORT_ADDRESS_ETH,
        }

        # POST to OpenSea API
        url = f"{self.settings.opensea_api_base}/v2/orders/{opensea_chain}/seaport/offers"

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-API-KEY": self.settings.opensea_api_key,
            # PATCH 2026-07-31: без браузерного UA Cloudflare отдаёт
            # "Error 1010: Access denied" на ВСЕ запросы к api.opensea.io.
            # Именно это глушило весь OpenSea-фронт, а не ключ.
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        }

        log.info(
            "Submitting OpenSea offer: url=%s offerer=%s price_wei=%s",
            url, parameters["offerer"][:14], parameters["offer"][0]["startAmount"]
        )

        try:
            response = self.transport.request_json(
                method="POST",
                url=url,
                headers=headers,
                body=json.dumps(body),
            )

            log.info("OpenSea offer response: %s", json.dumps(response, default=str)[:500])

            # Extract order hash or offer ID from response. A successful HTTP
            # response without a durable exchange ID is not an execution receipt.
            order_hash = response.get("order_hash") or response.get("hash") or response.get("id")
            if not order_hash:
                for key in ("orderHash", "offerId", "order_id", "offer_id"):
                    if key in response:
                        order_hash = response[key]
                        break

            order_id = str(order_hash or "").strip()
            if not order_id or order_id.lower() in {"pending", "none", "null"}:
                raise RuntimeError("OpenSea submit response missing order id")

            return {
                "offer_id": order_id,
                "order_id": order_id,
                "status": "submitted",
                "raw": response,
            }
        except Exception as exc:
            log.error("OpenSea offer submission failed: %s", exc)
            raise RuntimeError(f"Failed to submit offer to OpenSea: {exc}") from exc

    def _get(self, path: str, params: Mapping[str, Any]) -> dict[str, Any]:
        if not self.settings.opensea_api_key:
            raise RuntimeError('Missing OpenSea API key in environment')
        url, _request_path = build_url(self.settings.opensea_api_base, path, params)
        headers = {
            'Accept': 'application/json',
            'X-API-KEY': self.settings.opensea_api_key,
            # PATCH 2026-07-31: браузерный UA обязателен (Cloudflare 1010)
            'User-Agent': "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        }
        return self.transport.request_json(method='GET', url=url, headers=headers)
