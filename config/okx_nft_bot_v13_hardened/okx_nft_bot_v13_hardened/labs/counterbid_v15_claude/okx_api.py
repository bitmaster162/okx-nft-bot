"""
okx_api.py  –  Parasite-Killer v15
===================================
OKX Web3 NFT marketplace API client.

- Auth via HMAC-SHA256 (OK-ACCESS-KEY, OK-ACCESS-SIGN, OK-ACCESS-TIMESTAMP, OK-ACCESS-PASSPHRASE)
- fetch_offers(collection, chain, maker) — get existing offers
- submit_offer(signed_payload, dry_run=True) — submit signed order
- Rate limiting: 20 req/s
"""

import hashlib
import hmac
import json
import os
import time
from datetime import datetime
from typing import Any, Optional

try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False
    import requests as std_requests


# OKX Web3 API
OKX_API_BASE = "https://www.okx.com/api/v5/mktplace/nft"
OKX_OFFERS_ENDPOINT = f"{OKX_API_BASE}/markets/offers"

# Rate limit: 20 req/s
RATE_LIMIT_PER_SEC = 20
MIN_REQUEST_INTERVAL = 1.0 / RATE_LIMIT_PER_SEC  # ~0.05s


class RateLimiter:
    """Simple rate limiter (20 req/s)."""

    def __init__(self, rate_per_sec: int = RATE_LIMIT_PER_SEC):
        self.rate_per_sec = rate_per_sec
        self.min_interval = 1.0 / rate_per_sec
        self.last_request_time = 0.0

    def wait_if_needed(self) -> None:
        """Block until enough time has passed since last request."""
        elapsed = time.time() - self.last_request_time
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self.last_request_time = time.time()


class OKXAPIClient:
    """OKX Web3 NFT marketplace API client."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        api_passphrase: Optional[str] = None,
    ):
        """
        Initialize OKX API client.
        Loads credentials from environment or parameters.
        """
        self.api_key = api_key or os.environ.get("OK_ACCESS_KEY")
        self.api_secret = api_secret or os.environ.get("OK_ACCESS_SECRET")
        self.api_passphrase = api_passphrase or os.environ.get("OK_ACCESS_PASSPHRASE")

        if not all([self.api_key, self.api_secret, self.api_passphrase]):
            raise EnvironmentError(
                "OKX API credentials missing. Set OK_ACCESS_KEY, OK_ACCESS_SECRET, OK_ACCESS_PASSPHRASE"
            )

        self.base_url = OKX_API_BASE
        self.rate_limiter = RateLimiter()
        self.session = None

    def _get_session(self):
        """Lazy-init requests session (curl_cffi or standard)."""
        if self.session is None:
            if HAS_CURL_CFFI:
                self.session = cffi_requests.Session()
            else:
                self.session = std_requests.Session()
        return self.session

    def _sign_request(
        self,
        method: str,
        endpoint: str,
        body: str = "",
    ) -> dict[str, str]:
        """
        Generate OKX auth headers (HMAC-SHA256).
        Returns dict with auth headers.
        """
        timestamp = datetime.utcnow().isoformat() + "Z"

        # Signature = HMAC-SHA256(api_secret, message)
        # message = timestamp + method + requestPath + body
        request_path = endpoint.replace(self.base_url, "")
        message = timestamp + method + request_path + body

        signature = hmac.new(
            self.api_secret.encode(),
            message.encode(),
            hashlib.sha256,
        ).digest()
        signature_b64 = __import__("base64").b64encode(signature).decode()

        return {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": signature_b64,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.api_passphrase,
            "Content-Type": "application/json",
        }

    def fetch_offers(
        self,
        collection: str,
        chain: str = "bsc",
        maker: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        Fetch collection offers from OKX.

        Args:
            collection: NFT contract address
            chain: blockchain (bsc, eth, etc.)
            maker: optional filter by maker address
            limit: number of offers to fetch

        Returns:
            List of offer dicts
        """
        self.rate_limiter.wait_if_needed()

        params = {
            "chain": chain,
            "contractAddress": collection,
            "limit": limit,
        }
        if maker:
            params["maker"] = maker

        headers = self._sign_request("GET", OKX_OFFERS_ENDPOINT)
        session = self._get_session()

        try:
            resp = session.get(
                OKX_OFFERS_ENDPOINT,
                params=params,
                headers=headers,
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get("code") == "0":
                return data.get("data", [])
            else:
                raise RuntimeError(f"OKX API error: {data}")
        except Exception as e:
            raise RuntimeError(f"Failed to fetch offers from OKX: {e}")

    def submit_offer(
        self,
        signed_payload: dict[str, Any],
        *,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """
        Submit a signed counter-offer to OKX.

        Args:
            signed_payload: {parameters, signature} from seaport_signer.sign_order()
            dry_run: if True, print the request instead of submitting

        Returns:
            API response dict
        """
        if dry_run:
            return {
                "code": "0",
                "message": "DRY RUN",
                "dry_run": True,
                "payload": signed_payload,
            }

        self.rate_limiter.wait_if_needed()

        body = json.dumps(signed_payload)
        headers = self._sign_request("POST", OKX_OFFERS_ENDPOINT, body)
        session = self._get_session()

        try:
            resp = session.post(
                OKX_OFFERS_ENDPOINT,
                data=body,
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            raise RuntimeError(f"Failed to submit offer to OKX: {e}")

    def cancel_offer(
        self,
        order_id: str,
        *,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """
        Cancel a submitted offer.

        Args:
            order_id: OKX order ID
            dry_run: if True, don't actually cancel

        Returns:
            API response dict
        """
        if dry_run:
            return {
                "code": "0",
                "message": "DRY RUN",
                "dry_run": True,
                "order_id": order_id,
            }

        # POST /api/v5/mktplace/nft/markets/offers/{id}/cancel
        endpoint = f"{OKX_OFFERS_ENDPOINT}/{order_id}/cancel"
        self.rate_limiter.wait_if_needed()

        headers = self._sign_request("POST", endpoint)
        session = self._get_session()

        try:
            resp = session.post(endpoint, headers=headers, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            raise RuntimeError(f"Failed to cancel offer: {e}")
