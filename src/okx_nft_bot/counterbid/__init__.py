from __future__ import annotations

"""Dry-run counter-bidding engine for the separate BSC execution track."""

from typing import TYPE_CHECKING

from okx_nft_bot.counterbid.config import CollectionConfig, CounterbidConfigManager
from okx_nft_bot.counterbid.okx_api import OKXAPIClient, OfferRefreshResult
from okx_nft_bot.counterbid.approval_safety import install_approval_safety
from okx_nft_bot.counterbid.submit_single_attempt_safety import install_submit_single_attempt_safety
from okx_nft_bot.counterbid.cancel_effect_safety import install_cancel_effect_safety
from okx_nft_bot.counterbid.submit_safety import install_submit_safety
from okx_nft_bot.counterbid.receipt_safety import install_receipt_safety
from okx_nft_bot.counterbid.inventory_safety import install_inventory_safety
from okx_nft_bot.counterbid.receipt_reconciliation import install_receipt_reconciliation

install_approval_safety(OKXAPIClient)
install_submit_single_attempt_safety(OKXAPIClient)
install_cancel_effect_safety(OKXAPIClient)
install_submit_safety(OKXAPIClient)
install_receipt_safety(OKXAPIClient)
install_inventory_safety(OKXAPIClient)
install_receipt_reconciliation(OKXAPIClient)

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
