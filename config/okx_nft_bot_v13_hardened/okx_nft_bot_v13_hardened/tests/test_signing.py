import base64
import hashlib
import hmac

from okx_nft_bot.clients.okx import sign_okx_request


def test_sign_okx_request_matches_hmac_sha256_base64() -> None:
    timestamp = "2026-03-07T12:00:00.000Z"
    method = "GET"
    request_path = "/api/v5/mktplace/nft/markets/trades?chain=eth&collectionAddress=0xabc"
    secret = "secret123"

    expected = base64.b64encode(
        hmac.new(
            secret.encode("utf-8"),
            f"{timestamp}{method}{request_path}".encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode("utf-8")

    assert sign_okx_request(
        secret_key=secret,
        timestamp=timestamp,
        method=method,
        request_path=request_path,
        body="",
    ) == expected
