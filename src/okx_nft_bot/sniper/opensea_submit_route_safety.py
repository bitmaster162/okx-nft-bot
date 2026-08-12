from __future__ import annotations

from copy import copy
from functools import wraps
from typing import Any
from urllib.parse import urlsplit, urlunsplit


def canonical_opensea_api_base(value: Any) -> str:
    """Return an OpenSea API base that ends in exactly one ``/api`` segment.

    ``Settings`` defaults to ``https://api.opensea.io`` while older tests and
    some deployments may already provide a base ending in ``/api``.  The live
    submit implementation appends ``/v2/...`` directly, so the caller-side base
    must be normalized without mutating the shared settings object.
    """
    raw = str(value or "").strip()
    if not raw:
        raise RuntimeError("OpenSea API base unavailable")

    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("OpenSea API base must be an absolute HTTP(S) URL")
    if parsed.query or parsed.fragment:
        raise RuntimeError("OpenSea API base must not contain query or fragment")

    path = parsed.path.rstrip("/")
    if path.lower().endswith("/api"):
        canonical_path = path
    elif path:
        canonical_path = f"{path}/api"
    else:
        canonical_path = "/api"

    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            canonical_path,
            "",
            "",
        )
    )


def install_opensea_submit_route_safety(client_class: type[Any]) -> None:
    """Normalize the effectful OpenSea submit base without shared-state mutation."""
    current = client_class._submit_opensea_offer
    if getattr(current, "_r41_opensea_canonical_submit_route", False):
        return

    original = current

    @wraps(original)
    def canonical_submit(self: Any, *args: Any, **kwargs: Any) -> Any:
        settings = getattr(self, "settings", None)
        if settings is None:
            raise RuntimeError("OpenSea submit settings unavailable")

        canonical_base = canonical_opensea_api_base(
            getattr(settings, "opensea_api_base", None)
        )

        # Do not mutate the live client/settings object.  CounterBidder may reuse
        # it across cycles, and a route-normalization safety patch must not become
        # a hidden runtime configuration write or a cross-call race.
        cloned_client = copy(self)
        cloned_settings = copy(settings)
        cloned_settings.opensea_api_base = canonical_base
        cloned_client.settings = cloned_settings
        return original(cloned_client, *args, **kwargs)

    canonical_submit._r41_opensea_canonical_submit_route = True
    client_class._submit_opensea_offer = canonical_submit
