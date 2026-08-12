from __future__ import annotations

"""Dry-run counter-bidding engine for the separate BSC execution track."""

from typing import TYPE_CHECKING

from okx_nft_bot.counterbid.config import CollectionConfig, CounterbidConfigManager
from okx_nft_bot.counterbid.okx_api import OKXAPIClient, OfferRefreshResult
from okx_nft_bot.counterbid.approval_safety import install_approval_safety
from okx_nft_bot.counterbid.submit_safety import install_submit_safety

install_approval_safety(OKXAPIClient)
install_submit_safety(OKXAPIClient)

if TYPE_CHECKING:
    from okx_nft_bot.counterbid.engine import BatchResult, CounterBidTask, CounterBidder

__all__ = [
    "BatchResult",
    "CollectionConfig",
    "CounterBidTask",
    "CounterBidder",
    "CounterbidConfigManager",
    "OKXAPIClient",
    "OfferRefreshResult",
]


def __getattr__(name: str):
    if name in {"BatchResult", "CounterBidTask", "CounterBidder"}:
        from okx_nft_bot.counterbid.engine import BatchResult, CounterBidTask, CounterBidder

        exports = {
            "BatchResult": BatchResult,
            "CounterBidTask": CounterBidTask,
            "CounterBidder": CounterBidder,
        }
        return exports[name]
    raise AttributeError(name)
